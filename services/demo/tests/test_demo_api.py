from __future__ import annotations

import io
import json
import wave
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triaxis_common.config import ResilienceMode
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_demo import Provenance
from triaxis_demo.app import create_app
from triaxis_demo.demo_data import SecurityOps
from triaxis_demo.mocks import (
    AudioFormat,
    MockLlm,
    MockStt,
    MockTts,
    encode_wav,
    is_arabic,
    sniff_audio,
    split_sentences,
)
from triaxis_demo.simulation import REROUTE_S, FailoverSimulator

TAG = "ZR-TAG-demo-5b1e"  # stands in for customer content


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(llm=MockLlm(token_delay_s=0, first_token_delay_s=0))) as c:
        yield c


def _session(client: TestClient, brand: str = "brand-a") -> dict[str, str]:
    body = client.post("/api/session", json={"tenant": "demo-motors", "brand": brand}).json()
    return {"Authorization": f"Bearer {body['token']}"}


def _sse_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(chunk.removeprefix("data: ")) for chunk in text.strip().split("\n\n")]


# --------------------------------------------------------------------- pages, meta, headers


def test_index_and_static_assets_with_security_headers(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Triaxis" in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    for asset in ("app.js", "app.css", "charts.js"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_static_assets_make_no_external_requests() -> None:
    from triaxis_demo.app import STATIC_DIR

    for path in STATIC_DIR.glob("*.*"):
        text = path.read_text("utf-8")
        assert "http://" not in text.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in text


def test_meta_explains_labels(client: TestClient) -> None:
    labels = client.get("/api/meta").json()["labels"]
    assert set(labels) == {"live", "simulated", "demo_data"}


def test_ready_after_startup(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ready"}


# --------------------------------------------------------------------- overview and tenants


def test_overview_is_computed_from_the_repository(client: TestClient) -> None:
    data = client.get("/api/overview").json()
    sov = data["sovereignty"]
    assert sov["provenance"] == Provenance.LIVE
    assert sov["residency"] == ["SA"]
    assert sov["third_party_ai_apis"] == 0
    assert len(sov["models"]) >= 10
    assert sov["licence"]["deny"] == 0
    assert sov["licence"]["downloadable"] == 0  # no licence verified yet
    assert data["resilience"]["mode"] == "balanced"
    assert data["assurance"]["adrs"] >= 19
    tm = data["assurance"]["threat_model"]
    assert tm["implemented"] > 0
    assert tm["planned"] > 0
    assert data["slo"]["provenance"] == Provenance.DEMO_DATA


def test_tenants_come_from_config(client: TestClient) -> None:
    tenants = {t["id"]: t for t in client.get("/api/tenants").json()["tenants"]}
    demo = tenants["demo-motors"]
    assert [b["id"] for b in demo["brands"]] == ["brand-a", "brand-b", "brand-c"]
    assert demo["brands"][2]["workloads"] == ["llm"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"tenant": "nope", "brand": "brand-a"}, 404),
        ({"tenant": "demo-motors", "brand": "nope"}, 404),
        ({"tenant": "Demo Motors", "brand": "brand-a"}, 400),
        ({"tenant": "demo-motors", "brand": "brand-a", "extra": 1}, 400),
    ],
)
def test_session_rejects_invalid(client: TestClient, body: dict[str, object], status: int) -> None:
    assert client.post("/api/session", json=body).status_code == status


def test_session_scopes_match_brand_workloads(client: TestClient) -> None:
    body = client.post("/api/session", json={"tenant": "demo-motors", "brand": "brand-c"}).json()
    assert body["scopes"] == ["demo-motors:brand-c:llm"]
    assert body["expires_in"] == 900


# --------------------------------------------------------------------- playground: chat


def test_chat_streams_and_content_never_reaches_logs_or_audit(client: TestClient) -> None:
    res = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": f"Book a service {TAG}"}]},
        headers=_session(client),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(res.text)
    assert events[0]["meta"]["mode"] == "mock"
    assert events[-1]["done"] is True
    assert events[-1]["mode"] == "mock"
    reply = "".join(str(e.get("delta", "")) for e in events)
    assert reply == MockLlm.reply_for("Book a service")
    records = client.get("/api/audit").json()["records"]
    assert TAG not in json.dumps(records)
    chat = next(r for r in records if r.get("workload") == "llm")
    assert chat["tenant"] == "demo-motors"
    assert chat["brand"] == "brand-a"
    assert chat["status"] == 200


def test_chat_arabic_reply(client: TestClient) -> None:
    res = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "أريد حجز موعد صيانة"}]},
        headers=_session(client),
    )
    reply = "".join(str(e.get("delta", "")) for e in _sse_events(res.text))
    assert is_arabic(reply)


def test_chat_requires_token(client: TestClient) -> None:
    res = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "missing_token"


def test_cross_brand_call_is_denied(client: TestClient) -> None:
    headers = {**_session(client, "brand-a"), "X-Triaxis-Brand": "brand-b"}
    res = client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "insufficient_scope"


def test_brand_without_workload_is_denied(client: TestClient) -> None:
    res = client.post("/api/tts", json={"input": "hello"}, headers=_session(client, "brand-c"))
    assert res.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "hacker", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x" * 8001}]},
        {"messages": [{"role": "user", "content": "x"}], "temperature": 2},
    ],
)
def test_chat_validation_does_not_echo(client: TestClient, body: dict[str, object]) -> None:
    res = client.post("/api/chat", json=body, headers=_session(client))
    assert res.status_code == 400
    assert "hacker" not in res.text


# --------------------------------------------------------------------- playground: STT / TTS


def test_stt_with_sample_wav(client: TestClient) -> None:
    sample = client.get("/api/stt/sample").content
    res = client.post("/api/stt?language=ar", content=sample, headers=_session(client))
    body = res.json()
    assert res.status_code == 200
    assert body["audio_format"] == "wav"
    assert body["duration_ms"] == 2000
    assert is_arabic(body["text"])
    assert body["provenance"] == {
        "transcript": "simulated",
        "validation": "live",
        "classification": "live",
    }
    assert body["classification"]["audio"]["data_tags"] == ["voice_recording"]


def test_stt_rejects_non_audio_by_magic_bytes(client: TestClient) -> None:
    res = client.post(
        "/api/stt",
        content=f"plain text {TAG}".encode(),
        headers={**_session(client), "Content-Type": "audio/wav"},
    )
    assert res.status_code == 415
    assert TAG not in res.text


def test_stt_rejects_long_audio(client: TestClient) -> None:
    long_wav = encode_wav(b"\x00\x00" * 16_000 * 61)
    res = client.post("/api/stt", content=long_wav, headers=_session(client))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "audio_too_long"


def test_stt_rejects_oversized_body(client: TestClient) -> None:
    res = client.post(
        "/api/stt", content=b"RIFF" + b"\x00" * (9 * 1024 * 1024), headers=_session(client)
    )
    assert res.status_code == 413


def test_tts_returns_wav(client: TestClient) -> None:
    res = client.post(
        "/api/tts",
        json={"input": "First sentence. Second one؟ Third.", "voice": "deep"},
        headers=_session(client),
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.headers["x-triaxis-mode"] == "mock"
    assert res.headers["x-triaxis-decision"] == "allow"
    assert sniff_audio(res.content) is AudioFormat.WAV


# --------------------------------------------------------------------- mocks


@pytest.mark.parametrize(
    ("data", "fmt"),
    [
        (b"fLaC" + b"\x00" * 8, AudioFormat.FLAC),
        (b"OggS" + b"\x00" * 8, AudioFormat.OGG),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 8, AudioFormat.WEBM),
        (b"ID3" + b"\x00" * 8, AudioFormat.MP3),
        (b"\xff\xfb" + b"\x00" * 8, AudioFormat.MP3),
    ],
)
def test_sniff_audio_formats(data: bytes, fmt: AudioFormat) -> None:
    assert sniff_audio(data) is fmt


def test_mock_stt_edge_cases() -> None:
    stt = MockStt()
    assert stt.transcribe(b"OggS" + b"\x00" * 10, "en").duration_ms is None
    with pytest.raises(TriaxisError) as empty:
        stt.transcribe(b"", "en")
    assert empty.value.code is ErrorCode.INVALID_REQUEST
    broken = b"RIFF\x00\x00\x00\x00WAVEjunk"
    with pytest.raises(TriaxisError) as bad:
        stt.transcribe(broken, "en")
    assert bad.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE


def test_wav_with_zero_rate_is_rejected() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00")
    data = bytearray(buffer.getvalue())
    data[24:28] = (0).to_bytes(4, "little")  # sample rate field
    with pytest.raises(TriaxisError):
        MockStt().transcribe(bytes(data), "en")


def test_mock_tts_limits() -> None:
    tts = MockTts(max_chars=10)
    with pytest.raises(TriaxisError):
        tts.synthesise("   ", "warm")
    with pytest.raises(TriaxisError) as long:
        tts.synthesise("x" * 11, "warm")
    assert long.value.code is ErrorCode.TOO_MANY_TOKENS


def test_split_sentences_handles_arabic_punctuation() -> None:
    assert split_sentences("مرحبا. كيف حالك؟ شكرا") == ["مرحبا.", "كيف حالك؟", "شكرا"]
    assert split_sentences("One.\nTwo") == ["One.", "Two"]


async def test_mock_llm_stream_delays() -> None:
    words = [w async for w in MockLlm(token_delay_s=0, first_token_delay_s=0).stream("hello")]
    assert "".join(words) == MockLlm.reply_for("hello")


# --------------------------------------------------------------------- security operations


def test_security_is_labelled_demo_data(client: TestClient) -> None:
    data = client.get("/api/security").json()
    assert data["provenance"] == Provenance.DEMO_DATA
    assert "No SOC" in data["notice"]
    assert data["bas"]["prevented"] + data["bas"]["detected"] + data["bas"]["missed"] == len(
        data["bas"]["results"]
    )


def test_remediation_advance(client: TestClient) -> None:
    assert (
        client.post("/api/security/remediation/REM-208/advance").json()["status"] == "in_progress"
    )
    assert client.post("/api/security/remediation/REM-999/advance").status_code == 404
    assert client.post("/api/security/remediation/REM-205/advance").status_code == 400
    assert client.post("/api/security/remediation/drop-table/advance").status_code == 400


def test_security_ops_instances_are_independent() -> None:
    a, b = SecurityOps(), SecurityOps()
    a.advance("REM-208")
    assert b.snapshot()["remediation"][0]["status"] == "open"


# --------------------------------------------------------------------- infrastructure simulation


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_failover_lifecycle_option_b() -> None:
    clock = Clock()
    sim = FailoverSimulator(clock)
    state = sim.state()
    assert state["phase"] == "normal"
    assert state["sites"]["riyadh"]["realtime_share_pct"] == 50.0
    assert state["realtime_recovery"] is None

    sim.fail("riyadh")
    clock.now += 1
    assert sim.state()["phase"] == "detecting"
    clock.now += 2
    mid = sim.state()
    assert mid["phase"] == "rerouting"
    assert 0 < mid["sites"]["riyadh"]["realtime_share_pct"] < 50
    clock.now += REROUTE_S
    over = sim.state()
    assert over["phase"] == "failed_over"
    assert over["sites"]["riyadh"]["health"] == "down"
    assert over["sites"]["jeddah"]["realtime_share_pct"] == 100.0
    assert over["sites"]["riyadh"]["batch_share_pct"] == 0.0
    assert over["realtime_recovery"]["within_target"] is True
    assert over["realtime_recovery"]["simulated_seconds"] <= 300
    assert over["batch_backlog_jobs"] > 0

    clock.now += 30  # a longer outage builds a backlog that takes a while to drain
    sim.restore()
    clock.now += 1
    rebalancing = sim.state()
    assert rebalancing["phase"] == "rebalancing"
    assert rebalancing["sites"]["riyadh"]["health"] == "degraded"
    clock.now += 3
    assert sim.state()["phase"] == "catching_up"
    clock.now += 60
    done = sim.state()
    assert done["phase"] == "recovered"
    assert done["batch_backlog_jobs"] == 0
    assert any("caught up" in e["event"] for e in done["timeline"])
    sim.reset()
    assert sim.state()["phase"] == "normal"


def test_failover_option_a_serves_batch() -> None:
    clock = Clock()
    sim = FailoverSimulator(clock)
    sim.set_mode(ResilienceMode.MIRROR)
    sim.fail("jeddah")
    clock.now += 10
    state = sim.state()
    assert state["mode"] == "mirror"
    assert state["batch_backlog_jobs"] == 0
    assert state["sites"]["riyadh"]["batch_share_pct"] == 100.0
    assert any("Option A" in e["event"] for e in state["timeline"])


def test_failover_invalid_transitions() -> None:
    sim = FailoverSimulator(Clock())
    with pytest.raises(TriaxisError):
        sim.restore()
    with pytest.raises(TriaxisError):
        sim.fail("dammam")
    sim.fail("riyadh")
    with pytest.raises(TriaxisError):
        sim.fail("jeddah")
    with pytest.raises(TriaxisError):
        sim.set_mode(ResilienceMode.MIRROR)


def test_infra_api(client: TestClient) -> None:
    assert client.get("/api/infra").json()["phase"] == "normal"
    assert client.post("/api/infra/mode", json={"mode": "mirror"}).json()["mode"] == "mirror"
    assert client.post("/api/infra/mode", json={"mode": "active-passive"}).status_code == 400
    assert (
        client.post("/api/infra/failover", json={"site": "riyadh"}).json()["failed_site"]
        == "riyadh"
    )
    assert client.post("/api/infra/failover", json={"site": "jeddah"}).status_code == 400
    assert client.post("/api/infra/restore").json()["phase"] == "rebalancing"
    assert client.post("/api/infra/reset").json()["phase"] == "normal"


# --------------------------------------------------------------------- audit


def test_audit_limit_and_validation(client: TestClient) -> None:
    client.get("/api/meta")
    assert len(client.get("/api/audit?limit=1").json()["records"]) == 1
    assert client.get("/api/audit?limit=0").status_code == 400


def test_audit_buffer_skips_invalid_lines() -> None:
    from triaxis_demo.audit import AuditBuffer

    buf = AuditBuffer(capacity=2)
    buf.record('{"prompt": "secret"}')
    assert buf.records() == []


def test_main_runs_uvicorn_on_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    import logging

    from triaxis_common import logging as tlog
    from triaxis_demo import __main__ as entry

    calls: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(kw, app=app))
    monkeypatch.setattr(entry, "harden_process", lambda *_a, **_k: None)
    root = logging.getLogger()
    saved = list(root.handlers), root.level, tlog._state.sink, tlog._state.strict
    try:
        entry.main(["--port", "9099"])
    finally:
        logging.captureWarnings(False)
        root.handlers = saved[0]
        root.setLevel(saved[1])
        tlog._state.sink = saved[2]
        tlog.set_strict(True)
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 9099
