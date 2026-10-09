# 0002 — Monorepo, uv workspace, Python 3.12, hash-pinned dependencies

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Seven Python services share auth, logging, config and error handling. Each
service must ship a minimal image whose SBOM contains only what it uses.
Dependencies must be pinned with hashes (supply-chain requirement).

## Decision

- One Git repository. The repo root is the Triaxis tree.
- A [uv](https://docs.astral.sh/uv/) workspace: the root `pyproject.toml`
  holds shared tool configuration (ruff, mypy, pytest, coverage) and dev
  dependencies; each service is a workspace member under
  `services/<svc>/` with its own `pyproject.toml` and `src/triaxis_<svc>/`.
  Shared code lives in `services/common` (package `triaxis_common`).
- One `uv.lock` for the whole workspace. Images are built from
  `uv export --frozen --package <svc> --no-dev` (uv emits `--hash` lines by
  default) and installed with `pip install --require-hashes --no-deps`, so
  only locked artefacts with matching hashes are installed.
- Python **3.12**, pinned in `.python-version` and `requires-python`.
- Repository tooling in `tools/` is a plain package imported as `tools.*`.

## Consequences

- `uv sync --all-packages` gives a laptop the full environment.
- Adding a dependency to one service updates the shared lock; reviewers see
  the full diff.
- Strict `mypy` and `ruff` run over the whole workspace.

## Alternatives considered

- Poetry or pip-tools: slower, weaker workspace support.
- One repo per service: duplicated common code and inconsistent versions.
