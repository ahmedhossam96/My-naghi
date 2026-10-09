"""Fail if the CI workflow copies drift from the canonical definitions (ADR 0003).

Canonical files live in ``.ci/workflows/``. Each must have a byte-identical copy in
``.github/workflows/`` and ``.forgejo/workflows/``, and those directories must not
contain workflows that are missing from ``.ci/workflows/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CANONICAL = Path(".ci/workflows")
MIRRORS = (Path(".github/workflows"), Path(".forgejo/workflows"))


def find_drift(root: Path) -> list[str]:
    """Return human-readable problems; empty means in sync."""
    problems: list[str] = []
    canonical_dir = root / CANONICAL
    canonical = {p.name: p.read_bytes() for p in sorted(canonical_dir.glob("*.yml"))}
    if not canonical:
        problems.append(f"no workflows found in {CANONICAL}")
        return problems
    for mirror in MIRRORS:
        mirror_dir = root / mirror
        present = {p.name for p in mirror_dir.glob("*.yml")}
        for name, content in canonical.items():
            copy = mirror_dir / name
            if not copy.is_file():
                problems.append(f"{mirror / name}: missing copy of {CANONICAL / name}")
            elif copy.read_bytes() != content:
                problems.append(f"{mirror / name}: differs from {CANONICAL / name}")
        problems.extend(
            f"{mirror / extra}: not present in {CANONICAL}"
            for extra in sorted(present - canonical.keys())
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    args = parser.parse_args(argv)
    problems = find_drift(args.root)
    for problem in problems:
        print(f"ci-sync: {problem}", file=sys.stderr)
    if not problems:
        print("ci-sync: workflow copies are in sync")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
