# 0003 — CI on GitHub Actions syntax, Forgejo-compatible

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

The repository is hosted on GitHub today. In production the platform's own
Forgejo instance (control cluster) runs CI, with no internet access. Forgejo
Actions runs GitHub Actions workflow syntax.

## Decision

- The canonical pipeline lives in `.ci/workflows/`. Identical copies are kept in
  `.github/workflows/` and `.forgejo/workflows/`; the `lint` job fails if they
  drift (`tools/check_ci_sync.py`).
- Workflows use only `actions/checkout`, pinned by full commit SHA. Everything
  else is installed from hash-pinned requirement files
  (`.ci/requirements/*.txt`) or run as version-pinned container images, so the
  pipeline can be mirrored into Harbor/Forgejo without marketplace actions.
- Jobs: lint, typecheck, unit tests + coverage gate, gitleaks, semgrep, bandit,
  pip-audit, SBOM (syft), licence gate, no-weights check. Trivy is added in
  Milestone 2 when images exist.
- Container images used by CI are pinned by tag now and must be pinned by
  digest when they are mirrored into Harbor (tracked for Milestone 6).

## Consequences

- No third-party action can change underneath us.
- Semgrep rule packs (`p/owasp-top-ten`, `p/python`) are fetched from the
  Semgrep registry in GitHub CI. For air-gapped Forgejo CI they must be
  vendored into the repository (Milestone 6).

## Alternatives considered

- Marketplace actions (setup-uv, gitleaks-action): convenient but not
  mirror-friendly and harder to pin.
