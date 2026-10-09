"""Live audit view: the allow-listed log records this process emits.

The buffer subscribes to the real ``triaxis_common.logging`` output. Because
every line has already passed the allow-list schema (ADR 0007), the audit view
can only ever show metadata — request IDs, tenant, brand, workload, status,
timings and sizes — never prompts, transcripts or audio.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any

from triaxis_common import logging as tlog


class AuditBuffer:
    def __init__(self, capacity: int = 500) -> None:
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def attach(self) -> None:
        tlog.add_listener(self.record)

    def detach(self) -> None:
        tlog.remove_listener(self.record)

    def records(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            lines = list(self._lines)[-limit:]
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if tlog.validate_log_line(line):  # defensive: show schema-valid lines only
                out.append(json.loads(line))
        return out
