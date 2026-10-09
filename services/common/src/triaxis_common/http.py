"""HTTP plumbing shared by every service.

- :class:`RequestContextMiddleware`: request IDs, request context, and one
  allow-listed ``request_completed`` / ``request_rejected`` / ``request_failed``
  record per request (replaces the uvicorn access log, which contains paths).
- :class:`BodySizeLimitMiddleware`: rejects bodies above a cap, by
  ``Content-Length`` and by counting streamed bytes.
- :func:`install_error_handlers`: every error leaves as an
  :class:`~triaxis_common.errors.ErrorResponse` (ADR 0016).
- :func:`create_base_app`: a FastAPI app with all of the above plus
  ``/healthz`` and ``/readyz``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Final

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from triaxis_common import context
from triaxis_common.errors import (
    CATALOG,
    ErrorCode,
    ErrorResponse,
    LimitExceededError,
    TriaxisError,
    code_for_status,
)
from triaxis_common.logging import AllowListLogger, Event, Level
from triaxis_common.types import is_request_id

REQUEST_ID_HEADER: Final = "x-request-id"
_STATE_ERROR_CODE: Final = "triaxis_error_code"
_STATE_ERROR_CLASS: Final = "triaxis_error_class"


def error_response(
    code: ErrorCode, *, request_id: str | None = None, retry_after_s: int | None = None
) -> JSONResponse:
    spec = CATALOG[code]
    headers: dict[str, str] = {}
    if spec.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after_s is not None:
        headers["Retry-After"] = str(retry_after_s)
    body = ErrorResponse.for_code(code, request_id=request_id)
    return JSONResponse(body.model_dump(mode="json"), status_code=spec.status, headers=headers)


def _note_error(request: Request, code: ErrorCode, exc: BaseException) -> None:
    request.state.triaxis_error_code = code
    request.state.triaxis_error_class = type(exc).__name__


async def _triaxis_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, TriaxisError):  # pragma: no cover - registered for TriaxisError
        return await _unhandled_error(request, exc)
    _note_error(request, exc.code, exc)
    return error_response(
        exc.code, request_id=context.current().request_id, retry_after_s=exc.retry_after_s
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    # No field values and no locations: locations can contain client-chosen keys.
    _note_error(request, ErrorCode.INVALID_REQUEST, exc)
    return error_response(ErrorCode.INVALID_REQUEST, request_id=context.current().request_id)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registered for it
        return await _unhandled_error(request, exc)
    code = code_for_status(exc.status_code)
    _note_error(request, code, exc)
    return error_response(code, request_id=context.current().request_id)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    _note_error(request, ErrorCode.INTERNAL_ERROR, exc)
    return error_response(ErrorCode.INTERNAL_ERROR, request_id=context.current().request_id)


async def _websocket_validation_error(websocket: WebSocket, _exc: Exception) -> None:
    # FastAPI's default puts the validation errors, including client input, into
    # the close reason. Close with a fixed catalogue code instead.
    await websocket.close(code=WS_1008_POLICY_VIOLATION, reason=ErrorCode.INVALID_REQUEST.value)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(TriaxisError, _triaxis_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(WebSocketRequestValidationError, _websocket_validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)


class RequestContextMiddleware:
    """Assigns a request ID and logs one allow-listed record per request."""

    def __init__(self, app: ASGIApp, logger: AllowListLogger) -> None:
        self._app = app
        self._logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = context.set_context(context.RequestContext(request_id=request_id))
        state: dict[str, Any] = scope.setdefault("state", {})
        started = time.perf_counter()
        status = 500
        bytes_in = 0
        bytes_out = 0

        async def counting_receive() -> Message:
            nonlocal bytes_in
            message = await receive()
            if message["type"] == "http.request":
                bytes_in += len(message.get("body", b""))
            return message

        async def tagging_send(message: Message) -> None:
            nonlocal status, bytes_out
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                bytes_out += len(message.get("body", b""))
            await send(message)

        failure: BaseException | None = None
        try:
            await self._app(scope, counting_receive, tagging_send)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if scope["type"] == "http":
                self._log(
                    state=state,
                    status=status,
                    failure=failure,
                    started=started,
                    bytes_in=bytes_in,
                    bytes_out=bytes_out,
                )
            context.reset_context(token)

    def _log(
        self,
        *,
        state: dict[str, Any],
        status: int,
        failure: BaseException | None,
        started: float,
        bytes_in: int,
        bytes_out: int,
    ) -> None:
        fields: dict[str, object] = {
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
        }
        code = state.get(_STATE_ERROR_CODE)
        if isinstance(code, ErrorCode):
            fields["error_code"] = code
        error_class = state.get(_STATE_ERROR_CLASS)
        if failure is not None:
            fields["error_class"] = type(failure).__name__
        elif isinstance(error_class, str):
            fields["error_class"] = error_class
        if status >= 500 or failure is not None:
            self._logger.log(Event.REQUEST_FAILED, Level.ERROR, **fields)  # type: ignore[arg-type]
        elif status >= 400:
            self._logger.log(Event.REQUEST_REJECTED, Level.INFO, **fields)  # type: ignore[arg-type]
        else:
            self._logger.log(Event.REQUEST_COMPLETED, Level.INFO, **fields)  # type: ignore[arg-type]


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode():
            try:
                candidate = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            return candidate if is_request_id(candidate) else None
    return None


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than ``max_bytes`` (HTTP 413)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            await self._reject(scope, receive, send)
            return
        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            # Once over the cap, tell the app the client went away so it stops
            # reading; the 413 is sent below (frameworks turn exceptions raised
            # from receive() into generic 400s).
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return  # the app's response to the fake disconnect is dropped
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not response_started:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        state: dict[str, Any] = scope.setdefault("state", {})
        state[_STATE_ERROR_CODE] = ErrorCode.PAYLOAD_TOO_LARGE
        state[_STATE_ERROR_CLASS] = LimitExceededError.__name__
        response = error_response(
            ErrorCode.PAYLOAD_TOO_LARGE, request_id=context.current().request_id
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def create_base_app(
    *,
    logger: AllowListLogger,
    max_body_bytes: int,
    title: str = "triaxis",
    version: str = "0.1.0",
) -> FastAPI:
    """FastAPI app with Triaxis middleware, error handlers and health probes.

    Interactive docs are disabled; OpenAPI specs are published from
    ``docs/api/`` instead. Set ``app.state.ready = True`` only after models are
    loaded and warmed.
    """
    app = FastAPI(title=title, version=version, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    install_error_handlers(app)
    # Order: the context middleware is outermost so every response carries a
    # request ID and is logged, including 413s from the size limit.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    app.add_middleware(RequestContextMiddleware, logger=logger)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False, response_model=None)
    async def readyz(request: Request) -> dict[str, str] | JSONResponse:
        if not request.app.state.ready:
            return error_response(ErrorCode.NOT_READY, request_id=context.current().request_id)
        return {"status": "ready"}

    return app
