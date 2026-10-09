# 0010 — Model origin switchable per tenant

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Enterprise customers may have policies about the country or organisation
that produced a model. MY NAGHI may approve different origins than a later
tenant.

## Decision

- Each registry entry records `origin.organization` and `origin.country`
  (ISO 3166-1 alpha-2).
- Each tenant config lists `approved_origin_countries` and selects models per
  profile: `stt`, `tts`, `llm_voice`, `llm_chat`, with optional overrides per
  brand.
- `tools.model_registry.validate_tenant_selection()` rejects a selection that
  points at an unknown model, a model of the wrong task, a model whose origin
  is not approved, or (in production) a model that is not `approved`.
- Switching origin is a tenant config change. No code change.

## Consequences

- The LLM pool may host several models; the router (Milestone 4) routes by the
  tenant's selected model ID.
