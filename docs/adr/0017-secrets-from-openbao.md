# 0017 — Secrets from OpenBao at runtime

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Secrets must never be in code, images, logs or committed env files. OpenBao
(backed by an HSM over PKCS#11) is the secret store.

## Decision

- `triaxis_common.secrets.SecretProvider` protocol with:
  - `OpenBaoSecretProvider`: Kubernetes auth (service-account JWT) to an
    in-cluster OpenBao address, reads KV v2, caches the client token until
    shortly before lease expiry. No secret is ever logged; values are wrapped in
    `SecretValue`, whose `repr`/`str` print `***`.
  - `FileSecretProvider`: reads files from a directory (dev/tests, or an
    OpenBao Agent sidecar rendering to a tmpfs). Path traversal is rejected.
- gitleaks runs in pre-commit and CI.
- `.env` files are git-ignored; only `.env.example` without values may be
  committed.

## Consequences

- Pods need a service account bound to an OpenBao role (Milestone 6).
