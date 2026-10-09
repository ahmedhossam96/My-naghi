"""Licence gate (ADR 0009). Deny by default.

    python -m tools.license_gate [--registry models/registry.yaml]

Exit status is non-zero when any model or dataset is denied, when an
``approved`` model has a licence that still needs review, or when a dataset is
not ``licensed``/``consented``.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from tools.model_registry import (
    DEFAULT_REGISTRY,
    DatasetEntry,
    License,
    ModelEntry,
    Registry,
    SourceKind,
    Status,
    load_registry,
)

ALLOWED_SPDX: Final[frozenset[str]] = frozenset(
    {"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC"}
)
ALLOWED_CONSENT: Final[frozenset[str]] = frozenset({"licensed", "consented"})
# Weights must be safetensors; everything else must be small config/tokenizer text.
ALLOWED_ARTEFACT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".safetensors", ".json", ".txt", ".model", ".tiktoken", ".yaml", ".yml", ".jinja", ".md"}
)
NON_COMMERCIAL: Final = re.compile(
    r"(?ix)"
    r"(?:^|[^a-z])nc(?:[^a-z]|$)"  # CC-BY-NC-4.0, "NC", "-NC-SA"
    r"|non[\s_-]?commercial"
    r"|research[\s_-]?(?:only|use|purposes)"
    r"|not[\s_-]+for[\s_-]+commercial"
    r"|academic[\s_-]?(?:only|use)"
    r"|evaluation[\s_-]?only"
    r"|personal[\s_-]?use[\s_-]?only"
)


class Verdict(StrEnum):
    ALLOW = "allow"
    REVIEW_APPROVED = "review_approved"
    NEEDS_REVIEW = "needs_review"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str  # "model" | "dataset"
    id: str
    verdict: Verdict
    reasons: tuple[str, ...]
    blocking: bool


def evaluate_license(lic: License) -> tuple[Verdict, list[str]]:
    texts = [t for t in (lic.spdx, lic.name, lic.notes) if t]
    if any(NON_COMMERCIAL.search(t) for t in texts):
        return Verdict.DENY, ["non-commercial or research-only licence"]
    if lic.spdx in ALLOWED_SPDX:
        return Verdict.ALLOW, []
    if lic.approval is not None:
        return Verdict.REVIEW_APPROVED, [f"approved by {lic.approval.approved_by}"]
    return Verdict.NEEDS_REVIEW, ["licence outside the SPDX allow-list without approval record"]


def _artefact_problems(entry: ModelEntry) -> list[str]:
    return [
        f"artefact '{a.filename}' is not safetensors or an allowed config file"
        for a in entry.artefacts
        if PurePosixPath(a.filename).suffix.lower() not in ALLOWED_ARTEFACT_SUFFIXES
    ]


def evaluate_model(entry: ModelEntry) -> Finding:
    verdict, reasons = evaluate_license(entry.license)
    artefact_problems = _artefact_problems(entry)
    if artefact_problems:
        verdict = Verdict.DENY
        reasons = reasons + artefact_problems
    if entry.status is Status.CANDIDATE and entry.license.verified_on is None:
        reasons = [*reasons, "candidate: licence not yet verified from the model card"]
    blocking = verdict is Verdict.DENY or (
        verdict is Verdict.NEEDS_REVIEW and entry.status is Status.APPROVED
    )
    return Finding("model", entry.id, verdict, tuple(reasons), blocking)


def evaluate_dataset(entry: DatasetEntry) -> Finding:
    reasons: list[str] = []
    verdict = Verdict.ALLOW
    if entry.consent_basis not in ALLOWED_CONSENT:
        verdict = Verdict.DENY
        reasons.append("dataset is not tagged 'licensed' or 'consented'")
    if entry.source_kind is SourceKind.PLATFORM_TRAFFIC:
        verdict = Verdict.DENY
        reasons.append("customer platform traffic may never be used (rule 2)")
    if entry.license is not None:
        lic_verdict, lic_reasons = evaluate_license(entry.license)
        if lic_verdict is Verdict.DENY or (
            lic_verdict is Verdict.NEEDS_REVIEW and verdict is not Verdict.DENY
        ):
            verdict = lic_verdict
        reasons += lic_reasons
    blocking = verdict in (Verdict.DENY, Verdict.NEEDS_REVIEW)
    return Finding("dataset", entry.id, verdict, tuple(reasons), blocking)


def check_dataset_allowed(registry: Registry, dataset_id: str) -> None:
    """Called by eval / fine-tuning tooling before reading any dataset."""
    entry = registry.dataset(dataset_id)
    if entry is None:
        raise PermissionError(f"dataset '{dataset_id}' is not in the registry")
    finding = evaluate_dataset(entry)
    if finding.blocking:
        raise PermissionError(f"dataset '{dataset_id}' refused: {'; '.join(finding.reasons)}")


def run_gate(registry: Registry) -> list[Finding]:
    return [evaluate_model(m) for m in registry.models] + [
        evaluate_dataset(d) for d in registry.datasets
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args(argv)
    findings = run_gate(load_registry(args.registry))
    for f in findings:
        mark = "FAIL" if f.blocking else "ok  "
        detail = f" — {'; '.join(f.reasons)}" if f.reasons else ""
        print(f"{mark} {f.kind:7} {f.id:28} {f.verdict}{detail}")
    failures = [f for f in findings if f.blocking]
    if failures:
        print(f"licence gate: {len(failures)} blocking finding(s)", file=sys.stderr)
        return 1
    print(f"licence gate: {len(findings)} entries checked, none blocking")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
