"""Classified AI pipeline for the demo API (ADR 0020, 0021).

Every path follows the same order:

    authenticate (route) → validate → classify + policy → model adapter →
    output guard → response

Rules enforced here:

- Content that the policy blocks never reaches a model adapter.
- Redaction happens before the adapter is called.
- Model output is classified (``client_response``) and masked as required
  before it leaves, segment by segment while streaming.
- Model ids come from the tenant configuration, never from the client.
- Nothing is persisted; audio, transcripts and text live in memory for the
  request only. Audit events are metadata-only.
- In ``real`` mode an unavailable or unapproved model is an error; there is no
  fallback to the mock adapters.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from triaxis_common.classification import ClassificationResult
from triaxis_common.config import ModelSelection, TenantConfig
from triaxis_common.errors import CATALOG, ErrorCode, TriaxisError
from triaxis_common.inference import ChatMessage, InferenceClients, ModelHealth, ModelTask
from triaxis_common.logging import AllowListLogger, Event, Level
from triaxis_common.policy import Enforced, PolicyEngine, merge_results, summarise
from triaxis_common.taxonomy import Classification, DataTag, Destination
from triaxis_common.types import Workload
from triaxis_demo.mocks import sniff_audio, wav_duration_ms

MAX_AUDIO_MS: Final = 60_000
OUTPUT_SEGMENT_MAX: Final = 240
_BOUNDARY: Final = re.compile(r"[.!?\u061F\u06D4\n]")
SUMMARY_PROMPT: Final = (
    "Summarise the customer's request in one short sentence and state the intent "
    "(for example: service booking, sales enquiry, complaint). Do not repeat identifiers."
)
VOICE_PROMPT: Final = (
    "You are a concise, polite automotive dealership voice assistant. Answer in the "
    "caller's language in at most two sentences."
)


class PolicyBlockedError(TriaxisError):
    """Raised before any model call when the policy blocks the content."""

    code = ErrorCode.POLICY_BLOCKED

    def __init__(self, result: ClassificationResult) -> None:
        super().__init__()
        self.result = result


@dataclass(frozen=True, slots=True)
class RequestScope:
    tenant: TenantConfig
    brand: str
    request_id: str | None

    @property
    def models(self) -> ModelSelection:
        return self.tenant.models_for(self.brand)


def _error_event(exc: TriaxisError) -> dict[str, Any]:
    return {"error": {"code": exc.code.value, "message": CATALOG[exc.code].message}}


class OutputGuard:
    """Classifies streamed model output segment by segment before release."""

    def __init__(self, engine: PolicyEngine, scope: RequestScope, workload: Workload) -> None:
        self._engine = engine
        self._ctx = engine.context(
            scope.tenant.id, scope.brand, workload, correlation_id=scope.request_id
        )
        self._buffer = ""
        self.results: list[ClassificationResult] = []

    def _release(self, segment: str) -> str:
        enforced = self._engine.enforce(
            segment, self._ctx, Destination.CLIENT_RESPONSE, audit=False
        )
        self.results.append(enforced.result)
        return enforced.text if enforced.text is not None else "[REDACTED]"

    def feed(self, delta: str) -> str:
        self._buffer += delta
        cut = -1
        for match in _BOUNDARY.finditer(self._buffer):
            cut = match.end()
        if cut < 0 and len(self._buffer) < OUTPUT_SEGMENT_MAX:
            return ""
        if cut < 0:
            cut = len(self._buffer)
        segment, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._release(segment)

    def flush(self) -> str:
        if not self._buffer:
            return ""
        segment, self._buffer = self._buffer, ""
        return self._release(segment)

    def summary(self) -> ClassificationResult | None:
        if not self.results:
            return None
        merged = merge_results(self.results)
        self._engine.audit(merged)
        return merged


class AiPipeline:
    def __init__(
        self, engine: PolicyEngine, clients: InferenceClients, logger: AllowListLogger
    ) -> None:
        self.engine = engine
        self.clients = clients
        self._logger = logger

    @property
    def mode(self) -> str:
        return self.clients.mode.value

    # ----------------------------------------------------------------- helpers

    def _enforce_or_block(
        self,
        text: str,
        scope: RequestScope,
        workload: Workload,
        destination: Destination,
        **ctx_kw: Any,
    ) -> Enforced:
        ctx = self.engine.context(
            scope.tenant.id, scope.brand, workload, correlation_id=scope.request_id, **ctx_kw
        )
        enforced = self.engine.enforce(text, ctx, destination)
        if enforced.blocked:
            raise PolicyBlockedError(enforced.result)
        return enforced

    def _log_inference(
        self,
        task: ModelTask,
        model: str,
        *,
        ok: bool,
        started: float,
        scope: RequestScope,
        exc: BaseException | None = None,
        tokens_out: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "inference_mode": self.clients.mode,
            "model": model,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "tenant": scope.tenant.id,
            "brand": scope.brand,
        }
        if tokens_out is not None:
            fields["tokens_out"] = tokens_out
        if exc is not None:
            fields["error_class"] = type(exc).__name__
            if isinstance(exc, TriaxisError):
                fields["error_code"] = exc.code
        del task
        self._logger.log(
            Event.INFERENCE_COMPLETED if ok else Event.INFERENCE_FAILED,
            Level.INFO if ok else Level.WARNING,
            **fields,
        )

    def _llm(self) -> Any:
        if self.clients.llm is None:
            raise TriaxisError(ErrorCode.MODEL_UNAVAILABLE)
        return self.clients.llm

    async def _complete(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        scope: RequestScope,
        max_tokens: int = 256,
    ) -> str:
        started = time.perf_counter()
        parts: list[str] = []
        try:
            async for delta in self._llm().stream_chat(model, messages, max_tokens=max_tokens):
                parts.append(delta)
        except TriaxisError as exc:
            self._log_inference(
                ModelTask.LLM, model, ok=False, started=started, scope=scope, exc=exc
            )
            raise
        self._log_inference(
            ModelTask.LLM, model, ok=True, started=started, scope=scope, tokens_out=len(parts)
        )
        return "".join(parts)

    # ----------------------------------------------------------------- chat

    async def start_chat(
        self,
        messages: Sequence[ChatMessage],
        scope: RequestScope,
        requested_level: Classification | None,
    ) -> AsyncIterator[bytes]:
        """Classify and enforce, prime the model stream, then return SSE events.

        Errors before the first token raise (proper HTTP status); errors after
        it are delivered as an SSE ``error`` event.
        """
        enforced = [
            self._enforce_or_block_soft(m.content, scope, requested_level) for m in messages
        ]
        merged_in = merge_results([e.result for e in enforced])
        if any(e.blocked for e in enforced):
            raise PolicyBlockedError(merged_in)
        safe_messages = [
            ChatMessage(role=m.role, content=e.text or "")
            for m, e in zip(messages, enforced, strict=True)
        ]
        model = scope.models.llm_chat
        started = time.perf_counter()
        stream = self._llm().stream_chat(model, safe_messages, max_tokens=512)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = ""
        except TriaxisError as exc:
            self._log_inference(
                ModelTask.LLM, model, ok=False, started=started, scope=scope, exc=exc
            )
            await stream.aclose()
            raise
        ttft_ms = round((time.perf_counter() - started) * 1000, 1)
        return self._chat_events(
            stream=stream,
            first=first,
            model=model,
            scope=scope,
            input_result=merged_in,
            ttft_ms=ttft_ms,
            started=started,
        )

    def _enforce_or_block_soft(
        self, text: str, scope: RequestScope, requested_level: Classification | None
    ) -> Enforced:
        ctx = self.engine.context(
            scope.tenant.id,
            scope.brand,
            Workload.LLM,
            correlation_id=scope.request_id,
            requested_level=requested_level,
        )
        return self.engine.enforce(text, ctx, Destination.INFERENCE)

    async def _chat_events(
        self,
        *,
        stream: AsyncIterator[str],
        first: str,
        model: str,
        scope: RequestScope,
        input_result: ClassificationResult,
        ttft_ms: float,
        started: float,
    ) -> AsyncIterator[bytes]:
        guard = OutputGuard(self.engine, scope, Workload.LLM)
        tokens = 0
        yield _sse(
            {
                "meta": {
                    "mode": self.mode,
                    "model": model,
                    "input_classification": summarise(input_result),
                }
            }
        )
        try:
            if first:
                tokens += 1
                if out := guard.feed(first):
                    yield _sse({"delta": out})
            async for delta in stream:
                tokens += 1
                if out := guard.feed(delta):
                    yield _sse({"delta": out})
            if out := guard.flush():
                yield _sse({"delta": out})
        except TriaxisError as exc:
            self._log_inference(
                ModelTask.LLM,
                model,
                ok=False,
                started=started,
                scope=scope,
                exc=exc,
                tokens_out=tokens,
            )
            yield _sse(_error_event(exc))
            return
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
        output = guard.summary()
        self._log_inference(
            ModelTask.LLM, model, ok=True, started=started, scope=scope, tokens_out=tokens
        )
        yield _sse(
            {
                "done": True,
                "mode": self.mode,
                "model": model,
                "ttft_ms": ttft_ms,
                "tokens_out": tokens,
                "output_classification": summarise(output) if output else None,
            }
        )

    # ----------------------------------------------------------------- speech-to-text

    @staticmethod
    def validate_audio(data: bytes) -> tuple[str, int | None]:
        if not data:
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        fmt = sniff_audio(data)
        duration = wav_duration_ms(data) if fmt.value == "wav" else None
        if duration is not None and duration > MAX_AUDIO_MS:
            raise TriaxisError(ErrorCode.AUDIO_TOO_LONG)
        return fmt.value, duration

    async def transcribe(
        self, data: bytes, language: str, scope: RequestScope, *, workload: Workload = Workload.STT
    ) -> tuple[str, ClassificationResult, ClassificationResult, dict[str, Any]]:
        """Audio → transcript. Returns (client text, audio result, transcript result, info)."""
        audio_format, duration = self.validate_audio(data)
        audio = self._enforce_or_block(
            "",
            scope,
            workload,
            Destination.INFERENCE,
            declared_tags=frozenset({DataTag.VOICE_RECORDING}),
        )
        if self.clients.stt is None:
            raise TriaxisError(ErrorCode.MODEL_UNAVAILABLE)
        model = scope.models.stt
        started = time.perf_counter()
        try:
            result = await self.clients.stt.transcribe(
                model, data, language=language, media_type=f"audio/{audio_format}"
            )
        except TriaxisError as exc:
            self._log_inference(
                ModelTask.STT, model, ok=False, started=started, scope=scope, exc=exc
            )
            raise
        self._log_inference(ModelTask.STT, model, ok=True, started=started, scope=scope)
        transcript = self._enforce_or_block(
            result.text, scope, workload, Destination.CLIENT_RESPONSE, source=audio.result
        )
        info = {
            "audio_format": audio_format,
            "duration_ms": duration or result.duration_ms,
            "bytes": len(data),
            "model": model,
            "mode": self.mode,
            "stt_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return transcript.text or "", audio.result, transcript.result, info

    async def analyse_transcript(
        self, text: str, source: ClassificationResult, scope: RequestScope
    ) -> dict[str, Any]:
        """Authorised downstream analytics on a transcript (LLM summary)."""
        ctx = self.engine.context(
            scope.tenant.id,
            scope.brand,
            Workload.LLM,
            correlation_id=scope.request_id,
            source=source,
        )
        enforced = self.engine.enforce(text, ctx, Destination.INFERENCE)
        if enforced.blocked:
            return {"status": "blocked", "classification": summarise(enforced.result)}
        model = scope.models.llm_chat
        reply = await self._complete(
            model,
            [
                ChatMessage(role="system", content=SUMMARY_PROMPT),
                ChatMessage(role="user", content=enforced.text or ""),
            ],
            scope,
        )
        guard = OutputGuard(self.engine, scope, Workload.LLM)
        summary = guard.feed(reply) + guard.flush()
        out = guard.summary()
        return {
            "status": "completed",
            "summary": summary,
            "model": model,
            "input_classification": summarise(enforced.result),
            "output_classification": summarise(out) if out else None,
        }

    # ----------------------------------------------------------------- text-to-speech

    async def synthesise(
        self, text: str, voice: str, scope: RequestScope, workload: Workload = Workload.TTS
    ) -> tuple[bytes, str, ClassificationResult]:
        enforced = self._enforce_or_block(text, scope, workload, Destination.INFERENCE)
        if self.clients.tts is None:
            raise TriaxisError(ErrorCode.MODEL_UNAVAILABLE)
        model = scope.models.tts
        started = time.perf_counter()
        try:
            speech = await self.clients.tts.synthesise(model, enforced.text or "", voice=voice)
        except TriaxisError as exc:
            self._log_inference(
                ModelTask.TTS, model, ok=False, started=started, scope=scope, exc=exc
            )
            raise
        self._log_inference(ModelTask.TTS, model, ok=True, started=started, scope=scope)
        return speech.audio, speech.media_type, enforced.result

    # ----------------------------------------------------------------- voice agent

    async def voice_turn(
        self, data: bytes, language: str, voice: str, scope: RequestScope
    ) -> dict[str, Any]:
        """One stateless turn: audio → STT → policy → LLM → output guard → TTS.

        VAD is not configured in the demo (whole utterances are uploaded).
        No conversation state is kept between turns, so tenants and brands
        cannot contaminate each other's sessions.
        """
        t0 = time.perf_counter()
        transcript_text, audio_res, transcript_res, info = await self.transcribe(
            data, language, scope, workload=Workload.VOICE
        )
        t_stt = time.perf_counter()
        llm_in = self._enforce_or_block(
            transcript_text, scope, Workload.VOICE, Destination.INFERENCE, source=transcript_res
        )
        reply = await self._complete(
            scope.models.llm_voice,
            [
                ChatMessage(role="system", content=VOICE_PROMPT),
                ChatMessage(role="user", content=llm_in.text or ""),
            ],
            scope,
            max_tokens=160,
        )
        t_llm = time.perf_counter()
        guard = OutputGuard(self.engine, scope, Workload.VOICE)
        reply_text = guard.feed(reply) + guard.flush()
        out = guard.summary()
        audio, media_type, tts_res = await self.synthesise(reply_text, voice, scope, Workload.VOICE)
        t_tts = time.perf_counter()
        return {
            "mode": self.mode,
            "vad": "not_configured",
            "transcript": transcript_text,
            "reply_text": reply_text,
            "audio_base64": base64.b64encode(audio).decode(),
            "media_type": media_type,
            "models": {
                "stt": scope.models.stt,
                "llm": scope.models.llm_voice,
                "tts": scope.models.tts,
            },
            "timings_ms": {
                "stt": round((t_stt - t0) * 1000, 1),
                "llm": round((t_llm - t_stt) * 1000, 1),
                "tts": round((t_tts - t_llm) * 1000, 1),
                "total": round((t_tts - t0) * 1000, 1),
            },
            "classification": {
                "audio": summarise(audio_res),
                "transcript": summarise(transcript_res),
                "llm_input": summarise(llm_in.result),
                "llm_output": summarise(out) if out else None,
                "tts_input": summarise(tts_res),
            },
            "audio_format": info["audio_format"],
        }

    # ----------------------------------------------------------------- status

    async def status(self, scope: RequestScope) -> dict[str, Any]:
        models = scope.models
        checks: dict[str, ModelHealth | None] = {}
        for name, client, model in (
            ("llm_chat", self.clients.llm, models.llm_chat),
            ("llm_voice", self.clients.llm, models.llm_voice),
            ("stt", self.clients.stt, models.stt),
            ("tts", self.clients.tts, models.tts),
        ):
            checks[name] = await client.health(model) if client is not None else None
        return {
            "mode": self.mode,
            "services": {
                name: (
                    h.model_dump(mode="json")
                    if h is not None
                    else {"status": "not_configured", "mode": self.mode}
                )
                for name, h in checks.items()
            },
        }


def decision_headers(result: ClassificationResult) -> dict[str, str]:
    return {
        "x-triaxis-classification": result.level.value,
        "x-triaxis-decision": result.decision.value,
        "x-triaxis-reasons": ",".join(r.value for r in result.reason_codes),
    }


def blocked_body(request_id: str | None) -> dict[str, Any]:
    spec = CATALOG[ErrorCode.POLICY_BLOCKED]
    return {
        "error": {
            "type": spec.type.value,
            "code": ErrorCode.POLICY_BLOCKED.value,
            "message": spec.message,
            "request_id": request_id,
        }
    }


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
