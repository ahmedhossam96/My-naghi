"""Triaxis backup and recovery (ADR 0022).

Scope: state that Triaxis owns and is allowed to keep — configuration
(tenants, data-handling policies, hardware profiles, schemas, backup spec),
model-registry metadata, deployment definitions and, optionally, the
metadata-only audit export. Never: prompts, completions, audio, transcripts
(zero retention), plaintext secrets (OpenBao has its own snapshot procedure)
or model weights (recovered by hash from the offline vault / Harbor).

    python -m tools.backup create  --root DIR --key-file KEK --operator-token T --jwks J
    python -m tools.backup verify  --root DIR --key-file KEK --backup-id ID ...
    python -m tools.backup list    --root DIR
    python -m tools.backup restore --root DIR --key-file KEK --backup-id ID --target DIR ...
    python -m tools.backup prune   --root DIR ...
    python -m tools.backup dev-keys --out DIR          # local test identity provider
    python -m tools.backup dev-token --keys DIR --sub alice --roles backup_operator

Security properties:

- Envelope encryption: a random 256-bit data key per backup (AES-256-GCM);
  the data key is wrapped by a :class:`KeyProvider` (OpenBao Transit in
  production, a local key-encryption key file for tests). Keys never sit next
  to backups in plaintext.
- The manifest is authenticated (HMAC-SHA256 with a key derived from the data
  key), so tampering is detected before anything is restored.
- Every file is scanned before inclusion; credentials or restricted
  identifiers fail the backup (fail closed). Denied file types (audio,
  transcripts, keys, weights, env files) are excluded and counted.
- Separation of duties: ``backup_operator`` creates, ``retention_admin``
  prunes, ``restore_operator`` restores into an isolated target; an in-place
  restore also needs a ``restore_approver`` who is a different person.
- Restores verify everything first, extract only regular files listed in the
  manifest (no traversal, links or devices), validate schemas and policies,
  and produce a metadata-only report.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import stat
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal, Protocol

import httpx
import yaml
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.check_no_weights import DENIED_SUFFIXES
from triaxis_common.classification import detect, normalise
from triaxis_common.config import require_in_cluster_url
from triaxis_common.logging import AllowListLogger, Event, Level, set_sink, validate_log_line
from triaxis_common.taxonomy import DataTag
from triaxis_common.types import Slug, is_slug

MANIFEST_VERSION: Final = 1
FORMAT: Final = "triaxis-backup"
MANIFEST: Final = "manifest.json"
ARCHIVE: Final = "archive.tar.enc"
VERIFICATIONS: Final = "_verifications.jsonl"
RESTORE_TESTS: Final = "_restore-tests.jsonl"
DEFAULT_SPEC: Final = Path("config/backup/backup-spec.yaml")
MAX_ARCHIVE_BYTES: Final = 256 * 1024 * 1024
BLOCKING_TAGS: Final = frozenset(
    {DataTag.CREDENTIAL, DataTag.NATIONAL_ID, DataTag.PAYMENT_CARD, DataTag.IBAN}
)
ALWAYS_DENIED: Final = (
    "*.wav",
    "*.mp3",
    "*.flac",
    "*.ogg",
    "*.opus",
    "*.webm",
    "*.m4a",
    "*transcript*",
    "*prompt*",
    "*completion*",
    "*.pem",
    "*.key",
    "*.p12",
    ".env",
    ".env.*",
    *(f"*{suffix}" for suffix in DENIED_SUFFIXES),
)
_LOG: Final = AllowListLogger("backup")


# --------------------------------------------------------------------- errors


class BackupError(Exception):
    """Base error. ``code`` is a safe identifier; messages never carry content."""

    code = "backup_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}{': ' + detail if detail else ''}")


class UnauthorizedError(BackupError):
    code = "unauthorized"


class DestinationUnavailableError(BackupError):
    code = "destination_unavailable"


class SecretDetectedError(BackupError):
    code = "sensitive_content_detected"


class IntegrityError(BackupError):
    code = "integrity_check_failed"


class TamperedManifestError(BackupError):
    code = "manifest_tampered"


class KeyUnavailableError(BackupError):
    code = "key_unavailable"


class IncompatibleVersionError(BackupError):
    code = "incompatible_version"


class UnsafeArchiveError(BackupError):
    code = "unsafe_archive"


class RestoreValidationError(BackupError):
    code = "restore_validation_failed"


class BackupNotFoundError(BackupError):
    code = "backup_not_found"


# --------------------------------------------------------------------- authorisation


class Role(StrEnum):
    BACKUP_OPERATOR = "backup_operator"
    BACKUP_AUDITOR = "backup_auditor"
    RETENTION_ADMIN = "retention_admin"
    RESTORE_OPERATOR = "restore_operator"
    RESTORE_APPROVER = "restore_approver"


@dataclass(frozen=True, slots=True)
class Operator:
    subject: str
    roles: frozenset[Role]

    def require(self, role: Role) -> None:
        if role not in self.roles:
            _LOG.log(Event.AUTHZ_DENIED, Level.WARNING, operator=self.subject)
            raise UnauthorizedError(role.value)


def operator_from_claims(claims: dict[str, Any]) -> Operator:
    subject = claims.get("sub")
    roles = claims.get("triaxis_roles", [])
    if not isinstance(subject, str) or not is_slug(subject) or not isinstance(roles, list):
        raise UnauthorizedError("invalid operator identity")
    valid = {r.value for r in Role}
    return Operator(subject, frozenset(Role(r) for r in roles if isinstance(r, str) and r in valid))


# --------------------------------------------------------------------- key providers


class KeyProvider(Protocol):
    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        """Return (key reference, wrapped data key). Never the key itself."""
        ...

    def unwrap(self, key_ref: str, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    """Key-encryption key from a local file (tests, isolated recovery drills).

    The file must be readable by its owner only and must not live inside the
    backup root.
    """

    def __init__(self, key_file: Path) -> None:
        try:
            mode = key_file.stat().st_mode
            raw = bytes.fromhex(key_file.read_text("ascii").strip())
        except (OSError, ValueError):
            raise KeyUnavailableError("key file unreadable") from None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyUnavailableError("key file permissions too open")
        if len(raw) != 32:
            raise KeyUnavailableError("key must be 256 bits")
        self._kek = raw
        self._ref = "local:" + hashlib.sha256(raw).hexdigest()[:16]
        self.path = key_file.resolve()

    @staticmethod
    def generate(key_file: Path) -> None:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secrets.token_bytes(32).hex())

    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        return self._ref, aes_key_wrap(self._kek, data_key)

    def unwrap(self, key_ref: str, wrapped: bytes) -> bytes:
        if not hmac.compare_digest(key_ref, self._ref):
            raise KeyUnavailableError("backup was wrapped with a different key")
        try:
            return aes_key_unwrap(self._kek, wrapped)
        except InvalidUnwrap:
            raise KeyUnavailableError("unwrap failed") from None


class OpenBaoTransitKeyProvider:
    """Wraps data keys with OpenBao's Transit engine (key material stays in OpenBao/HSM)."""

    def __init__(self, address: str, key_name: str, token: str, client: httpx.Client) -> None:
        self._address = require_in_cluster_url(address, allow_http=False).rstrip("/")
        if not is_slug(key_name):
            raise ValueError("invalid transit key name")
        self._key = key_name
        self._token = token
        self._client = client

    def _post(self, op: str, body: dict[str, str]) -> dict[str, Any]:
        try:
            resp = self._client.post(
                f"{self._address}/v1/transit/{op}/{self._key}",
                json=body,
                headers={"X-Vault-Token": self._token},
                timeout=5.0,
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            raise KeyUnavailableError("transit unavailable") from None
        return data

    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        data = self._post("encrypt", {"plaintext": base64.b64encode(data_key).decode()})
        ciphertext = data.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise KeyUnavailableError("transit response invalid")
        return f"openbao-transit:{self._key}", ciphertext.encode()

    def unwrap(self, key_ref: str, wrapped: bytes) -> bytes:
        if key_ref != f"openbao-transit:{self._key}":
            raise KeyUnavailableError("backup was wrapped with a different key")
        data = self._post("decrypt", {"ciphertext": wrapped.decode()})
        try:
            return base64.b64decode(data["plaintext"], validate=True)
        except (KeyError, ValueError, TypeError):
            raise KeyUnavailableError("transit response invalid") from None


# --------------------------------------------------------------------- spec


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Slug
    path: str
    include: tuple[str, ...] = ("*",)
    validate_as: Literal["yaml", "json", "audit_jsonl", "text"] = "text"
    optional: bool = False


class BackupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    spec_version: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]{1,3}$")]
    sources: tuple[Source, ...]
    deny: tuple[str, ...] = ()
    retention_days: Annotated[int, Field(ge=1, le=3650)] = 35
    max_file_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)] = 5 * 1024 * 1024


def load_spec(path: Path) -> BackupSpec:
    with path.open("rb") as fh:
        return BackupSpec.model_validate(yaml.safe_load(fh))


# --------------------------------------------------------------------- manifest


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Slug
    path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: int


class Manifest(BaseModel):
    """Operational metadata only: no content, no key material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["triaxis-backup"] = FORMAT
    manifest_version: int = MANIFEST_VERSION
    backup_id: Annotated[str, Field(pattern=r"^bk-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")]
    created_at: datetime
    expires_at: datetime
    created_by: Slug
    spec_version: str
    triaxis_version: str
    resources: tuple[Resource, ...]
    excluded: dict[str, Any]
    findings: dict[str, int]
    archive: dict[str, Any]
    encryption: dict[str, str]
    result: Literal["completed"] = "completed"
    mac: str = ""

    def signable(self) -> bytes:
        body = self.model_dump(mode="json", exclude={"mac"})
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _mac_key(data_key: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"triaxis-backup-manifest-mac"
    ).derive(data_key)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_backup_id(now: datetime) -> str:
    return f"bk-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------- collection


@dataclass
class Collected:
    files: list[tuple[Resource, bytes]] = field(default_factory=list)
    denied: int = 0
    findings: dict[str, int] = field(default_factory=dict)


def _denied(rel: str, patterns: Iterable[str]) -> bool:
    name = PurePosixPath(rel).name.lower()
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel.lower(), p) for p in patterns)


def _validate_file(source: Source, rel: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretDetectedError(f"{source.id}: non-text file") from None
    try:
        if source.validate_as == "yaml":
            yaml.safe_load(text)
        elif source.validate_as == "json":
            json.loads(text)
        elif source.validate_as == "audit_jsonl":
            for line in text.splitlines():
                if line.strip() and not validate_log_line(line):
                    raise SecretDetectedError(f"{source.id}: audit line outside allow-list")
    except (yaml.YAMLError, json.JSONDecodeError):
        raise IntegrityError(f"{source.id}: invalid {source.validate_as}") from None
    del rel


def _scan(data: bytes, collected: Collected, source_id: str) -> None:
    for det in detect(normalise(data.decode("utf-8"))):
        if det.tag in BLOCKING_TAGS:
            raise SecretDetectedError(f"{source_id}: {det.tag.value}")
        collected.findings[det.tag.value] = collected.findings.get(det.tag.value, 0) + 1


def collect(base: Path, spec: BackupSpec) -> Collected:
    collected = Collected()
    deny = (*ALWAYS_DENIED, *spec.deny)
    base = base.resolve()
    for source in spec.sources:
        root = (base / source.path).resolve()
        if not root.is_relative_to(base):
            raise UnsafeArchiveError(f"{source.id}: source outside base")
        if not root.exists():
            if source.optional:
                continue
            raise IntegrityError(f"{source.id}: source missing")
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise UnsafeArchiveError(f"{source.id}: symlinks are not backed up")
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            within = path.relative_to(root).as_posix() if root.is_dir() else path.name
            if not any(fnmatch.fnmatch(within, pat) for pat in source.include):
                continue
            if _denied(rel, deny):
                collected.denied += 1
                continue
            if path.stat().st_size > spec.max_file_bytes:
                raise IntegrityError(f"{source.id}: file exceeds size limit")
            data = path.read_bytes()
            _validate_file(source, rel, data)
            _scan(data, collected, source.id)
            collected.files.append(
                (Resource(source=source.id, path=rel, sha256=_sha256(data), size=len(data)), data)
            )
    return collected


def _tar(files: list[tuple[Resource, bytes]], mtime: float) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for resource, data in files:
            info = tarfile.TarInfo(resource.path)
            info.size = len(data)
            info.mode = 0o640
            info.mtime = int(mtime)
            info.uid = info.gid = 0
            info.uname = info.gname = "triaxis"
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# --------------------------------------------------------------------- service


@dataclass(frozen=True, slots=True)
class RestoreReport:
    backup_id: str
    target: str
    in_place: bool
    files: int
    checks: dict[str, str]
    result: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "target": self.target,
            "in_place": self.in_place,
            "files": self.files,
            "checks": self.checks,
            "result": self.result,
        }


class BackupService:
    def __init__(
        self,
        root: Path,
        keys: KeyProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        triaxis_version: str = "0.1.0",
    ) -> None:
        self.root = root
        self._keys = keys
        self._clock = clock
        self._version = triaxis_version
        key_path = getattr(keys, "path", None)
        if isinstance(key_path, Path) and key_path.is_relative_to(root.resolve()):
            raise KeyUnavailableError("key file must not be stored inside the backup root")

    # ----------------------------------------------------------------- create

    def create(
        self, base: Path, spec: BackupSpec, operator: Operator, backup_id: str | None = None
    ) -> Manifest:
        operator.require(Role.BACKUP_OPERATOR)
        if backup_id is not None and (self.root / backup_id / MANIFEST).is_file():
            return self.read_manifest(backup_id)  # idempotent retry of a completed job
        if not self.root.is_dir() or not os.access(self.root, os.W_OK):
            _LOG.log(
                Event.BACKUP_FAILED,
                Level.ERROR,
                operator=operator.subject,
                error_class=DestinationUnavailableError.__name__,
            )
            raise DestinationUnavailableError()
        now = self._clock()
        backup_id = backup_id or _new_backup_id(now)
        partial = self.root / f".partial-{backup_id}"
        _LOG.log(Event.BACKUP_STARTED, Level.INFO, job_id=backup_id, operator=operator.subject)
        try:
            collected = collect(base, spec)
            archive = _tar(collected.files, now.timestamp())
            data_key = AESGCM.generate_key(bit_length=256)
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(data_key).encrypt(nonce, archive, backup_id.encode())
            key_ref, wrapped = self._keys.wrap(data_key)
            manifest = Manifest(
                backup_id=backup_id,
                created_at=now,
                expires_at=now + timedelta(days=spec.retention_days),
                created_by=operator.subject,
                spec_version=spec.spec_version,
                triaxis_version=self._version,
                resources=tuple(r for r, _ in collected.files),
                excluded={
                    "transient_inference_content": "never_collected (zero retention)",
                    "secrets": "excluded; OpenBao raft snapshot procedure is separate",
                    "model_weights": "excluded; recovered by sha256 from offline vault/Harbor",
                    "denied_files": collected.denied,
                },
                findings=dict(sorted(collected.findings.items())),
                archive={"file": ARCHIVE, "sha256": _sha256(ciphertext), "size": len(ciphertext)},
                encryption={
                    "alg": "AES-256-GCM",
                    "key_ref": key_ref,
                    "wrapped_key_b64": base64.b64encode(wrapped).decode(),
                    "nonce_b64": base64.b64encode(nonce).decode(),
                },
            )
            mac = hmac.new(_mac_key(data_key), manifest.signable(), hashlib.sha256).hexdigest()
            manifest = manifest.model_copy(update={"mac": mac})
            partial.mkdir(mode=0o700)
            self._write(partial / ARCHIVE, ciphertext)
            self._write(partial / MANIFEST, manifest.model_dump_json(indent=2).encode())
            final = self.root / backup_id
            partial.rename(final)
            final.chmod(0o500)  # write-protected (not immutable; see ADR 0022)
        except BackupError as exc:
            self._cleanup(partial)
            _LOG.log(
                Event.BACKUP_FAILED,
                Level.ERROR,
                job_id=backup_id,
                operator=operator.subject,
                error_class=type(exc).__name__,
            )
            raise
        except (OSError, ValidationError) as exc:
            self._cleanup(partial)
            _LOG.log(
                Event.BACKUP_FAILED,
                Level.ERROR,
                job_id=backup_id,
                operator=operator.subject,
                error_class=type(exc).__name__,
            )
            raise DestinationUnavailableError() from None
        _LOG.log(
            Event.BACKUP_COMPLETED,
            Level.INFO,
            job_id=backup_id,
            operator=operator.subject,
            bytes_out=len(ciphertext),
        )
        return manifest

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _cleanup(path: Path) -> None:
        if path.exists():
            for p in [path, *path.rglob("*")]:
                p.chmod(0o700 if p.is_dir() else 0o600)
            shutil.rmtree(path)

    # ----------------------------------------------------------------- read / verify

    def _dir(self, backup_id: str) -> Path:
        if not backup_id.startswith("bk-") or "/" in backup_id or ".." in backup_id:
            raise BackupNotFoundError()
        path = self.root / backup_id
        if not (path / MANIFEST).is_file():
            raise BackupNotFoundError()
        return path

    def read_manifest(self, backup_id: str) -> Manifest:
        raw = (self._dir(backup_id) / MANIFEST).read_bytes()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise TamperedManifestError() from None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise TamperedManifestError()
        if data.get("manifest_version") != MANIFEST_VERSION:
            raise IncompatibleVersionError()
        try:
            return Manifest.model_validate(data)
        except ValidationError:
            raise TamperedManifestError() from None

    def _open(self, backup_id: str) -> tuple[Manifest, bytes]:
        """Verify manifest MAC, archive hash and decryption. Returns the plaintext archive."""
        manifest = self.read_manifest(backup_id)
        if manifest.backup_id != backup_id:
            raise TamperedManifestError()
        try:
            wrapped = base64.b64decode(manifest.encryption["wrapped_key_b64"], validate=True)
            nonce = base64.b64decode(manifest.encryption["nonce_b64"], validate=True)
        except (KeyError, ValueError):
            raise TamperedManifestError() from None
        data_key = self._keys.unwrap(manifest.encryption.get("key_ref", ""), wrapped)
        expected = hmac.new(_mac_key(data_key), manifest.signable(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, manifest.mac):
            raise TamperedManifestError()
        archive_path = self._dir(backup_id) / ARCHIVE
        try:
            ciphertext = archive_path.read_bytes()
        except OSError:
            raise IntegrityError("archive missing") from None
        if len(ciphertext) > MAX_ARCHIVE_BYTES or _sha256(ciphertext) != manifest.archive["sha256"]:
            raise IntegrityError("archive hash mismatch")
        try:
            plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, backup_id.encode())
        except Exception:  # noqa: BLE001 - cryptography raises InvalidTag
            raise IntegrityError("decryption failed") from None
        return manifest, plaintext

    def verify(self, backup_id: str, operator: Operator) -> Manifest:
        if not (
            {Role.BACKUP_OPERATOR, Role.BACKUP_AUDITOR, Role.RESTORE_OPERATOR} & operator.roles
        ):
            operator.require(Role.BACKUP_AUDITOR)
        try:
            manifest, archive = self._open(backup_id)
            members = self._members(manifest, archive)
        except BackupError as exc:
            self._record(VERIFICATIONS, backup_id, "failed", exc.code)
            raise
        del members
        self._record(VERIFICATIONS, backup_id, "verified", None)
        _LOG.log(Event.BACKUP_VERIFIED, Level.INFO, job_id=backup_id, operator=operator.subject)
        return manifest

    def _members(self, manifest: Manifest, archive: bytes) -> dict[str, bytes]:
        expected = {r.path: r for r in manifest.resources}
        out: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                path = PurePosixPath(member.name)
                if (
                    not member.isreg()
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or member.name in out
                ):
                    raise UnsafeArchiveError("member rejected")
                resource = expected.get(member.name)
                if resource is None or member.size != resource.size:
                    raise UnsafeArchiveError("member not in manifest")
                if _denied(member.name, ALWAYS_DENIED):
                    raise UnsafeArchiveError("denied file type in archive")
                handle = tar.extractfile(member)
                data = handle.read() if handle else b""
                if _sha256(data) != resource.sha256:
                    raise IntegrityError("file hash mismatch")
                out[member.name] = data
        if set(out) != set(expected):
            raise IntegrityError("archive incomplete")
        return out

    def _record(self, ledger: str, backup_id: str, result: str, code: str | None) -> None:
        line = json.dumps(
            {
                "backup_id": backup_id,
                "at": self._clock().isoformat(),
                "result": result,
                "code": code,
            }
        )
        try:
            with (self.root / ledger).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # status ledger is best effort; the operation result is authoritative

    # ----------------------------------------------------------------- restore

    def restore(
        self,
        backup_id: str,
        target: Path,
        operator: Operator,
        *,
        protected: Iterable[Path] = (),
        in_place: bool = False,
        approver: Operator | None = None,
    ) -> RestoreReport:
        operator.require(Role.RESTORE_OPERATOR)
        target = target.resolve()
        protected_paths = [p.resolve() for p in protected]
        if in_place:
            if approver is None or approver.subject == operator.subject:
                _LOG.log(
                    Event.AUTHZ_DENIED, Level.WARNING, job_id=backup_id, operator=operator.subject
                )
                raise UnauthorizedError("in-place restore needs a different approver")
            approver.require(Role.RESTORE_APPROVER)
        elif any(
            target == p or target.is_relative_to(p) or p.is_relative_to(target)
            for p in protected_paths
        ):
            raise UnauthorizedError("target overlaps a protected live path; use in-place approval")
        if target.is_relative_to(self.root.resolve()):
            raise UnsafeArchiveError("target inside backup root")
        if not in_place and target.exists() and any(target.iterdir()):
            raise UnsafeArchiveError("restore target must be empty")
        _LOG.log(Event.RESTORE_STARTED, Level.INFO, job_id=backup_id, operator=operator.subject)
        staging = target.parent / f".restore-{backup_id}-{uuid.uuid4().hex[:6]}"
        try:
            manifest, archive = self._open(backup_id)
            members = self._members(manifest, archive)
            staging.mkdir(mode=0o700, parents=True)
            for rel, data in sorted(members.items()):
                dest = (staging / rel).resolve()
                if not dest.is_relative_to(staging.resolve()):
                    raise UnsafeArchiveError("path escapes target")
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            checks = validate_restored(staging)
            if any(v != "ok" for v in checks.values()):
                raise RestoreValidationError(",".join(k for k, v in checks.items() if v != "ok"))
            self._promote(staging, target, backup_id, in_place=in_place)
        except BackupError as exc:
            self._cleanup(staging)
            self._record(RESTORE_TESTS, backup_id, "failed", exc.code)
            _LOG.log(
                Event.RESTORE_FAILED,
                Level.ERROR,
                job_id=backup_id,
                operator=operator.subject,
                error_class=type(exc).__name__,
            )
            raise
        report = RestoreReport(backup_id, str(target), in_place, len(members), checks, "restored")
        self._record(RESTORE_TESTS, backup_id, "restored", None)
        _LOG.log(Event.RESTORE_COMPLETED, Level.INFO, job_id=backup_id, operator=operator.subject)
        return report

    @staticmethod
    def _promote(staging: Path, target: Path, backup_id: str, *, in_place: bool) -> None:
        if not in_place:
            if target.exists():
                target.rmdir()  # empty (checked above)
            staging.rename(target)
            return
        # In place: keep the previous version of every restored file for rollback.
        rollback = target / f".pre-restore-{backup_id}"
        for src in sorted(p for p in staging.rglob("*") if p.is_file()):
            rel = src.relative_to(staging)
            live = target / rel
            if live.exists():
                (rollback / rel).parent.mkdir(parents=True, exist_ok=True)
                live.rename(rollback / rel)
            live.parent.mkdir(parents=True, exist_ok=True)
            src.rename(live)
        shutil.rmtree(staging)

    # ----------------------------------------------------------------- list / prune

    def list_backups(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for path in sorted(self.root.iterdir()):
            if not path.name.startswith("bk-"):
                continue
            try:
                m = self.read_manifest(path.name)
            except BackupError as exc:
                out.append({"backup_id": path.name, "state": exc.code})
                continue
            out.append(
                {
                    "backup_id": m.backup_id,
                    "created_at": m.created_at.isoformat(),
                    "expires_at": m.expires_at.isoformat(),
                    "created_by": m.created_by,
                    "files": len(m.resources),
                    "archive_bytes": m.archive["size"],
                    "spec_version": m.spec_version,
                    "state": "completed (unverified listing)",
                }
            )
        return out

    def ledger(self, name: str) -> list[dict[str, Any]]:
        path = self.root / name
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def prune(self, operator: Operator) -> list[str]:
        operator.require(Role.RETENTION_ADMIN)
        now = self._clock()
        removed: list[str] = []
        for path in sorted(self.root.iterdir()):
            if path.name.startswith(".partial-"):
                self._cleanup(path)  # leftover of an interrupted job
                continue
            if not path.name.startswith("bk-"):
                continue
            try:
                manifest = self.read_manifest(path.name)
            except BackupError:
                continue  # never auto-delete something we cannot read; report via list
            if manifest.expires_at <= now:
                self._cleanup(path)
                removed.append(path.name)
                _LOG.log(
                    Event.BACKUP_PRUNED, Level.INFO, job_id=path.name, operator=operator.subject
                )
        return removed


def validate_restored(root: Path) -> dict[str, str]:
    """Schema, policy, registry and tenant-boundary checks on restored config."""
    from tools.license_gate import run_gate  # noqa: PLC0415
    from tools.model_registry import load_registry  # noqa: PLC0415
    from triaxis_common.config import load_hardware_profiles, load_tenants  # noqa: PLC0415
    from triaxis_common.policy import load_policies  # noqa: PLC0415

    checks: dict[str, str] = {}

    def check(name: str, fn: Callable[[], object]) -> object:
        try:
            result = fn()
        except (ValidationError, ValueError, OSError, yaml.YAMLError, KeyError):
            checks[name] = "failed"
            return None
        checks[name] = "ok"
        return result

    def nonempty(loaded: dict[str, Any]) -> dict[str, Any]:
        if not loaded:
            raise ValueError("nothing restored")
        return loaded

    tenants = check("tenants", lambda: nonempty(load_tenants(root / "config/tenants")))
    policies = check("policies", lambda: load_policies(root / "config/policies"))
    check("hardware_profiles", lambda: load_hardware_profiles(root / "config/hardware-profiles"))
    registry = check("model_registry", lambda: load_registry(root / "models/registry.yaml"))
    if registry is not None:
        blocking = [f for f in run_gate(registry) if f.blocking]  # type: ignore[arg-type]
        checks["licence_gate"] = "ok" if not blocking else "failed"
    if isinstance(tenants, dict) and isinstance(policies, dict):
        refs_ok = all(t.data_policy in policies for t in tenants.values())
        checks["tenant_policy_references"] = "ok" if refs_ok else "failed"
        brands_ok = all(len({b.id for b in t.brands}) == len(t.brands) for t in tenants.values())
        checks["tenant_brand_boundaries"] = "ok" if brands_ok and tenants else "failed"
    if not tenants:
        checks["tenant_brand_boundaries"] = "failed"
        checks["tenant_policy_references"] = "failed"
    return checks


# --------------------------------------------------------------------- CLI


def _operator(args: argparse.Namespace, token_file: Path | None = None) -> Operator:
    import jwt  # noqa: PLC0415

    token_path = token_file or args.operator_token
    if token_path is None or args.jwks is None:
        raise UnauthorizedError("operator token and JWKS are required")
    try:
        token = Path(token_path).read_text("ascii").strip()
        jwks = json.loads(Path(args.jwks).read_text("utf-8"))
        header = jwt.get_unverified_header(token)
        key = next(jwt.PyJWK.from_dict(k) for k in jwks["keys"] if k.get("kid") == header["kid"])
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=["ES256", "RS256"],
            audience="triaxis-backup",
            issuer=args.issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except (OSError, ValueError, KeyError, StopIteration, jwt.PyJWTError):
        raise UnauthorizedError("operator token rejected") from None
    if claims["exp"] - claims["iat"] > 3600:
        raise UnauthorizedError("operator token lifetime too long")
    return operator_from_claims(claims)


def _dev_keys(out: Path) -> None:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from jwt.algorithms import ECAlgorithm  # noqa: PLC0415

    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(out / "dev-idp.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    jwk: dict[str, Any] = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="dev-backup-idp", alg="ES256", use="sig")
    (out / "jwks.json").write_text(json.dumps({"keys": [jwk]}, indent=2), "utf-8")


def _dev_token(keys: Path, sub: str, roles: list[str], issuer: str) -> str:
    import jwt  # noqa: PLC0415

    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": "triaxis-backup",
        "sub": sub,
        "iat": now,
        "exp": now + 900,
        "triaxis_roles": roles,
    }
    return jwt.encode(
        claims,
        (keys / "dev-idp.key").read_bytes(),
        algorithm="ES256",
        headers={"kid": "dev-backup-idp"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triaxis backup and recovery (ADR 0022)")
    parser.add_argument(
        "command",
        choices=[
            "create",
            "verify",
            "list",
            "restore",
            "prune",
            "status",
            "dev-keys",
            "dev-token",
            "gen-key",
        ],
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--base", type=Path, default=Path())
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--backup-id")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--operator-token", type=Path)
    parser.add_argument("--approver-token", type=Path)
    parser.add_argument("--jwks", type=Path)
    parser.add_argument("--issuer", default="triaxis-backup-idp")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--keys", type=Path)
    parser.add_argument("--sub")
    parser.add_argument("--roles", default="")
    args = parser.parse_args(argv)
    # Results go to stdout as JSON; allow-listed log records go to stderr.
    previous_sink = set_sink(lambda line: print(line, file=sys.stderr))
    try:
        return _run(args)
    finally:
        set_sink(previous_sink)


def _run(args: argparse.Namespace) -> int:
    try:
        if args.command == "dev-keys":
            _dev_keys(args.out)
            print(f"backup: dev identity keys written to {args.out}")
            return 0
        if args.command == "dev-token":
            print(
                _dev_token(
                    args.keys, args.sub, [r for r in args.roles.split(",") if r], args.issuer
                )
            )
            return 0
        if args.command == "gen-key":
            LocalKeyProvider.generate(args.key_file)
            print(f"backup: key-encryption key written to {args.key_file} (mode 0600)")
            return 0
        if args.root is None:
            raise DestinationUnavailableError("--root is required")
        if args.command in ("list", "status"):
            service = BackupService(args.root, NoKeys())
            print(
                json.dumps(
                    {
                        "backups": service.list_backups(),
                        "verifications": service.ledger(VERIFICATIONS)[-10:],
                        "restore_tests": service.ledger(RESTORE_TESTS)[-10:],
                    },
                    indent=2,
                )
            )
            return 0
        service = BackupService(args.root, LocalKeyProvider(args.key_file))
        operator = _operator(args)
        if args.command == "create":
            manifest = service.create(args.base, load_spec(args.spec), operator, args.backup_id)
            print(
                json.dumps(
                    {
                        "backup_id": manifest.backup_id,
                        "files": len(manifest.resources),
                        "excluded": manifest.excluded,
                    },
                    indent=2,
                )
            )
        elif args.command == "verify":
            manifest = service.verify(args.backup_id, operator)
            print(json.dumps({"backup_id": manifest.backup_id, "result": "verified"}))
        elif args.command == "restore":
            approver = _operator(args, args.approver_token) if args.in_place else None
            report = service.restore(
                args.backup_id,
                args.target,
                operator,
                in_place=args.in_place,
                approver=approver,
                protected=[] if args.in_place else [args.base / "config", args.base / "models"],
            )
            print(json.dumps(report.as_dict(), indent=2))
        elif args.command == "prune":
            print(json.dumps({"removed": service.prune(operator)}))
    except BackupError as exc:
        print(f"backup: {exc.code}", file=sys.stderr)
        return 2
    return 0


class NoKeys:
    """Listing needs no key; any attempt to unwrap fails closed."""

    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        del data_key
        raise KeyUnavailableError()

    def unwrap(self, key_ref: str, wrapped: bytes) -> bytes:
        del key_ref, wrapped
        raise KeyUnavailableError()


if __name__ == "__main__":
    raise SystemExit(main())
