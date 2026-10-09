# 0004 — Resilience Option B (balanced), configurable

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Two active sites (Riyadh, Jeddah) share traffic 50/50. If one site fails, the
other must keep real-time traffic (voice agent, chat) running. Mirroring full
batch capacity in both sites doubles GPU cost for work that can wait.

## Decision

`resilience.mode` in configuration, with two values:

| Mode | Name | Real-time capacity per site | Batch during a site outage |
|---|---|---|---|
| `balanced` (default) | Option B | 100% of global real-time peak | queued (memory only), caught up after recovery within the batch deadline (ADR 0014) |
| `mirror` | Option A | 100% of global real-time peak | 100% of global batch peak, no queueing |

Validation rules (enforced in `triaxis_common.config.ResilienceConfig`):

- `realtime_capacity_per_site_pct` must be 100 in both modes.
- `mirror` requires `batch_capacity_during_outage_pct = 100`.
- `balanced` allows 0–99 (default 0: batch queues entirely during an outage).

## Consequences

- Capacity planning per site = real-time peak (full) + batch share (normal
  operation). Cells (1 STT/TTS + 1 LLM server) are added when peak GPU use
  exceeds 60%.
- The batch queue is memory-only (ADR 0006); a queued job lost in a *second*
  failure is resubmitted by the caller. This is documented in the tenant
  onboarding guide.

## Alternatives considered

- Active/passive: wastes a whole site and slows failover.
