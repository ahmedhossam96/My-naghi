# Triaxis — Build Plan (v1, awaiting approval)

Status: **approved** 2026-10-09 (open questions resolved with the proposed defaults).
Milestone 1: **complete, awaiting review**.
Scope of this document: overall repository structure, milestone roadmap, the
decisions taken (each becomes an ADR in Milestone 1), and the detailed plan for
**Milestone 1 — Foundations**.

---

## 1. Repository layout

The Git repository root *is* the `triaxis/` tree (no extra nesting directory).
Changes versus the brief are marked **[+]** (added) or **[~]** (changed).

```
.
├── CLAUDE.md
├── README.md                    [~] replaced with product overview + quickstart
├── pyproject.toml               [+] uv workspace root: shared ruff/mypy/pytest/coverage config
├── uv.lock                      [+] single lockfile for the workspace (hash-pinned)
├── .pre-commit-config.yaml      [+] ruff, mypy, gitleaks, license-gate, no-weights check
├── .gitleaks.toml  .semgrepignore  .gitattributes  .gitignore   [+]
├── docs/
│   ├── plan.md                  this file
│   ├── architecture.md
│   ├── threat-model.md          STRIDE + ASVS L2 + OWASP LLM Top 10 mapping
│   ├── slo.md                   (M5/M7; skeleton in M1)
│   ├── adr/                     NNNN-title.md (MADR format)
│   ├── api/                     OpenAPI 3.1 (M2)
│   └── runbooks/
├── services/
│   ├── common/                  package `triaxis_common` (M1)
│   ├── gateway/  router/  stt/  tts/  llm/  guardrails/  voice/
│   │   each Python service = its own uv workspace member:
│   │   services/<svc>/pyproject.toml, src/triaxis_<svc>/, tests/ (unit only), Containerfile
├── models/                      [+] registry data only (never weights)
│   └── registry.yaml            model entries: id, version, sha256, license, languages, hw profile…
├── config/                      [+] non-secret config
│   ├── hardware-profiles/       h100.yaml, h100-mig-3g40gb.yaml, l40s.yaml, cpu-dev.yaml
│   ├── tenants/                 per-tenant config (quotas, brands, guardrail policy, model selection)
│   │   └── example-tenant.yaml  (MY NAGHI added in M3/M4 as onboarding proof)
│   └── schema/                  JSON Schemas generated from the Pydantic models
├── deploy/  helm/  k8s/  ansible/  compose/ [+]
├── observability/
├── eval/
├── tests/
│   ├── unit/                    cross-cutting unit tests (logging schema gate, etc.)
│   ├── contract/ integration/ security/ load/ chaos/ zero_retention/
│   └── fixtures/                synthetic, tagged data only
├── tools/
│   ├── license_gate.py  model_registry.py  sbom.sh  sign.sh
│   └── check_no_weights.py      [+] blocks *.safetensors/*.bin/*.pt/*.gguf/*.onnx etc. from commits
└── .ci/
    ├── workflows/               canonical pipeline definitions (GitHub/Forgejo Actions syntax)
    └── README.md
    .github/workflows/ and .forgejo/workflows/ → thin copies kept in sync by a CI check [+]
```

Why per-service packages: each service builds its own minimal image with only
its own dependencies (smaller attack surface, smaller SBOM), while `uv`
workspaces give one lockfile and one `uv run pytest` for the whole repo.

---

## 2. Decisions (each becomes an ADR in `docs/adr/` during M1)

| ADR | Decision | Notes / alternatives |
|---|---|---|
| 0001 | Record decisions as ADRs (MADR template) | — |
| 0002 | **Monorepo, uv workspace, Python 3.12**, `uv.lock` + `uv export --require-hashes` for image builds | Poetry/pip-tools rejected: uv is faster, native workspaces, hash-pinned export |
| 0003 | **CI: GitHub Actions syntax, kept Forgejo-Actions compatible** (no marketplace-only features; actions pinned by commit SHA; mirrored into Harbor/Forgejo later) | Repo currently lives on GitHub |
| 0004 | **Resilience Option B (balanced)**, selectable via `resilience.mode: balanced \| mirror` | Option A = full mirror; capacity math in `docs/architecture.md` |
| 0005 | **Orchestration: RKE2**; `docker compose` for dev/CI integration | — |
| 0006 | **Zero-retention architecture**: in-memory buffers only, `TMPDIR`→tmpfs, `tempfile` usage banned by lint rule, core dumps off (`RLIMIT_CORE=0` set at process start + container `ulimit`), NATS JetStream `storage: memory` + TTL, no content in logs/traces/metrics/errors | Swap-off documented as host requirement (enforced by Ansible in M6) |
| 0007 | **Allow-list logging & telemetry**: one `LogRecord` Pydantic model with `extra="forbid"`; same allow-list applied to OTel span attributes and Prometheus label names | Fields: `ts, level, event, request_id, tenant, brand, workload, service, duration_ms, timings{stage→ms}, status, bytes_in, bytes_out, audio_ms, tokens_in, tokens_out, error_class`. `event` and `error_class` are enums, never free text |
| 0008 | **Auth token model** (see §4.3): Keycloak-issued RS256/ES256 JWT, ≤15 min lifetime, scope entries `<tenant>:<brand>:<workload>`; re-validated in every service; JWKS fetched only from the in-cluster Keycloak (no egress) and cached | Algorithm allow-list; `none`/HS* rejected |
| 0009 | **Model registry + license gate, deny-by-default**: allowed SPDX IDs = Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, ISC. Anything else (custom/community licenses, e.g. Jais, ALLaM, Llama-style) → `needs_review`, blocked until an explicit approval record (who, date, licence URL, sha256 of licence text) is committed. Any NC / research-only marker → hard `deny`, not overridable | Licence text is verified from the model card when the model is added, not assumed from this plan |
| 0010 | **Model origin switchable per tenant**: tenant config selects a model profile (`voice`, `chat`, `stt`, `tts`) by registry ID; registry entries carry `origin` metadata; tenants list approved origins | — |
| 0011 | **Safetensors-only loading**: loader refuses any other format; registry sha256 verified before load | — |
| 0012 | **Dev/CI models = deterministic mocks** behind the same interfaces (`SttEngine`, `TtsEngine`, `LlmClient`), plus optional tiny CPU models | Full stack runs on a laptop |
| 0013 | **Hardware profiles in config** (H100 LLM; H100-MIG or L40S STT/TTS; cpu-dev) | — |
| 0014 | **Batch analytics deadline: same day** | — |
| 0015 | **Application boundary**: Triaxis = model APIs + optional reference voice pipeline; tenants own business logic/CRM | — |
| 0016 | **Error contract**: `{"error":{"type","code","message","request_id"}}` (OpenAI-shaped). `message` comes from a fixed catalog keyed by `code`; never interpolates request data | — |
| 0017 | **Secrets via OpenBao at runtime**: `SecretProvider` interface; prod impl = OpenBao (Kubernetes auth, via httpx), dev impl = files on tmpfs; no secrets in env files committed to git | — |
| 0018 | **Saudi national ID / Iqama check digit** — *open, see Questions* | — |

---

## 3. Milestone roadmap (stop for review after each)

| # | Milestone | Key exit criteria |
|---|---|---|
| 1 | **Foundations** | Skeleton, CLAUDE.md, ADRs, CI green (lint, mypy strict, unit, coverage ≥85% on `common`, gitleaks, semgrep, bandit, pip-audit, SBOM), `triaxis_common`, license gate + registry, threat model v1 |
| 1.5 | Client-demo prototype (ADR 0019) | Local branded UI: executive dashboard, AI playground, security operations (demo data), Riyadh/Jeddah failover simulation, tenants and audit; every panel labelled live/simulated/demo data |
| 2a | Classification, adapters, backup (continuation, phases A–F) | ADR 0020–0022: enforced classification/policy, OpenAI-compatible self-hosted adapters with model gate and explicit mock/real mode, classified chat/STT/TTS/voice pipeline in the demo, encrypted integrity-checked backup with isolated restore. Real inference **blocked** until a model is approved and served (see `docs/status/capability-matrix.md`) |
| 2 | Core inference services | STT/TTS/LLM adapter with mocks + tiny models; OpenAPI 3.1 specs; schemathesis contract tests; compose dev stack |
| 3 | Gateway & security | Envoy + Coraza (proxy-wasm) + mTLS + Keycloak JWT + per-brand rate limits; guardrails (Presidio + Saudi recognizers, injection filter, topic policy); security tests; zero-retention suite v1 |
| 4 | Router & lanes | Real-time/batch lanes, priorities, quotas, NATS memory queue, batch job API, resilience mode |
| 5 | Voice pipeline | VAD→STT→LLM→TTS streaming, latency spans, voice-to-voice test on synthetic audio |
| 6 | Kubernetes & hardening | Helm charts, NetworkPolicies, PSS restricted, Kyverno verify-images, KEDA, MIG profiles, Ansible CIS + LUKS2/Clevis/Tang/TPM2, two-site staging |
| 7 | Observability & SLOs | Dashboards, burn-rate alerts, load tests + `tests/load/report.py`, Wazuh/Falco rules |
| 8 | Resilience & chaos | Replica/gateway/site failover tests, DR runbook, backup/restore, offline vault export |
| 9 | Evaluation & red-teaming | WER/LLM/MOS harnesses, garak + promptfoo (AR/EN/mixed), report templates |
| 10 | Release readiness | cosign-signed releases, SBOM per release, changelog, versioned charts, guides, ASVS L2 / LLM Top 10 review checklist |

---

## 4. Milestone 1 — Foundations: detailed plan

### 4.1 Commits (in order, each small and reviewable)

1. `chore: repo skeleton` — directories, `.gitignore`, `.gitattributes`, README, CLAUDE.md v0.
2. `docs: ADRs 0001–0018` — the table above, fully written.
3. `build: uv workspace + tooling config` — root `pyproject.toml` (ruff rule set incl. `S` bandit rules, `mypy --strict`, pytest, coverage), `services/common` package scaffold, lockfile.
4. `ci: pipeline` — jobs below + pre-commit config.
5. `feat(common): errors` — error catalog + exception types + FastAPI handlers.
6. `feat(common): allow-list logging` — logger, redaction-by-construction, schema gate test.
7. `feat(common): config` — settings, tenant config, hardware profiles, resilience mode.
8. `feat(common): auth` — JWT validation, scope model, JWKS cache, FastAPI dependency.
9. `feat(common): tracing + metrics` — OTel setup with attribute allow-list processor, histogram definitions.
10. `feat(common): zero-retention runtime guards` — core-dump disable, TMPDIR check, `tempfile` guard.
11. `feat(tools): model registry + license gate` — plus `check_no_weights.py`, `sbom.sh`, `sign.sh` (stubs that call syft/cosign).
12. `docs: architecture.md, threat-model.md v1, slo.md skeleton` and CLAUDE.md update.

### 4.2 `services/common` (`triaxis_common`) modules

| Module | Content |
|---|---|
| `errors.py` | `ErrorCode` enum → (HTTP status, fixed message). `TriaxisError` hierarchy. FastAPI exception handlers incl. Pydantic `RequestValidationError` (field *locations* only, never input values). |
| `logging.py` | `LogRecord` model (`extra="forbid"`, strict types, enums for `event`/`error_class`). `get_logger()` returns a wrapper whose only API is `log(event, **allowed_fields)`; stdlib/third-party loggers routed through a handler that drops `msg`/`args`/`exc_info` text and emits only `error_class`. JSON output to stdout. |
| `config.py` | `pydantic-settings` service settings (`extra="forbid"`); `TenantConfig` (brands, workloads, quotas, guardrail policy ref, model selection, approved origins); `HardwareProfile`; `ResilienceMode`. Loaded from YAML, JSON Schema exported to `config/schema/`. |
| `auth.py` | `Scope(tenant, brand, workload)` parser (strict regex, lowercase ASCII slug, no wildcards); `TokenValidator` (issuer, audience, `exp`, `nbf`, `iat`, `exp-iat ≤ 900s`, leeway ≤ 30 s, alg allow-list, `kid` required); `JwksCache` (in-cluster URL, TTL, refresh-on-unknown-kid with rate limit); FastAPI `require_scope(workload)` dependency enforcing that the path's tenant/brand match the token; constant-time comparisons (`hmac.compare_digest`) wherever secrets/IDs are compared. Library: `PyJWT[crypto]` or `joserfc` (decided in ADR 0008 after a quick audit). |
| `tracing.py` | OTel tracer/meter setup (OTLP to in-cluster collector only); `AllowListSpanProcessor` drops non-allow-listed attributes; histograms `ttft_ms`, `time_to_first_audio_ms`, `stt_partial_latency_ms`, `voice_to_voice_ms`; label allow-list for Prometheus. |
| `secrets.py` | `SecretProvider` protocol, `OpenBaoProvider`, `FileProvider` (dev/tests). |
| `runtime.py` | `harden_process()`: `RLIMIT_CORE=0`, `prctl(PR_SET_DUMPABLE, 0)` on Linux, verify `TMPDIR` is a tmpfs (fail closed in prod mode), set `tempfile.tempdir`; request-ID middleware. |
| ~~`limits.py`~~ | Folded into `http.py` (`BodySizeLimitMiddleware`); duration/token caps arrive with the services in M2. |

### 4.3 Token / scope model

- Scope string: `<tenant>:<brand>:<workload>`, e.g. `mynaghi:toyota:stt`.
  `workload ∈ {stt, tts, llm, voice, batch}`. No wildcards.
- Claims required: `iss`, `aud` (`triaxis`), `exp`, `iat`, `nbf`, `sub`, `azp`,
  `tenant`, `scope`. Token rejected if any scope entry names a tenant ≠ `tenant` claim.
- A request is authorised only if the token contains the exact scope for the
  `(tenant, brand, workload)` resolved from the request. Deny by default.

### 4.4 Tools

- `tools/model_registry.py` — CLI + library: `add`, `verify` (sha256), `list`,
  `show`; reads/writes `models/registry.yaml`; schema-validated; rejects
  non-safetensors artefacts.
- `tools/license_gate.py` — evaluates every registry entry: `allow` /
  `needs_review` / `deny`; detects NC/research-only markers in licence IDs and
  licence text; exits non-zero on deny or unapproved review. Runs in
  pre-commit and CI. Also used by the eval/fine-tuning tooling later to refuse
  datasets not tagged `licensed`/`consented` (dataset registry shares the gate).
- Initial registry entries in M1 are **metadata placeholders with
  `status: candidate`** (no download, no sha256 yet). Licences are verified from
  the model card when each model is actually added (M2+).

### 4.5 CI pipeline (`.ci/workflows/ci.yml`)

Jobs (all must pass; actions pinned by SHA):
`lint` (ruff check + format) · `typecheck` (mypy strict) · `unit` (pytest +
hypothesis, coverage gate ≥85% on `triaxis_common` and auth) · `secrets`
(gitleaks) · `sast` (semgrep `p/owasp-top-ten` + `p/python`, bandit; fail on
high) · `deps` (pip-audit against hash-pinned export; fail on high/critical) ·
`sbom` (syft → CycloneDX artefact) · `license-gate` · `no-weights`.
Trivy image scanning starts in M2 when the first images exist.

### 4.6 Tests in M1

- **Logging:** schema gate — a test captures every record emitted during the
  whole unit run and fails on any field outside the schema; hypothesis test
  that arbitrary strings passed to any logging path never appear in output;
  third-party logger messages are dropped.
- **Auth:** valid; expired; not-yet-valid; lifetime > 15 min; wrong issuer;
  wrong audience; missing/malformed scope; wrong workload; cross-tenant;
  cross-brand; `alg=none`; HS256 with public key (alg confusion); unknown
  `kid`; tampered signature; JWKS refresh rate-limit. Hypothesis on scope parser.
- **Errors:** every `ErrorCode` maps to a status; responses never contain request
  input (property test with tagged input).
- **Config:** unknown fields rejected; resilience mode parsing; tenant example
  validates; JSON Schema up to date.
- **Tools:** licence gate allow/review/deny matrix incl. NC markers; registry
  sha256 verify; non-safetensors rejected; no-weights check.
- **Runtime:** core-dump limit set; TMPDIR not tmpfs → fails in prod mode.

### 4.7 Threat model v1 (`docs/threat-model.md`)

Data-flow diagram (customer → private link → gateway → router → lanes → GPU
pools; control cluster; inter-site WireGuard), trust boundaries, STRIDE table per
component, ASVS L2 chapter mapping (V1–V14) with "where enforced / which test",
OWASP LLM Top 10 (2025) mapping (LLM01 prompt injection … LLM10 unbounded
consumption), and a list of residual risks/open items.

### 4.8 Exit criteria for M1

`uv run pytest` green, coverage ≥85% on `common`, mypy strict clean, ruff clean,
CI pipeline green on the branch, CLAUDE.md and docs updated, results shown for
review.

Environment note: the build container has `uv` and Python 3.13; uv will provision
3.12. gitleaks/semgrep/trivy/syft are not preinstalled locally — they run in CI;
I will install them locally where the network allows and report anything I could
only run in CI.

---

## 5. Questions / items needing your decision

1. **Saudi national ID / Iqama check digit (ADR 0018).** I am not aware of a
   publicly published authoritative specification from NIC/Absher. The widely
   used algorithm is a Luhn-variant over the 10 digits (first digit 1 = citizen,
   2 = resident). Proposal: implement that, label it "de-facto, unverified" in the
   ADR, and swap in your authoritative spec if you can provide one. Do you have one?
2. **Licence policy (ADR 0009).** OK with the SPDX allow-list above and
   "custom licences need a committed approval record"? This affects Jais,
   ALLaM, AceGPT and possibly some Qwen releases, and pyannote's gated models.
3. **CI host.** GitHub Actions now, Forgejo-compatible — confirm.
4. **README.** OK to replace the current `README.md` ("My-naghi / Ai Workflows")
   with the Triaxis overview? (It is a file I did not create.)
