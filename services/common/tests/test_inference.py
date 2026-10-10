"""Contract tests for the OpenAI-compatible adapters (ADR 0021).

These use an in-process fake server (httpx.MockTransport). They prove the
adapters speak the protocol and handle failures safely; they do NOT prove that
a real model works — that requires an approved self-hosted endpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tools.model_registry import ModelEntry, Registry, RegistryApproval
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.inference import (
    Approval,
    ApprovalStatus,
    ChatMessage,
    EndpointConfig,
    InferenceSettings,
    ModelTask,
    OpenAICompatibleLlm,
    OpenAICompatibleStt,
    OpenAICompatibleTts,
    ServiceStatus,
    build_real_clients,
)
from triaxis_common.secrets import SecretValue
from triaxis_common.taxonomy import InferenceMode
from triaxis_common.types import Environment

BASE = "http://vllm.inference.svc:8000"
MSGS = [ChatMessage(role="user", content="hello")]


def approve_all(model_id: str, task: ModelTask) -> Approval:
    return Approval(model_id, ApprovalStatus.APPROVED)


def approve_none(model_id: str, task: ModelTask) -> Approval:
    return Approval(model_id, ApprovalStatus.NOT_APPROVED)


class FakeServer:
    """Records requests; each handler call pops the next scripted response."""

    def __init__(self, *responses: Callable[[httpx.Request], Any]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0)(request)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, httpx.Response)
        return result


def sse(*deltas: str, done: bool = True) -> Callable[[httpx.Request], httpx.Response]:
    lines = [": keep-alive"]
    for d in deltas:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": d}}]}))
    lines.append("data: " + json.dumps({"choices": [{"delta": {}}]}))
    if done:
        lines.append("data: [DONE]")
    body = "\n\n".join(lines) + "\n\n"
    return lambda _r: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def status(code: int, body: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(code, json=body or {"error": "SECRET-UPSTREAM-DETAIL"})


def raise_(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def make(kind: Any, server: FakeServer, approval: Any = approve_all, **cfg: Any) -> Any:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    adapter = kind(EndpointConfig(base_url=BASE, **cfg), client, approval, sleep=fake_sleep)
    adapter.sleeps = sleeps
    return adapter


async def collect(gen: AsyncIterator[str]) -> str:
    return "".join([d async for d in gen])


async def expect_error(coro: Any, code: ErrorCode) -> TriaxisError:
    with pytest.raises(TriaxisError) as excinfo:
        await coro
    assert excinfo.value.code is code
    assert "SECRET-UPSTREAM-DETAIL" not in str(excinfo.value)
    return excinfo.value


# --------------------------------------------------------------------- settings


def test_production_refuses_mock_mode() -> None:
    with pytest.raises(ValidationError, match="no mock fallback"):
        InferenceSettings(mode=InferenceMode.MOCK, environment=Environment.PROD)


def test_production_real_needs_every_endpoint() -> None:
    with pytest.raises(ValidationError, match="needs"):
        InferenceSettings(
            mode=InferenceMode.REAL,
            environment=Environment.STAGING,
            llm=EndpointConfig(base_url="https://vllm.inference.svc"),
        )


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://8.8.8.8:8000"])
def test_endpoints_must_be_in_cluster(url: str) -> None:
    with pytest.raises(ValidationError):
        InferenceSettings(mode=InferenceMode.REAL, llm=EndpointConfig(base_url=url))


def test_http_only_outside_production() -> None:
    InferenceSettings(mode=InferenceMode.REAL, llm=EndpointConfig(base_url=BASE))
    ep = EndpointConfig(base_url=BASE)
    with pytest.raises(ValidationError):
        InferenceSettings(
            mode=InferenceMode.REAL, environment=Environment.PROD, llm=ep, stt=ep, tts=ep
        )


def test_build_real_clients() -> None:
    ep = EndpointConfig(base_url=BASE, api_key_ref="inference/vllm")
    settings = InferenceSettings(mode=InferenceMode.REAL, llm=ep)
    clients = build_real_clients(
        settings, httpx.AsyncClient(), approve_all, {"inference/vllm": SecretValue("k")}
    )
    assert isinstance(clients.llm, OpenAICompatibleLlm)
    assert clients.stt is None
    assert clients.tts is None
    with pytest.raises(ValueError, match="real"):
        build_real_clients(
            InferenceSettings(mode=InferenceMode.MOCK), httpx.AsyncClient(), approve_all
        )


# --------------------------------------------------------------------- LLM streaming


async def test_stream_chat_success_and_payload() -> None:
    server = FakeServer(sse("Mar", "haba", "!"))
    llm = make(OpenAICompatibleLlm, server)
    assert await collect(llm.stream_chat("qwen3.5-27b", MSGS, max_tokens=64)) == "Marhaba!"
    (req,) = server.requests
    assert req.url.path == "/v1/chat/completions"
    body = json.loads(req.content)
    assert body == {
        "model": "qwen3.5-27b",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "max_tokens": 64,
    }
    assert "authorization" not in req.headers


async def test_api_key_sent_as_bearer() -> None:
    server = FakeServer(sse("ok"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    llm = OpenAICompatibleLlm(
        EndpointConfig(base_url=BASE), client, approve_all, api_key=SecretValue("demo-key")
    )
    await collect(llm.stream_chat("m", MSGS))
    assert server.requests[0].headers["authorization"] == "Bearer demo-key"


async def test_unapproved_model_never_reaches_endpoint() -> None:
    server = FakeServer()
    llm = make(OpenAICompatibleLlm, server, approval=approve_none)
    await expect_error(collect(llm.stream_chat("unvetted", MSGS)), ErrorCode.MODEL_UNAVAILABLE)
    assert server.requests == []


async def test_retry_once_on_503_before_output() -> None:
    server = FakeServer(status(503), sse("ok"))
    llm = make(OpenAICompatibleLlm, server, max_retries=1, backoff_s=0.5)
    assert await collect(llm.stream_chat("m", MSGS)) == "ok"
    assert llm.sleeps == [0.5]
    assert len(server.requests) == 2


async def test_retries_are_bounded() -> None:
    server = FakeServer(status(503), status(503), status(503))
    llm = make(OpenAICompatibleLlm, server, max_retries=1)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.MODEL_UNAVAILABLE)
    assert len(server.requests) == 2


async def test_connect_error_retried_then_fails() -> None:
    server = FakeServer(raise_(httpx.ConnectError("down")), raise_(httpx.ConnectError("down")))
    llm = make(OpenAICompatibleLlm, server, max_retries=1)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.MODEL_UNAVAILABLE)
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (429, ErrorCode.RATE_LIMITED),
        (401, ErrorCode.MODEL_UNAVAILABLE),
        (403, ErrorCode.MODEL_UNAVAILABLE),
        (404, ErrorCode.MODEL_UNAVAILABLE),
        (400, ErrorCode.UPSTREAM_ERROR),
        (500, ErrorCode.MODEL_UNAVAILABLE),
        (504, ErrorCode.TIMEOUT),
    ],
)
async def test_status_mapping_not_retried(code: int, expected: ErrorCode) -> None:
    server = FakeServer(status(code))
    llm = make(OpenAICompatibleLlm, server, max_retries=3)
    await expect_error(collect(llm.stream_chat("m", MSGS)), expected)
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "event: weird\n\n",
        "data: {not json}\n\n",
        'data: {"choices": []}\n\n',
        'data: {"choices": [{"delta": {"content": 5}}]}\n\n',
        'data: {"choices": [{"delta": {"content": "partial"}}]}\n\n',  # no [DONE]
    ],
)
async def test_invalid_upstream_stream(body: str) -> None:
    server = FakeServer(lambda _r: httpx.Response(200, text=body))
    llm = make(OpenAICompatibleLlm, server)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.UPSTREAM_ERROR)


async def test_read_timeout_maps_to_timeout() -> None:
    server = FakeServer(raise_(httpx.ReadTimeout("slow")))
    llm = make(OpenAICompatibleLlm, server)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.TIMEOUT)


async def test_protocol_error_mid_stream_not_retried() -> None:
    server = FakeServer(raise_(httpx.RemoteProtocolError("reset")))
    llm = make(OpenAICompatibleLlm, server, max_retries=2)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.UPSTREAM_ERROR)
    assert len(server.requests) == 1


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _chunk(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()


async def test_total_deadline_enforced_during_stream() -> None:
    stream = SlowStream([_chunk("a"), _chunk("b"), _chunk("c"), b"data: [DONE]\n\n"], 0.05)
    server = FakeServer(lambda _r: httpx.Response(200, stream=stream))
    llm = make(OpenAICompatibleLlm, server, total_timeout_s=0.08)
    await expect_error(collect(llm.stream_chat("m", MSGS)), ErrorCode.TIMEOUT)


async def test_cancellation_closes_upstream_stream() -> None:
    stream = SlowStream([_chunk("a"), _chunk("b"), b"data: [DONE]\n\n"], 0)
    server = FakeServer(lambda _r: httpx.Response(200, stream=stream))
    llm = make(OpenAICompatibleLlm, server)
    gen = llm.stream_chat("m", MSGS)
    assert await anext(gen) == "a"
    await gen.aclose()  # e.g. the browser disconnected
    assert stream.closed


async def test_connect_error_after_output_is_not_retried() -> None:
    class Breaking(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _chunk("partial")
            raise httpx.ConnectError("dropped")

    server = FakeServer(lambda _r: httpx.Response(200, stream=Breaking()), sse("dup"))
    llm = make(OpenAICompatibleLlm, server, max_retries=2)
    out: list[str] = []

    async def consume() -> None:
        async for d in llm.stream_chat("m", MSGS):
            out.append(d)

    with pytest.raises(TriaxisError) as excinfo:
        await consume()
    assert out == ["partial"]  # no duplicated output from a retry
    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert len(server.requests) == 1


# --------------------------------------------------------------------- health


async def test_health_states() -> None:
    models = {"data": [{"id": "qwen3.5-27b"}]}
    server = FakeServer(
        lambda _r: httpx.Response(200, json=models),
        lambda _r: httpx.Response(200, json=models),
        status(500),
        lambda _r: httpx.Response(200, json={"nope": 1}),
        raise_(httpx.ConnectError("down")),
    )
    llm = make(OpenAICompatibleLlm, server)
    assert (await llm.health("qwen3.5-27b")).status is ServiceStatus.READY
    assert (await llm.health("other")).status is ServiceStatus.MODEL_NOT_SERVED
    assert (await llm.health("qwen3.5-27b")).status is ServiceStatus.DEGRADED
    assert (await llm.health("qwen3.5-27b")).reason == "invalid_models_response"
    down = await llm.health("qwen3.5-27b")
    assert down.status is ServiceStatus.UNREACHABLE
    assert "vllm" not in down.model_dump_json()  # no URLs in health views


async def test_health_unapproved_skips_network() -> None:
    server = FakeServer()
    llm = make(OpenAICompatibleLlm, server, approval=approve_none)
    health = await llm.health("m")
    assert health.status is ServiceStatus.UNAPPROVED
    assert health.reason == "model_not_approved"
    assert server.requests == []


# --------------------------------------------------------------------- STT


async def test_stt_success_multipart() -> None:
    server = FakeServer(lambda _r: httpx.Response(200, json={"text": "مرحبا", "duration": 1.5}))
    stt = make(OpenAICompatibleStt, server)
    result = await stt.transcribe(
        "whisper-large-v3", b"RIFF....WAVE", language="ar", media_type="audio/wav"
    )
    assert result.text == "مرحبا"
    assert result.duration_ms == 1500
    body = server.requests[0].content
    assert b'name="model"' in body
    assert b"whisper-large-v3" in body
    assert b'name="language"' in body


async def test_stt_retry_and_errors() -> None:
    server = FakeServer(status(503), lambda _r: httpx.Response(200, json={"text": "ok"}))
    stt = make(OpenAICompatibleStt, server)
    assert (
        await stt.transcribe("m", b"x", language="en", media_type="audio/wav")
    ).duration_ms is None
    for responder, code in [
        (status(500), ErrorCode.MODEL_UNAVAILABLE),
        (lambda _r: httpx.Response(200, text="not json"), ErrorCode.UPSTREAM_ERROR),
        (lambda _r: httpx.Response(200, json={"text": 3}), ErrorCode.UPSTREAM_ERROR),
        (raise_(httpx.ReadTimeout("slow")), ErrorCode.TIMEOUT),
    ]:
        stt = make(OpenAICompatibleStt, FakeServer(responder), max_retries=0)
        await expect_error(stt.transcribe("m", b"x", language="en", media_type="audio/wav"), code)


async def test_stt_unapproved_and_total_timeout() -> None:
    server = FakeServer()
    stt = make(OpenAICompatibleStt, server, approval=approve_none)
    await expect_error(
        stt.transcribe("m", b"x", language="en", media_type="audio/wav"),
        ErrorCode.MODEL_UNAVAILABLE,
    )
    assert server.requests == []

    async def slow(_r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"text": "late"})

    stt = make(OpenAICompatibleStt, FakeServer(slow), total_timeout_s=0.05)
    await expect_error(
        stt.transcribe("m", b"x", language="en", media_type="audio/wav"), ErrorCode.TIMEOUT
    )


async def test_stt_connect_errors_exhaust_retries() -> None:
    server = FakeServer(raise_(httpx.ConnectError("a")), raise_(httpx.ConnectError("b")))
    stt = make(OpenAICompatibleStt, server, max_retries=1)
    await expect_error(
        stt.transcribe("m", b"x", language="en", media_type="audio/wav"),
        ErrorCode.MODEL_UNAVAILABLE,
    )
    server = FakeServer(status(502), status(502))
    stt = make(OpenAICompatibleStt, server, max_retries=1)
    await expect_error(
        stt.transcribe("m", b"x", language="en", media_type="audio/wav"),
        ErrorCode.MODEL_UNAVAILABLE,
    )


# --------------------------------------------------------------------- TTS


async def test_tts_success_and_validation() -> None:
    wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
    server = FakeServer(lambda _r: httpx.Response(200, content=wav))
    tts = make(OpenAICompatibleTts, server)
    speech = await tts.synthesise("chatterbox-multilingual", "hello", voice="warm")
    assert speech.media_type == "audio/wav"
    assert json.loads(server.requests[0].content)["response_format"] == "wav"
    tts = make(OpenAICompatibleTts, FakeServer(lambda _r: httpx.Response(200, content=b"<html>")))
    await expect_error(tts.synthesise("m", "hello", voice="warm"), ErrorCode.UPSTREAM_ERROR)
    tts = make(OpenAICompatibleTts, FakeServer(lambda _r: httpx.Response(200, content=b"ID3abc")))
    assert (await tts.synthesise("m", "hi", voice="warm")).media_type == "application/octet-stream"
    tts = make(OpenAICompatibleTts, FakeServer(status(429)))
    await expect_error(tts.synthesise("m", "hi", voice="warm"), ErrorCode.RATE_LIMITED)


# --------------------------------------------------------------------- registry gate


def _entry(**kw: Any) -> ModelEntry:
    data: dict[str, Any] = {
        "id": "m1",
        "task": "llm",
        "version": "1",
        "origin": {"organization": "Org", "country": "SA"},
        "license": {"spdx": "Apache-2.0", "verified_on": "2026-10-01"},
        "languages": ["ar"],
        "status": "approved",
        "source": {"repository": "org/m1", "revision": "a" * 40},
        "hardware_profiles": ["h100-llm"],
        "artefacts": [{"filename": "model.safetensors", "sha256": "b" * 64, "size_bytes": 1}],
    }
    data.update(kw)
    return ModelEntry.model_validate(data)


@pytest.mark.parametrize(
    ("entry", "model_id", "task", "status_"),
    [
        (_entry(), "m1", ModelTask.LLM, ApprovalStatus.APPROVED),
        (_entry(), "other", ModelTask.LLM, ApprovalStatus.UNKNOWN),
        (_entry(), "m1", ModelTask.STT, ApprovalStatus.TASK_MISMATCH),
        (_entry(status="candidate"), "m1", ModelTask.LLM, ApprovalStatus.NOT_APPROVED),
        (
            _entry(license={"spdx": "CC-BY-NC-4.0", "verified_on": "2026-10-01"}),
            "m1",
            ModelTask.LLM,
            ApprovalStatus.LICENCE_NOT_CLEARED,
        ),
    ],
)
def test_registry_approval(
    entry: ModelEntry, model_id: str, task: ModelTask, status_: ApprovalStatus
) -> None:
    gate = RegistryApproval(Registry(models=[entry]))
    assert gate(model_id, task).status is status_


def test_repository_registry_approves_nothing_yet() -> None:
    gate = RegistryApproval.from_file()
    for model_id, task in [
        ("qwen3.5-27b", ModelTask.LLM),
        ("whisper-large-v3", ModelTask.STT),
        ("chatterbox-multilingual", ModelTask.TTS),
    ]:
        assert gate(model_id, task).status is ApprovalStatus.NOT_APPROVED
