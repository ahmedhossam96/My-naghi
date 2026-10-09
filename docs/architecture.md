# Triaxis architecture

Status: v1 (Milestone 1). Components marked *(M n)* are built in that milestone.

## 1. Purpose and boundary

Triaxis serves speech-to-text (STT), text-to-speech (TTS) and LLM inference to
enterprise tenants, with all processing inside the tenant's jurisdiction, on
self-hosted models, with **zero retention** of customer content. It exposes
model APIs plus an optional reference voice pipeline; tenant applications own
business logic and CRM integration (ADR 0015). The first tenant is MY NAGHI
Motors (Saudi Arabia), but everything is multi-tenant and onboarding is
configuration only.

## 2. Topology

```
                     Tenant back-ends (private links only — no public endpoint)
                                   │
                    Global traffic manager (health-checked, 50/50)
                     ┌─────────────┴──────────────┐
             Site A — Riyadh                 Site B — Jeddah
   ┌─────────────────────────────────┐  ┌─────────────────────────────────┐
   │ Gateway pair + VIP          (M3)│  │ (same as Site A)                │
   │  Envoy · Coraza WAF · mTLS ·    │  │                                 │
   │  JWT ext_authz · rate limits    │  │                                 │
   │            │                    │  │                                 │
   │ Workload router             (M4)│  │                                 │
   │  ├─ real-time lane (P1,         │  │                                 │
   │  │   reserved capacity)         │  │                                 │
   │  └─ batch lane (P2, NATS        │  │                                 │
   │      JetStream, memory only)    │  │                                 │
   │            │                    │  │                                 │
   │ Guardrails                  (M3)│  │                                 │
   │            │                    │  │                                 │
   │ GPU pools                       │  │                                 │
   │  ├─ STT/TTS (H100 MIG / L40S)(M2)│  │                                 │
   │  └─ LLM (vLLM, cache-aware) (M2)│  │                                 │
   │                                 │  │                                 │
   │ Control cluster             (M6)│  │                                 │
   │  OpenBao+HSM · Keycloak · Harbor│  │                                 │
   │  Forgejo · Prometheus/Grafana/  │  │                                 │
   │  Loki · Wazuh                   │  │                                 │
   └───────────────┬─────────────────┘  └────────────────┬────────────────┘
                   └──────── WireGuard (artefacts, config, ───────┘
                             security records — never customer data)

   Shared: 24/7 SOC · isolated model-preparation server · offline write-once vault
```

### Request path (real-time)

1. Tenant back-end obtains a 15-minute token from Keycloak (client
   credentials) with scopes `<tenant>:<brand>:<workload>` (ADR 0008).
2. Private link → gateway VIP. Envoy terminates mTLS, Coraza applies WAF rules,
   ext_authz validates the JWT, per-(tenant, brand, workload) rate limits apply.
3. Router selects the lane (real-time priority 1) and the tenant's model
   (ADR 0010), enforcing quotas per brand.
4. Guardrails normalise Unicode, mask PII (Saudi recognizers), screen for
   prompt injection, enforce the tenant topic policy.
5. Inference service re-validates the JWT (defense in depth), runs the model,
   streams results back.
6. Nothing about the content is written anywhere; only allow-listed metadata
   (request ID, tenant, brand, workload, timings, sizes, status, error class)
   reaches logs, traces and metrics (ADR 0007).

### Batch path

`POST /v1/batch/jobs` → router → NATS JetStream stream with
`storage: memory`, short TTL, TLS → batch workers → result held in memory until
delivered or TTL (≤ 24 h), then deleted. Deadline: same day (ADR 0014).

## 3. Scaling: cells

A **cell** = 1 STT/TTS GPU server + 1 LLM GPU server. Capacity grows by adding
cells. A cell is added when peak GPU utilisation exceeds **60%**
(`cell_scale_up_threshold_pct` in the hardware profile, ADR 0013). KEDA scales
replicas within the cells that exist (M6).

## 4. Resilience (ADR 0004)

| | Option B — `balanced` (default) | Option A — `mirror` |
|---|---|---|
| Normal operation | 50/50 across sites | 50/50 across sites |
| Real-time capacity per site | 100% of global real-time peak | 100% |
| Batch capacity per site | its normal share | 100% of global batch peak |
| During a site outage | all real-time on the surviving site; batch queued in memory and caught up after recovery within the deadline | both lanes on the surviving site, no queueing |

Failover targets (tested in M8): model replica < 60 s, gateway < 10 s, site
≤ 5 min for real-time traffic.

## 5. Zero retention (ADR 0006)

Defense in depth from code to host:

| Layer | Control |
|---|---|
| Code | in-memory buffers; `tempfile`/`pickle`/`marshal`/`shelve`/`dill`/`torch.load` banned by lint; allow-list logging, tracing, metrics; fixed error catalogue |
| Process | `harden_process()`: `RLIMIT_CORE=0`, not dumpable, `TMPDIR` on tmpfs (fail closed in staging/prod) |
| Container | read-only root fs, tmpfs `TMPDIR`, no persistent volumes on inference pods, no shell |
| Queue | NATS JetStream memory storage, TTL, TLS; store directory must not exist |
| Host | swap off, core dumps off, LUKS2 + Clevis/Tang + TPM2 |
| Proof | zero-retention suite (M3+) searches every file, log, trace, metric and queue store for tagged synthetic content |

## 6. Security architecture (summary)

See `docs/threat-model.md` for STRIDE analysis and the ASVS L2 / OWASP LLM
Top 10 mapping.

- **Identity**: Keycloak, 15-minute tokens, `tenant:brand:workload` scopes,
  re-validated in every service.
- **Transport**: mTLS at the gateway; TLS between services; WireGuard between
  sites.
- **Secrets**: OpenBao backed by an HSM (PKCS#11); services read at runtime
  (ADR 0017).
- **Supply chain**: hash-pinned dependencies, SBOM (syft), cosign signatures
  with an OpenBao-held key, Kyverno verify-images; models in a registry with a
  licence gate and sha256 pins (ADR 0009, 0011).
- **Network**: default-deny ingress and egress NetworkPolicies; no runtime
  egress outside the cluster; every configured endpoint is validated to be
  in-cluster at start-up.

## 7. Code organisation

| Path | What |
|---|---|
| `services/common` | `triaxis_common`: auth, allow-list logging, config, errors, tracing/metrics, secrets, runtime guards, HTTP plumbing |
| `services/<svc>` | one uv workspace member per service, each with its own minimal image |
| `config/` | tenants, hardware profiles, generated JSON Schemas |
| `models/registry.yaml` | model and dataset registry (metadata only) |
| `tools/` | licence gate, registry CLI, no-weights check, SBOM/sign helpers |
| `deploy/` | Helm, Kubernetes policies, Ansible, compose |
