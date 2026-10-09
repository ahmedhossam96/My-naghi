"""Model and dataset registry (ADR 0009, 0010, 0011).

The registry (``models/registry.yaml``) records every model Triaxis may serve
and every dataset the evaluation / fine-tuning tooling may read. It holds
metadata only — never weights.

    python -m tools.model_registry check
    python -m tools.model_registry list
    python -m tools.model_registry show <model-id>
    python -m tools.model_registry verify <model-id> --dir <artefact-dir>
    python -m tools.model_registry hash <file>...
    python -m tools.model_registry check-tenants [--tenants-dir DIR] [--require-approved]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from triaxis_common.config import CountryCode, TenantConfig, load_tenants
from triaxis_common.types import ModelId, Slug

DEFAULT_REGISTRY = Path("models/registry.yaml")
DEFAULT_TENANTS = Path("config/tenants")
CHUNK = 1024 * 1024

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
LanguageTag = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
Repository = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Task(StrEnum):
    STT = "stt"
    TTS = "tts"
    LLM = "llm"
    VAD = "vad"
    DIARIZATION = "diarization"


class Status(StrEnum):
    CANDIDATE = "candidate"  # metadata only; must not be downloaded until verified
    APPROVED = "approved"  # verified licence, pinned revision, hashed artefacts
    RETIRED = "retired"


class Source(_Strict):
    repository: Repository | None = None
    revision: GitRevision | None = None


class Origin(_Strict):
    organization: ShortText
    country: CountryCode | None = None


class LicenseApproval(_Strict):
    """Recorded approval for a licence outside the SPDX allow-list."""

    approved_by: ShortText
    approved_on: date
    license_url: Annotated[str, Field(pattern=r"^https://\S+$")]
    license_text_sha256: Sha256


class License(_Strict):
    spdx: Annotated[str, Field(pattern=r"^[A-Za-z0-9.+-]{1,64}$")] | None = None
    name: ShortText | None = None
    url: Annotated[str, Field(pattern=r"^https://\S+$")] | None = None
    verified_on: date | None = None
    notes: Annotated[str, Field(max_length=500)] = ""
    approval: LicenseApproval | None = None

    @model_validator(mode="after")
    def _identified(self) -> Self:
        if self.spdx is None and self.name is None:
            raise ValueError("a licence needs an spdx id or a name")
        return self


class Artefact(_Strict):
    filename: Annotated[str, Field(min_length=1, max_length=200)]
    sha256: Sha256
    size_bytes: PositiveInt

    @field_validator("filename")
    @classmethod
    def _relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("artefact filename must be a relative path inside the model dir")
        return value


class ModelEntry(_Strict):
    id: ModelId
    task: Task
    version: Annotated[str, Field(min_length=1, max_length=64)]
    source: Source = Source()
    origin: Origin
    license: License
    languages: Annotated[list[LanguageTag], Field(min_length=1)]
    hardware_profiles: list[Slug] = []
    format: Literal["safetensors"] = "safetensors"
    artefacts: list[Artefact] = []
    status: Status = Status.CANDIDATE
    notes: Annotated[str, Field(max_length=1000)] = ""

    @model_validator(mode="after")
    def _approved_is_complete(self) -> Self:
        if self.status is Status.APPROVED:
            missing = [
                name
                for name, ok in (
                    ("artefacts", bool(self.artefacts)),
                    ("license.verified_on", self.license.verified_on is not None),
                    ("source.repository", self.source.repository is not None),
                    ("source.revision", self.source.revision is not None),
                    ("hardware_profiles", bool(self.hardware_profiles)),
                )
                if not ok
            ]
            if missing:
                raise ValueError(f"approved model is missing: {', '.join(missing)}")
        return self


class SourceKind(StrEnum):
    THIRD_PARTY_LICENSED = "third_party_licensed"
    PUBLIC_LICENSED = "public_licensed"
    COMMISSIONED = "commissioned"  # e.g. consented voice-actor recordings
    SYNTHETIC = "synthetic"
    PLATFORM_TRAFFIC = "platform_traffic"  # customer content: never allowed


class DatasetEntry(_Strict):
    id: ModelId
    purpose: Literal["evaluation", "fine_tuning"]
    # Free text on purpose: the gate (not the schema) refuses anything other
    # than "licensed" or "consented", so a refusal is reported, not a parse error.
    consent_basis: Annotated[str, Field(max_length=64)]
    source_kind: SourceKind
    license: License | None = None
    languages: Annotated[list[LanguageTag], Field(min_length=1)]
    sha256: Sha256 | None = None
    notes: Annotated[str, Field(max_length=1000)] = ""


class Registry(_Strict):
    schema_version: Literal[1] = 1
    models: list[ModelEntry]
    datasets: list[DatasetEntry] = []

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for kind, ids in (
            ("model", [m.id for m in self.models]),
            ("dataset", [d.id for d in self.datasets]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {kind} ids")
        return self

    def model(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def dataset(self, dataset_id: str) -> DatasetEntry | None:
        return next((d for d in self.datasets if d.id == dataset_id), None)


def load_registry(path: Path = DEFAULT_REGISTRY) -> Registry:
    with path.open("rb") as fh:
        return Registry.model_validate(yaml.safe_load(fh))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artefacts(entry: ModelEntry, directory: Path) -> list[str]:
    """Compare files in ``directory`` against the registry. Empty list = OK."""
    problems: list[str] = []
    if not entry.artefacts:
        return [f"{entry.id}: no artefacts recorded"]
    base = directory.resolve()
    for artefact in entry.artefacts:
        path = (base / artefact.filename).resolve()
        if not path.is_relative_to(base):
            problems.append(f"{artefact.filename}: escapes the model directory")
        elif path.is_symlink() or (base / artefact.filename).is_symlink():
            problems.append(f"{artefact.filename}: symlinks are not accepted")
        elif not path.is_file():
            problems.append(f"{artefact.filename}: missing")
        elif path.stat().st_size != artefact.size_bytes:
            problems.append(f"{artefact.filename}: size mismatch")
        elif sha256_file(path) != artefact.sha256:
            problems.append(f"{artefact.filename}: sha256 mismatch")
    return problems


def can_download(entry: ModelEntry) -> bool:
    """True only for a commercially usable, verified licence (ADR 0009)."""
    from tools.license_gate import Verdict, evaluate_model  # noqa: PLC0415 - avoids cycle

    verdict = evaluate_model(entry).verdict
    return (
        verdict in (Verdict.ALLOW, Verdict.REVIEW_APPROVED)
        and entry.license.verified_on is not None
        and entry.status is not Status.RETIRED
    )


_PROFILE_TASKS: dict[str, Task] = {
    "stt": Task.STT,
    "tts": Task.TTS,
    "llm_voice": Task.LLM,
    "llm_chat": Task.LLM,
}


def validate_tenant_selection(
    tenant: TenantConfig, registry: Registry, *, require_approved: bool
) -> list[str]:
    """ADR 0010: selected models exist, fit the task and have an approved origin."""
    problems: list[str] = []
    selections = [("tenant", tenant.models)] + [
        (f"brand {b.id}", b.models) for b in tenant.brands if b.models is not None
    ]
    for scope, selection in selections:
        for profile, task in _PROFILE_TASKS.items():
            model_id = getattr(selection, profile)
            entry = registry.model(model_id)
            where = f"{tenant.id} {scope} {profile}"
            if entry is None:
                problems.append(f"{where}: unknown model '{model_id}'")
                continue
            if entry.task is not task:
                problems.append(f"{where}: '{model_id}' is a {entry.task} model, not {task}")
            if entry.status is Status.RETIRED:
                problems.append(f"{where}: '{model_id}' is retired")
            elif require_approved and entry.status is not Status.APPROVED:
                problems.append(f"{where}: '{model_id}' is not approved")
            country = entry.origin.country
            if country is None or country not in tenant.approved_origin_countries:
                problems.append(f"{where}: origin of '{model_id}' is not approved by the tenant")
    return problems


# --------------------------------------------------------------------- CLI


def _cmd_check(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    print(f"registry: {len(registry.models)} models, {len(registry.datasets)} datasets, schema OK")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    for m in load_registry(args.registry).models:
        lic = m.license.spdx or m.license.name
        print(f"{m.id:28} {m.task:12} {m.status:10} {lic}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    entry = load_registry(args.registry).model(args.model_id)
    if entry is None:
        print(f"registry: unknown model '{args.model_id}'", file=sys.stderr)
        return 1
    print(yaml.safe_dump(entry.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    entry = load_registry(args.registry).model(args.model_id)
    if entry is None:
        print(f"registry: unknown model '{args.model_id}'", file=sys.stderr)
        return 1
    problems = verify_artefacts(entry, args.dir)
    for problem in problems:
        print(f"verify: {problem}", file=sys.stderr)
    if not problems:
        print(f"verify: {entry.id}: {len(entry.artefacts)} artefacts match")
    return 1 if problems else 0


def _cmd_hash(args: argparse.Namespace) -> int:
    """Print artefact entries ready to paste into the registry."""
    for path in args.files:
        print(
            f"- filename: {path.name}\n  sha256: {sha256_file(path)}\n  size_bytes: "
            f"{path.stat().st_size}"
        )
    return 0


def _cmd_check_tenants(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    problems: list[str] = []
    for tenant in load_tenants(args.tenants_dir).values():
        problems += validate_tenant_selection(
            tenant, registry, require_approved=args.require_approved
        )
    for problem in problems:
        print(f"tenants: {problem}", file=sys.stderr)
    if not problems:
        print("tenants: model selections are valid")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triaxis model registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(func=_cmd_check)
    sub.add_parser("list").set_defaults(func=_cmd_list)
    show = sub.add_parser("show")
    show.add_argument("model_id")
    show.set_defaults(func=_cmd_show)
    verify = sub.add_parser("verify")
    verify.add_argument("model_id")
    verify.add_argument("--dir", type=Path, required=True)
    verify.set_defaults(func=_cmd_verify)
    hash_cmd = sub.add_parser("hash")
    hash_cmd.add_argument("files", type=Path, nargs="+")
    hash_cmd.set_defaults(func=_cmd_hash)
    tenants = sub.add_parser("check-tenants")
    tenants.add_argument("--tenants-dir", type=Path, default=DEFAULT_TENANTS)
    tenants.add_argument("--require-approved", action="store_true")
    tenants.set_defaults(func=_cmd_check_tenants)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
