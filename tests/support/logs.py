"""Per-test log capture helper."""

from __future__ import annotations

import json
from typing import Any


class LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    def text(self) -> str:
        return "\n".join(self.lines)
