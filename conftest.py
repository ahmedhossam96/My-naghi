"""Session-wide test configuration.

Log schema gate (ADR 0007): every log line emitted during the test session is
captured and validated against the allow-list schema. Any violation fails the
build, even if every individual test passed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support import log_gate
from tests.support.logs import LogCapture
from triaxis_common import logging as tlog


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    """Capture log output of one test (the session schema gate still sees it)."""
    capture = LogCapture()
    previous = tlog.set_sink(capture)
    try:
        yield capture
    finally:
        tlog.set_sink(previous)


def pytest_configure() -> None:
    tlog.set_strict(True)
    tlog.set_audit_hook(log_gate.record)


def pytest_sessionfinish(session: pytest.Session) -> None:
    tlog.set_audit_hook(None)
    violations = [
        line for line in log_gate.captured_log_lines() if not tlog.validate_log_line(line)
    ]
    if violations:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"LOG SCHEMA GATE: {len(violations)} record(s) outside the allow-list schema",
                red=True,
            )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
