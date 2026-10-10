# Triaxis — Sovereign AI Inference Platform

Triaxis is Quantum Seal's multi-tenant inference platform for speech-to-text
(STT), text-to-speech (TTS) and large language models (LLM). All processing
stays inside the customer's jurisdiction, on self-hosted models, with zero
retention of customer content.

- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Threat model: [`docs/threat-model.md`](docs/threat-model.md)
- Decisions: [`docs/adr/`](docs/adr/)
- Build plan and milestones: [`docs/plan.md`](docs/plan.md)
- Contributor / agent guide: [`CLAUDE.md`](CLAUDE.md)

## Client demo (prototype)

```bash
uv sync --all-packages
uv run python -m triaxis_demo     # http://127.0.0.1:8080, offline, simulated inference
```

See [`services/demo/README.md`](services/demo/README.md) for the views and a demo script.
Status of real vs simulated capabilities: [`docs/status/capability-matrix.md`](docs/status/capability-matrix.md).

## Quickstart (development)

```bash
uv sync --all-packages          # Python 3.12 is provisioned by uv
uv run pytest                   # unit tests + coverage gate
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python -m tools.license_gate
```

## Product rules (contractual)

1. Zero retention of customer content (memory only; never in logs, traces, metrics or errors).
2. No training on customer data.
3. Data residency: no runtime egress outside the cluster.
4. Tenant and brand isolation (`tenant:brand:workload` scoped tokens).
5. Signed supply chain (cosign + SBOM, enforced by Kyverno).
6. Models load from `safetensors` only.
