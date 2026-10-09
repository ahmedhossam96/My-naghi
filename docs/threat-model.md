# Triaxis threat model — v1

Status: v1 (Milestone 1). Reviewed at the end of every milestone; the
"Status" columns track what is implemented and tested.

Method: data-flow diagram → trust boundaries → STRIDE per element → controls
mapped to OWASP ASVS 4.0.3 Level 2 and the OWASP Top 10 for LLM Applications
(2025) → residual risks.

## 1. Assets

| Asset | Sensitivity | Notes |
|---|---|---|
| Customer audio, text, transcripts, completions | **Critical** | Must exist only in process memory for the request (rule 1) |
| PII inside content (national ID, Iqama, IBAN, card, phone) | Critical | Masked by guardrails before the LLM where policy requires |
| Access tokens, client secrets | High | 15-minute tokens; secrets only in OpenBao |
| Signing keys (cosign, PKI, SSH CA) | Critical | HSM-backed in OpenBao |
| Model weights and registry | High | Integrity (tampering) and licence compliance |
| Tenant configuration, quotas, guardrail policies | Medium | Integrity; tenant isolation depends on it |
| Allow-listed telemetry | Low–Medium | Contains tenant/brand IDs and timings only |

## 2. Data-flow diagram and trust boundaries

```
 [Tenant back-end] ==TB1 private link + mTLS==> [Gateway: Envoy/Coraza/ext_authz]
                                                      │ TB2 (cluster network, TLS)
                                                      ▼
                                            [Router] ──► [NATS JetStream (memory)]
                                                      │
                                                      ▼
                                            [Guardrails] ──► [STT | TTS | LLM (vLLM)]
                                                      │
  [Keycloak] ◄─JWKS (in-cluster)── services      [OpenBao/HSM] ◄─ secrets ── services
  [Prometheus/Loki/Tempo] ◄── allow-listed telemetry only
  ═══TB3 WireGuard═══ other site (artefacts, config, security records only)
  ═══TB4 air gap═══ model-preparation server → Harbor (signed artefacts only)
  ═══TB5═══ offline write-once vault
```

| Boundary | Crossing | Primary controls |
|---|---|---|
| TB1 tenant → gateway | requests with content | private link, mTLS, WAF, JWT, rate limits, size caps |
| TB2 gateway → services | requests with content | TLS, JWT re-validation, NetworkPolicy allow-list |
| TB3 site ↔ site | no customer data | WireGuard, data-classification rule, egress policy |
| TB4 model prep → cluster | model artefacts | licence gate, safetensors only, sha256, cosign, Kyverno |
| TB5 vault | security records, config | write-once media, offline |

## 3. STRIDE

| # | Element | Threat | STRIDE | Control | Where | Status |
|---|---|---|---|---|---|---|
| S1 | Gateway, services | Forged or replayed token | S | RS/PS/ES256 only, `kid` required, JWK alg match, `exp`/`iat`/lifetime ≤ 900 s, iss/aud checks | `triaxis_common.auth` | ✅ M1 (tests) |
| S2 | Services | Bypassing the gateway | S/E | JWT re-validated in every service; NetworkPolicy allows only gateway → router → services | auth (M1), k8s (M6) | ✅ / ⏳ |
| S3 | Keycloak JWKS | Attacker-controlled key set | S | JWKS URL validated in-cluster + HTTPS in prod; no `jku`/`x5u` honoured | `JwksCache` | ✅ M1 |
| T1 | Model artefacts | Swapped/backdoored weights | T | registry sha256, safetensors only, cosign + Kyverno, isolated model-prep server | tools (M1), Kyverno (M6) | ✅ / ⏳ |
| T2 | Pickle deserialisation | RCE via model file | T/E | `pickle`/`torch.load` banned by lint; non-safetensors artefacts denied by gate; no-weights commit check | ruff, licence gate | ✅ M1 |
| T3 | Supply chain | Malicious dependency | T | hash-pinned lock, pip-audit, SBOM, trivy (M2), signed images | CI | ✅ M1 (trivy M2) |
| T4 | Tenant config | Quota or policy tampering | T | config in Git with review, schema validation, signed releases | config models | ✅ M1 |
| R1 | All | Disputed actions without content logs | R | allow-listed request records with request ID, tenant, brand, status, timings; Wazuh/auditd | logging (M1), Wazuh (M7) | ✅ / ⏳ |
| I1 | Logs/traces/metrics | Customer content in telemetry | I | allow-list schemas; third-party log messages discarded; span sanitiser; label allow-list; session-wide schema gate in CI | logging, tracing | ✅ M1 |
| I2 | Error responses | Input echoed in errors | I | fixed error catalogue; no Pydantic locations/values; WebSocket close reason fixed | errors, http | ✅ M1 |
| I3 | Disk | Content spilled to temp files, swap or core dumps | I | `tempfile` banned; TMPDIR on tmpfs (fail closed); RLIMIT_CORE=0, not dumpable; swap off on hosts | runtime (M1), Ansible (M6) | ✅ / ⏳ |
| I4 | Batch queue | Content persisted by NATS | I | JetStream memory storage, TTL, TLS; zero-retention test asserts no store dir | router (M4) | ⏳ |
| I5 | Cross-tenant | Brand A reads Brand B's results | I | exact-scope authorisation; job IDs bound to (tenant, brand); constant-time comparison | auth (M1), router (M4) | ✅ / ⏳ |
| I6 | Network | Data exfiltration / residency breach | I | default-deny egress; all endpoints validated in-cluster at start-up; no third-party AI APIs | config (M1), k8s (M6) | ✅ / ⏳ |
| I7 | LLM | PII leakage in completions | I | Presidio + Saudi recognizers on input and output; topic policy | guardrails (M3) | ⏳ |
| D1 | Gateway/router | Flooding / quota exhaustion by one brand | D | per-(tenant, brand, workload) rate limits and quotas; real-time reserved capacity | gateway (M3), router (M4) | ⏳ |
| D2 | Services | Oversized payloads, long audio, token bombs | D | body cap (Content-Length and streamed), duration and token caps, strict timeouts | http (M1), services (M2) | ✅ / ⏳ |
| D3 | JWKS refresh | Unknown-`kid` refresh storm | D | ≤ 1 refresh per 30 s; stale keys served ≤ 1 h | `JwksCache` | ✅ M1 |
| E1 | Containers | Container escape / privilege escalation | E | non-root, read-only fs, no capabilities, seccomp RuntimeDefault, no shell, PSS restricted | images (M2), k8s (M6) | ⏳ |
| E2 | Token scope | Wildcard / cross-tenant scope | E | no wildcards; malformed or cross-tenant scope rejects the whole token | auth | ✅ M1 |

## 4. OWASP ASVS 4.0.3 Level 2 mapping

| Chapter | Requirement focus | Triaxis control | Status |
|---|---|---|---|
| V1 Architecture | threat model, secure SDLC, ADRs | this document, `docs/adr/`, CI gates | ✅ M1 |
| V2 Authentication | service authentication | OAuth 2.0 client credentials via Keycloak; mTLS | ⏳ M3 |
| V3 Session management | short-lived tokens | 15-minute JWTs, no server sessions | ✅ M1 |
| V4 Access control | deny by default, least privilege | exact `tenant:brand:workload` scopes, re-validated per service | ✅ M1 |
| V5 Validation, sanitisation | strict input validation | Pydantic `extra="forbid"`, caps, magic-byte audio validation (M2), Unicode normalisation (M3) | ✅ / ⏳ |
| V6 Cryptography | approved algorithms, key management | RS/PS/ES256, HSM-backed keys, TLS 1.2+ | ✅ / ⏳ |
| V7 Error handling & logging | no sensitive data in logs/errors | allow-list logger, fixed error catalogue, schema gate | ✅ M1 |
| V8 Data protection | no caching/persistence of sensitive data | zero-retention controls (ADR 0006) | ✅ / ⏳ |
| V9 Communications | TLS everywhere | mTLS gateway, TLS internal, WireGuard | ⏳ M3/M6 |
| V10 Malicious code | integrity of dependencies and artefacts | hash-pinned lock, SBOM, cosign, licence gate | ✅ / ⏳ |
| V11 Business logic | quotas and abuse limits | per-brand quotas, priority lanes | ⏳ M4 |
| V12 Files and resources | upload limits, no execution | body caps, in-memory handling, no temp files | ✅ / ⏳ |
| V13 API | schema-validated REST/WebSocket | OpenAPI 3.1 + schemathesis | ⏳ M2 |
| V14 Configuration | hardened build, secrets out of code | gitleaks, OpenBao, distroless non-root images | ✅ / ⏳ |

## 5. OWASP Top 10 for LLM Applications (2025)

| ID | Risk | Control | Status |
|---|---|---|---|
| LLM01 | Prompt injection | input classifier + heuristics (AR/EN/mixed), system-prompt isolation, garak/promptfoo suites | ⏳ M3/M9 |
| LLM02 | Sensitive information disclosure | PII masking in/out, zero retention, no training on customer data | ✅ (retention, M1) / ⏳ M3 |
| LLM03 | Supply chain | model registry, licence gate, safetensors only, sha256, signed artefacts | ✅ M1 / ⏳ M6 |
| LLM04 | Data and model poisoning | fine-tuning only on registered `licensed`/`consented` datasets; never platform traffic | ✅ M1 (gate) |
| LLM05 | Improper output handling | outputs treated as untrusted by tenants; topic policy; no tool execution in Triaxis | ⏳ M3 |
| LLM06 | Excessive agency | Triaxis exposes no tools/actions; reference voice pipeline has no side effects (ADR 0015) | ✅ by design |
| LLM07 | System prompt leakage | system prompt isolated from user content; red-team tests | ⏳ M3/M9 |
| LLM08 | Vector and embedding weaknesses | no RAG/vector store in scope | n/a |
| LLM09 | Misinformation | per-tenant topic policy; evaluation rubric | ⏳ M3/M9 |
| LLM10 | Unbounded consumption | token/duration/size caps, quotas, rate limits, timeouts | ✅ (size, M1) / ⏳ M2–M4 |

## 6. Residual risks and open items

1. **Saudi national ID / Iqama check digit** uses a de-facto algorithm, not an
   authoritative specification (ADR 0018). Mitigated by also masking
   checksum-failing candidates.
2. **Semgrep rule packs** are fetched from the Semgrep registry in GitHub CI;
   air-gapped Forgejo CI needs vendored rules (M6).
3. **CI container images** (gitleaks, syft) are pinned by tag, not digest,
   until mirrored into Harbor (M6).
4. **Model licences** in the registry are claims from the brief; each must be
   verified from the model card before download (ADR 0009).
5. **VAD and diarization models** are not distributed as safetensors and need
   conversion on the model-preparation server or replacement (ADR 0011).
6. **Memory-only batch queue**: a second failure during an outage loses queued
   jobs; callers must resubmit (documented for tenants).
7. **Starlette multipart** spools uploads over 1 MiB to a temp file; this lands
   on the tmpfs `TMPDIR` (enforced), and M2 services must cap uploads and
   prefer non-multipart bodies where possible.
