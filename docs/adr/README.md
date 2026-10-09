# Architecture Decision Records

Format: [MADR](https://adr.github.io/madr/) (simplified, see `template.md`).
Numbering is sequential and never reused. To change a decision, add a new ADR
that supersedes the old one and update the old one's status.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0002](0002-monorepo-uv-workspace.md) | Monorepo, uv workspace, Python 3.12, hash-pinned dependencies | accepted |
| [0003](0003-ci-platform.md) | CI on GitHub Actions syntax, Forgejo-compatible | accepted |
| [0004](0004-resilience-mode.md) | Resilience Option B (balanced), configurable | accepted |
| [0005](0005-orchestration.md) | RKE2 Kubernetes; docker compose for development | accepted |
| [0006](0006-zero-retention.md) | Zero-retention architecture | accepted |
| [0007](0007-allow-list-logging.md) | Allow-list logging and telemetry | accepted |
| [0008](0008-token-and-scope-model.md) | Token and scope model | accepted |
| [0009](0009-model-registry-license-gate.md) | Model registry and licence gate (deny by default) | accepted |
| [0010](0010-model-origin-per-tenant.md) | Model origin switchable per tenant | accepted |
| [0011](0011-safetensors-only.md) | Safetensors-only model loading | accepted |
| [0012](0012-dev-models-and-mocks.md) | Deterministic mocks and tiny models for dev/CI | accepted |
| [0013](0013-hardware-profiles.md) | Hardware profiles in configuration | accepted |
| [0014](0014-batch-deadline.md) | Batch analytics deadline: same day | accepted |
| [0015](0015-application-boundary.md) | Application boundary | accepted |
| [0016](0016-error-contract.md) | Error contract | accepted |
| [0017](0017-secrets-from-openbao.md) | Secrets from OpenBao at runtime | accepted |
| [0018](0018-saudi-id-check-digit.md) | Saudi national ID / Iqama check digit | accepted (provisional) |
| [0019](0019-demo-prototype.md) | Client-demo prototype (Milestone 1.5) | accepted |
