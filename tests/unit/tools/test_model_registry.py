from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tools import model_registry
from tools.model_registry import (
    Registry,
    Status,
    can_download,
    load_registry,
    sha256_file,
    validate_tenant_selection,
    verify_artefacts,
)
from triaxis_common.config import load_tenant_config

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_TENANT = REPO / "config/tenants/example-tenant.yaml"


def _entry(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "m1",
        "task": "llm",
        "version": "1",
        "origin": {"organization": "Org", "country": "SA"},
        "license": {"spdx": "Apache-2.0"},
        "languages": ["ar", "en"],
    }
    data.update(overrides)
    return data


def _registry(*entries: dict[str, Any]) -> Registry:
    return Registry.model_validate({"schema_version": 1, "models": list(entries)})


def test_repository_registry_is_valid_and_all_candidates() -> None:
    registry = load_registry(REPO / "models/registry.yaml")
    assert len(registry.models) >= 10
    assert all(m.status is Status.CANDIDATE for m in registry.models)
    assert all(m.format == "safetensors" for m in registry.models)
    # Nothing in the repository registry is downloadable yet: no licence is verified.
    assert not any(can_download(m) for m in registry.models)


@pytest.mark.parametrize(
    "overrides",
    [
        {"format": "pytorch"},
        {"id": "Bad ID"},
        {"languages": []},
        {"languages": ["Arabic"]},
        {"license": {}},
        {"origin": {"organization": "Org", "country": "Saudi"}},
        {"unknown": True},
        {"artefacts": [{"filename": "../escape.safetensors", "sha256": "a" * 64, "size_bytes": 1}]},
        {"artefacts": [{"filename": "/abs.safetensors", "sha256": "a" * 64, "size_bytes": 1}]},
        {"artefacts": [{"filename": "m.safetensors", "sha256": "XYZ", "size_bytes": 1}]},
        {"source": {"repository": "org/model", "revision": "main"}},  # revision must be a SHA
        {"status": "approved"},  # incomplete approved entry
    ],
)
def test_invalid_entries(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _registry(_entry(**overrides))


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate model"):
        _registry(_entry(), _entry())
    with pytest.raises(ValidationError, match="duplicate dataset"):
        Registry.model_validate(
            {
                "models": [],
                "datasets": [
                    {
                        "id": "d",
                        "purpose": "evaluation",
                        "consent_basis": "licensed",
                        "source_kind": "synthetic",
                        "languages": ["ar"],
                    }
                ]
                * 2,
            }
        )


def test_lookup_helpers() -> None:
    registry = _registry(_entry())
    assert registry.model("m1") is not None
    assert registry.model("nope") is None
    assert registry.dataset("nope") is None


def _verified(tmp_path: Path) -> tuple[Registry, Path]:
    model_dir = tmp_path / "m1"
    model_dir.mkdir()
    weights = model_dir / "model.safetensors"
    weights.write_bytes(b"\x00" * 128)
    config = model_dir / "config.json"
    config.write_text("{}", "utf-8")
    entry = _entry(
        status="approved",
        source={"repository": "org/m1", "revision": "f" * 40},
        hardware_profiles=["h100-llm"],
        license={"spdx": "Apache-2.0", "verified_on": "2026-10-01"},
        artefacts=[
            {"filename": "model.safetensors", "sha256": sha256_file(weights), "size_bytes": 128},
            {"filename": "config.json", "sha256": sha256_file(config), "size_bytes": 2},
        ],
    )
    return _registry(entry), model_dir


def test_verify_artefacts_ok_and_downloadable(tmp_path: Path) -> None:
    registry, model_dir = _verified(tmp_path)
    entry = registry.model("m1")
    assert entry is not None
    assert verify_artefacts(entry, model_dir) == []
    assert can_download(entry)


def test_verify_detects_tampering(tmp_path: Path) -> None:
    registry, model_dir = _verified(tmp_path)
    entry = registry.model("m1")
    assert entry is not None
    (model_dir / "model.safetensors").write_bytes(b"\x01" * 128)
    assert verify_artefacts(entry, model_dir) == ["model.safetensors: sha256 mismatch"]
    (model_dir / "model.safetensors").write_bytes(b"\x01" * 10)
    assert verify_artefacts(entry, model_dir) == ["model.safetensors: size mismatch"]
    (model_dir / "config.json").unlink()
    assert "config.json: missing" in verify_artefacts(entry, model_dir)


def test_verify_rejects_symlinks(tmp_path: Path) -> None:
    registry, model_dir = _verified(tmp_path)
    entry = registry.model("m1")
    assert entry is not None
    real = model_dir / "config.json"
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", "utf-8")
    real.unlink()
    real.symlink_to(target)
    problems = verify_artefacts(entry, model_dir)
    assert any("symlink" in p or "escapes" in p for p in problems)


def test_verify_without_artefacts(tmp_path: Path) -> None:
    entry = _registry(_entry()).model("m1")
    assert entry is not None
    assert verify_artefacts(entry, tmp_path) == ["m1: no artefacts recorded"]


def test_can_download_rules() -> None:
    unverified = _registry(_entry()).model("m1")
    assert unverified is not None
    assert not can_download(unverified)
    nc = _registry(_entry(license={"spdx": "CC-BY-NC-4.0", "verified_on": "2026-10-01"}))
    entry = nc.model("m1")
    assert entry is not None
    assert not can_download(entry)
    retired = _registry(
        _entry(status="retired", license={"spdx": "MIT", "verified_on": "2026-10-01"})
    ).model("m1")
    assert retired is not None
    assert not can_download(retired)


def test_sha256_file_streams(tmp_path: Path) -> None:
    path = tmp_path / "f"
    data = b"x" * (model_registry.CHUNK * 2 + 7)
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------- tenants


def test_example_tenant_selection_is_valid() -> None:
    tenant = load_tenant_config(EXAMPLE_TENANT)
    registry = load_registry(REPO / "models/registry.yaml")
    assert validate_tenant_selection(tenant, registry, require_approved=False) == []
    # In production every selected model must be approved; none are yet.
    assert validate_tenant_selection(tenant, registry, require_approved=True)


def test_tenant_selection_problems() -> None:
    tenant = load_tenant_config(EXAMPLE_TENANT)
    data = tenant.model_dump(mode="json")
    data["approved_origin_countries"] = ["SA"]
    data["models"]["stt"] = "qwen3.5-27b"  # wrong task
    data["models"]["tts"] = "missing-model"
    data["brands"][1]["models"] = {**data["models"], "llm_chat": "retired-llm"}
    tenant = type(tenant).model_validate(data)
    registry = load_registry(REPO / "models/registry.yaml")
    registry = Registry(
        models=[
            *registry.models,
            model_registry.ModelEntry.model_validate(_entry(id="retired-llm", status="retired")),
        ]
    )
    problems = validate_tenant_selection(tenant, registry, require_approved=False)
    text = "\n".join(problems)
    assert "is a llm model, not stt" in text
    assert "unknown model 'missing-model'" in text
    assert "origin of 'qwen3.5-27b' is not approved" in text
    assert "brand brand-b llm_chat: 'retired-llm' is retired" in text


# --------------------------------------------------------------------- CLI


def test_cli_commands(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert model_registry.main(["check"]) == 0
    assert model_registry.main(["list"]) == 0
    assert "whisper-large-v3" in capsys.readouterr().out
    assert model_registry.main(["show", "whisper-large-v3"]) == 0
    assert "openai/whisper-large-v3" in capsys.readouterr().out
    assert model_registry.main(["show", "nope"]) == 1
    assert model_registry.main(["verify", "nope", "--dir", str(tmp_path)]) == 1
    assert model_registry.main(["verify", "whisper-large-v3", "--dir", str(tmp_path)]) == 1
    assert model_registry.main(["check-tenants"]) == 0
    assert model_registry.main(["check-tenants", "--require-approved"]) == 1
    f = tmp_path / "model.safetensors"
    f.write_bytes(b"abc")
    capsys.readouterr()
    assert model_registry.main(["hash", str(f)]) == 0
    out = capsys.readouterr().out
    assert hashlib.sha256(b"abc").hexdigest() in out
    assert "size_bytes: 3" in out


def test_cli_verify_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry, model_dir = _verified(tmp_path)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(registry.model_dump(mode="json")), "utf-8")
    assert (
        model_registry.main(["--registry", str(path), "verify", "m1", "--dir", str(model_dir)]) == 0
    )
    assert "2 artefacts match" in capsys.readouterr().out
