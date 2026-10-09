# Service level objectives — engineering targets

Status: skeleton (Milestone 1). Load tests and the benchmark report generator
arrive in Milestone 7; voice-to-voice instrumentation in Milestone 5.

These are **engineering targets**, not customer commitments. They are
configurable and must be validated on the reference hardware profiles
(`h100-llm`, `h100-mig-3g40gb-speech`; see `config/hardware-profiles/`)
before any contractual commitment.

## Targets (p95, nominal load)

| Stage | Target | Metric / span |
|---|---|---|
| Gateway + auth + guardrails overhead | < 30 ms | `gateway_overhead_ms` |
| Streaming STT partial transcript after speech | < 300 ms | `stt_partial_latency_ms` |
| LLM time to first token (voice profile) | < 500 ms | `ttft_ms{workload="voice"}` |
| TTS time to first audio chunk | < 300 ms | `time_to_first_audio_ms` |
| Voice-to-voice (end of caller speech → first reply audio) | < 1.5 s | `voice_to_voice_ms` |
| Chat completion first token | < 700 ms | `ttft_ms{workload="llm"}` |
| Batch transcription throughput | ≥ 50× real-time per GPU slice (to be measured) | load-test report |

The histogram names are defined in `triaxis_common.tracing.LatencyMetric`.
Labels are restricted to `tenant`, `brand`, `workload`, `lane`, `site`,
`status_class`, `model` (ADR 0007).

## Availability and failover targets

| Event | Recovery target |
|---|---|
| Model replica killed | < 60 s |
| Gateway instance killed | < 10 s |
| Site lost (two-site staging) | real-time ≤ 5 min; batch queued and caught up within the batch deadline |

## Validation (to be completed)

- Load tests (k6 or Locust) fail the build when p95 exceeds a target on the
  reference profile (M7).
- `tests/load/report.py` produces a Markdown benchmark report (M7).
- Burn-rate alerts per SLO (M7).
