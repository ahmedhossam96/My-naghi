# CLAUDE.md — Triaxis

Triaxis is Quantum Seal's sovereign, multi-tenant AI inference platform (STT,
TTS, LLM) with zero retention of customer content. First tenant: MY NAGHI
Motors (KSA). Read `docs/architecture.md` and `docs/threat-model.md` before
changing anything security-relevant; decisions live in `docs/adr/`.

**Current milestone: 1 — Foundations (complete, awaiting review).**
Next: Milestone 2 — core inference services (STT, TTS, LLM adapter with mocks,
OpenAPI specs, contract tests, compose stack). Roadmap: `docs/plan.md`.

## Architecture in one paragraph

Two active sites (Riyadh, Jeddah), 50/50 behind a health-checked traffic
manager, private links only. Per site: gateway pair (Envoy + Coraza + mTLS +
JWT + rate limits) → workload router (real-time lane P1, batch lane P2 on NATS
JetStream memory storage) → guardrails → GPU pools (STT/TTS on MIG slices, LLM
on vLLM) plus a control cluster (OpenBao+HSM, Keycloak, Harbor, Forgejo,
Prometheus/Grafana/Loki, Wazuh). Scaling unit = cell (1 STT/TTS + 1 LLM
server), added at 60% peak GPU. Resilience Option B (balanced) by default,
Option A (mirror) configurable.

## Commands

```bash
uv sync --all-packages                          # install (uv provisions Python 3.12)
uv run pytest                                   # unit tests + 85% coverage gate + log schema gate
uv run ruff check . && uv run ruff format --check .
uv run mypy                                     # strict
uv run bandit -c pyproject.toml -r services tools --severity-level high
uv run python -m tools.license_gate             # model/dataset licence gate
uv run python -m tools.model_registry check     # registry schema (also: list, show, verify, hash, check-tenants)
uv run python -m tools.model_registry check-tenants
uv run python -m tools.export_schemas [--check] # JSON Schemas in config/schema/
uv run python -m tools.check_no_weights --all   # no weights/pickles/large binaries tracked
uv run python -m tools.check_ci_sync            # .github/.forgejo workflow copies match .ci/
tools/sbom.sh dir:. sbom.cdx.json               # CycloneDX SBOM (syft or its container)
uv run pre-commit install                       # gitleaks, ruff, mypy, licence gate, no-weights
```

CI (`.ci/workflows/ci.yml`, copied to `.github/` and `.forgejo/`): lint,
typecheck, unit, policy (licence gate, registry, tenants, schemas, no-weights),
gitleaks, bandit + semgrep, pip-audit, SBOM. Edit `.ci/workflows/ci.yml` and
copy it to both mirrors.

## Layout

- `services/common/src/triaxis_common/` — shared library:
  `types` (slugs, enums) · `errors` (fixed catalogue, ErrorResponse) ·
  `logging` (allow-list logger) · `context` (request context) · `config`
  (ServiceSettings, TenantConfig, HardwareProfile, ResilienceConfig) · `auth`
  (TokenValidator, JwksCache, authorize, require_scope) · `tracing`
  (AllowListSpanExporter, LatencyMetrics) · `secrets` (OpenBao/File providers)
  · `runtime` (harden_process) · `http` (middleware, error handlers,
  create_base_app with /healthz and /readyz).
- `services/<svc>/` — one uv workspace member per service (from M2).
- `config/` — tenants, hardware profiles, generated JSON Schemas.
- `models/registry.yaml` — model/dataset registry (metadata only).
- `tools/` — licence gate, registry CLI, schema export, no-weights, CI sync, sbom.sh, sign.sh.
- `tests/support/` — test-only helpers (token minting, log capture). Never imported by services.

## Conventions (non-negotiable)

1. **Zero retention.** Content lives in memory only. Never log, trace, label or
   echo content. Log only via `AllowListLogger.log(Event.X, **allowed_fields)`;
   adding a field means changing `LogRecord` and ADR 0007. `tempfile`, `pickle`,
   `marshal`, `shelve`, `dill`, `torch.load` are banned by ruff. Call
   `harden_process()` first at service start-up.
2. **Errors** are `TriaxisError(ErrorCode.X)`; messages come from `CATALOG` and
   never interpolate request data.
3. **Auth** in every service: `Depends(require_scope(Workload.X))`. Deny by
   default. Scopes are exactly `<tenant>:<brand>:<workload>`.
4. **No runtime egress.** Every configured URL goes through
   `require_in_cluster_url`. Never call third-party AI APIs from code or tests.
5. **Models**: register in `models/registry.yaml`, verify the licence on the
   model card, run the licence gate. Safetensors only. Never download
   non-commercial models. Datasets must be `licensed` or `consented`.
6. **Config models** use `extra="forbid"`; YAML via `yaml.safe_load` only.
7. Python 3.12, strict mypy, ruff (line length 100), absolute imports,
   `from __future__ import annotations`.
8. Small commits; no secrets, weights or customer data. Tests for every
   contract rule.

## Known environment notes

- Current Starlette's `TestClient` needs `httpx2` (dev dependency).
- Semgrep registry rules are fetched in GitHub CI; they are not reachable from
  every dev environment. gitleaks and syft run from pinned container images in
  CI, or local binaries if installed.
