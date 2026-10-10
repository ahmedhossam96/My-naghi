"""Self-hosted inference adapters (ADR 0021).

OpenAI-compatible HTTP clients for the self-hosted runtimes Triaxis targets
(vLLM for LLMs; faster-whisper / vLLM transcription servers for STT; an
OpenAI-compatible speech server for TTS). Design rules:

- **Explicit mode.** :class:`InferenceSettings` declares ``mock`` or ``real``.
  Staging and production refuse ``mock``; ``real`` never falls back to mock.
- **Model gate first.** Before any network call, the model id is checked by an
  :class:`ApprovalChecker` (registry + licence gate). An unapproved model is
  rejected without contacting the endpoint.
- **In-cluster only.** Endpoint URLs pass ``require_in_cluster_url``.
- **Bounded retries.** Only connection failures and 502/503 *before any output*
  are retried, at most ``max_retries`` times with exponential backoff. Partial
  output is never retried (no duplicated tokens).
- **Safe errors.** Upstream failures map to catalogue codes; upstream bodies and
  customer content never reach logs or error messages.
- **Cancellation.** Streams are consumed inside ``async with`` blocks, so
  closing the generator (client disconnect) closes the upstream connection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, model_validator

from triaxis_common.config import require_in_cluster_url
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.secrets import SecretValue
from triaxis_common.taxonomy import InferenceMode
from triaxis_common.types import Environment

RETRYABLE_STATUS: Final = frozenset({502, 503})
AUDIO_MAGIC: Final = (b"RIFF", b"fLaC", b"OggS", b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


class ModelTask(StrEnum):
    LLM = "llm"
    STT = "stt"
    TTS = "tts"


class UpstreamError(TriaxisError):
    """A model endpoint failed. ``code`` is a catalogue code; no upstream text kept."""

    code = ErrorCode.MODEL_UNAVAILABLE


# --------------------------------------------------------------------- approval


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    UNKNOWN = "model_unknown"
    TASK_MISMATCH = "model_task_mismatch"
    NOT_APPROVED = "model_not_approved"
    LICENCE_NOT_CLEARED = "licence_not_cleared"


@dataclass(frozen=True, slots=True)
class Approval:
    model_id: str
    status: ApprovalStatus

    @property
    def approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED


class ApprovalChecker(Protocol):
    def __call__(self, model_id: str, task: ModelTask) -> Approval: ...


# --------------------------------------------------------------------- settings


class EndpointConfig(BaseModel):
    """One OpenAI-compatible endpoint. Credentials come from OpenBao, not here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    connect_timeout_s: PositiveFloat = 2.0
    read_timeout_s: PositiveFloat = 30.0
    total_timeout_s: PositiveFloat = 120.0
    max_retries: Annotated[int, Field(ge=0, le=3)] = 1
    backoff_s: Annotated[float, Field(ge=0, le=5)] = 0.2
    api_key_ref: Annotated[str, Field(pattern=r"^[A-Za-z0-9_/-]{1,128}$")] | None = None


class InferenceSettings(BaseModel):
    """Which mode a service runs in and where its models are."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: InferenceMode
    environment: Environment = Environment.DEV
    llm: EndpointConfig | None = None
    stt: EndpointConfig | None = None
    tts: EndpointConfig | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.environment.is_production_like and self.mode is not InferenceMode.REAL:
            raise ValueError("staging and prod require inference mode 'real' (no mock fallback)")
        allow_http = not self.environment.is_production_like
        for name in ("llm", "stt", "tts"):
            endpoint: EndpointConfig | None = getattr(self, name)
            if endpoint is not None:
                require_in_cluster_url(endpoint.base_url, allow_http=allow_http)
        if self.mode is InferenceMode.REAL and self.environment.is_production_like:
            missing = [n for n in ("llm", "stt", "tts") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"real inference in {self.environment.value} needs: {missing}")
        return self


# --------------------------------------------------------------------- protocols and values


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ServiceStatus(StrEnum):
    READY = "ready"
    SIMULATED = "simulated"
    NOT_CONFIGURED = "not_configured"
    UNAPPROVED = "unapproved"
    UNREACHABLE = "unreachable"
    MODEL_NOT_SERVED = "model_not_served"
    DEGRADED = "degraded"


class ModelHealth(BaseModel):
    """Safe status view: no URLs, credentials or upstream messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: ModelTask
    mode: InferenceMode
    model_id: str
    status: ServiceStatus
    reason: str | None = None
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    language: str
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    audio: bytes
    media_type: str


class LlmClient(Protocol):
    mode: InferenceMode

    def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage], *, max_tokens: int
    ) -> AsyncIterator[str]: ...

    async def health(self, model_id: str) -> ModelHealth: ...


class SttClient(Protocol):
    mode: InferenceMode

    async def transcribe(
        self, model_id: str, audio: bytes, *, language: str, media_type: str
    ) -> Transcription: ...

    async def health(self, model_id: str) -> ModelHealth: ...


class TtsClient(Protocol):
    mode: InferenceMode

    async def synthesise(self, model_id: str, text: str, *, voice: str) -> SpeechAudio: ...

    async def health(self, model_id: str) -> ModelHealth: ...


# --------------------------------------------------------------------- shared HTTP plumbing


def _now() -> datetime:
    return datetime.now(UTC)


def _map_status(status: int) -> UpstreamError:
    if status in (401, 403):
        return UpstreamError(ErrorCode.MODEL_UNAVAILABLE)  # misconfiguration; never leak
    if status == 404:
        return UpstreamError(ErrorCode.MODEL_UNAVAILABLE)
    if status == 429:
        return UpstreamError(ErrorCode.RATE_LIMITED)
    if status in (408, 504):
        return UpstreamError(ErrorCode.TIMEOUT)
    if status >= 500:
        return UpstreamError(ErrorCode.MODEL_UNAVAILABLE)
    return UpstreamError(ErrorCode.UPSTREAM_ERROR)


class _OpenAICompatibleBase:
    mode = InferenceMode.REAL
    task: ModelTask

    def __init__(
        self,
        config: EndpointConfig,
        client: httpx.AsyncClient,
        approval: ApprovalChecker,
        *,
        api_key: SecretValue | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._cfg = config
        self._base = config.base_url.rstrip("/")
        self._client = client
        self._approval = approval
        self._api_key = api_key
        self._sleep = sleep

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._cfg.read_timeout_s, connect=self._cfg.connect_timeout_s)

    def _headers(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._api_key.reveal()}"}

    def _require_approved(self, model_id: str) -> None:
        if not self._approval(model_id, self.task).approved:
            raise UpstreamError(ErrorCode.MODEL_UNAVAILABLE)

    async def _with_retries(self, call: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            try:
                return await call()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                if attempt >= self._cfg.max_retries:
                    raise UpstreamError(ErrorCode.MODEL_UNAVAILABLE) from exc
            except httpx.TimeoutException as exc:
                raise UpstreamError(ErrorCode.TIMEOUT) from exc
            except _RetryableStatusError as exc:
                if attempt >= self._cfg.max_retries:
                    raise _map_status(exc.status) from None
            await self._sleep(self._cfg.backoff_s * (2**attempt))
            attempt += 1

    async def _bounded(self, call: Callable[[], Any]) -> Any:
        try:
            async with asyncio.timeout(self._cfg.total_timeout_s):
                return await self._with_retries(call)
        except TimeoutError:
            raise UpstreamError(ErrorCode.TIMEOUT) from None

    async def health(self, model_id: str) -> ModelHealth:
        approval = self._approval(model_id, self.task)
        if not approval.approved:
            return ModelHealth(
                task=self.task,
                mode=self.mode,
                model_id=model_id,
                status=ServiceStatus.UNAPPROVED,
                reason=approval.status.value,
                checked_at=_now(),
            )
        try:
            resp = await self._client.get(
                f"{self._base}/v1/models",
                headers=self._headers(),
                timeout=httpx.Timeout(3.0, connect=self._cfg.connect_timeout_s),
            )
        except httpx.HTTPError as exc:
            return ModelHealth(
                task=self.task,
                mode=self.mode,
                model_id=model_id,
                status=ServiceStatus.UNREACHABLE,
                reason=type(exc).__name__,
                checked_at=_now(),
            )
        if resp.status_code != 200:
            return ModelHealth(
                task=self.task,
                mode=self.mode,
                model_id=model_id,
                status=ServiceStatus.DEGRADED,
                reason=f"http_{resp.status_code}",
                checked_at=_now(),
            )
        try:
            served = {m["id"] for m in resp.json()["data"]}
        except (ValueError, KeyError, TypeError):
            return ModelHealth(
                task=self.task,
                mode=self.mode,
                model_id=model_id,
                status=ServiceStatus.DEGRADED,
                reason="invalid_models_response",
                checked_at=_now(),
            )
        status = ServiceStatus.READY if model_id in served else ServiceStatus.MODEL_NOT_SERVED
        return ModelHealth(
            task=self.task, mode=self.mode, model_id=model_id, status=status, checked_at=_now()
        )


class _RetryableStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


# --------------------------------------------------------------------- LLM


class OpenAICompatibleLlm(_OpenAICompatibleBase):
    """Chat completions with SSE streaming (vLLM ``/v1/chat/completions``)."""

    task = ModelTask.LLM

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage], *, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        self._require_approved(model_id)
        payload = {
            "model": model_id,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "max_tokens": max_tokens,
        }
        loop = asyncio.get_running_loop()
        # An explicit deadline, not asyncio.timeout(): a timeout scope spanning
        # ``yield`` would cancel whatever the consumer awaits between chunks.
        deadline = loop.time() + self._cfg.total_timeout_s
        emitted = False
        attempt = 0
        while True:
            try:
                async with self._client.stream(
                    "POST",
                    f"{self._base}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=self._timeout(),
                ) as resp:
                    if resp.status_code in RETRYABLE_STATUS:
                        raise _RetryableStatusError(resp.status_code)  # noqa: TRY301
                    if resp.status_code != 200:
                        raise _map_status(resp.status_code)
                    async for delta in self._parse_sse(resp):
                        if loop.time() > deadline:
                            raise UpstreamError(ErrorCode.TIMEOUT)
                        emitted = True
                        yield delta
                    return
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if emitted or attempt >= self._cfg.max_retries:
                    raise UpstreamError(ErrorCode.MODEL_UNAVAILABLE) from exc
            except _RetryableStatusError as exc:
                if attempt >= self._cfg.max_retries:
                    raise _map_status(exc.status) from None
            except httpx.TimeoutException as exc:
                raise UpstreamError(ErrorCode.TIMEOUT) from exc
            except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                raise UpstreamError(ErrorCode.UPSTREAM_ERROR) from exc
            if loop.time() > deadline:
                raise UpstreamError(ErrorCode.TIMEOUT)
            await self._sleep(self._cfg.backoff_s * (2**attempt))
            attempt += 1

    @staticmethod
    async def _parse_sse(resp: httpx.Response) -> AsyncIterator[str]:
        done = False
        async for line in resp.aiter_lines():
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                raise UpstreamError(ErrorCode.UPSTREAM_ERROR)
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                event = json.loads(data)
                choice = event["choices"][0]
                delta = choice.get("delta", {}).get("content")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise UpstreamError(ErrorCode.UPSTREAM_ERROR) from None
            if delta:
                if not isinstance(delta, str):
                    raise UpstreamError(ErrorCode.UPSTREAM_ERROR)
                yield delta
        if not done:
            raise UpstreamError(ErrorCode.UPSTREAM_ERROR)  # truncated stream


# --------------------------------------------------------------------- STT


class OpenAICompatibleStt(_OpenAICompatibleBase):
    """``/v1/audio/transcriptions``. The multipart body is built in memory."""

    task = ModelTask.STT

    async def transcribe(
        self, model_id: str, audio: bytes, *, language: str, media_type: str
    ) -> Transcription:
        self._require_approved(model_id)

        async def call() -> httpx.Response:
            resp = await self._client.post(
                f"{self._base}/v1/audio/transcriptions",
                files={"file": ("audio", audio, media_type)},
                data={"model": model_id, "language": language, "response_format": "json"},
                headers=self._headers(),
                timeout=self._timeout(),
            )
            if resp.status_code in RETRYABLE_STATUS:
                raise _RetryableStatusError(resp.status_code)
            return resp

        resp: httpx.Response = await self._bounded(call)
        if resp.status_code != 200:
            raise _map_status(resp.status_code)
        try:
            body = resp.json()
            text = body["text"]
            duration = body.get("duration")
        except (ValueError, KeyError, TypeError):
            raise UpstreamError(ErrorCode.UPSTREAM_ERROR) from None
        if not isinstance(text, str):
            raise UpstreamError(ErrorCode.UPSTREAM_ERROR)
        duration_ms = int(float(duration) * 1000) if isinstance(duration, int | float) else None
        return Transcription(text=text, language=language, duration_ms=duration_ms)


# --------------------------------------------------------------------- TTS


class OpenAICompatibleTts(_OpenAICompatibleBase):
    """``/v1/audio/speech``; the response must be recognisable audio."""

    task = ModelTask.TTS

    async def synthesise(self, model_id: str, text: str, *, voice: str) -> SpeechAudio:
        self._require_approved(model_id)

        async def call() -> httpx.Response:
            resp = await self._client.post(
                f"{self._base}/v1/audio/speech",
                json={"model": model_id, "input": text, "voice": voice, "response_format": "wav"},
                headers=self._headers(),
                timeout=self._timeout(),
            )
            if resp.status_code in RETRYABLE_STATUS:
                raise _RetryableStatusError(resp.status_code)
            return resp

        resp: httpx.Response = await self._bounded(call)
        if resp.status_code != 200:
            raise _map_status(resp.status_code)
        audio = resp.content
        if not audio.startswith(AUDIO_MAGIC):
            raise UpstreamError(ErrorCode.UPSTREAM_ERROR)
        media_type = "audio/wav" if audio.startswith(b"RIFF") else "application/octet-stream"
        return SpeechAudio(audio=audio, media_type=media_type)


@dataclass(frozen=True, slots=True)
class InferenceClients:
    mode: InferenceMode
    llm: LlmClient | None
    stt: SttClient | None
    tts: TtsClient | None


def build_real_clients(
    settings: InferenceSettings,
    client: httpx.AsyncClient,
    approval: ApprovalChecker,
    api_keys: dict[str, SecretValue] | None = None,
) -> InferenceClients:
    """Real adapters for every configured endpoint (missing ones stay ``None``)."""
    if settings.mode is not InferenceMode.REAL:
        raise ValueError("build_real_clients requires mode 'real'")
    keys = api_keys or {}

    def make(kind: type[_OpenAICompatibleBase], cfg: EndpointConfig | None) -> Any:
        if cfg is None:
            return None
        return kind(cfg, client, approval, api_key=keys.get(cfg.api_key_ref or ""))

    return InferenceClients(
        mode=InferenceMode.REAL,
        llm=make(OpenAICompatibleLlm, settings.llm),
        stt=make(OpenAICompatibleStt, settings.stt),
        tts=make(OpenAICompatibleTts, settings.tts),
    )
