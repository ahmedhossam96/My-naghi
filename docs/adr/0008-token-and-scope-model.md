# 0008 — Token and scope model

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Contract rule 4: every request carries a token scoped to
`tenant:brand:workload`, and one brand can never read, affect or exhaust
another's quota. The gateway validates tokens, but services must re-validate
(defense in depth). There is no runtime egress (rule 3), so signing keys come
from the in-cluster Keycloak only.

## Decision

### Token

- Issued by Keycloak (OAuth 2.0 client credentials for tenant back-ends).
- Signed with **RS256, PS256 or ES256** only. `none`, all `HS*` and any other
  algorithm are rejected before signature verification. The JWK's own `alg`,
  if present, must match the header.
- `kid` is required.
- Required claims: `iss`, `aud` (`triaxis`), `exp`, `iat`, `sub`, `azp`,
  `tenant`, `scope`. `nbf` is checked when present.
- Lifetime `exp - iat` ≤ **900 s** (15 minutes). Clock leeway ≤ 30 s.
- Maximum encoded token size 8 KiB.

### Scope

- Entries in the space-separated `scope` claim that contain `:` are Triaxis
  scopes and must match `<tenant>:<brand>:<workload>` exactly:
  - `tenant`, `brand`: lowercase slug `[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?`
  - `workload`: one of `stt`, `tts`, `llm`, `voice`, `batch`
  - no wildcards.
- Standard OIDC scopes without `:` (`profile`, `email`) are ignored.
- The token is **rejected** if any Triaxis scope is malformed or names a
  tenant other than the `tenant` claim, or if it has no Triaxis scope.

### Authorisation per request

- Tenant = the `tenant` claim. An optional `X-Triaxis-Tenant` header must match
  it.
- Brand = the `X-Triaxis-Brand` header, or, when absent, the single brand the
  token holds for the requested workload (ambiguous → 400).
- The request is allowed only if the token holds exactly
  `<tenant>:<brand>:<workload>`. Deny by default. Comparisons use
  `hmac.compare_digest` and do not short-circuit.

### Keys

- `JwksCache` fetches the JWKS from the in-cluster Keycloak URL (validated to
  be an in-cluster host), caches it (TTL 5 min), refreshes on an unknown `kid`
  at most once per 30 s, and keeps serving the last good key set for up to 1 h
  if Keycloak is unreachable.
- Library: **PyJWT** (`PyJWT[crypto]`), widely used, explicit algorithm
  allow-list per call.

## Consequences

- Keycloak needs a protocol mapper that adds the `tenant` claim and client
  scopes named `<tenant>:<brand>:<workload>`. Onboarding a tenant is Keycloak
  configuration plus a tenant config file — no code change.
- Rate limits and quotas are keyed by `(tenant, brand, workload)`.
