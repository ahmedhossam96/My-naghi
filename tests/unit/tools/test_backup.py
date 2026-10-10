"""Backup and recovery tests (ADR 0022). Synthetic configuration only."""

from __future__ import annotations

import base64
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.support import synthetic as syn
from tests.support.logs import LogCapture
from tools import backup as bk
from tools.backup import (
    ARCHIVE,
    MANIFEST,
    BackupService,
    Collected,
    LocalKeyProvider,
    OpenBaoTransitKeyProvider,
    Operator,
    Resource,
    Role,
    load_spec,
)
from triaxis_common import logging as tlog

REPO = Path(__file__).resolve().parents[3]
SENTINEL = "SENTINEL-BACKUP-0099"

OPERATOR = Operator("alice", frozenset({Role.BACKUP_OPERATOR}))
RESTORER = Operator("bob", frozenset({Role.RESTORE_OPERATOR}))
APPROVER = Operator("carol", frozenset({Role.RESTORE_APPROVER}))
RETENTION = Operator("dave", frozenset({Role.RETENTION_ADMIN}))
AUDITOR = Operator("erin", frozenset({Role.BACKUP_AUDITOR}))
NOBODY = Operator("mallory", frozenset())


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """A synthetic Triaxis state tree (copied config + decoys that must be excluded)."""
    root = tmp_path / "state"
    shutil.copytree(REPO / "config", root / "config")
    (root / "models").mkdir(parents=True)
    shutil.copy(REPO / "models/registry.yaml", root / "models/registry.yaml")
    (root / "deploy").mkdir()
    (root / "deploy/README.md").write_text("# deploy\n", "utf-8")
    # Decoys: transient content and secrets that must never enter a backup.
    (root / "deploy/call.wav").write_bytes(b"RIFF....WAVE" + SENTINEL.encode())
    (root / "deploy/transcript-2026-10-09.md").write_text(f"{SENTINEL} customer said", "utf-8")
    (root / "deploy/prompt-cache.md").write_text(f"{SENTINEL} prompt", "utf-8")
    (root / "deploy/.env").write_text(f"TOKEN={SENTINEL}", "utf-8")
    audit = root / "var/audit"
    audit.mkdir(parents=True)
    line = json.dumps(
        {
            "ts": "2026-10-09T10:00:00Z",
            "level": "info",
            "event": "request_completed",
            "service": "stt",
            "status": 200,
        }
    )
    (audit / "2026-10-09.jsonl").write_text(line + "\n", "utf-8")
    return root


@pytest.fixture
def spec(base: Path) -> bk.BackupSpec:
    return load_spec(base / "config/backup/backup-spec.yaml")


@pytest.fixture
def keyfile(tmp_path: Path) -> Path:
    path = tmp_path / "keys" / "kek.hex"
    path.parent.mkdir(mode=0o700)
    LocalKeyProvider.generate(path)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(tmp_path: Path, keyfile: Path, clock: Clock) -> BackupService:
    root = tmp_path / "backups"
    root.mkdir()
    return BackupService(root, LocalKeyProvider(keyfile), clock=clock)


def _decrypt(service: BackupService, backup_id: str) -> bytes:
    _, archive = service._open(backup_id)
    return archive


# --------------------------------------------------------------------- happy path


def test_backup_verify_restore_isolated(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    assert manifest.created_by == "alice"
    paths = {r.path for r in manifest.resources}
    assert "config/tenants/mynaghi.yaml" in paths
    assert "models/registry.yaml" in paths
    assert "var/audit/2026-10-09.jsonl" in paths
    # Included-but-denied files (transcript, prompt); .wav/.env never match the includes.
    assert manifest.excluded["denied_files"] == 2
    assert oct((service.root / manifest.backup_id).stat().st_mode & 0o777) == "0o500"

    assert service.verify(manifest.backup_id, AUDITOR).backup_id == manifest.backup_id

    target = tmp_path / "restore-test"
    report = service.restore(
        manifest.backup_id, target, RESTORER, protected=[base / "config", base / "models"]
    )
    assert report.result == "restored"
    assert set(report.checks.values()) == {"ok"}
    assert "tenant_brand_boundaries" in report.checks
    for rel in paths:
        assert (target / rel).read_bytes() == (base / rel).read_bytes()
    assert service.ledger(bk.VERIFICATIONS)[-1]["result"] == "verified"
    assert service.ledger(bk.RESTORE_TESTS)[-1]["result"] == "restored"


def test_restored_tenants_and_brands_match(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    from triaxis_common.config import load_tenants

    manifest = service.create(base, spec, OPERATOR)
    service.restore(manifest.backup_id, tmp_path / "r", RESTORER)
    original = load_tenants(base / "config/tenants")
    restored = load_tenants(tmp_path / "r/config/tenants")
    assert original == restored
    assert {b.id for b in restored["mynaghi"].brands} == {"mynaghi-sa", "bmw", "mini"}


def test_transient_content_and_secrets_excluded(
    service: BackupService, base: Path, spec: bk.BackupSpec, keyfile: Path
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    archive = _decrypt(service, manifest.backup_id)
    assert SENTINEL.encode() not in archive
    manifest_text = (service.root / manifest.backup_id / MANIFEST).read_text()
    assert SENTINEL not in manifest_text
    assert keyfile.read_text().strip() not in manifest_text  # no key material
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        names = tar.getnames()
    assert not any(n.endswith((".wav", ".env")) or "transcript" in n for n in names)


def test_no_secrets_or_content_in_logs(
    service: BackupService,
    base: Path,
    spec: bk.BackupSpec,
    keyfile: Path,
    tmp_path: Path,
    logs: LogCapture,
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    service.verify(manifest.backup_id, OPERATOR)
    service.restore(manifest.backup_id, tmp_path / "r", RESTORER)
    text = logs.text()
    assert keyfile.read_text().strip() not in text
    assert SENTINEL not in text
    events = [r["event"] for r in logs.records]
    assert events == [
        "backup_started",
        "backup_completed",
        "backup_verified",
        "restore_started",
        "restore_completed",
    ]
    assert all(tlog.validate_log_line(line) for line in logs.lines)


def test_idempotent_retry_with_explicit_id(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    first = service.create(base, spec, OPERATOR, backup_id="bk-20261009T120000Z-0000abcd")
    again = service.create(base, spec, OPERATOR, backup_id="bk-20261009T120000Z-0000abcd")
    assert first.mac == again.mac
    assert [p.name for p in service.root.iterdir() if p.name.startswith("bk-")] == [
        "bk-20261009T120000Z-0000abcd"
    ]


# --------------------------------------------------------------------- integrity


def test_corrupt_archive_detected(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    archive = service.root / manifest.backup_id / ARCHIVE
    (service.root / manifest.backup_id).chmod(0o700)
    archive.chmod(0o600)
    data = bytearray(archive.read_bytes())
    data[100] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(bk.IntegrityError):
        service.verify(manifest.backup_id, OPERATOR)
    with pytest.raises(bk.IntegrityError):
        service.restore(manifest.backup_id, tmp_path / "r", RESTORER)
    assert not (tmp_path / "r").exists()
    assert service.ledger(bk.VERIFICATIONS)[-1]["code"] == "integrity_check_failed"


def _rewrite_manifest(service: BackupService, backup_id: str, mutate: Any) -> None:
    path = service.root / backup_id / MANIFEST
    (service.root / backup_id).chmod(0o700)
    path.chmod(0o600)
    data = json.loads(path.read_text())
    path.write_text(mutate(data) if callable(mutate) else mutate)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: json.dumps({**d, "created_by": "mallory"}), bk.TamperedManifestError),
        (lambda d: json.dumps({**d, "resources": d["resources"][1:]}), bk.TamperedManifestError),
        (lambda d: json.dumps({**d, "mac": "0" * 64}), bk.TamperedManifestError),
        (lambda d: json.dumps({**d, "extra": 1}), bk.TamperedManifestError),
        (lambda d: json.dumps({**d, "format": "other"}), bk.TamperedManifestError),
        (lambda d: json.dumps({**d, "manifest_version": 2}), bk.IncompatibleVersionError),
        ("{not json", bk.TamperedManifestError),
        (
            lambda d: json.dumps({**d, "encryption": {**d["encryption"], "nonce_b64": "%%"}}),
            bk.TamperedManifestError,
        ),
    ],
)
def test_tampered_or_invalid_manifest(
    service: BackupService, base: Path, spec: bk.BackupSpec, mutate: Any, error: type[Exception]
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    _rewrite_manifest(service, manifest.backup_id, mutate)
    with pytest.raises(error):
        service.verify(manifest.backup_id, OPERATOR)


def test_manifest_backup_id_mismatch(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    other = service.root / "bk-20991231T000000Z-deadbeef"
    shutil.copytree(service.root / manifest.backup_id, other)
    with pytest.raises(bk.TamperedManifestError):
        service.verify(other.name, OPERATOR)


def test_missing_archive(service: BackupService, base: Path, spec: bk.BackupSpec) -> None:
    manifest = service.create(base, spec, OPERATOR)
    (service.root / manifest.backup_id).chmod(0o700)
    (service.root / manifest.backup_id / ARCHIVE).unlink()
    with pytest.raises(bk.IntegrityError):
        service.verify(manifest.backup_id, OPERATOR)


@pytest.mark.parametrize("backup_id", ["../etc", "bk-x/../../y", "nothing-here", "bk-missing"])
def test_unknown_or_unsafe_backup_id(service: BackupService, backup_id: str) -> None:
    with pytest.raises(bk.BackupNotFoundError):
        service.verify(backup_id, OPERATOR)


# --------------------------------------------------------------------- keys


def test_wrong_or_missing_key(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    other = tmp_path / "keys2" / "kek.hex"
    other.parent.mkdir(mode=0o700)
    LocalKeyProvider.generate(other)
    stranger = BackupService(service.root, LocalKeyProvider(other))
    with pytest.raises(bk.KeyUnavailableError):
        stranger.verify(manifest.backup_id, OPERATOR)
    with pytest.raises(bk.KeyUnavailableError):
        LocalKeyProvider(tmp_path / "absent.hex")


def test_key_file_checks(tmp_path: Path) -> None:
    loose = tmp_path / "loose.hex"
    loose.write_text("00" * 32)
    loose.chmod(0o644)
    with pytest.raises(bk.KeyUnavailableError, match="key_unavailable"):
        LocalKeyProvider(loose)
    short = tmp_path / "short.hex"
    short.write_text("00" * 8)
    short.chmod(0o600)
    with pytest.raises(bk.KeyUnavailableError):
        LocalKeyProvider(short)
    root = tmp_path / "backups"
    root.mkdir()
    inside = root / "kek.hex"
    LocalKeyProvider.generate(inside)
    with pytest.raises(bk.KeyUnavailableError):
        BackupService(root, LocalKeyProvider(inside))


def test_unwrap_with_tampered_wrapped_key(keyfile: Path) -> None:
    provider = LocalKeyProvider(keyfile)
    ref, wrapped = provider.wrap(b"k" * 32)
    with pytest.raises(bk.KeyUnavailableError):
        provider.unwrap(ref, wrapped[:-1] + bytes([wrapped[-1] ^ 1]))
    with pytest.raises(bk.KeyUnavailableError):
        provider.unwrap("local:0000", wrapped)


def test_openbao_transit_provider() -> None:
    store: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Vault-Token"] == "hvs-test"
        body = json.loads(request.content)
        if request.url.path.endswith("/encrypt/triaxis-backup"):
            store["ct"] = "vault:v1:" + body["plaintext"][::-1]
            return httpx.Response(200, json={"data": {"ciphertext": store["ct"]}})
        if request.url.path.endswith("/decrypt/triaxis-backup"):
            return httpx.Response(200, json={"data": {"plaintext": body["ciphertext"][9:][::-1]}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenBaoTransitKeyProvider(
        "https://openbao.control.svc:8200", "triaxis-backup", "hvs-test", client
    )
    ref, wrapped = provider.wrap(b"d" * 32)
    assert ref == "openbao-transit:triaxis-backup"
    assert provider.unwrap(ref, wrapped) == b"d" * 32
    with pytest.raises(bk.KeyUnavailableError):
        provider.unwrap("openbao-transit:other", wrapped)
    broken = OpenBaoTransitKeyProvider(
        "https://openbao.control.svc:8200",
        "triaxis-backup",
        "hvs-test",
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500))),
    )
    with pytest.raises(bk.KeyUnavailableError):
        broken.wrap(b"d" * 32)
    odd = OpenBaoTransitKeyProvider(
        "https://openbao.control.svc:8200",
        "triaxis-backup",
        "hvs-test",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"data": {"ciphertext": 7, "plaintext": "%%"}})
            )
        ),
    )
    with pytest.raises(bk.KeyUnavailableError):
        odd.wrap(b"d" * 32)
    with pytest.raises(bk.KeyUnavailableError):
        odd.unwrap("openbao-transit:triaxis-backup", b"x")
    with pytest.raises(ValueError, match="cluster"):
        OpenBaoTransitKeyProvider("https://vault.example.com", "k", "t", client)
    with pytest.raises(ValueError, match="transit key"):
        OpenBaoTransitKeyProvider("https://openbao.control.svc", "Bad Name", "t", client)


# --------------------------------------------------------------------- authorisation


def test_role_separation(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path, logs: LogCapture
) -> None:
    with pytest.raises(bk.UnauthorizedError):
        service.create(base, spec, RESTORER)
    manifest = service.create(base, spec, OPERATOR)
    with pytest.raises(bk.UnauthorizedError):
        service.verify(manifest.backup_id, NOBODY)
    with pytest.raises(bk.UnauthorizedError):
        service.restore(manifest.backup_id, tmp_path / "r", OPERATOR)
    with pytest.raises(bk.UnauthorizedError):
        service.prune(OPERATOR)
    assert "authz_denied" in [r["event"] for r in logs.records]


def test_in_place_restore_needs_distinct_approver(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    both = Operator("bob", frozenset({Role.RESTORE_OPERATOR, Role.RESTORE_APPROVER}))
    with pytest.raises(bk.UnauthorizedError):
        service.restore(manifest.backup_id, base, RESTORER, in_place=True)
    with pytest.raises(bk.UnauthorizedError):
        service.restore(manifest.backup_id, base, both, in_place=True, approver=both)
    with pytest.raises(bk.UnauthorizedError):
        service.restore(manifest.backup_id, base, RESTORER, in_place=True, approver=AUDITOR)


def test_in_place_restore_with_approval_keeps_rollback(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    live = base / "config/tenants/mynaghi.yaml"
    original = live.read_bytes()
    live.write_text(live.read_text().replace("MY NAGHI Motors", "Changed Name"))
    report = service.restore(manifest.backup_id, base, RESTORER, in_place=True, approver=APPROVER)
    assert report.in_place
    assert live.read_bytes() == original
    rollback = base / f".pre-restore-{manifest.backup_id}/config/tenants/mynaghi.yaml"
    assert "Changed Name" in rollback.read_text()


def test_restore_target_protections(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    with pytest.raises(bk.UnauthorizedError):
        service.restore(manifest.backup_id, base / "config", RESTORER, protected=[base / "config"])
    with pytest.raises(bk.UnsafeArchiveError):
        service.restore(manifest.backup_id, service.root / "inside", RESTORER)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "file").write_text("x")
    with pytest.raises(bk.UnsafeArchiveError):
        service.restore(manifest.backup_id, busy, RESTORER)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert service.restore(manifest.backup_id, empty, RESTORER).result == "restored"


def test_operator_from_claims() -> None:
    op = bk.operator_from_claims({"sub": "alice", "triaxis_roles": ["backup_operator", "x"]})
    assert op.roles == {Role.BACKUP_OPERATOR}
    for claims in (
        {"sub": "Alice Smith", "triaxis_roles": []},
        {"sub": "a", "triaxis_roles": "x"},
        {},
    ):
        with pytest.raises(bk.UnauthorizedError):
            bk.operator_from_claims(claims)


# --------------------------------------------------------------------- failure handling


def test_secret_in_source_fails_closed(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    (base / "deploy/values.yaml").write_text(f"aws_key: {syn.aws_key()}\n")
    with pytest.raises(bk.SecretDetectedError):
        service.create(base, spec, OPERATOR)
    assert list(service.root.iterdir()) == []  # no partial or complete backup left


def test_restricted_identifier_in_source_fails_closed(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    (base / "deploy/notes.md").write_text(f"customer id {syn.saudi_id()}\n")
    with pytest.raises(bk.SecretDetectedError):
        service.create(base, spec, OPERATOR)


def test_confidential_findings_recorded_not_blocking(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    (base / "deploy/contacts.md").write_text("ops on-call: oncall@example.sa\n")
    manifest = service.create(base, spec, OPERATOR)
    assert manifest.findings == {"email_address": 1}
    assert "oncall@example.sa" not in manifest.model_dump_json()


@pytest.mark.parametrize(
    ("rel", "content", "error"),
    [
        ("config/tenants/bad.yaml", "key: [unclosed", bk.IntegrityError),
        ("config/schema/bad.json", "{nope", bk.IntegrityError),
        ("var/audit/bad.jsonl", '{"event": "x", "prompt": "hi"}\n', bk.SecretDetectedError),
    ],
)
def test_invalid_source_files(
    service: BackupService,
    base: Path,
    spec: bk.BackupSpec,
    rel: str,
    content: str,
    error: type[Exception],
) -> None:
    (base / rel).write_text(content)
    with pytest.raises(error):
        service.create(base, spec, OPERATOR)


def test_binary_or_oversized_or_symlinked_sources(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    (base / "deploy/blob.cfg").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(bk.SecretDetectedError):
        service.create(base, spec, OPERATOR)
    (base / "deploy/blob.cfg").unlink()
    (base / "deploy/big.md").write_text("x" * (spec.max_file_bytes + 1))
    with pytest.raises(bk.IntegrityError):
        service.create(base, spec, OPERATOR)
    (base / "deploy/big.md").unlink()
    (base / "deploy/link.md").symlink_to(base / "deploy/README.md")
    with pytest.raises(bk.UnsafeArchiveError):
        service.create(base, spec, OPERATOR)


def test_missing_required_source_and_escape(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    shutil.rmtree(base / "config/policies")
    with pytest.raises(bk.IntegrityError):
        service.create(base, spec, OPERATOR)
    escaping = spec.model_copy(update={"sources": (bk.Source(id="x", path="../outside"),)})
    with pytest.raises(bk.UnsafeArchiveError):
        service.create(base, escaping, OPERATOR)


def test_destination_unavailable(
    tmp_path: Path, keyfile: Path, base: Path, spec: bk.BackupSpec
) -> None:
    svc = BackupService(tmp_path / "missing-root", LocalKeyProvider(keyfile))
    with pytest.raises(bk.DestinationUnavailableError):
        svc.create(base, spec, OPERATOR)


def test_interrupted_backup_leaves_nothing(
    service: BackupService, base: Path, spec: bk.BackupSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    original = BackupService._write

    def flaky(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        original(path, data)

    monkeypatch.setattr(BackupService, "_write", staticmethod(flaky))
    with pytest.raises(bk.DestinationUnavailableError):
        service.create(base, spec, OPERATOR)
    assert list(service.root.iterdir()) == []


def test_restore_validation_failure_cleans_up(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    tenant = base / "config/tenants/mynaghi.yaml"
    data = yaml.safe_load(tenant.read_text())
    data["data_policy"] = "ghost-policy"  # incompatible: references a missing policy
    tenant.write_text(yaml.safe_dump(data))
    manifest = service.create(base, spec, OPERATOR)
    target = tmp_path / "r"
    with pytest.raises(bk.RestoreValidationError, match="tenant_policy_references"):
        service.restore(manifest.backup_id, target, RESTORER)
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".restore-")]
    assert service.ledger(bk.RESTORE_TESTS)[-1]["result"] == "failed"


def test_incompatible_config_version_rejected_on_restore(
    service: BackupService, base: Path, spec: bk.BackupSpec, tmp_path: Path
) -> None:
    tenant = base / "config/tenants/mynaghi.yaml"
    data = yaml.safe_load(tenant.read_text())
    data["schema_version"] = 2
    tenant.write_text(yaml.safe_dump(data))
    manifest = service.create(base, spec, OPERATOR)
    with pytest.raises(bk.RestoreValidationError, match="tenants"):
        service.restore(manifest.backup_id, tmp_path / "r", RESTORER)


def test_validate_restored_reports_each_check(tmp_path: Path) -> None:
    checks = bk.validate_restored(tmp_path)  # nothing there
    assert checks["tenants"] == "failed"
    assert checks["tenant_brand_boundaries"] == "failed"


def test_unsafe_archive_members_rejected(
    service: BackupService,
    base: Path,
    spec: bk.BackupSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"x: 1\n"
    evil = Resource(
        source="tenant-config", path="../escape.yaml", sha256=bk._sha256(data), size=len(data)
    )
    monkeypatch.setattr(bk, "collect", lambda *_a: Collected(files=[(evil, data)]))
    manifest = service.create(base, spec, OPERATOR)
    with pytest.raises(bk.UnsafeArchiveError):
        service.restore(manifest.backup_id, tmp_path / "r", RESTORER)
    assert not (tmp_path / "escape.yaml").exists()


def _tar_with(member: tarfile.TarInfo, payload: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(member, io.BytesIO(payload) if payload else None)
    return buf.getvalue()


def test_members_reject_links_and_unlisted(
    service: BackupService, base: Path, spec: bk.BackupSpec
) -> None:
    manifest = service.create(base, spec, OPERATOR)
    link = tarfile.TarInfo("config/tenants/link.yaml")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(bk.UnsafeArchiveError):
        service._members(manifest, _tar_with(link))
    unlisted = tarfile.TarInfo("config/tenants/new.yaml")
    unlisted.size = 2
    with pytest.raises(bk.UnsafeArchiveError):
        service._members(manifest, _tar_with(unlisted, b"x\n"))
    first = manifest.resources[0]
    wrong = tarfile.TarInfo(first.path)
    wrong.size = first.size
    with pytest.raises(bk.IntegrityError):
        service._members(manifest, _tar_with(wrong, b"z" * first.size))
    partial = tarfile.TarInfo(first.path)
    partial.size = first.size
    data = (service.root.parent / "state" / first.path).read_bytes()
    with pytest.raises(bk.IntegrityError, match="incomplete"):
        service._members(manifest, _tar_with(partial, data))


# --------------------------------------------------------------------- retention


def test_retention_expiry_and_partial_cleanup(
    service: BackupService, base: Path, spec: bk.BackupSpec, clock: Clock
) -> None:
    short = spec.model_copy(update={"retention_days": 1})
    old = service.create(base, short, OPERATOR)
    clock.now += timedelta(hours=1)
    keep = service.create(base, spec, OPERATOR)
    (service.root / ".partial-bk-20261009T000000Z-00000000").mkdir()
    (service.root / "bk-20261001T000000Z-ffffffff").mkdir()  # unreadable: never auto-deleted
    clock.now += timedelta(days=2)
    assert service.prune(RETENTION) == [old.backup_id]
    names = {p.name for p in service.root.iterdir()}
    assert keep.backup_id in names
    assert old.backup_id not in names
    assert "bk-20261001T000000Z-ffffffff" in names
    assert not any(n.startswith(".partial-") for n in names)
    listing = {row["backup_id"]: row for row in service.list_backups()}
    assert listing["bk-20261001T000000Z-ffffffff"]["state"] == "backup_not_found"


def test_list_without_root(tmp_path: Path, keyfile: Path) -> None:
    assert BackupService(tmp_path / "none", LocalKeyProvider(keyfile)).list_backups() == []
    assert BackupService(tmp_path / "none", LocalKeyProvider(keyfile)).ledger("x") == []


def test_ledger_skips_corrupt_lines(service: BackupService) -> None:
    (service.root / bk.VERIFICATIONS).write_text('{"ok": 1}\nnot json\n')
    assert service.ledger(bk.VERIFICATIONS) == [{"ok": 1}]


# --------------------------------------------------------------------- CLI


def test_cli_end_to_end(tmp_path: Path, base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    keys = tmp_path / "idp"
    root = tmp_path / "backups"
    root.mkdir()
    kek = tmp_path / "kek" / "kek.hex"
    kek.parent.mkdir(mode=0o700)
    assert bk.main(["gen-key", "--key-file", str(kek)]) == 0
    assert bk.main(["dev-keys", "--out", str(keys)]) == 0
    tokens: dict[str, Path] = {}
    for sub, roles in [
        ("alice", "backup_operator"),
        ("bob", "restore_operator"),
        ("carol", "restore_approver"),
        ("dave", "retention_admin"),
        ("mallory", ""),
    ]:
        capsys.readouterr()
        assert bk.main(["dev-token", "--keys", str(keys), "--sub", sub, "--roles", roles]) == 0
        tokens[sub] = tmp_path / f"{sub}.jwt"
        tokens[sub].write_text(capsys.readouterr().out.strip())
    common = [
        "--root",
        str(root),
        "--key-file",
        str(kek),
        "--jwks",
        str(keys / "jwks.json"),
        "--base",
        str(base),
        "--spec",
        str(base / "config/backup/backup-spec.yaml"),
    ]

    capsys.readouterr()
    assert bk.main(["create", *common, "--operator-token", str(tokens["alice"])]) == 0
    backup_id = json.loads(capsys.readouterr().out)["backup_id"]
    assert (
        bk.main(
            ["verify", *common, "--backup-id", backup_id, "--operator-token", str(tokens["alice"])]
        )
        == 0
    )
    assert (
        bk.main(
            [
                "restore",
                *common,
                "--backup-id",
                backup_id,
                "--target",
                str(tmp_path / "drill"),
                "--operator-token",
                str(tokens["bob"]),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert bk.main(["status", "--root", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["backups"][0]["backup_id"] == backup_id
    assert status["restore_tests"][-1]["result"] == "restored"
    assert bk.main(["prune", *common, "--operator-token", str(tokens["dave"])]) == 0
    # Unauthorised and invalid tokens are rejected with exit code 2.
    assert bk.main(["create", *common, "--operator-token", str(tokens["mallory"])]) == 2
    (tmp_path / "junk.jwt").write_text("not-a-token")
    assert bk.main(["create", *common, "--operator-token", str(tmp_path / "junk.jwt")]) == 2
    assert bk.main(["create", *common]) == 2
    assert bk.main(["list"]) == 2
    in_place = [
        "restore",
        *common,
        "--backup-id",
        backup_id,
        "--target",
        str(base),
        "--in-place",
        "--operator-token",
        str(tokens["bob"]),
        "--approver-token",
        str(tokens["carol"]),
    ]
    assert bk.main(in_place) == 0


def test_cli_rejects_long_lived_token(tmp_path: Path, base: Path) -> None:
    import time

    import jwt

    keys = tmp_path / "idp"
    bk._dev_keys(keys)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "triaxis-backup-idp",
            "aud": "triaxis-backup",
            "sub": "alice",
            "iat": now,
            "exp": now + 7200,
            "triaxis_roles": ["backup_operator"],
        },
        (keys / "dev-idp.key").read_bytes(),
        algorithm="ES256",
        headers={"kid": "dev-backup-idp"},
    )
    (tmp_path / "t.jwt").write_text(token)
    kek = tmp_path / "k" / "kek.hex"
    kek.parent.mkdir(mode=0o700)
    LocalKeyProvider.generate(kek)
    (tmp_path / "b").mkdir()
    assert (
        bk.main(
            [
                "create",
                "--root",
                str(tmp_path / "b"),
                "--key-file",
                str(kek),
                "--jwks",
                str(keys / "jwks.json"),
                "--operator-token",
                str(tmp_path / "t.jwt"),
                "--base",
                str(base),
                "--spec",
                str(base / "config/backup/backup-spec.yaml"),
            ]
        )
        == 2
    )


def test_no_key_listing_provider() -> None:
    nk = bk.NoKeys()
    with pytest.raises(bk.KeyUnavailableError):
        nk.wrap(b"x")
    with pytest.raises(bk.KeyUnavailableError):
        nk.unwrap("r", b"x")


def test_repository_spec_scope() -> None:
    spec = load_spec(REPO / "config/backup/backup-spec.yaml")
    ids = {s.id for s in spec.sources}
    assert {"tenant-config", "data-policies", "model-registry", "audit-metadata"} <= ids
    assert base64  # imported for symmetry with other tests
