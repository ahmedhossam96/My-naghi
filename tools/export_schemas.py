"""Export JSON Schemas for configuration files to ``config/schema/``.

python -m tools.export_schemas           # write
python -m tools.export_schemas --check   # fail if the committed schemas are stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from tools.backup import BackupSpec
from tools.model_registry import Registry
from triaxis_common.config import HardwareProfile, TenantConfig
from triaxis_common.policy import HandlingPolicy

SCHEMA_DIR = Path("config/schema")
MODELS: dict[str, type[BaseModel]] = {
    "tenant.schema.json": TenantConfig,
    "hardware-profile.schema.json": HardwareProfile,
    "model-registry.schema.json": Registry,
    "data-policy.schema.json": HandlingPolicy,
    "backup-spec.schema.json": BackupSpec,
}


def render(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def stale(root: Path) -> list[str]:
    out: list[str] = []
    for name, model in MODELS.items():
        path = root / SCHEMA_DIR / name
        if not path.is_file() or path.read_text("utf-8") != render(model):
            out.append(name)
    return out


def write(root: Path) -> None:
    (root / SCHEMA_DIR).mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        (root / SCHEMA_DIR / name).write_text(render(model), "utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path())
    args = parser.parse_args(argv)
    if args.check:
        names = stale(args.root)
        for name in names:
            print(
                f"schemas: {SCHEMA_DIR / name} is stale; run python -m tools.export_schemas",
                file=sys.stderr,
            )
        return 1 if names else 0
    write(args.root)
    print(f"schemas: wrote {len(MODELS)} files to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
