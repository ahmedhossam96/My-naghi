# Capability matrix and acceptance evidence

Last updated: 2026-10-09 (Milestone 2 continuation, phases A–F).
Branch: `claude/triaxis-platform-build-w849po`. Changes are **uncommitted**
pending authorisation.

Status values: **Implemented** (works, with test evidence), **Partially
Implemented**, **Mocked** (labelled simulation), **Missing**, **Blocked**
(cannot be verified here; dependency stated).

## 1. Baseline (Phase A, before changes)

| Check | Result |
|---|---|
| `uv run pytest` | 440 passed, coverage 99.05 % |
| ruff check / format, mypy strict, bandit (high) | clean |
| licence gate, registry, tenants, schemas, no-weights, CI sync | pass |
| Inference runtimes in this environment | none: no GPU, no vLLM/Ollama/whisper/piper, no listening model ports |
| Approved models in registry | 0 of 11 (all `candidate`, licences unverified) |
| `services/{gateway,router,stt,tts,llm,guardrails,voice}` | README placeholders only |
| Demo AI | `MockLlm` canned replies, `MockStt` canned transcripts, `MockTts` tones |

## 2. Final evidence (after changes, this environment)

| Check | Result |
|---|---|
| `uv run pytest` | **655 passed**, coverage 98.75 %, log schema gate clean |
| ruff check, ruff format --check | clean (120 files) |
| mypy --strict | no issues (62 source files) |
| bandit (high, medium confidence) | 0 issues |
| pip-audit (locked set, hashes) | no known vulnerabilities |
| gitleaks (working tree, `--no-git`) | no leaks |
| licence gate / registry / check-tenants / schemas / no-weights / CI sync | pass |
| semgrep | **NOT RUN** (registry rules unreachable from this environment; runs in GitHub CI) |
| GitHub CI on these changes | **NOT RUN** (changes not pushed, awaiting authorisation) |
| Hidden/bidi character scan (new test) | pass |
| Backup drill on repository config (CLI) | create → verify → isolated restore: 20 files, all 7 validation checks `ok` |
| Browser check (Playwright/Chromium) | Chat, STT + analytics, Voice Agent, Classification, Infrastructure (backup card) render; only console errors are the expected 404 (optional logo) and 422 (deliberate blocked sample) |

## 3. Capabilities

| Capability | Owner | Status | Implementation | Automated tests | Manual acceptance | Security / classification notes |
|---|---|---|---|---|---|---|
| Authenticated chat request | demo API (`/api/chat`) + `triaxis_common.auth` | Implemented | `require_scope(LLM)`, tenant from token | `test_chat_requires_token`, `test_cross_brand_call_is_denied`, `test_cross_tenant_header_rejected` | Playground "Try to call as …" | Deny by default |
| Input validation and size limits | `triaxis_common.http`, pydantic models | Implemented | 8 MiB body cap, 8 000 chars/message, 50 messages, `extra=forbid` | `test_chat_validation_does_not_echo`, body-limit tests | — | Errors never echo input |
| Classification before inference | `triaxis_common.classification` / `policy` | Implemented | Every message classified; strictest decision wins | `test_policy.py`, `test_restricted_identifiers_redacted_before_model` | Classification tab | Labels only in audit |
| Policy evaluation and redaction | `PolicyEngine.enforce` | Implemented | Tenant `data_policy`, brand overrides | `test_tenant_policy_difference`, `test_brand_override_difference` | Synthetic ID sample | Fail closed on missing policy |
| Blocked content never reaches model | pipeline | Implemented | `PolicyBlockedError` before adapter | `test_blocked_content_never_reaches_model`, `test_tts_redacts_and_blocks_before_synthesis`, `test_voice_blocked_transcript_stops_pipeline` | Synthetic secret sample → 422 | |
| Real LLM inference | `OpenAICompatibleLlm` | **Blocked** | Adapter implemented and wired; real mode selectable | Contract tests vs fake server (`test_inference.py`), `test_real_mode_shows_model_output` | **Not run**: needs an approved model and a self-hosted endpoint (`docs/local-testing.md` §4) | Unapproved → 503, endpoint not contacted |
| Streaming, timeout, cancellation, retry, upstream errors | adapter + pipeline | Implemented (vs fake server) | SSE; explicit deadline; bounded retries before output; SSE `error` after output | `test_total_deadline_enforced_during_stream`, `test_cancellation_closes_upstream_stream`, `test_connect_error_after_output_is_not_retried`, status-mapping tests | — | No upstream text leaks |
| Output safeguards | `OutputGuard` | Implemented | Segment-wise `client_response` enforcement | `test_model_output_is_masked` | — | |
| Mock mode, clearly labelled | `triaxis_demo.mocks` | Mocked | `mode: mock` on every response; UI mode indicator; staging/prod reject mock | `test_production_refuses_mock_mode`, `test_status_endpoint` | Mode indicator | No silent fallback in real mode (`test_real_mode_unapproved_model_fails_closed`) |
| Audio upload / recording validation | pipeline | Implemented | Magic bytes, 60 s WAV cap, raw body (no multipart spooling) | `test_stt_rejects_invalid_audio`, `test_stt_rejects_oversized_body` | Non-audio sample → 415 | |
| Real STT | `OpenAICompatibleStt` | **Blocked** | Adapter + wiring | `test_stt_real_transcript_classification_and_analytics` (fake server) | Not run: no approved STT model/endpoint | Audio baseline confidential |
| Transcript classification and propagation | policy engine | Implemented | Audio → transcript → analytics propagation | `test_audio_to_transcript_to_analytics_propagation`, `test_stt_analytics_*` | STT + analytics checkbox | Restricted never in batch |
| Downstream analytics | `analyse_transcript` | Implemented (mock / fake server) | LLM summary; requires LLM scope | `test_stt_analytics_requires_llm_scope`, `test_stt_analytics_blocked_by_strict_policy` | — | |
| Temporary data cleanup / no disk writes | pipeline | Implemented | In-memory only; `del` in `finally` | `test_sentinel_never_reaches_logs_audit_or_disk` | — | TMPDIR on tmpfs in prod (ADR 0006) |
| Streaming (WebSocket) STT | — | Missing | Not in current architecture code | — | — | Milestone 2 STT service |
| Real TTS | `OpenAICompatibleTts` | **Blocked** | Adapter + wiring; response must be audio | `test_tts_real_mode_returns_model_audio` (fake server) | Not run: no approved TTS model/endpoint | |
| Voice-agent pipeline | `/api/voice/turn` | Partially Implemented | STT → policy → LLM (voice model) → output guard → TTS; stateless; VAD not configured | `test_voice_turn_*`, `test_voice_turns_are_stateless_across_brands`, `test_voice_turn_real_mode` | Voice Agent tab (mock) | Voice only for brands with `voice` workload (mynaghi.sa, BMW, MINI) |
| Brand restriction for voice | tenant config + scopes | Implemented | `config/tenants/mynaghi.yaml` | `test_voice_denied_for_other_brands` | Switch brand | |
| Inference service status view | `/api/inference/status` | Implemented | Health per profile; no URLs | `test_status_endpoint` | Inference Services card | Requires token |
| Infrastructure / failover view | demo | Mocked | Labelled simulation; rules live | existing simulation tests | — | |
| SOC / BAS / remediation | demo | Mocked (demo data) | Labelled | existing tests | — | |
| Audit view | `/api/audit` | Implemented | Live allow-listed records incl. classification decisions | sentinel tests | Search for sent phrase → no match | Metadata only |
| Backup creation | `tools/backup.py` | Implemented | Envelope encryption, HMAC manifest, fail-closed scan | `test_backup.py` (49 tests) | Runbook drill | Excludes transient content and secrets |
| Integrity verification | `tools/backup.py` | Implemented | MAC, archive hash, GCM, per-file hashes | corruption/tamper tests | Runbook §2 | |
| Isolated restore + validation | `tools/backup.py` | Implemented | Safe extraction; 7 checks | restore tests | Runbook §3 (executed, §2 above) | |
| Backup/restore authorisation | `tools/backup.py` | Implemented | Roles + distinct approver for in-place | `test_role_separation`, `test_in_place_restore_needs_distinct_approver`, CLI token tests | — | |
| Retention / expiry | `tools/backup.py` | Implemented | `expires_at`, `prune` | `test_retention_expiry_and_partial_cleanup` | — | |
| Backup status view | `/api/backup/status` | Implemented | Unverified listing + ledgers | `test_backup_status_endpoint` | Infrastructure view | |
| Scheduled backups, off-site replication, WORM, OpenBao snapshots, measured RPO/RTO | — | Missing | — | — | — | Milestones 6/8 |
| Model licensing / artefact verification | `tools/license_gate.py`, registry | Implemented | Unchanged; now also gates inference | `test_registry_approval`, `test_repository_registry_approves_nothing_yet` | — | Never bypassed |

## 4. Definition of done (continuation prompt §11)

| Item | Status | Evidence |
|---|---|---|
| Milestone 1 regression tests pass | ✅ | 655 passed (incl. all M1 suites) |
| Matrix distinguishes real vs simulated | ✅ | This document |
| Classification enforced by backend | ✅ | `test_policy.py`, `test_pipeline.py` |
| Restricted data blocked/handled per policy | ✅ | credential block, identifier redaction tests |
| Redaction before model invocation | ✅ | `test_restricted_identifiers_redacted_before_model` |
| Real chat inference verified or explicitly blocked | ⛔ Blocked | No approved model; no endpoint (§3) |
| Real STT and TTS verified or explicitly blocked | ⛔ Blocked | Same |
| Tenant/brand isolation positive and negative evidence | ✅ | auth, cross-brand, cross-tenant, voice-brand tests |
| No content or secrets in logs/traces/metrics/audit | ✅ (logs, audit, temp dirs); traces/metrics covered by M1 allow-list tests | sentinel tests; M1 tracing tests |
| Transient inference data excluded from backups | ✅ | `test_transient_content_and_secrets_excluded` |
| Backup creation, verification, isolated restore tests | ✅ | `test_backup.py`, CLI drill |
| Backup/restore authorisation enforced | ✅ | role tests |
| Licensing and artefact verification enforced | ✅ | licence gate + `RegistryApproval` |
| Demo mode cannot be confused with production inference | ✅ | mode indicator, per-response `mode`, prod rejects mock |
| CI and security checks have recorded outcomes | ⚠ Partial | Local outcomes recorded (§2); GitHub CI and semgrep NOT RUN on these changes |
| Documentation matches implementation | ✅ | ADR 0020–0022, guides listed in §6 |
| Remaining blockers identified | ✅ | §5 |

## 5. Remaining blockers and unverified production requirements

1. **No approved model.** Every registry entry is `candidate`. Approving requires
   licence verification from the model card, pinned revision and artefact
   hashes via the model-preparation workflow (ADR 0009/0011). Product owner and
   legal must approve custom licences (Jais, ALLaM, AceGPT, pyannote).
2. **No self-hosted endpoints/GPUs** in this environment. Real LLM, STT, TTS
   and voice acceptance (local-testing guide §4) must run on reference hardware.
3. **VAD and streaming (WebSocket) STT** not implemented (Milestone 5 / 2).
4. **Saudi ID checksum** remains the de-facto algorithm (ADR 0018).
5. **Classification coverage gaps**: names, addresses, free-text sensitive
   categories; Presidio recognizers planned in the guardrails service (M3).
6. **Backup DR completeness**: scheduler, replication, WORM, OpenBao snapshot,
   control-cluster product backups, measured RPO/RTO (M6/M8).
7. **Registry packaging**: `RegistryApproval` lives in `tools/`; service images
   need it packaged (M2 service build).
8. **Gateway, router, guardrails services** (M3/M4) not built; the demo enforces
   in-process.
9. **CI evidence** for this change set pending push authorisation.
10. **Production readiness** additionally requires environment-specific
    integration, deployment, penetration testing, performance and recovery
    validation. Local tests passing is not production readiness.

## 6. Documents

ADR 0020 (classification), ADR 0021 (inference), ADR 0022 (backup),
`docs/data-classification.md`, `docs/model-adapters.md`,
`docs/backup-recovery.md`, `docs/runbooks/backup-restore.md`,
`docs/local-testing.md`, updated `docs/threat-model.md`, `docs/plan.md`,
`CLAUDE.md`.
