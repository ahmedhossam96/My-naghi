from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from tests.support.logs import LogCapture
from triaxis_common import context
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.http import BodySizeLimitMiddleware, create_base_app
from triaxis_common.logging import AllowListLogger

TAG = "ZR-TAG-c0ffee42"


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def _client() -> TestClient:
    app = create_base_app(logger=AllowListLogger("stt"), max_body_bytes=256)

    @app.post("/v1/echo")
    async def echo(body: Echo) -> dict[str, int]:
        return {"length": len(body.text)}

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError(TAG)

    @app.get("/v1/limited")
    async def limited() -> None:
        raise TriaxisError(ErrorCode.RATE_LIMITED, retry_after_s=7)

    @app.get("/v1/request-id")
    async def request_id() -> dict[str, str | None]:
        return {"request_id": context.current().request_id}

    return TestClient(app, raise_server_exceptions=False)


def test_health_and_readiness() -> None:
    client = _client()
    assert client.get("/healthz").json() == {"status": "ok"}
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["error"]["code"] == "not_ready"
    client.app.state.ready = True  # type: ignore[attr-defined]
    assert client.get("/readyz").json() == {"status": "ready"}


def test_success_is_logged_with_allow_listed_fields(logs: LogCapture) -> None:
    response = _client().post("/v1/echo", json={"text": TAG})
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    (record,) = logs.records
    assert record["event"] == "request_completed"
    assert record["status"] == 200
    assert record["bytes_in"] > 0
    assert record["bytes_out"] > 0
    assert TAG not in logs.text()


def test_validation_error_does_not_echo_input(logs: LogCapture) -> None:
    response = _client().post("/v1/echo", json={"text": 1, TAG: TAG})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert TAG not in response.text
    assert TAG not in logs.text()
    assert logs.records[0]["event"] == "request_rejected"
    assert logs.records[0]["error_code"] == "invalid_request"


def test_unhandled_exception_is_generic(logs: LogCapture) -> None:
    response = _client().get("/v1/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert TAG not in response.text
    assert TAG not in logs.text()
    failed = [r for r in logs.records if r["event"] == "request_failed"]
    assert failed
    assert failed[0]["error_class"] == "RuntimeError"


def test_triaxis_error_with_retry_after() -> None:
    response = _client().get("/v1/limited")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_not_found_and_method_not_allowed() -> None:
    client = _client()
    assert client.get(f"/v1/{TAG}").json()["error"]["code"] == "not_found"
    assert TAG not in client.get(f"/v1/{TAG}").text
    assert client.get("/v1/echo").json()["error"]["code"] == "method_not_allowed"


def test_incoming_request_id_is_kept_when_valid() -> None:
    client = _client()
    kept = client.get("/v1/request-id", headers={"x-request-id": "abcdef12-3456"})
    assert kept.json() == {"request_id": "abcdef12-3456"}
    assert kept.headers["x-request-id"] == "abcdef12-3456"


@pytest.mark.parametrize("bad", ["short", "has spaces in it", TAG + "!" * 80, "zr١٢٣٤٥٦"])
def test_invalid_incoming_request_id_is_replaced(bad: str) -> None:
    response = _client().get("/v1/request-id", headers=[(b"x-request-id", bad.encode())])
    assert response.json()["request_id"] != bad
    assert len(response.json()["request_id"]) == 32


def test_body_too_large_by_content_length(logs: LogCapture) -> None:
    response = _client().post("/v1/echo", json={"text": "x" * 1000})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert logs.records[0]["event"] == "request_rejected"


def test_body_too_large_when_streamed_without_length() -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b'{"text": "' + b"x" * 100

    response = _client().post(
        "/v1/echo",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_invalid_content_length_header_falls_back_to_counting() -> None:
    client = _client()
    response = client.post(
        "/v1/echo",
        content=b'{"text": "ok"}',
        headers={"content-type": "application/json", "content-length": "14"},
    )
    assert response.status_code == 200


def test_body_limit_requires_positive_cap() -> None:
    async def app(scope: object, receive: object, send: object) -> None:
        return None

    with pytest.raises(ValueError, match="positive"):
        BodySizeLimitMiddleware(app, 0)


def test_websocket_scope_gets_request_id() -> None:
    app = create_base_app(logger=AllowListLogger("stt"), max_body_bytes=256)

    @app.websocket("/v1/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"request_id": context.current().request_id})
        await websocket.close()

    with TestClient(app).websocket_connect("/v1/ws") as conn:
        assert len(conn.receive_json()["request_id"]) == 32


def test_websocket_validation_error_does_not_echo_input() -> None:
    app = create_base_app(logger=AllowListLogger("stt"), max_body_bytes=256)

    @app.websocket("/v1/ws")
    async def ws(websocket: WebSocket, sample_rate: int) -> None:
        await websocket.accept()

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        TestClient(app).websocket_connect(f"/v1/ws?sample_rate={TAG}"),
    ):
        pass
    assert excinfo.value.code == 1008
    assert excinfo.value.reason == "invalid_request"
    assert TAG not in str(excinfo.value.reason)


async def test_lifespan_scope_passes_through() -> None:
    from triaxis_common.http import RequestContextMiddleware

    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    async def noop() -> dict[str, object]:
        return {}

    async def nosend(message: object) -> None:
        return None

    mw = RequestContextMiddleware(inner, AllowListLogger("stt"))  # type: ignore[arg-type]
    await mw({"type": "lifespan"}, noop, nosend)
    limit = BodySizeLimitMiddleware(inner, 10)  # type: ignore[arg-type]
    await limit({"type": "lifespan"}, noop, nosend)
    assert seen == ["lifespan", "lifespan"]
