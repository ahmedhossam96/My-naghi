"""Classified AI pipeline: chat, STT (+analytics), TTS, voice turn, status.

Real-mode tests run the real OpenAI-compatible adapters against an in-process
fake server. They prove wiring, policy enforcement and failure handling — not
that a real model works (see docs/status/capability-matrix.md).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from tests.support import synthetic as syn
from tests.support.log_gate import captured_log_lines
from triaxis_common.inference import (
    Approval,
    ApprovalStatus,
    ChatMessage,
    EndpointConfig,
    InferenceClients,
    InferenceSettings,
    ModelHealth,
    ModelTask,
    build_real_clients,
)
from triaxis_common.taxonomy import InferenceMode
from triaxis_demo.app import DEFAULT_REPO, create_app
from triaxis_demo.mocks import MockLlm, MockStt, MockSttClient, MockTts, MockTtsClient, encode_wav

SENTINEL = "SENTINEL-ZR-4471"
BASE = "http://vllm.inference.svc:8000"


# --------------------------------------------------------------------- helpers


class SpyLlm:
    """Records exactly what reaches the model adapter."""

    mode = InferenceMode.MOCK

    def __init__(self, reply: str = "Noted. We will call you back.") -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage], *, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        del model_id, max_tokens
        self.calls.append(list(messages))
        for word in self.reply.split(" "):
            yield word + " "

    async def health(self, model_id: str) -> ModelHealth:
        raise NotImplementedError


class SpyTts(MockTtsClient):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def synthesise(self, model_id: str, text: str, *, voice: str) -> Any:
        self.texts.append(text)
        return await super().synthesise(model_id, text, voice=voice)


def spy_clients(llm: SpyLlm | None = None, tts: SpyTts | None = None) -> InferenceClients:
    return InferenceClients(
        mode=InferenceMode.MOCK, llm=llm or SpyLlm(), stt=MockSttClient(), tts=tts or SpyTts()
    )


def session(client: TestClient, tenant: str = "mynaghi", brand: str = "bmw") -> dict[str, str]:
    body = client.post("/api/session", json={"tenant": tenant, "brand": brand}).json()
    return {"Authorization": f"Bearer {body['token']}", "X-Triaxis-Brand": brand}


def events(text: str) -> list[dict[str, Any]]:
    return [json.loads(c.removeprefix("data: ")) for c in text.strip().split("\n\n")]


def reply_of(text: str) -> str:
    return "".join(str(e.get("delta", "")) for e in events(text))


def chat(client: TestClient, headers: dict[str, str], content: str, **extra: Any) -> Any:
    return client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": content}], **extra},
        headers=headers,
    )


def wav(seconds: float = 1.0) -> bytes:
    return encode_wav(b"\x00\x00" * int(16000 * seconds))


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    opened: list[TestClient] = []

    def factory(**kwargs: Any) -> TestClient:
        c = TestClient(create_app(**kwargs), raise_server_exceptions=False)
        c.__enter__()
        opened.append(c)
        return c

    yield factory
    for c in opened:
        c.__exit__(None, None, None)


def repo_with(
    tmp_path: Path, tenant: dict[str, Any] | None = None, policy_for: dict[str, str] | None = None
) -> Path:
    for sub in ("config", "models", "docs"):
        shutil.copytree(DEFAULT_REPO / sub, tmp_path / sub)
    if tenant is not None:
        (tmp_path / "config/tenants" / f"{tenant['id']}.yaml").write_text(yaml.safe_dump(tenant))
    for tenant_id, policy in (policy_for or {}).items():
        path = tmp_path / "config/tenants" / f"{tenant_id}.yaml"
        data = yaml.safe_load(path.read_text())
        data["data_policy"] = policy
        path.write_text(yaml.safe_dump(data))
    return tmp_path


# --------------------------------------------------------------------- real-mode fake server


class FakeOpenAIServer:
    def __init__(
        self,
        reply: str = "Real model reply.",
        transcript: str = "hello from audio",
        fail_chat: int | None = None,
    ) -> None:
        self.reply = reply
        self.transcript = transcript
        self.fail_chat = fail_chat
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "qwen3.5-27b"}, {"id": "qwen3.5-35b-a3b"}]}
            )
        if path == "/v1/chat/completions":
            if self.fail_chat:
                return httpx.Response(self.fail_chat, json={"error": "x"})
            chunks = [
                f"data: {json.dumps({'choices': [{'delta': {'content': w + ' '}}]})}"
                for w in self.reply.split(" ")
            ]
            return httpx.Response(200, text="\n\n".join([*chunks, "data: [DONE]"]) + "\n\n")
        if path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": self.transcript, "duration": 1.0})
        if path == "/v1/audio/speech":
            return httpx.Response(200, content=wav(0.2))
        return httpx.Response(404)


def real_clients(server: FakeOpenAIServer, *, approved: bool = True) -> InferenceClients:
    def approval(model_id: str, task: ModelTask) -> Approval:
        return Approval(
            model_id, ApprovalStatus.APPROVED if approved else ApprovalStatus.NOT_APPROVED
        )

    ep = EndpointConfig(base_url=BASE, max_retries=0)
    settings = InferenceSettings(mode=InferenceMode.REAL, llm=ep, stt=ep, tts=ep)
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return build_real_clients(settings, http, approval)


# --------------------------------------------------------------------- chat: classification gate


def test_restricted_identifiers_redacted_before_model(
    make_client: Callable[..., TestClient],
) -> None:
    llm = SpyLlm()
    client = make_client(inference=spy_clients(llm))
    ident = syn.saudi_id()
    res = chat(client, session(client), f"My national ID is {ident}, book a service")
    assert res.status_code == 200
    (call,) = llm.calls
    assert call[-1].content == "My national ID is [NATIONAL_ID], book a service"
    meta = events(res.text)[0]["meta"]
    assert meta["input_classification"]["decision"] == "redact"
    assert meta["input_classification"]["level"] == "restricted"


def test_blocked_content_never_reaches_model(make_client: Callable[..., TestClient]) -> None:
    llm = SpyLlm()
    client = make_client(inference=spy_clients(llm))
    res = chat(client, session(client), f"my AWS key is {syn.aws_key()}")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "policy_blocked"
    assert res.headers["x-triaxis-decision"] == "block"
    assert res.headers["x-triaxis-classification"] == "restricted"
    assert syn.aws_key() not in res.text
    assert llm.calls == []


def test_client_may_raise_but_not_lower(make_client: Callable[..., TestClient]) -> None:
    client = make_client(inference=spy_clients())
    h = session(client)
    raised = events(chat(client, h, "showroom hours?", classification="confidential").text)
    assert raised[0]["meta"]["input_classification"]["level"] == "confidential"
    lowered = events(chat(client, h, f"card {syn.card()}", classification="public").text)
    assert lowered[0]["meta"]["input_classification"]["level"] == "restricted"


def test_model_output_is_masked(make_client: Callable[..., TestClient]) -> None:
    llm = SpyLlm(reply=f"Your card {syn.card()} is on file. Anything else?")
    client = make_client(inference=spy_clients(llm))
    res = chat(client, session(client), "what card do you have?")
    text = reply_of(res.text)
    assert syn.card() not in text
    assert "[PAYMENT_CARD]" in text
    done = events(res.text)[-1]
    assert done["output_classification"]["decision"] == "redact"


def test_cross_tenant_header_rejected(make_client: Callable[..., TestClient]) -> None:
    client = make_client()
    headers = {**session(client), "X-Triaxis-Tenant": "demo-motors"}
    assert chat(client, headers, "hi").status_code == 403


# --------------------------------------------------------------------- chat: real mode


def test_real_mode_shows_model_output(make_client: Callable[..., TestClient]) -> None:
    server = FakeOpenAIServer(reply="Real model reply.")
    client = make_client(inference=real_clients(server))
    res = chat(client, session(client), "When is the showroom open?")
    assert res.status_code == 200
    evs = events(res.text)
    assert evs[0]["meta"]["mode"] == "real"
    assert evs[0]["meta"]["model"] == "qwen3.5-27b"  # from tenant config, not the client
    assert reply_of(res.text).strip() == "Real model reply."
    assert MockLlm.reply_for("x") not in res.text
    sent = json.loads(
        next(r for r in server.requests if r.url.path == "/v1/chat/completions").content
    )
    assert sent["model"] == "qwen3.5-27b"


def test_real_mode_unapproved_model_fails_closed(make_client: Callable[..., TestClient]) -> None:
    server = FakeOpenAIServer()
    client = make_client(inference=real_clients(server, approved=False))
    res = chat(client, session(client), "hello")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "model_unavailable"
    assert server.requests == []  # never contacted
    assert MockLlm.reply_for("hello") not in res.text  # no mock fallback


@pytest.mark.parametrize(
    ("status", "code"), [(503, "model_unavailable"), (429, "rate_limited"), (400, "upstream_error")]
)
def test_real_mode_upstream_failure_before_output(
    make_client: Callable[..., TestClient], status: int, code: str
) -> None:
    client = make_client(inference=real_clients(FakeOpenAIServer(fail_chat=status)))
    res = chat(client, session(client), "hello")
    assert res.json()["error"]["code"] == code


def test_real_mode_failure_mid_stream_is_an_sse_error(
    make_client: Callable[..., TestClient],
) -> None:
    class Truncating(FakeOpenAIServer):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            chunk = json.dumps({"choices": [{"delta": {"content": "partial"}}]})
            return httpx.Response(200, text=f"data: {chunk}\n\n")  # no [DONE]

    client = make_client(inference=real_clients(Truncating()))
    evs = events(chat(client, session(client), "hello").text)
    assert evs[-1]["error"]["code"] == "upstream_error"


def test_status_endpoint(make_client: Callable[..., TestClient]) -> None:
    mock = make_client()
    h = session(mock)
    body = mock.get("/api/inference/status", headers=h).json()
    assert body["mode"] == "mock"
    assert {s["status"] for s in body["services"].values()} == {"simulated"}
    assert mock.get("/api/inference/status").status_code == 401
    real = make_client(inference=real_clients(FakeOpenAIServer(), approved=False))
    services = real.get("/api/inference/status", headers=session(real)).json()["services"]
    assert services["llm_chat"]["status"] == "unapproved"
    assert services["llm_chat"]["reason"] == "model_not_approved"
    ok = make_client(inference=real_clients(FakeOpenAIServer()))
    ready = ok.get("/api/inference/status", headers=session(ok)).json()["services"]
    assert ready["llm_chat"]["status"] == "ready"
    assert ready["stt"]["status"] == "model_not_served"  # fake serves only LLMs
    empty = make_client(
        inference=InferenceClients(mode=InferenceMode.REAL, llm=None, stt=None, tts=None)
    )
    assert (
        empty.get("/api/inference/status", headers=session(empty)).json()["services"]["tts"][
            "status"
        ]
        == "not_configured"
    )
    assert chat(empty, session(empty), "hi").json()["error"]["code"] == "model_unavailable"


# --------------------------------------------------------------------- classify endpoint


def test_classify_endpoint(make_client: Callable[..., TestClient]) -> None:
    client = make_client()
    h = session(client)
    out = client.post("/api/classify", json={"text": f"IBAN {syn.saudi_iban()}"}, headers=h).json()
    assert out["classification"]["decision"] == "redact"
    assert out["forwarded_text"] == "IBAN [IBAN]"
    blocked = client.post("/api/classify", json={"text": f"pwd={'z' * 9}"}, headers=h).json()
    assert blocked["blocked"] is True
    assert blocked["forwarded_text"] is None
    assert client.post("/api/classify", json={"text": "x"}).status_code == 401
    other_brand = {**h, "X-Triaxis-Brand": "mini"}
    assert client.post("/api/classify", json={"text": "x"}, headers=other_brand).status_code == 403


# --------------------------------------------------------------------- STT and analytics


def test_stt_real_transcript_classification_and_analytics(
    make_client: Callable[..., TestClient],
) -> None:
    server = FakeOpenAIServer(
        transcript="call me on 0551234567", reply="Customer wants a callback."
    )
    client = make_client(inference=real_clients(server))
    res = client.post("/api/stt?language=en&analytics=true", content=wav(), headers=session(client))
    body = res.json()
    assert res.status_code == 200
    assert body["text"] == "call me on 0551234567"  # caller's own transcript
    assert body["mode"] == "real"
    assert body["provenance"]["transcript"] == "live"
    assert body["classification"]["transcript"]["level"] == "confidential"
    assert "phone_number" in body["classification"]["transcript"]["data_tags"]
    assert body["analytics"]["status"] == "completed"
    assert body["analytics"]["summary"].strip() == "Customer wants a callback."


def test_stt_analytics_redacts_restricted_transcript(
    make_client: Callable[..., TestClient],
) -> None:
    llm = SpyLlm(reply="Payment question.")

    class CardStt(MockSttClient):
        async def transcribe(
            self, model_id: str, audio: bytes, *, language: str, media_type: str
        ) -> Any:
            result = await super().transcribe(
                model_id, audio, language=language, media_type=media_type
            )
            return type(result)(
                text=f"my card is {syn.card()}", language=language, duration_ms=result.duration_ms
            )

    clients = InferenceClients(mode=InferenceMode.MOCK, llm=llm, stt=CardStt(), tts=SpyTts())
    client = make_client(inference=clients)
    body = client.post("/api/stt?analytics=true", content=wav(), headers=session(client)).json()
    assert body["text"] == "my card is [PAYMENT_CARD]"  # restricted output masked
    assert body["classification"]["transcript"]["level"] == "restricted"
    (call,) = llm.calls
    assert syn.card() not in call[-1].content


def test_stt_analytics_blocked_by_strict_policy(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    llm = SpyLlm()

    class IdStt(MockSttClient):
        async def transcribe(
            self, model_id: str, audio: bytes, *, language: str, media_type: str
        ) -> Any:
            r = await super().transcribe(model_id, audio, language=language, media_type=media_type)
            return type(r)(text=f"id {syn.saudi_id()}", language=language, duration_ms=None)

    repo = repo_with(tmp_path, policy_for={"mynaghi": "strict"})
    client = make_client(
        repo=repo,
        inference=InferenceClients(mode=InferenceMode.MOCK, llm=llm, stt=IdStt(), tts=SpyTts()),
    )
    body = client.post("/api/stt?analytics=true", content=wav(), headers=session(client)).json()
    assert body["analytics"]["status"] == "blocked"
    assert llm.calls == []


def test_stt_analytics_requires_llm_scope(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    tenant = yaml.safe_load((DEFAULT_REPO / "config/tenants/demo-tenant.yaml").read_text())
    tenant.update(id="sttonly", display_name="STT Only")
    tenant["brands"] = [
        {
            "id": "calls",
            "display_name": "Calls",
            "guardrail_policy": "x",
            "workloads": ["stt"],
            "quotas": {"stt": {"requests_per_minute": 1, "concurrent_streams": 1}},
        }
    ]
    client = make_client(repo=repo_with(tmp_path, tenant=tenant))
    h = session(client, "sttonly", "calls")
    assert client.post("/api/stt", content=wav(), headers=h).status_code == 200
    assert client.post("/api/stt?analytics=true", content=wav(), headers=h).status_code == 403


@pytest.mark.parametrize(
    "payload", [b"", b"not audio at all", encode_wav(b"\x00\x00" * 16000 * 61)]
)
def test_stt_rejects_invalid_audio(make_client: Callable[..., TestClient], payload: bytes) -> None:
    client = make_client()
    assert client.post("/api/stt", content=payload, headers=session(client)).status_code in (
        400,
        415,
    )


def test_stt_upstream_failure(make_client: Callable[..., TestClient]) -> None:
    class Down(FakeOpenAIServer):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectError("down")

    client = make_client(inference=real_clients(Down()))
    res = client.post("/api/stt", content=wav(), headers=session(client))
    assert res.json()["error"]["code"] == "model_unavailable"


# --------------------------------------------------------------------- TTS


def test_tts_redacts_and_blocks_before_synthesis(make_client: Callable[..., TestClient]) -> None:
    tts = SpyTts()
    client = make_client(inference=spy_clients(tts=tts))
    h = session(client)
    ok = client.post(
        "/api/tts", json={"input": f"Your ID {syn.saudi_id()} is verified."}, headers=h
    )
    assert ok.status_code == 200
    assert tts.texts == ["Your ID [NATIONAL_ID] is verified."]
    blocked = client.post("/api/tts", json={"input": f"key {syn.github_token()}"}, headers=h)
    assert blocked.status_code == 422
    assert len(tts.texts) == 1


def test_tts_real_mode_returns_model_audio(make_client: Callable[..., TestClient]) -> None:
    client = make_client(inference=real_clients(FakeOpenAIServer()))
    res = client.post("/api/tts", json={"input": "Hello."}, headers=session(client))
    assert res.status_code == 200
    assert res.headers["x-triaxis-mode"] == "real"
    assert res.content.startswith(b"RIFF")


# --------------------------------------------------------------------- voice agent


@pytest.mark.parametrize(
    ("tenant", "brand"),
    [
        ("mynaghi", "mynaghi-sa"),
        ("mynaghi", "bmw"),
        ("mynaghi", "mini"),
        ("demo-motors", "brand-a"),
    ],
)
def test_voice_turn_for_voice_enabled_brands(
    make_client: Callable[..., TestClient], tenant: str, brand: str
) -> None:
    client = make_client(llm=MockLlm(token_delay_s=0, first_token_delay_s=0))
    res = client.post(
        "/api/voice/turn?language=ar", content=wav(), headers=session(client, tenant, brand)
    )
    body = res.json()
    assert res.status_code == 200
    assert body["mode"] == "mock"
    assert body["vad"] == "not_configured"
    assert base64.b64decode(body["audio_base64"]).startswith(b"RIFF")
    assert set(body["classification"]) == {
        "audio",
        "transcript",
        "llm_input",
        "llm_output",
        "tts_input",
    }
    assert body["classification"]["audio"]["level"] == "confidential"
    assert body["models"]["llm"] == "qwen3.5-35b-a3b"


@pytest.mark.parametrize(
    ("tenant", "brand"), [("demo-motors", "brand-b"), ("demo-motors", "brand-c")]
)
def test_voice_denied_for_other_brands(
    make_client: Callable[..., TestClient], tenant: str, brand: str
) -> None:
    client = make_client()
    res = client.post("/api/voice/turn", content=wav(), headers=session(client, tenant, brand))
    assert res.status_code == 403


def test_voice_turn_real_mode(make_client: Callable[..., TestClient]) -> None:
    server = FakeOpenAIServer(transcript="When can I collect my car?", reply="Tomorrow at ten.")
    client = make_client(inference=real_clients(server))
    body = client.post("/api/voice/turn", content=wav(), headers=session(client)).json()
    assert body["transcript"] == "When can I collect my car?"
    assert body["reply_text"].strip() == "Tomorrow at ten."
    paths = [r.url.path for r in server.requests]
    assert paths == ["/v1/audio/transcriptions", "/v1/chat/completions", "/v1/audio/speech"]
    llm_body = json.loads(server.requests[1].content)
    assert llm_body["model"] == "qwen3.5-35b-a3b"


def test_voice_turns_are_stateless_across_brands(make_client: Callable[..., TestClient]) -> None:
    llm = SpyLlm()
    client = make_client(inference=spy_clients(llm))
    client.post("/api/voice/turn", content=wav(), headers=session(client, "mynaghi", "bmw"))
    client.post("/api/voice/turn", content=wav(), headers=session(client, "mynaghi", "mini"))
    assert len(llm.calls) == 2
    assert all(len(call) == 2 for call in llm.calls)  # system + this turn only: no history
    records = [
        r
        for r in client.get("/api/audit?limit=200").json()["records"]
        if r.get("workload") == "voice" and r["event"] == "request_completed"
    ]
    assert {r["brand"] for r in records} == {"bmw", "mini"}


def test_voice_blocked_transcript_stops_pipeline(make_client: Callable[..., TestClient]) -> None:
    llm, tts = SpyLlm(), SpyTts()

    class SecretStt(MockSttClient):
        async def transcribe(
            self, model_id: str, audio: bytes, *, language: str, media_type: str
        ) -> Any:
            r = await super().transcribe(model_id, audio, language=language, media_type=media_type)
            return type(r)(text=f"my password: {'q' * 10}", language=language, duration_ms=None)

    client = make_client(
        inference=InferenceClients(mode=InferenceMode.MOCK, llm=llm, stt=SecretStt(), tts=tts)
    )
    res = client.post("/api/voice/turn", content=wav(), headers=session(client))
    assert res.status_code == 422
    assert llm.calls == []
    assert tts.texts == []


# --------------------------------------------------------------------- zero retention


def _snapshot(dirs: list[Path]) -> set[Path]:
    out: set[Path] = set()
    for d in dirs:
        if d.is_dir():
            out |= {p for p in d.rglob("*") if p.is_file()}
    return out


def test_sentinel_never_reaches_logs_audit_or_disk(make_client: Callable[..., TestClient]) -> None:
    server = FakeOpenAIServer(transcript=f"{SENTINEL} transcript", reply=f"{SENTINEL} reply.")
    client = make_client(inference=real_clients(server))
    h = session(client)
    watched = [Path(tempfile.gettempdir()), Path(os.environ.get("TMPDIR", "/tmp"))]
    before = _snapshot(watched)
    lines_before = len(captured_log_lines())

    chat(client, h, f"{SENTINEL} please book a service")
    client.post("/api/stt?analytics=true", content=wav(), headers=h)
    client.post("/api/tts", json={"input": f"{SENTINEL} hello."}, headers=h)
    client.post("/api/voice/turn", content=wav(), headers=h)
    client.post("/api/classify", json={"text": f"{SENTINEL} card {syn.card()}"}, headers=h)
    chat(client, h, f"{SENTINEL} key {syn.aws_key()}")  # blocked path

    new_lines = captured_log_lines()[lines_before:]
    assert new_lines, "expected log records from the requests"
    assert not any(SENTINEL in line for line in new_lines)
    audit = json.dumps(client.get("/api/audit?limit=500").json())
    assert SENTINEL not in audit
    assert syn.card() not in audit
    created = _snapshot(watched) - before
    assert not any(SENTINEL.encode() in p.read_bytes() for p in created if p.stat().st_size < 10**7)
    decisions = [json.loads(ln) for ln in new_lines if '"classification_decision"' in ln]
    assert {d["decision"] for d in decisions} >= {"allow", "block"}


def test_mock_engines_still_usable() -> None:
    assert MockStt().transcribe(wav(), "en").duration_ms == 1000
    assert MockTts().synthesise("Hello.", "warm").sentences == 1


def test_inference_from_env() -> None:
    from triaxis_demo.__main__ import inference_from_env

    assert inference_from_env({}) is None
    assert inference_from_env({"TRIAXIS_INFERENCE_MODE": "mock"}) is None
    clients = inference_from_env(
        {"TRIAXIS_INFERENCE_MODE": "real", "TRIAXIS_LLM_URL": "http://127.0.0.1:8000"}
    )
    assert clients is not None
    assert clients.mode is InferenceMode.REAL
    assert clients.llm is not None
    assert clients.stt is None
    with pytest.raises(ValueError, match="cluster"):
        inference_from_env(
            {"TRIAXIS_INFERENCE_MODE": "real", "TRIAXIS_LLM_URL": "https://api.openai.com"}
        )


def test_env_real_mode_blocks_unapproved_registry_models(
    make_client: Callable[..., TestClient],
) -> None:
    """With the repository registry (no approved models), real mode fails closed."""
    from triaxis_demo.__main__ import inference_from_env

    clients = inference_from_env(
        {"TRIAXIS_INFERENCE_MODE": "real", "TRIAXIS_LLM_URL": "http://127.0.0.1:9"}
    )
    client = make_client(inference=clients)
    res = chat(client, session(client), "hello")
    assert res.status_code == 503
    status = client.get("/api/inference/status", headers=session(client)).json()
    assert status["services"]["llm_chat"]["reason"] == "model_not_approved"


def test_tts_and_voice_upstream_failures(make_client: Callable[..., TestClient]) -> None:
    class SpeechDown(FakeOpenAIServer):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/audio/speech":
                self.requests.append(request)
                return httpx.Response(503)
            return super().__call__(request)

    client = make_client(inference=real_clients(SpeechDown()))
    h = session(client)
    assert (
        client.post("/api/tts", json={"input": "Hi."}, headers=h).json()["error"]["code"]
        == "model_unavailable"
    )
    voice_llm_down = make_client(inference=real_clients(FakeOpenAIServer(fail_chat=503)))
    res = voice_llm_down.post("/api/voice/turn", content=wav(), headers=session(voice_llm_down))
    assert res.status_code == 503
    analytics_down = make_client(inference=real_clients(FakeOpenAIServer(fail_chat=503)))
    res = analytics_down.post(
        "/api/stt?analytics=true", content=wav(), headers=session(analytics_down)
    )
    assert res.json()["error"]["code"] == "model_unavailable"


def test_backup_status_endpoint(make_client: Callable[..., TestClient], tmp_path: Path) -> None:
    from tools.backup import BackupService, LocalKeyProvider, Operator, Role, load_spec

    unconfigured = make_client()
    body = unconfigured.get("/api/backup/status", headers=session(unconfigured)).json()
    assert body == {
        "provenance": "live",
        "configured": False,
        "state": "unavailable",
        "reason": "TRIAXIS_BACKUP_DIR not set",
    }
    assert unconfigured.get("/api/backup/status").status_code == 401

    root = tmp_path / "backups"
    root.mkdir()
    kek = tmp_path / "k" / "kek.hex"
    kek.parent.mkdir(mode=0o700)
    LocalKeyProvider.generate(kek)
    empty = make_client(backup_root=root)
    assert empty.get("/api/backup/status", headers=session(empty)).json()["state"] == "no_backups"
    svc = BackupService(root, LocalKeyProvider(kek))
    op = Operator("alice", frozenset({Role.BACKUP_OPERATOR}))
    manifest = svc.create(
        DEFAULT_REPO, load_spec(DEFAULT_REPO / "config/backup/backup-spec.yaml"), op
    )
    svc.verify(manifest.backup_id, op)
    configured = make_client(backup_root=root)
    status = configured.get("/api/backup/status", headers=session(configured)).json()
    assert status["state"] == "ok"
    assert status["latest"]["backup_id"] == manifest.backup_id
    assert status["verifications"][-1]["result"] == "verified"
