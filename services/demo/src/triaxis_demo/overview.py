"""Executive overview: Sovereignty, Resilience and Assurance.

Sovereignty and Assurance figures are computed at request time from the
repository's real registry, licence gate, tenant configuration and docs
(LIVE). Service-level figures are DEMO DATA until load tests exist (M7).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from triaxis_common.config import TenantConfig
from triaxis_demo import Provenance

SLO_TILES: Final[list[dict[str, Any]]] = [
    {
        "key": "gateway",
        "label": "Gateway + auth + guardrails",
        "value": 18,
        "target": 30,
        "unit": "ms",
    },
    {"key": "stt", "label": "STT partial transcript", "value": 240, "target": 300, "unit": "ms"},
    {
        "key": "ttft_voice",
        "label": "LLM first token (voice)",
        "value": 410,
        "target": 500,
        "unit": "ms",
    },
    {"key": "tts", "label": "TTS first audio", "value": 220, "target": 300, "unit": "ms"},
    {"key": "v2v", "label": "Voice-to-voice", "value": 1210, "target": 1500, "unit": "ms"},
    {"key": "ttft_chat", "label": "Chat first token", "value": 520, "target": 700, "unit": "ms"},
]

ZERO_RETENTION_CONTROLS: Final[list[dict[str, str]]] = [
    {"control": "Allow-list logging; content cannot be logged", "where": "triaxis_common.logging"},
    {"control": "Span sanitiser and metric label allow-list", "where": "triaxis_common.tracing"},
    {"control": "Fixed error catalogue; errors never echo input", "where": "triaxis_common.errors"},
    {
        "control": "Core dumps disabled; TMPDIR on tmpfs (fail closed)",
        "where": "triaxis_common.runtime",
    },
    {"control": "Temp files and pickle banned by lint rules", "where": "pyproject.toml (ruff)"},
    {"control": "Session-wide log schema gate in CI", "where": "conftest.py"},
]

_STRIDE_ROW: Final = re.compile(r"^\|\s*[STRIDE]\d+\s*\|")


def _threat_model_status(repo: Path) -> dict[str, int]:
    path = repo / "docs" / "threat-model.md"
    implemented = planned = 0
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return {"implemented": 0, "planned": 0}
    for line in lines:
        if _STRIDE_ROW.match(line):
            status = line.rstrip("|").rsplit("|", 1)[-1]
            if "⏳" in status and "✅" not in status:
                planned += 1
            else:
                implemented += 1
    return {"implemented": implemented, "planned": planned}


def build_overview(
    repo: Path, tenants: dict[str, TenantConfig], resilience: dict[str, Any]
) -> dict[str, Any]:
    from tools.license_gate import Verdict, evaluate_model  # noqa: PLC0415 - repo tooling
    from tools.model_registry import can_download, load_registry  # noqa: PLC0415

    registry = load_registry(repo / "models" / "registry.yaml")
    models = []
    for entry in registry.models:
        finding = evaluate_model(entry)
        models.append(
            {
                "id": entry.id,
                "task": entry.task.value,
                "origin": entry.origin.organization,
                "country": entry.origin.country,
                "licence": entry.license.spdx or entry.license.name,
                "verdict": finding.verdict.value,
                "status": entry.status.value,
                "downloadable": can_download(entry),
            }
        )
    verdicts = [m["verdict"] for m in models]
    adrs = sorted((repo / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    return {
        "sovereignty": {
            "provenance": Provenance.LIVE,
            "residency": sorted({t.data_residency for t in tenants.values()}),
            "tenants": len(tenants),
            "third_party_ai_apis": 0,
            "models": models,
            "licence": {
                "allow": verdicts.count(Verdict.ALLOW.value),
                "review_approved": verdicts.count(Verdict.REVIEW_APPROVED.value),
                "needs_review": verdicts.count(Verdict.NEEDS_REVIEW.value),
                "deny": verdicts.count(Verdict.DENY.value),
                "downloadable": sum(1 for m in models if m["downloadable"]),
            },
        },
        "resilience": {
            "provenance": Provenance.LIVE,
            "mode": resilience["mode"],
            "rules": resilience["rules"],
            "phase": resilience["phase"],
            "sites": {k: v["health"] for k, v in resilience["sites"].items()},
            "site_state_provenance": Provenance.SIMULATED,
            "targets": {"replica_s": 60, "gateway_s": 10, "site_s": 300},
        },
        "assurance": {
            "provenance": Provenance.LIVE,
            "adrs": len(adrs),
            "threat_model": _threat_model_status(repo),
            "zero_retention_controls": ZERO_RETENTION_CONTROLS,
            "frameworks": ["OWASP ASVS 4.0.3 Level 2", "OWASP Top 10 for LLM Applications 2025"],
        },
        "slo": {"provenance": Provenance.DEMO_DATA, "p95": SLO_TILES},
    }
