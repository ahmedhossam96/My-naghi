# 0012 — Deterministic mocks and tiny models for dev/CI

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

The full stack must run on a laptop with `docker compose`, and CI has no GPUs.
Product code and tests must never call third-party AI APIs.

## Decision

- Every model sits behind an interface (`SttEngine`, `TtsEngine`, `LlmClient`,
  `VadEngine`) defined in Milestone 2.
- Dev/CI implementations are **deterministic mocks** (e.g. STT returns a
  transcript derived from a tone pattern in synthetic audio; LLM echoes a
  templated reply token by token) with realistic streaming behaviour and
  configurable latency.
- Optionally, tiny CPU models with permissive licences can be used for smoke
  tests; they go through the registry and licence gate like any other model.
- Tests that need network access to anything outside the compose network fail.

## Consequences

- Latency targets are validated on reference GPU hardware, not in CI (see
  `docs/slo.md`). CI checks behaviour and the instrumentation.
