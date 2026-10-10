# Backup scope, retention and recovery design

Status: v1 (2026-10-09). Implements ADR 0022. Tooling: `tools/backup.py`.
Operating procedure: `docs/runbooks/backup-restore.md`.

## 1. State inventory

| State | Owner | Persistent? | In Triaxis backup? | Recovery method |
|---|---|---|---|---|
| Tenant and brand configuration (`config/tenants`) | Triaxis | Yes (Git + deploy) | **Yes** | Backup restore or Git |
| Data-handling policies (`config/policies`) | Triaxis | Yes | **Yes** | Backup restore or Git |
| Hardware profiles, JSON Schemas, backup spec | Triaxis | Yes | **Yes** | Backup restore or Git |
| Model registry metadata and provenance (`models/registry.yaml`) | Triaxis | Yes | **Yes** | Backup restore or Git |
| Deployment definitions (`deploy/`) | Triaxis | Yes | **Yes** (text formats) | Backup restore or Git |
| Metadata-only audit export (`var/audit/*.jsonl`) | Triaxis | When retention is authorised | **Yes**, if present; each line must pass the allow-list schema | Backup restore |
| Model weights | Triaxis (artefacts) | Yes | **No** | Re-fetch by sha256 from the offline write-once vault / Harbor; registry holds hashes |
| Secrets, PKI, signing keys | OpenBao + HSM | Yes | **No** (never as plaintext) | OpenBao raft snapshot procedure (HSM-sealed), separate runbook (M6/M8) |
| Prompts, completions, audio, transcripts | Customer | **No** (zero retention) | **Never** | Not recoverable by design |
| Business records the tenant asks to retain | MY NAGHI applications | Tenant's systems | **No** | Tenant's backup; Triaxis stores none (ADR 0015) |
| Keycloak realm, Harbor, Forgejo, observability data | Control cluster | Yes | Not yet | Product-native backups (M6/M8) |

Backups never contain customer content: the runtime has no store of it, the
collector excludes denied types, and any restricted identifier or credential in
an included file fails the backup.

## 2. Format and integrity

```
<root>/<backup-id>/            0500 (write-protected)
  manifest.json                0400  operational metadata + HMAC
  archive.tar.enc              0400  AES-256-GCM(tar)
<root>/_verifications.jsonl          verification results (metadata)
<root>/_restore-tests.jsonl          restore results (metadata)
```

- Per-backup random 256-bit data key; GCM nonce 96-bit; AAD = backup id.
- Data key wrapped by the key provider: OpenBao Transit (`openbao-transit:<key>`)
  in production; local KEK file (`local:<fingerprint>`) for drills and tests.
- Manifest MAC: HMAC-SHA256, key = HKDF(data key, "triaxis-backup-manifest-mac").
- Manifest contents: backup id, timestamps, creator, spec and Triaxis versions,
  resource list (path, sha256, size), excluded categories, finding counts
  (confidential tags only), archive hash and size, key reference and wrapped key,
  nonce. No content, no key material.

## 3. Retention

- `retention_days` in the spec (default 35); `expires_at` written per backup.
- `prune` (role `retention_admin`) deletes expired backups and leftovers of
  interrupted jobs; unreadable backups are reported, never auto-deleted.
- Legal hold and WORM retention are storage properties (object lock) to be
  configured on the production target (M8). Until then: write-protected only.

## 4. Roles

| Role | Can |
|---|---|
| `backup_operator` | create, verify |
| `backup_auditor` | verify, list |
| `retention_admin` | prune |
| `restore_operator` | restore into an isolated target, verify |
| `restore_approver` | approve an in-place restore (must differ from the operator) |

## 5. Recovery objectives (targets, not measurements)

| Item | Target | Status |
|---|---|---|
| Configuration RPO | ≤ 24 h (daily backup) | Target; scheduler not deployed |
| Configuration restore to isolated target | ≤ 15 min | Target; local drill completes in seconds on synthetic data |
| Site rebuild (with model re-fetch) | ≤ 4 h | Target; not exercised |

## 6. Not yet implemented (blockers for "DR complete")

1. Scheduled execution (Kubernetes CronJob) and alerting on backup age.
2. Replication to Site B and the offline vault; WORM/object-lock target.
3. OpenBao raft snapshot procedure and HSM unseal drill.
4. Control-cluster product backups (Keycloak, Harbor, Forgejo).
5. Timed recovery drills with measured RPO/RTO in staging.
