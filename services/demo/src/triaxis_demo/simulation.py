"""Simulated two-site failover (Riyadh / Jeddah).

A deterministic, clock-driven state machine: no background tasks. State is
computed from the failover and restore timestamps whenever it is read. The
capacity rules come from the real ``ResilienceConfig`` (ADR 0004); site health,
traffic and GPU figures are simulated.

Demo time runs faster than real time: one demo second represents
``TIME_SCALE`` simulated seconds, so a ~3 minute recovery plays out in ~6 s.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from triaxis_common.config import ResilienceConfig, ResilienceMode
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_demo import Provenance

SITES: Final = ("riyadh", "jeddah")
SITE_NAMES: Final = {"riyadh": "Site A — Riyadh", "jeddah": "Site B — Jeddah"}
TIME_SCALE: Final = 30.0
DETECT_S: Final = 2.0  # health checks fail, traffic manager marks the site down
REROUTE_S: Final = 6.0  # real-time fully served by the surviving site
REBALANCE_S: Final = 3.0  # after restore, back to 50/50
BATCH_IN_PER_S: Final = 40.0  # batch jobs arriving per demo second (queued during outage)
BATCH_DRAIN_PER_S: Final = 120.0  # catch-up rate after restore
REALTIME_TARGET_S: Final = 300.0  # docs/slo.md: site loss, real-time <= 5 min
CELLS_PER_SITE: Final = 2
_OUTAGE_PHASES: Final = frozenset({"detecting", "rerouting", "failed_over"})


@dataclass
class _Outage:
    site: str
    started: float
    ended: float | None = None


class FailoverSimulator:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._config = ResilienceConfig()
        self._outage: _Outage | None = None

    # ----------------------------------------------------------------- commands

    def set_mode(self, mode: ResilienceMode) -> None:
        if self._active():
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        batch_pct = 100 if mode is ResilienceMode.MIRROR else 0
        # Validated by the real resilience rules (raises on an invalid combination).
        self._config = ResilienceConfig(mode=mode, batch_capacity_during_outage_pct=batch_pct)

    def fail(self, site: str) -> None:
        if site not in SITES or self._active():
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        self._outage = _Outage(site, self._clock())

    def restore(self) -> None:
        if self._outage is None or self._outage.ended is not None:
            raise TriaxisError(ErrorCode.INVALID_REQUEST)
        self._outage.ended = self._clock()

    def reset(self) -> None:
        self._outage = None

    # ----------------------------------------------------------------- state

    def _active(self) -> bool:
        return self._outage is not None and self._outage.ended is None

    def _backlog(self, now: float) -> float:
        if self._outage is None or not self._config.queues_batch_during_outage:
            return 0.0
        end = self._outage.ended if self._outage.ended is not None else now
        queued = BATCH_IN_PER_S * (end - self._outage.started)
        drained = BATCH_DRAIN_PER_S * max(0.0, now - end) if self._outage.ended else 0.0
        return max(0.0, queued - drained)

    def _phase(self, now: float) -> tuple[str, float]:  # noqa: PLR0911 - one return per phase
        """Phase name and seconds since its reference point."""
        if self._outage is None:
            return "normal", 0.0
        if self._outage.ended is None:
            elapsed = now - self._outage.started
            if elapsed < DETECT_S:
                return "detecting", elapsed
            if elapsed < REROUTE_S:
                return "rerouting", elapsed
            return "failed_over", elapsed
        since_restore = now - self._outage.ended
        if since_restore < REBALANCE_S:
            return "rebalancing", since_restore
        if self._backlog(now) > 0:
            return "catching_up", since_restore
        return "recovered", since_restore

    def state(self) -> dict[str, Any]:
        now = self._clock()
        phase, elapsed = self._phase(now)
        failed = self._outage.site if self._outage else None
        survivor = next((s for s in SITES if s != failed), None)
        backlog = self._backlog(now)
        sites = {name: self._site(name, phase, elapsed, failed) for name in SITES}
        return {
            "provenance": {
                "capacity_rules": Provenance.LIVE,
                "site_state": Provenance.SIMULATED,
                "metrics": Provenance.DEMO_DATA,
            },
            "time_scale": TIME_SCALE,
            "mode": self._config.mode.value,
            "rules": {
                "realtime_capacity_per_site_pct": self._config.realtime_capacity_per_site_pct,
                "batch_capacity_during_outage_pct": self._config.batch_capacity_during_outage_pct,
                "queues_batch_during_outage": self._config.queues_batch_during_outage,
            },
            "phase": phase,
            "failed_site": failed,
            "surviving_site": survivor if failed else None,
            "sites": sites,
            "batch_backlog_jobs": round(backlog),
            "realtime_recovery": self._recovery(phase, elapsed),
            "timeline": self._timeline(now),
        }

    def _site(self, name: str, phase: str, elapsed: float, failed: str | None) -> dict[str, Any]:
        is_failed = failed == name
        outage = phase in _OUTAGE_PHASES
        if phase == "rerouting":
            progress = (elapsed - DETECT_S) / (REROUTE_S - DETECT_S)
        elif phase == "rebalancing":
            progress = 1 - elapsed / REBALANCE_S
        elif phase == "failed_over":
            progress = 1.0
        else:  # normal, detecting (traffic still sent to the dead site), caught up
            progress = 0.0
        share = 50.0 * (1 - progress) if is_failed else 50.0 * (1 + progress)
        if self._config.queues_batch_during_outage and outage:
            batch_share = 0.0 if is_failed else 50.0  # failed site's batch work is queued
        else:
            batch_share = share
        down = is_failed and outage
        if down:
            health = "down"
        elif is_failed and phase == "rebalancing":
            health = "degraded"
        else:
            health = "healthy"
        catch_up = 10.0 if phase == "catching_up" else 0.0
        gpu = 0.0 if down else 34.0 + 0.62 * (share - 50.0) + catch_up
        return {
            "name": SITE_NAMES[name],
            "health": health,
            "realtime_share_pct": round(share, 1),
            "batch_share_pct": round(batch_share, 1),
            "gpu_utilisation_pct": round(gpu, 1),
            "cells": CELLS_PER_SITE,
            "gateway_pair": "down" if down else "active/active",
        }

    def _recovery(self, phase: str, elapsed: float) -> dict[str, Any] | None:
        if self._outage is None:
            return None
        simulated = (
            min(elapsed, REROUTE_S) * TIME_SCALE
            if self._outage.ended is None
            else (REROUTE_S * TIME_SCALE)
        )
        done = phase not in ("detecting", "rerouting")
        return {
            "simulated_seconds": round(simulated),
            "target_seconds": REALTIME_TARGET_S,
            "complete": done,
            "within_target": done and REROUTE_S * TIME_SCALE <= REALTIME_TARGET_S,
        }

    def _timeline(self, now: float) -> list[dict[str, str]]:
        if self._outage is None:
            return []
        o = self._outage
        site, other = SITE_NAMES[o.site], SITE_NAMES[next(s for s in SITES if s != o.site)]
        queued = self._config.queues_batch_during_outage
        candidates: list[tuple[float, str]] = [
            (o.started, f"{site} stops responding (simulated outage)."),
            (o.started + DETECT_S, f"Health checks fail; traffic manager removes {site}."),
            (o.started + REROUTE_S, f"Real-time traffic fully served by {other}."),
            (
                o.started + REROUTE_S,
                "Batch analytics queued in memory (Option B)."
                if queued
                else f"Batch analytics served by {other} (Option A).",
            ),
        ]
        if o.ended is not None:
            candidates += [
                (o.ended, f"{site} restored; health checks passing."),
                (o.ended + REBALANCE_S, "Traffic rebalanced 50/50."),
            ]
            if queued:
                drain_s = BATCH_IN_PER_S * (o.ended - o.started) / BATCH_DRAIN_PER_S
                candidates.append((o.ended + max(drain_s, REBALANCE_S), "Batch backlog caught up."))
        events = []
        for at, text in sorted(candidates, key=lambda c: c[0]):
            if at <= now:
                sim = (at - o.started) * TIME_SCALE
                events.append({"t": f"T+{int(sim // 60):02d}:{int(sim % 60):02d}", "event": text})
        return events
