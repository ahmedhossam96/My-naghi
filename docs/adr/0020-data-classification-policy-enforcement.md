# 0020 — Data classification and policy enforcement

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal); tenant DPO to co-own policies

## Context

Triaxis must decide, before content reaches a model, a store or any other
processing boundary, what may happen to it. A label in the UI is not enough:
the decision must be enforced by the backend, fail closed, be explainable
without revealing content, and be configurable per tenant and brand without
code changes. Restricted data must never be sent to an LLM to ask whether it
is safe.

## Decision

1. **Taxonomy** (`triaxis_common.taxonomy`): ordered levels Public < Internal <
   Confidential < Restricted; data-type tags (national ID/Iqama, IBAN, payment
   card, phone, e-mail, VIN, credential, customer record, voice recording);
   separate privacy tags (personal, sensitive personal, credentials/secrets,
   regulated financial); decisions ALLOW < REDACT < RESTRICT < BLOCK;
   destinations (inference, client response, persistence, export, backup);
   required controls; audit-safe reason codes. Details: `docs/data-classification.md`.
2. **Deterministic classifier** (`triaxis_common.classification`): Unicode NFKC
   normalisation, hidden-character stripping and Arabic-Indic digit mapping
   for detection; pattern detectors with validators (de-facto Saudi ID check
   per ADR 0018, ISO 13616 mod-97, Luhn); field-level rules for records; a
   workload baseline from policy; propagation from a source result (audio →
   transcript → analytics); a client-requested level that can only raise the
   result. The result type contains labels, counts and codes only, never content.
3. **Policy engine** (`triaxis_common.policy`): versioned YAML policies in
   `config/policies/` (schema in `config/schema/data-policy.schema.json`);
   tenants reference one with `data_policy`; brand overrides inside the policy.
   The strictest applicable rule wins: level rule per destination, tag rules,
   allowed workloads per level (anything else → BLOCK). Missing policy or any
   engine error → `classification_unavailable` (HTTP 503), nothing processed.
4. **Enforcement points**: chat input (all messages), model output (streamed,
   segment by segment) as `client_response`, audio before STT (voice-recording
   baseline), transcripts before return and before analytics, TTS input, the
   voice pipeline at every stage, and backup collection (restricted tags fail
   the backup). Future RAG indexing/retrieval, exports and business-record
   persistence must call `PolicyEngine.enforce` with their destination.
5. **Audit**: one `classification_decision` event per enforcement with level,
   decision, data tags, reason codes and policy version (allow-list fields added
   to `LogRecord`, see ADR 0007 amendment). Streaming output segments are
   aggregated into one event.
6. **No downgrade by request.** Reclassification to a lower level is not
   possible through any API; it requires a reviewed policy change in Git.

## Consequences

- Policies are explicit and testable; changing handling for a tenant or brand is
  a configuration change with review.
- Pattern detection has false positives (fails towards masking) and false
  negatives (e.g. free-text health information, names). Presidio-based
  recognizers in the guardrails service (Milestone 3) add signals; the
  deterministic engine remains the enforcement boundary.
- The taxonomy is not, by itself, evidence of legal compliance (PDPL, PCI DSS);
  mapping to MY NAGHI policies must be confirmed by the tenant's DPO.
