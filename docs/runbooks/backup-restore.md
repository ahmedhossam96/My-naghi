# Runbook: backup, verification and restore

Applies to `tools/backup.py` (ADR 0022). Production uses OpenBao Transit for
key wrapping and Keycloak-issued operator tokens; the commands below show the
local drill set-up, which uses a local key file and a dev identity provider.
**Never** use the dev identity provider or a local key file in production.

## 0. Prerequisites

- Operator tokens from the identity provider carrying `triaxis_roles`.
- Key-encryption key: OpenBao Transit key `triaxis-backup` (prod) or a local
  0600 key file **outside** the backup root (drill).
- Backup root on the approved target storage (write-protected; WORM in prod).

Local drill set-up:

```bash
export ROOT=/srv/triaxis-backups DRILL=$HOME/triaxis-drill
mkdir -p "$ROOT" && mkdir -m 700 "$DRILL"
uv run python -m tools.backup gen-key  --key-file "$DRILL/kek.hex"
uv run python -m tools.backup dev-keys --out "$DRILL/idp"
uv run python -m tools.backup dev-token --keys "$DRILL/idp" --sub alice --roles backup_operator  > "$DRILL/alice.jwt"
uv run python -m tools.backup dev-token --keys "$DRILL/idp" --sub bob   --roles restore_operator > "$DRILL/bob.jwt"
COMMON="--root $ROOT --key-file $DRILL/kek.hex --jwks $DRILL/idp/jwks.json"
```

## 1. Create a backup (role `backup_operator`)

```bash
uv run python -m tools.backup create $COMMON --operator-token "$DRILL/alice.jwt"
```

Expected: JSON with `backup_id`, file count and the excluded categories.
Exit code 2 with a safe code on failure:

| Code | Meaning | Action |
|---|---|---|
| `sensitive_content_detected` | A credential or restricted identifier was found in an included file, or a non-text file | Remove the secret from the source (secrets belong in OpenBao), re-run |
| `integrity_check_failed` | Invalid YAML/JSON, missing required source, oversize file | Fix the source |
| `destination_unavailable` | Root missing/not writable, disk error | Check storage; the job left no partial backup |
| `unsafe_archive` | Symlink or path outside the base | Remove the link |
| `unauthorized` | Token missing, invalid, too long-lived (> 1 h) or lacks the role | Obtain the right token |

Retries are safe. Passing the same `--backup-id` again returns the existing
completed backup.

## 2. Verify (roles `backup_operator`, `backup_auditor` or `restore_operator`)

```bash
uv run python -m tools.backup verify $COMMON --backup-id <id> --operator-token "$DRILL/alice.jwt"
```

Verification checks the manifest MAC, the archive hash, decryption and every
file hash. `manifest_tampered`, `integrity_check_failed` and `key_unavailable`
mean **do not restore**: escalate to the SOC as a potential integrity incident.

## 3. Restore test into an isolated target (role `restore_operator`)

```bash
uv run python -m tools.backup restore $COMMON --backup-id <id> \
  --target /srv/restore-tests/<id> --operator-token "$DRILL/bob.jwt"
```

The target must be empty and must not overlap live `config/` or `models/`.
The report lists the checks (`tenants`, `policies`, `hardware_profiles`,
`model_registry`, `licence_gate`, `tenant_policy_references`,
`tenant_brand_boundaries`); all must be `ok`. On failure nothing is left behind.

## 4. In-place restore (production change, two people)

Requires a change ticket, a `restore_operator` and a **different**
`restore_approver`:

```bash
uv run python -m tools.backup restore $COMMON --backup-id <id> --target <live-base> \
  --in-place --operator-token bob.jwt --approver-token carol.jwt
```

Previous files are kept under `<live-base>/.pre-restore-<id>/` for rollback.
After restoring: run `uv run pytest`, the licence gate and `check-tenants`;
redeploy through the normal signed pipeline.

## 5. Retention (role `retention_admin`)

```bash
uv run python -m tools.backup prune $COMMON --operator-token dave.jwt
```

## 6. Status

```bash
uv run python -m tools.backup status --root "$ROOT"
```

Shows the unverified listing plus the last verification and restore-test
results. The demo shows the same data when started with `TRIAXIS_BACKUP_DIR`.

## 7. What a backup never contains

Prompts, completions, audio, transcripts, plaintext secrets, model weights.
If anyone asks for customer conversation data "from the backups", the answer is
that Triaxis does not retain it (contract rule 1).
