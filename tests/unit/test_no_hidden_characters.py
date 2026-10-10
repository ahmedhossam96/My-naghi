"""Fail on invisible / bidi control characters in source (Trojan Source, CVE-2021-42574).

Arabic text is welcome in tests and data; invisible formatting characters are
not. Use ``chr(0x...)`` or escapes when code needs them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".yaml", ".yml", ".toml", ".json", ".sh"}
HIDDEN = {
    chr(cp)
    for cp in (
        0x00AD,
        *range(0x200B, 0x2010),
        *range(0x202A, 0x202F),
        *range(0x2060, 0x2065),
        *range(0x2066, 0x206A),
        0xFEFF,
    )
}


def _tracked_and_new_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [REPO / p for p in out.split("\0") if p and Path(p).suffix in SUFFIXES]


def test_no_hidden_or_bidi_control_characters() -> None:
    offenders = []
    for path in _tracked_and_new_files():
        if not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if HIDDEN & set(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not offenders, offenders
