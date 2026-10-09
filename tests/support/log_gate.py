"""Buffer for the session-wide log schema gate (see the root conftest.py)."""

from __future__ import annotations

_captured: list[str] = []


def record(line: str) -> None:
    _captured.append(line)


def captured_log_lines() -> list[str]:
    """All log lines emitted so far in this test session (copy)."""
    return list(_captured)
