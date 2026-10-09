# 0009 — Model registry and licence gate (deny by default)

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Triaxis is a commercial product. A model with a non-commercial or
research-only licence must never be downloaded, let alone served. Licences of
open-weight models vary and change between releases, so the licence must be
verified from the model card when each model is added.

## Decision

All models (and evaluation/fine-tuning datasets) are recorded in
`models/registry.yaml`, validated by `tools/model_registry.py`, and checked by
`tools/license_gate.py` in pre-commit and CI.

### Model entry

`id`, `task`, `version`, `source` (repository, revision), `origin`
(organisation, ISO country), `license` (SPDX id or name, URL, `verified_on`,
optional `approval`), `languages`, `hardware_profiles`, `format`
(`safetensors` only), `artefacts` (filename, sha256, size), `status`
(`candidate` | `approved` | `retired`), `notes`.

### Verdicts

| Verdict | Rule |
|---|---|
| `deny` | Licence id, name or notes contain a non-commercial / research-only marker (`NC`, `NonCommercial`, `research only`, …). **Not overridable.** Also: any artefact that is not `.safetensors` or an allowed config/tokenizer file. |
| `allow` | SPDX id in the allow-list: `Apache-2.0`, `MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`. |
| `review_approved` | Any other licence with a committed `approval` record: `approved_by`, `approved_on`, `license_url`, `license_text_sha256`. |
| `needs_review` | Any other licence without approval. |

- The gate exits non-zero on any `deny`, and on `needs_review` for an
  `approved` model.
- An `approved` model must have `license.verified_on`, at least one artefact,
  and a sha256 for every artefact.
- `candidate` entries are metadata only. `model_registry.can_download()`
  returns `False` unless the verdict is `allow`/`review_approved` **and** the
  licence has been verified.

### Datasets

Dataset entries carry `consent_basis` and `source_kind`. The gate refuses any
dataset whose `consent_basis` is not `licensed` or `consented`, and any dataset
whose `source_kind` is `platform_traffic` (contract rule 2: no training on
customer data, ever).

## Consequences

- Custom-licence models (Jais, ALLaM, AceGPT, some gated models) need a
  recorded approval before use.
- Initial entries are `candidate` with the licence *claimed* in the brief;
  they are re-verified when the model is actually added.
