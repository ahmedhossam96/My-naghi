"""Allow-list structured logging (ADR 0007).

Services log only through :class:`AllowListLogger`. Every record is validated
against :class:`LogRecord` (``extra="forbid"``, strict types, enums instead of
free text) before it is written. A record that does not fit the schema is never
emitted: in strict mode (tests, CI) the logger raises; otherwise it emits a
``log_schema_violation`` record that contains no caller-supplied values.

Standard-library and third-party loggers are routed through
:class:`ThirdPartyHandler`, which discards message text and arguments.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    ValidationError,
)

from triaxis_common import context
from triaxis_common.errors import ErrorCode
from triaxis_common.types import (
    LOGGER_NAME_PATTERN,
    Identifier,
    Lane,
    LoggerName,
    RequestId,
    Slug,
    Workload,
    is_slug,
)

_LOGGER_NAME_RE: Final = re.compile(LOGGER_NAME_PATTERN)


class Level(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @classmethod
    def from_stdlib(cls, levelno: int) -> Level:
        if levelno >= logging.CRITICAL:
            return cls.CRITICAL
        if levelno >= logging.ERROR:
            return cls.ERROR
        if levelno >= logging.WARNING:
            return cls.WARNING
        if levelno >= logging.INFO:
            return cls.INFO
        return cls.DEBUG


_SEVERITY: Final[dict[Level, int]] = {
    Level.DEBUG: 10,
    Level.INFO: 20,
    Level.WARNING: 30,
    Level.ERROR: 40,
    Level.CRITICAL: 50,
}


class Event(StrEnum):
    """What happened. Free-text messages do not exist in Triaxis logs."""

    SERVICE_STARTING = "service_starting"
    SERVICE_READY = "service_ready"
    SERVICE_STOPPING = "service_stopping"
    CONFIG_LOADED = "config_loaded"
    MODEL_LOADED = "model_loaded"
    MODEL_WARMED = "model_warmed"
    REQUEST_COMPLETED = "request_completed"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_FAILED = "request_failed"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXCEEDED = "quota_exceeded"
    JWKS_REFRESHED = "jwks_refreshed"
    JWKS_REFRESH_FAILED = "jwks_refresh_failed"
    SECRET_FETCHED = "secret_fetched"  # noqa: S105 - event name
    SECRET_FETCH_FAILED = "secret_fetch_failed"  # noqa: S105 - event name
    DEPENDENCY_ERROR = "dependency_error"
    RUNTIME_GUARD_FAILED = "runtime_guard_failed"
    THIRD_PARTY_LOG = "third_party_log"
    LOG_SCHEMA_VIOLATION = "log_schema_violation"


class Stage(StrEnum):
    """Named pipeline stages for the ``timings`` map and span names."""

    AUTH = "auth"
    GUARDRAILS_IN = "guardrails_in"
    GUARDRAILS_OUT = "guardrails_out"
    QUEUE = "queue"
    VAD = "vad"
    STT = "stt"
    STT_PARTIAL = "stt_partial"
    LLM = "llm"
    TTFT = "ttft"
    TTS = "tts"
    FIRST_AUDIO = "first_audio"
    VOICE_TO_VOICE = "voice_to_voice"
    TOTAL = "total"


class LogRecord(BaseModel):
    """The complete set of fields a Triaxis log record may contain."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ts: AwareDatetime
    level: Level
    event: Event
    service: Slug
    site: Slug | None = None
    source: LoggerName | None = None
    request_id: RequestId | None = None
    tenant: Slug | None = None
    brand: Slug | None = None
    workload: Workload | None = None
    lane: Lane | None = None
    duration_ms: NonNegativeFloat | None = None
    timings: dict[Stage, NonNegativeFloat] | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    bytes_in: NonNegativeInt | None = None
    bytes_out: NonNegativeInt | None = None
    audio_ms: NonNegativeInt | None = None
    tokens_in: NonNegativeInt | None = None
    tokens_out: NonNegativeInt | None = None
    error_class: Identifier | None = None
    error_code: ErrorCode | None = None


ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(LogRecord.model_fields)

Sink = Callable[[str], None]


class LogSchemaViolationError(Exception):
    """Raised in strict mode when a record does not fit :class:`LogRecord`.

    The message lists only offending field *names* (code-defined keyword
    arguments), never values.
    """


def _stdout_sink(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sink: Sink = _stdout_sink
        self.strict = False
        self.min_level = Level.INFO
        self.site: str | None = None
        self.audit_hook: Sink | None = None
        self.listeners: list[Sink] = []


_state = _State()


def _emit(line: str) -> None:
    with _state.lock:
        _state.sink(line)
        if _state.audit_hook is not None:
            _state.audit_hook(line)
        for listener in _state.listeners:
            listener(line)


def set_strict(strict: bool) -> None:
    """Strict mode raises on schema violations instead of degrading (tests, CI)."""
    _state.strict = strict


def validate_log_line(line: str) -> bool:
    """True if ``line`` is one JSON log record that fits the allow-list schema.

    Used by the test-session schema gate and by the zero-retention suite.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or not set(data) <= ALLOWED_FIELDS:
        return False
    try:
        LogRecord.model_validate_json(line)
    except ValidationError:
        return False
    return True


def set_audit_hook(hook: Sink | None) -> None:
    """Install a callback that receives every emitted line (test schema gate)."""
    _state.audit_hook = hook


def add_listener(listener: Sink) -> None:
    """Receive every emitted (already validated, allow-listed) line, e.g. an audit view."""
    with _state.lock:
        _state.listeners.append(listener)


def remove_listener(listener: Sink) -> None:
    with _state.lock:
        if listener in _state.listeners:
            _state.listeners.remove(listener)


def _violation_field_names(exc: ValidationError) -> list[str]:
    names = {str(err["loc"][0]) for err in exc.errors(include_input=False) if err["loc"]}
    return sorted(n for n in names if n.isidentifier())


class AllowListLogger:
    """Structured logger whose only inputs are allow-listed fields."""

    def __init__(self, service: str, bound: Mapping[str, object] | None = None) -> None:
        self._service = service
        self._bound: dict[str, object] = dict(bound or {})

    @property
    def service(self) -> str:
        return self._service

    def bind(self, **fields: object) -> AllowListLogger:
        return AllowListLogger(self._service, {**self._bound, **fields})

    def log(
        self,
        event: Event,
        level: Level = Level.INFO,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if exc is not None:
            fields["error_class"] = type(exc).__name__
        self._log(event, level, fields, strict=_state.strict)

    def _log(
        self, event: Event, level: Level, fields: Mapping[str, object], *, strict: bool
    ) -> None:
        if level.severity < _state.min_level.severity:
            return
        data: dict[str, Any] = {
            "ts": datetime.now(UTC),
            "level": level,
            "event": event,
            "service": self._service,
        }
        if _state.site is not None:
            data["site"] = _state.site
        data.update(context.current().as_fields())
        data.update(self._bound)
        data.update(fields)
        try:
            record = LogRecord.model_validate(data)
        except ValidationError as err:
            if strict:
                names = ", ".join(_violation_field_names(err)) or "<unknown>"
                raise LogSchemaViolationError(
                    f"log record does not match the allow-list schema; fields: {names}"
                ) from None
            record = LogRecord(
                ts=datetime.now(UTC),
                level=Level.ERROR,
                event=Event.LOG_SCHEMA_VIOLATION,
                service=self._service if is_slug(self._service) else "unknown",
            )
        _emit(record.model_dump_json(exclude_none=True))

    def debug(self, event: Event, **fields: object) -> None:
        self._log(event, Level.DEBUG, fields, strict=_state.strict)

    def info(self, event: Event, **fields: object) -> None:
        self._log(event, Level.INFO, fields, strict=_state.strict)

    def warning(self, event: Event, **fields: object) -> None:
        self._log(event, Level.WARNING, fields, strict=_state.strict)

    def error(self, event: Event, **fields: object) -> None:
        self._log(event, Level.ERROR, fields, strict=_state.strict)


class ThirdPartyHandler(logging.Handler):
    """Routes stdlib/third-party log records to the allow-list logger.

    The record's message and arguments are discarded unconditionally; only the
    level, the logger name and (if present) the exception class survive.
    """

    def __init__(self, logger: AllowListLogger) -> None:
        super().__init__(level=logging.NOTSET)
        self._logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields: dict[str, object] = {}
            if _LOGGER_NAME_RE.fullmatch(record.name):
                fields["source"] = record.name
            if record.exc_info and record.exc_info[0] is not None:
                fields["error_class"] = record.exc_info[0].__name__
            # Never strict here: a third-party record must not crash the caller.
            self._logger._log(
                Event.THIRD_PARTY_LOG, Level.from_stdlib(record.levelno), fields, strict=False
            )
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802, ARG002 - stdlib API
        # The default implementation prints the record (with content) to stderr.
        return


def configure_logging(
    service: str,
    *,
    site: str | None = None,
    strict: bool = False,
    level: Level = Level.INFO,
    sink: Sink | None = None,
) -> AllowListLogger:
    """Configure process-wide logging and return the service logger.

    Replaces every handler on the root logger with :class:`ThirdPartyHandler`,
    captures Python warnings, and silences uvicorn's access log (the HTTP
    middleware emits an allow-listed ``request_completed`` record instead).
    """
    _state.strict = strict
    _state.min_level = level
    _state.site = site
    _state.sink = sink or _stdout_sink
    logger = AllowListLogger(service)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(ThirdPartyHandler(logger))
    root.setLevel(logging.DEBUG if level is Level.DEBUG else logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        named = logging.getLogger(name)
        named.handlers.clear()
        named.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return logger


def get_logger(service: str) -> AllowListLogger:
    return AllowListLogger(service)
