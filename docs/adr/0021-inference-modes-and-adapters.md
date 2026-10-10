# 0021 — Inference modes and self-hosted model adapters

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

The demo answered with canned sentences, canned transcripts and tone audio.
That is acceptable as a labelled simulation but is not inference. Real
inference must use approved, self-hosted models only, must never silently fall
back to simulated output, and must be testable without GPUs in CI.

## Decision

- **Explicit mode** (`InferenceMode`): `mock` or `real`, declared in
  `InferenceSettings`. Staging and production reject `mock` at validation time.
  Every response carries its mode; the UI shows it.
- **OpenAI-compatible adapters** (`triaxis_common.inference`):
  `OpenAICompatibleLlm` (`/v1/chat/completions`, SSE streaming),
  `OpenAICompatibleStt` (`/v1/audio/transcriptions`, multipart built in memory),
  `OpenAICompatibleTts` (`/v1/audio/speech`, response must be audio). This is
  the API exposed by vLLM and common self-hosted STT/TTS servers.
- **Model gate before network**: an `ApprovalChecker`
  (`tools.model_registry.RegistryApproval`) requires the model to exist, match
  the task, be `approved` and have a verified commercial licence that passes the
  licence gate. An unapproved model fails with `model_unavailable` and the
  endpoint is never contacted.
- **Model selection from tenant configuration** (`models.llm_chat`,
  `llm_voice`, `stt`, `tts`, brand overrides), never from the client.
- **Failure handling**: in-cluster URLs only; connect/read/total timeouts;
  bounded retries (connection errors and 502/503 before any output, max 3,
  exponential backoff); no retry after partial output; upstream statuses mapped
  to catalogue codes (401/403/404/5xx → `model_unavailable`, 429 →
  `rate_limited`, 408/504 → `timeout`, malformed/truncated → `upstream_error`);
  upstream bodies never logged or returned; closing the stream closes the
  upstream connection (client cancellation).
- **Health**: `health()` reports `unapproved`, `unreachable`, `degraded`,
  `model_not_served` or `ready` without URLs or upstream messages.
- **Mock adapters** (`triaxis_demo.mocks.Mock*Client`) implement the same
  protocols and are used only in mock mode.

## Consequences

- Real inference is proven only when an approved model is served by a
  self-hosted endpoint and the real-mode tests/acceptance steps pass against it.
  As of this ADR no registry model is approved, so real inference is BLOCKED
  (see `docs/status/capability-matrix.md`).
- Revision pinning is enforced at deployment (vLLM `--revision`, artefact
  sha256 verification on the model-preparation server); the OpenAI API does not
  expose revisions, so the adapter verifies the served model name only.
- The registry currently lives in `tools/`; services that run outside the
  repository need it packaged (follow-up for Milestone 2 service images).
