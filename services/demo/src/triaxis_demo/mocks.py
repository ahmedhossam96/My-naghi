"""Simulated inference engines (ADR 0012, ADR 0019).

Deterministic, dependency-free stand-ins with the shape of the real engines:
the LLM streams tokens, STT validates audio by magic bytes and returns a
transcript, TTS synthesises audio in memory. No model is loaded and nothing
is written to disk.
"""

from __future__ import annotations

import asyncio
import io
import math
import re
import struct
import wave
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.inference import (
    ChatMessage,
    InferenceClients,
    ModelHealth,
    ModelTask,
    ServiceStatus,
    SpeechAudio,
    Transcription,
)
from triaxis_common.taxonomy import InferenceMode

_ARABIC: Final = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Sentence boundaries, including Arabic question mark, comma and full stop.
SENTENCE_END: Final = re.compile(r"(?<=[.!?\u061F\u06D4\u061B])\s+|\n+")
SAMPLE_RATE: Final = 16_000


def is_arabic(text: str) -> bool:
    return len(_ARABIC.findall(text)) >= max(1, len(text.strip()) // 4)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_END.split(text) if s.strip()]


# --------------------------------------------------------------------- LLM

_EN_REPLY = (
    "Thank you for your question. This answer comes from the simulated Triaxis "
    "language model, which runs inside the demo process with no external calls. "
    "In production, the same request would be served by a self-hosted model in "
    "the Riyadh or Jeddah site, with personal data masked by the guardrails "
    "service before inference. Nothing in this conversation is stored."
)
_AR_REPLY = (
    "شكراً على سؤالك. هذه الإجابة صادرة عن نموذج لغوي محاكى داخل العرض التجريبي "
    "لمنصة ترياكسيس دون أي اتصال خارجي. في بيئة الإنتاج تُعالج الطلبات نفسها "
    "بنموذج مستضاف ذاتياً في موقع الرياض أو جدة، مع إخفاء البيانات الشخصية قبل "
    "المعالجة. لا يُحفظ أي جزء من هذه المحادثة."
)


@dataclass(frozen=True, slots=True)
class LlmResult:
    tokens_in: int
    tokens_out: int


class MockLlm:
    """Streams a canned, language-matched reply word by word."""

    def __init__(self, token_delay_s: float = 0.03, first_token_delay_s: float = 0.18) -> None:
        self._token_delay_s = token_delay_s
        self._first_token_delay_s = first_token_delay_s

    @staticmethod
    def reply_for(prompt: str) -> str:
        return _AR_REPLY if is_arabic(prompt) else _EN_REPLY

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        words = self.reply_for(prompt).split(" ")
        await asyncio.sleep(self._first_token_delay_s)
        for index, word in enumerate(words):
            if index:
                await asyncio.sleep(self._token_delay_s)
            yield word if index == 0 else " " + word

    @staticmethod
    def count_tokens(text: str) -> int:
        """Rough whitespace token count (display only)."""
        return len(text.split())


# --------------------------------------------------------------------- audio


class AudioFormat(StrEnum):
    WAV = "wav"
    FLAC = "flac"
    OGG = "ogg"
    WEBM = "webm"
    MP3 = "mp3"


def sniff_audio(data: bytes) -> AudioFormat:
    """Identify audio by magic bytes, never by file name or declared type."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return AudioFormat.WAV
    if data[:4] == b"fLaC":
        return AudioFormat.FLAC
    if data[:4] == b"OggS":
        return AudioFormat.OGG
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return AudioFormat.WEBM
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return AudioFormat.MP3
    raise TriaxisError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)


def wav_duration_ms(data: bytes) -> int:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except (wave.Error, EOFError, struct.error):
        raise TriaxisError(ErrorCode.UNSUPPORTED_MEDIA_TYPE) from None
    if rate <= 0:
        raise TriaxisError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
    return int(frames * 1000 / rate)


_EN_TRANSCRIPTS = (
    "I would like to book a service appointment for next Tuesday morning.",
    "Can you tell me whether the hybrid model is available in white?",
    "I am calling about the warranty extension on my vehicle.",
)
_AR_TRANSCRIPTS = (
    "أرغب في حجز موعد صيانة يوم الثلاثاء القادم صباحاً.",
    "هل الطراز الهجين متوفر باللون الأبيض؟",
    "أتصل بخصوص تمديد الضمان على سيارتي.",
)


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str
    audio_format: AudioFormat
    duration_ms: int | None


class MockStt:
    """Validates the audio, then returns a canned transcript chosen by its size."""

    def __init__(self, max_duration_ms: int = 60_000) -> None:
        self._max_duration_ms = max_duration_ms

    def transcribe(self, data: bytes, language: str) -> Transcript:
        if not data:
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        fmt = sniff_audio(data)
        duration = wav_duration_ms(data) if fmt is AudioFormat.WAV else None
        if duration is not None and duration > self._max_duration_ms:
            raise TriaxisError(ErrorCode.AUDIO_TOO_LONG)
        choices = _AR_TRANSCRIPTS if language == "ar" else _EN_TRANSCRIPTS
        return Transcript(choices[len(data) % len(choices)], language, fmt, duration)


# --------------------------------------------------------------------- TTS


def _tone(freq: float, ms: int, amplitude: float = 0.35) -> bytes:
    """A short sine tone with a soft envelope, 16-bit mono PCM."""
    count = SAMPLE_RATE * ms // 1000
    ramp = max(1, count // 8)
    frames = bytearray()
    for i in range(count):
        envelope = min(1.0, i / ramp, (count - i) / ramp)
        value = amplitude * envelope * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)
        frames += struct.pack("<h", int(value * 32767))
    return bytes(frames)


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def encode_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class Speech:
    wav: bytes
    sentences: int
    duration_ms: int


class MockTts:
    """Renders one soft tone per word, sentence by sentence. Clearly not speech."""

    def __init__(self, max_chars: int = 2000) -> None:
        self.max_chars = max_chars

    def synthesise(self, text: str, voice: str) -> Speech:
        if not text.strip():
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        if len(text) > self.max_chars:
            raise TriaxisError(ErrorCode.TOO_MANY_TOKENS)
        base = 220.0 if voice == "deep" else 330.0
        sentences = split_sentences(text) or [text]
        pcm = bytearray()
        for sentence in sentences:
            for index, _word in enumerate(sentence.split()):
                pcm += _tone(base * (1 + 0.12 * (index % 4)), 110)
                pcm += _silence(40)
            pcm += _silence(220)
        return Speech(encode_wav(bytes(pcm)), len(sentences), len(pcm) * 1000 // (2 * SAMPLE_RATE))

    @staticmethod
    def sample_wav(seconds: float = 2.0) -> bytes:
        """A synthetic sample clip for the STT playground."""
        ms = int(seconds * 1000)
        return encode_wav(_tone(440.0, ms // 2) + _tone(554.0, ms - ms // 2))


# --------------------------------------------------------------------- protocol adapters
# The same interfaces as the real OpenAI-compatible adapters in
# triaxis_common.inference, so the demo pipeline is identical in both modes.
# Every result is labelled mode="mock"; real mode never falls back to these.


def _simulated_health(task: ModelTask, model_id: str) -> ModelHealth:
    return ModelHealth(
        task=task,
        mode=InferenceMode.MOCK,
        model_id=model_id,
        status=ServiceStatus.SIMULATED,
        reason="mock_mode",
        checked_at=datetime.now(UTC),
    )


class MockLlmClient:
    mode = InferenceMode.MOCK

    def __init__(self, engine: MockLlm | None = None) -> None:
        self._engine = engine or MockLlm()

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage], *, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        del model_id, max_tokens
        prompt = next((m.content for m in reversed(messages) if m.role == "user"), "")
        async for delta in self._engine.stream(prompt):
            yield delta

    async def health(self, model_id: str) -> ModelHealth:
        return _simulated_health(ModelTask.LLM, model_id)


class MockSttClient:
    mode = InferenceMode.MOCK

    def __init__(self, engine: MockStt | None = None) -> None:
        self._engine = engine or MockStt()

    async def transcribe(
        self, model_id: str, audio: bytes, *, language: str, media_type: str
    ) -> Transcription:
        del model_id, media_type
        result = self._engine.transcribe(audio, language)
        return Transcription(text=result.text, language=language, duration_ms=result.duration_ms)

    async def health(self, model_id: str) -> ModelHealth:
        return _simulated_health(ModelTask.STT, model_id)


class MockTtsClient:
    mode = InferenceMode.MOCK

    def __init__(self, engine: MockTts | None = None) -> None:
        self._engine = engine or MockTts()

    async def synthesise(self, model_id: str, text: str, *, voice: str) -> SpeechAudio:
        del model_id
        return SpeechAudio(audio=self._engine.synthesise(text, voice).wav, media_type="audio/wav")

    async def health(self, model_id: str) -> ModelHealth:
        return _simulated_health(ModelTask.TTS, model_id)


def mock_clients(llm: MockLlm | None = None) -> InferenceClients:
    return InferenceClients(
        mode=InferenceMode.MOCK,
        llm=MockLlmClient(llm),
        stt=MockSttClient(),
        tts=MockTtsClient(),
    )
