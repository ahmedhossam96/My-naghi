# Local testing guide

## 1. Quality gates (what CI runs)

```bash
uv sync --all-packages
uv run pytest                      # all suites + coverage gate + log schema gate
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run bandit -c pyproject.toml -r services tools --severity-level high --confidence-level medium
uv run python -m tools.license_gate
uv run python -m tools.model_registry check && uv run python -m tools.model_registry check-tenants
uv run python -m tools.export_schemas --check
uv run python -m tools.check_no_weights --all
```

## 2. What the automated suites prove

| Suite | Proves | Does **not** prove |
|---|---|---|
| `services/common/tests/test_classification.py`, `test_policy.py` | Detection (incl. Arabic-Indic digits, hidden characters), levels, propagation, no downgrade, policy decisions per tenant/brand/destination, fail-closed behaviour, metadata-only audit | Legal compliance; detection of names/free-text sensitive data |
| `services/common/tests/test_inference.py` | Adapters speak the OpenAI-compatible protocol (fake server), model gate before network, retries/timeouts/cancellation/error mapping | That any real model works |
| `services/demo/tests/test_pipeline.py` | Blocked content never reaches an adapter; redaction before the adapter; output masking; real-mode wiring against a fake server; no mock fallback; voice brand restrictions; stateless turns; sentinel absent from logs, audit and temp dirs | Real model quality, latency or availability |
| `tests/unit/tools/test_backup.py` | Backup/verify/restore, tamper and corruption detection, key handling, role separation, exclusions, retention, safe extraction, CLI | Off-site replication, WORM, scheduled runs, measured RPO/RTO |

## 3. Running the demo

```bash
uv run python -m triaxis_demo                                  # mock mode
TRIAXIS_BACKUP_DIR=/srv/triaxis-backups uv run python -m triaxis_demo   # with backup status
```

## 4. Real-inference acceptance test (currently BLOCKED)

Step-by-step: `docs/runbooks/run-demo-with-real-models.md` (approval with `tools.model_onboard`, download, serve, start, acceptance).

Needs: an approved registry entry for each selected model (verified licence,
pinned revision, artefact hashes) and self-hosted OpenAI-compatible endpoints.

1. Approve the model in `models/registry.yaml`; `python -m tools.license_gate` passes.
2. Serve it, e.g. `vllm serve <hf-repo> --revision <sha> --served-model-name qwen3.5-27b --port 8000`
   (on the GPU host; weights fetched only via the model-preparation workflow).
3. Start the demo with `TRIAXIS_INFERENCE_MODE=real TRIAXIS_LLM_URL=http://127.0.0.1:8000`.
4. Inference Services card shows `ready`; chat shows `real model`; reply differs per prompt.
5. Send the synthetic ID sample → model receives `[NATIONAL_ID]` (check vLLM request
   logging is disabled in prod; use a local proxy capture only in the drill).
6. Stop the endpoint → chat returns `model_unavailable`; no simulated text appears.
7. Repeat for STT (Arabic sample audio, real transcript), TTS (real speech) and a voice turn.
8. Record results in `docs/status/capability-matrix.md` with date, model ids and revisions.

## 5. Backup drill

See `docs/runbooks/backup-restore.md` §0–§3. The drill backs up the repository
configuration, verifies it, restores it into an isolated directory and checks
schemas, policies, the licence gate and tenant/brand boundaries.
