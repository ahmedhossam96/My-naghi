# Triaxis client-demo prototype

A local, clickable demo of the Triaxis experience, styled to the Quantum Seal
Brand Guidelines v1.0. **Not a production component** (ADR 0019).

```bash
uv sync --all-packages
uv run python -m triaxis_demo            # http://127.0.0.1:8080  (--port to change)
```

Runs fully offline: no CDN, no external requests, no model downloads. Binds to
127.0.0.1 only.

## Views

| # | View | Live (real Triaxis code) | Simulated / demo data |
|---|---|---|---|
| 01 | Executive Overview | registry + licence gate, tenant residency, resilience rules, ADR and threat-model counts | SLO values |
| 02 | AI Playground | JWT validation and scope checks, cross-brand denial, request validation, magic-byte audio validation, size limits, error catalogue | mock LLM (SSE), mock STT transcript, tone-based TTS |
| 03 | Security Operations | — | SOC alerts, BAS results, remediation queue |
| 04 | Riyadh / Jeddah | Option A/B capacity rules (`ResilienceConfig`) | site health, failover timeline, traffic and GPU figures |
| 05 | Tenants & Audit | tenant/brand config, scopes, quotas, allow-list audit log | — |

Every panel shows a **Live**, **Simulated** or **Demo Data** label.

## Demo script (5 minutes)

1. **Overview**: SA residency, 0 third-party AI APIs, licence gate (nothing
   downloadable until verified), Option B, zero-retention controls.
2. **Playground → Chat**: run the Arabic sample; then *Try to call as Brand B* →
   HTTP 403 (the real scope check).
3. **Playground → STT**: *Use sample clip*, then *Send a non-audio file* → 415
   (magic-byte validation). **TTS**: synthesise and play.
4. **Riyadh / Jeddah**: *Simulate Riyadh outage* → detection, rerouting,
   real-time on Jeddah within the 5-minute target, batch queued; *Restore site*
   → rebalance and catch-up. Switch to Option A and repeat.
5. **Tenants & Audit**: search the audit log for the phrase you sent → no
   match. Records are metadata only.

## Branding

Colours, type and hierarchy follow the guidelines. The wordmark is not
recreated: place the official logo at
`src/triaxis_demo/static/brand/quantum-seal-logo.svg` and it replaces the text
header automatically.
