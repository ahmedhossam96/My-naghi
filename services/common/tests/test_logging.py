from __future__ import annotations

import json
import logging
import warnings
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.support.logs import LogCapture
from triaxis_common import context
from triaxis_common import logging as tlog
from triaxis_common.errors import ErrorCode
from triaxis_common.logging import (
    ALLOWED_FIELDS,
    AllowListLogger,
    Event,
    Level,
    LogRecord,
    LogSchemaViolationError,
    Stage,
    ThirdPartyHandler,
    validate_log_line,
)
from triaxis_common.types import Lane, Workload

TAG = "ZR-TAG-7f3a91"  # stands in for customer content


def test_record_contains_only_allowed_fields(logs: LogCapture) -> None:
    logger = AllowListLogger("stt")
    logger.info(
        Event.REQUEST_COMPLETED,
        request_id="req-12345678",
        tenant="mynaghi",
        brand="toyota",
        workload=Workload.STT,
        lane=Lane.REALTIME,
        duration_ms=12.5,
        timings={Stage.AUTH: 1.0, Stage.STT: 10.0},
        status=200,
        bytes_in=1024,
        bytes_out=64,
        audio_ms=3000,
        error_code=ErrorCode.TIMEOUT,
    )
    (record,) = logs.records
    assert set(record) <= ALLOWED_FIELDS
    assert record["event"] == "request_completed"
    assert record["timings"] == {"auth": 1.0, "stt": 10.0}
    assert validate_log_line(logs.lines[0])


def test_unknown_field_raises_in_strict_mode(logs: LogCapture) -> None:
    logger = AllowListLogger("stt")
    with pytest.raises(LogSchemaViolationError) as excinfo:
        logger.info(Event.REQUEST_COMPLETED, transcript=TAG)
    assert TAG not in str(excinfo.value)
    assert "transcript" in str(excinfo.value)
    assert logs.lines == []


def test_free_text_in_typed_field_raises_in_strict_mode(logs: LogCapture) -> None:
    logger = AllowListLogger("stt")
    with pytest.raises(LogSchemaViolationError) as excinfo:
        logger.info(Event.REQUEST_COMPLETED, tenant=f"mynaghi {TAG}")
    assert TAG not in str(excinfo.value)


def test_violation_degrades_without_content_when_not_strict(logs: LogCapture) -> None:
    tlog.set_strict(False)
    try:
        AllowListLogger("stt").info(Event.REQUEST_COMPLETED, transcript=TAG)
    finally:
        tlog.set_strict(True)
    (record,) = logs.records
    assert record["event"] == "log_schema_violation"
    assert TAG not in logs.text()


def test_invalid_service_name_degrades_to_unknown(logs: LogCapture) -> None:
    tlog.set_strict(False)
    try:
        AllowListLogger("Not A Slug").info(Event.SERVICE_READY)
    finally:
        tlog.set_strict(True)
    assert logs.records[0]["service"] == "unknown"


def test_context_and_bound_fields_are_merged(logs: LogCapture) -> None:
    token = context.set_context(context.RequestContext(request_id="req-abcdef12"))
    try:
        context.update_context(tenant="mynaghi", brand="lexus", workload=Workload.LLM)
        AllowListLogger("llm").bind(lane=Lane.BATCH).warning(Event.QUOTA_EXCEEDED)
    finally:
        context.reset_context(token)
    record = logs.records[0]
    assert record["request_id"] == "req-abcdef12"
    assert record["tenant"] == "mynaghi"
    assert record["brand"] == "lexus"
    assert record["workload"] == "llm"
    assert record["lane"] == "batch"
    assert record["level"] == "warning"
    assert context.current().request_id is None


def test_exception_logs_class_name_only(logs: LogCapture) -> None:
    try:
        raise ValueError(TAG)
    except ValueError as exc:
        AllowListLogger("tts").log(Event.REQUEST_FAILED, Level.ERROR, exc=exc)
    assert logs.records[0]["error_class"] == "ValueError"
    assert TAG not in logs.text()


def test_level_filtering(logs: LogCapture) -> None:
    previous = tlog._state.min_level
    tlog._state.min_level = Level.WARNING
    try:
        logger = AllowListLogger("tts")
        logger.debug(Event.SERVICE_READY)
        logger.info(Event.SERVICE_READY)
        logger.error(Event.DEPENDENCY_ERROR)
    finally:
        tlog._state.min_level = previous
    assert [r["event"] for r in logs.records] == ["dependency_error"]


@pytest.mark.parametrize(
    ("levelno", "expected"),
    [
        (logging.DEBUG, Level.DEBUG),
        (logging.INFO, Level.INFO),
        (logging.WARNING, Level.WARNING),
        (logging.ERROR, Level.ERROR),
        (logging.CRITICAL, Level.CRITICAL),
        (5, Level.DEBUG),
    ],
)
def test_level_from_stdlib(levelno: int, expected: Level) -> None:
    assert Level.from_stdlib(levelno) is expected


def test_third_party_messages_and_args_are_discarded(logs: LogCapture) -> None:
    std = logging.getLogger("thirdparty.lib")
    std.handlers = [ThirdPartyHandler(AllowListLogger("router"))]
    std.propagate = False
    std.setLevel(logging.INFO)
    try:
        std.warning("user said %s", TAG)
        try:
            raise KeyError(TAG)
        except KeyError:
            std.exception("failed on %s", TAG)
    finally:
        std.handlers = []
        std.propagate = True
    assert TAG not in logs.text()
    first, second = logs.records
    assert first == {**first, "event": "third_party_log", "source": "thirdparty.lib"}
    assert second["error_class"] == "KeyError"
    assert second["level"] == "error"


def test_third_party_handler_never_raises(logs: LogCapture) -> None:
    handler = ThirdPartyHandler(AllowListLogger("router"))
    record = logging.LogRecord("bad name with spaces!", logging.INFO, __file__, 1, TAG, (), None)
    handler.emit(record)  # invalid logger name is simply omitted
    assert "source" not in logs.records[0]
    handler.handleError(record)  # no-op, prints nothing


def test_third_party_handler_swallows_internal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = ThirdPartyHandler(AllowListLogger("router"))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(AllowListLogger, "_log", boom)
    handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None))


def test_configure_logging_routes_stdlib_and_warnings() -> None:
    capture = LogCapture()
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_sink = tlog._state.sink
    try:
        logger = tlog.configure_logging("gateway", site="riyadh", strict=True, sink=capture)
        assert logger.service == "gateway"
        logging.getLogger("uvicorn.error").error("boom %s", TAG)
        logging.getLogger("uvicorn.access").info("GET /v1/x?q=%s", TAG)  # silenced
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(TAG, UserWarning, stacklevel=1)
    finally:
        logging.captureWarnings(False)
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        tlog._state.sink = saved_sink
        tlog._state.site = None
        tlog._state.min_level = Level.INFO
        tlog.set_strict(True)
    assert TAG not in capture.text()
    records = capture.records
    assert {r["source"] for r in records} == {"uvicorn.error", "py.warnings"}
    assert all(r["site"] == "riyadh" for r in records)


def test_configure_logging_debug_level() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_sink = tlog._state.sink
    try:
        tlog.configure_logging("gateway", level=Level.DEBUG, strict=True, sink=LogCapture())
        assert root.level == logging.DEBUG
    finally:
        logging.captureWarnings(False)
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        tlog._state.sink = saved_sink
        tlog._state.min_level = Level.INFO
        tlog.set_strict(True)


def test_default_sink_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    tlog._stdout_sink('{"a":1}')
    assert capsys.readouterr().out == '{"a":1}\n'


def test_get_logger() -> None:
    assert tlog.get_logger("stt").service == "stt"


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00Z",
                "level": "info",
                "event": "service_ready",
                "service": "stt",
                "message": "hello",
            }
        ),
        json.dumps(
            {"ts": "2026-01-01T00:00:00Z", "level": "info", "event": "free text", "service": "stt"}
        ),
    ],
)
def test_validate_log_line_rejects(line: str) -> None:
    assert not validate_log_line(line)


def test_log_record_is_strict() -> None:
    with pytest.raises(ValueError, match="validation error"):
        LogRecord.model_validate(
            {
                "ts": datetime.now(UTC),
                "level": Level.INFO,
                "event": Event.SERVICE_READY,
                "service": "stt",
                "status": "200",
            }
        )


@settings(max_examples=200, deadline=None)
@given(
    content=st.text(min_size=6, max_size=200),
    field=st.sampled_from(
        ["tenant", "brand", "request_id", "source", "error_class", "site", "transcript", "message"]
    ),
)
def test_property_arbitrary_content_never_reaches_output(content: str, field: str) -> None:
    """Whatever string a caller passes, it is either a valid identifier or never emitted."""
    capture: list[str] = []
    previous = tlog._state.sink
    tlog._state.sink = capture.append
    tlog.set_strict(False)
    try:
        AllowListLogger("stt").info(Event.REQUEST_COMPLETED, **{field: content})
    finally:
        tlog.set_strict(True)
        tlog._state.sink = previous
    (line,) = capture
    assert validate_log_line(line)
    record = json.loads(line)
    if record["event"] == "request_completed":
        # Accepted only because it fits the field's strict pattern.
        assert field in {"tenant", "brand", "request_id", "source", "error_class", "site"}
    else:
        # Degraded record: no caller-supplied field survives.
        assert record["event"] == "log_schema_violation"
        assert set(record) == {"ts", "level", "event", "service"}


def test_listeners_receive_validated_lines(logs: LogCapture) -> None:
    received: list[str] = []
    tlog.add_listener(received.append)
    try:
        AllowListLogger("demo").info(Event.SERVICE_READY)
    finally:
        tlog.remove_listener(received.append)
        tlog.remove_listener(received.append)  # removing twice is a no-op
    AllowListLogger("demo").info(Event.SERVICE_READY)
    assert len(received) == 1
    assert validate_log_line(received[0])
