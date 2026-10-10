# 0022 — Backup and recovery

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Triaxis promises zero retention of customer content. Backups must not become a
side channel that retains what the runtime discards, must never hold plaintext
secrets, and must be verifiable and restorable under authorisation, with
tamper detection.

## Decision

- **Scope** (`config/backup/backup-spec.yaml`, `docs/backup-recovery.md`):
  tenant configuration, data-handling policies, hardware profiles, generated
  schemas, backup spec, model-registry metadata, deployment definitions, and the
  optional metadata-only audit export (validated line by line against the
  allow-list schema). Excluded by design: prompts, completions, audio,
  transcripts; plaintext secrets (OpenBao raft snapshots are a separate,
  HSM-protected procedure); model weights (recovered by sha256 from the offline
  write-once vault / Harbor).
- **Fail-closed collection**: denied file types (audio, transcripts, prompts,
  keys, env files, weight formats) are excluded and counted; any credential or
  restricted identifier detected in an included file fails the backup.
- **Format**: deterministic tar in memory → AES-256-GCM with a random per-backup
  data key (AAD = backup id) → data key wrapped by a `KeyProvider`
  (`OpenBaoTransitKeyProvider` in production; `LocalKeyProvider` with a 0600
  key file outside the backup root for drills/tests). The manifest holds
  operational metadata only and is authenticated with HMAC-SHA256 using a key
  derived from the data key.
- **Writes**: to `.partial-<id>` then atomic rename; files `0400`, directory
  `0500` (write-protected). This is **not** immutability; object-lock/WORM is a
  deployment property of the target storage (Milestone 8).
- **Authorisation and separation of duties**: operator identity from a signed
  token (`triaxis_roles`); `backup_operator` creates, `backup_auditor` verifies,
  `retention_admin` prunes, `restore_operator` restores to an isolated target;
  in-place restore also requires a `restore_approver` who is a different person.
- **Restore**: verify MAC, archive hash, decryption and every file hash; extract
  only regular files listed in the manifest (no traversal, links, devices,
  duplicates, denied types); target must be empty and not overlap live config
  unless in-place approval is given (previous files kept under
  `.pre-restore-<id>`); validate tenants, policies, hardware profiles, registry,
  licence gate, tenant→policy references and tenant/brand boundaries; produce a
  metadata-only report. Results append to `_verifications.jsonl` /
  `_restore-tests.jsonl` for status views.
- **Retention**: `expires_at` per backup (`retention_days`); `prune` removes
  expired backups and leftovers of interrupted jobs; unreadable backups are
  never auto-deleted.

## Consequences

- Disaster recovery is not complete: scheduled execution (CronJob), off-site
  replication to the second site and the offline vault, WORM storage, OpenBao
  snapshot integration and timed recovery drills (RPO/RTO measurement) are
  Milestone 6/8 work. See `docs/runbooks/backup-restore.md`.
