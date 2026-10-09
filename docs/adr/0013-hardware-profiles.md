# 0013 — Hardware profiles in configuration

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Defaults: H100 for the LLM; H100 MIG slices or L40S for STT/TTS. Hardware will
change over the product's life and may differ per site.

## Decision

- Hardware profiles live in `config/hardware-profiles/*.yaml`, validated by
  `triaxis_common.config.HardwareProfile`: GPU model, memory, GPUs per server,
  optional MIG profile, pool role (`llm`, `stt_tts`, `dev`) and the
  cell scale-up threshold (default 60% peak GPU use).
- Initial profiles: `h100-llm`, `h100-mig-3g40gb-speech`, `l40s-speech`,
  `cpu-dev`.
- Registry entries list the profiles a model has been validated on.

## Consequences

- SLO targets in `docs/slo.md` are tied to a named reference profile.
