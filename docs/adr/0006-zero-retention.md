# 0006 — Zero-retention architecture

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Contract rule 1: audio, text, transcripts and completions exist in process
memory for the duration of a request only. They must never reach disk, logs,
traces, metrics or error messages.

## Decision

Controls, from code outwards:

1. **Code**
   - Content is held in in-memory buffers (`bytes`, `io.BytesIO`). The
     `tempfile`, `pickle`, `marshal`, `shelve` and `dill` modules are banned in
     service code by a ruff `banned-api` rule.
   - `triaxis_common.runtime.harden_process()` runs at start-up: sets
     `RLIMIT_CORE=0`, clears the process "dumpable" flag on Linux, points
     `TMPDIR` and `tempfile.tempdir` at a tmpfs and **refuses to start** in
     `staging`/`prod` if that path is not a tmpfs.
   - Logging, tracing and metrics are allow-list only (ADR 0007).
   - Error messages come from a fixed catalogue (ADR 0016).
2. **Containers**: read-only root filesystem, a tmpfs (`medium: Memory`) for
   `TMPDIR`, `ulimit -c 0`, no shell. No persistent volumes on inference pods.
3. **Queue**: NATS JetStream streams use `storage: memory`, a short
   `max_age`, and TLS. The JetStream store directory must not exist; the
   zero-retention suite asserts this.
4. **Hosts**: swap disabled, core dumps disabled system-wide
   (`kernel.core_pattern=|/bin/false`), encrypted disks (LUKS2 + Clevis/Tang +
   TPM2). Enforced by Ansible (Milestone 6) and documented as a host requirement.
5. **Proof**: the zero-retention test suite (Milestone 3 onward) sends tagged
   synthetic content through every endpoint and asserts the tag appears in no
   file, log, trace, metric or queue store, locally and against staging.

## Consequences

- No request content can be used for debugging after the fact. Operators get
  request IDs, timings, sizes and error classes only. This is deliberate.
- Libraries that spill to disk (some audio decoders, `SpooledTemporaryFile` in
  multipart parsing) must be configured or replaced. Starlette's multipart
  parser spools to a temp file above 1 MiB; services that accept multipart
  uploads must keep `TMPDIR` on tmpfs (enforced above) and cap upload size.
