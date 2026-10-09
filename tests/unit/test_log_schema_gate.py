"""The session-wide log schema gate (root conftest.py) is itself tested here."""

from __future__ import annotations

import json

from tests.support.log_gate import captured_log_lines
from triaxis_common.logging import AllowListLogger, Event, validate_log_line


def test_gate_sees_emitted_records() -> None:
    AllowListLogger("router").info(Event.SERVICE_READY, request_id="gate-test-0001")
    lines = captured_log_lines()
    assert any(json.loads(line).get("request_id") == "gate-test-0001" for line in lines)


def test_every_record_so_far_fits_the_schema() -> None:
    lines = captured_log_lines()
    assert lines, "the gate captured nothing; is the audit hook installed?"
    assert all(validate_log_line(line) for line in lines)


def test_gate_would_reject_extra_fields() -> None:
    good = json.loads(captured_log_lines()[-1])
    assert not validate_log_line(json.dumps({**good, "prompt": "hello"}))
