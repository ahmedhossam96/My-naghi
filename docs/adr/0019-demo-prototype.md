# 0019 — Client-demo prototype (Milestone 1.5)

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Before Milestone 2, the product owner needs a clickable, locally runnable
demo for clients: an executive dashboard (Sovereignty, Resilience,
Assurance), an AI playground (Chat, STT, TTS), security operations, a
Riyadh/Jeddah failover view, and tenant/brand and audit views. Most backend
services do not exist yet, and a demo must never overstate what is built.

## Decision

- A separate workspace member, `services/demo` (`triaxis_demo`): FastAPI on
  `triaxis_common.create_base_app`, serving a static single-page UI in plain
  HTML/CSS/JS. No npm, no build step, no CDN or other external requests (the
  UI works fully offline). Binds to `127.0.0.1` only.
- **Labelling rule.** Every panel and API response carries a provenance label:
  - `live`: computed at runtime by real Triaxis code or configuration
    (tenant config, model registry and licence gate, `ResilienceConfig` rules,
    JWT validation and scope checks, request validation, error catalogue,
    allow-list audit log, ADR and threat-model counts);
  - `simulated`: a mock standing in for a service not built yet (LLM, STT, TTS,
    site health and failover, demo identity provider standing in for Keycloak);
  - `demo_data`: illustrative values (SOC alerts, BAS results, remediation
    queue, SLO values, GPU and traffic figures).
- **Mocks** sit behind small engine classes (`MockLlm`, `MockStt`, `MockTts`)
  so Milestone 2 can replace them with real services.
- **Zero retention still applies.** Chat, transcripts and audio live in memory
  for the request; STT takes a raw body (no multipart spooling); the audit view
  shows only lines that already passed the allow-list schema. Tests assert that
  a tagged prompt never appears in logs or the audit view.
- **Security headers**: strict CSP (`default-src 'self'`, no inline script),
  `nosniff`, `no-referrer`, microphone allowed for self only, `no-store`.
- **Branding** follows Quantum Seal Brand Guidelines v1.0: Navy `#033495`,
  Indigo `#1B1464`, Sky `#77ACED`, neutrals Ink/Slate/Pale Blue/Hairline,
  Helvetica/Arial, Title Case headings, no ALL CAPS except short kicker
  labels. The logo is **not recreated**: the header uses the text lockup the
  guidelines themselves use in document headers ("Quantum Seal" / "The Future
  of Security"). Dropping the official file at
  `services/demo/src/triaxis_demo/static/brand/quantum-seal-logo.svg`
  replaces it automatically.
- **Functional status colours** (green `#2E7D5B`, amber `#A8660B`, red
  `#B42318`) are added for state only (health, severity, outcomes). They are not
  part of the brand palette and are never used decoratively. To confirm with the
  brand owner.
- The demo tenant is neutral and fictional ("Demo Motors", Brand A/B/C).
  Arabic UI (RTL) is deferred; the playground accepts and displays Arabic.

## Consequences

- The demo is not a production component: no Helm chart, not deployed, not part
  of the gateway path. It shares the CI quality gates (lint, types, tests,
  coverage).
- Clients can see live controls (scope denial, magic-byte validation,
  metadata-only audit) working, while simulated parts are clearly marked.
