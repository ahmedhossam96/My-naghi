# 0014 — Batch analytics deadline: same day

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Batch work (call transcription, analytics) is priority 2 and is queued during
a site outage under Option B (ADR 0004).

## Decision

- Default deadline: **same day** (results available before 23:59 Asia/Riyadh
  on the day of submission), configurable per tenant
  (`batch.deadline: same_day | hours:<n>`).
- Queue entries are memory-only with a TTL of at most 24 h; results are deleted
  from memory after delivery or TTL, whichever comes first.

## Consequences

- Capacity planning must leave enough batch headroom to catch up a queued
  backlog within the deadline after a site recovers.
