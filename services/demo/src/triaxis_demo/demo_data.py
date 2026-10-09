"""Illustrative security-operations data (DEMO DATA, ADR 0019).

None of this comes from a running SOC, BAS platform or ticketing system.
Values are fictional and exist to show how the views will look once Wazuh,
Falco and the breach-and-attack-simulation (BAS) suites are connected
(Milestones 7 and 9). MITRE ATT&CK technique IDs are real identifiers.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_demo import Provenance

SOC_ALERTS: Final[list[dict[str, Any]]] = [
    {
        "id": "SOC-1042",
        "time": "08:14",
        "severity": "high",
        "source": "Falco",
        "site": "riyadh",
        "title": "Unexpected shell spawned in an inference pod (blocked)",
        "status": "contained",
    },
    {
        "id": "SOC-1041",
        "time": "07:58",
        "severity": "medium",
        "source": "Wazuh",
        "site": "jeddah",
        "title": "Repeated failed SSH logins on a control-cluster node",
        "status": "investigating",
    },
    {
        "id": "SOC-1040",
        "time": "07:31",
        "severity": "high",
        "source": "Kyverno",
        "site": "riyadh",
        "title": "Unsigned image rejected at admission",
        "status": "contained",
    },
    {
        "id": "SOC-1039",
        "time": "06:47",
        "severity": "low",
        "source": "Gateway",
        "site": "jeddah",
        "title": "Rate limit reached for one brand (no impact on others)",
        "status": "closed",
    },
    {
        "id": "SOC-1038",
        "time": "05:20",
        "severity": "medium",
        "source": "Guardrails",
        "site": "riyadh",
        "title": "Prompt-injection attempt detected and refused",
        "status": "closed",
    },
    {
        "id": "SOC-1037",
        "time": "03:02",
        "severity": "critical",
        "source": "NetworkPolicy",
        "site": "jeddah",
        "title": "Egress attempt to an external address denied",
        "status": "investigating",
    },
    {
        "id": "SOC-1036",
        "time": "01:45",
        "severity": "low",
        "source": "Wazuh",
        "site": "riyadh",
        "title": "File-integrity change on an approved configuration file",
        "status": "closed",
    },
]

BAS_RESULTS: Final[list[dict[str, Any]]] = [
    {"technique": "T1190", "name": "Exploit Public-Facing Application", "outcome": "prevented"},
    {"technique": "T1078", "name": "Valid Accounts (stolen token replay)", "outcome": "prevented"},
    {"technique": "T1552", "name": "Unsecured Credentials", "outcome": "prevented"},
    {"technique": "T1610", "name": "Deploy Container (unsigned image)", "outcome": "prevented"},
    {"technique": "T1041", "name": "Exfiltration Over C2 Channel", "outcome": "prevented"},
    {"technique": "T1059", "name": "Command and Scripting Interpreter", "outcome": "detected"},
    {"technique": "T1110", "name": "Brute Force", "outcome": "detected"},
    {
        "technique": "T1071",
        "name": "Application Layer Protocol (DNS tunnel)",
        "outcome": "detected",
    },
    {"technique": "T1499", "name": "Endpoint Denial of Service", "outcome": "detected"},
    {"technique": "T1565", "name": "Data Manipulation (model artefact)", "outcome": "missed"},
]

_REMEDIATION: Final[list[dict[str, Any]]] = [
    {
        "id": "REM-208",
        "title": "Add sha256 re-verification at model load time (T1565 gap)",
        "severity": "high",
        "owner": "Platform",
        "due": "3 days",
        "status": "open",
    },
    {
        "id": "REM-207",
        "title": "Lower SSH lockout threshold on control-cluster nodes",
        "severity": "medium",
        "owner": "Infrastructure",
        "due": "7 days",
        "status": "in_progress",
    },
    {
        "id": "REM-206",
        "title": "Alert on DNS query volume per pod",
        "severity": "medium",
        "owner": "SOC",
        "due": "10 days",
        "status": "in_progress",
    },
    {
        "id": "REM-205",
        "title": "Rotate gateway mTLS intermediate certificate",
        "severity": "low",
        "owner": "Security",
        "due": "14 days",
        "status": "verified",
    },
]

STATUS_FLOW: Final = ("open", "in_progress", "resolved", "verified")


class SecurityOps:
    """Holds the remediation queue so the demo can advance items (in memory)."""

    def __init__(self) -> None:
        self._remediation = deepcopy(_REMEDIATION)

    def snapshot(self) -> dict[str, Any]:
        outcomes = [r["outcome"] for r in BAS_RESULTS]
        return {
            "provenance": Provenance.DEMO_DATA,
            "notice": "Illustrative data. No SOC, BAS or ticketing system is connected.",
            "soc": {
                "alerts": SOC_ALERTS,
                "open": sum(1 for a in SOC_ALERTS if a["status"] != "closed"),
                "by_severity": {
                    sev: sum(1 for a in SOC_ALERTS if a["severity"] == sev)
                    for sev in ("critical", "high", "medium", "low")
                },
            },
            "bas": {
                "results": BAS_RESULTS,
                "prevented": outcomes.count("prevented"),
                "detected": outcomes.count("detected"),
                "missed": outcomes.count("missed"),
                "last_run": "Today 02:00 (simulated schedule)",
            },
            "remediation": self._remediation,
        }

    def advance(self, item_id: str) -> dict[str, Any]:
        for item in self._remediation:
            if item["id"] == item_id:
                position = STATUS_FLOW.index(item["status"])
                if position == len(STATUS_FLOW) - 1:
                    raise TriaxisError(ErrorCode.INVALID_REQUEST)
                item["status"] = STATUS_FLOW[position + 1]
                return item
        raise TriaxisError(ErrorCode.NOT_FOUND)
