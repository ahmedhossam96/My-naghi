"""Triaxis client-demo prototype.

A local, single-process demo of the Triaxis experience. Model inference, SOC
data and site infrastructure are simulated; every API response carries a
``provenance`` label (``live`` / ``simulated`` / ``demo_data``) so the UI can
show which parts are real Triaxis code and which are stand-ins (ADR 0019).
Not a production component.
"""

from __future__ import annotations

from enum import StrEnum

__version__ = "0.1.0"


class Provenance(StrEnum):
    LIVE = "live"  # computed at runtime by real Triaxis code or configuration
    SIMULATED = "simulated"  # a mock standing in for a service not built yet
    DEMO_DATA = "demo_data"  # illustrative values, not measurements
