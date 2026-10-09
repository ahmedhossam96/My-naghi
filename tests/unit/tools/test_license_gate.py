from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tools import license_gate
from tools.license_gate import (
    Verdict,
    check_dataset_allowed,
    evaluate_dataset,
    evaluate_license,
    evaluate_model,
    run_gate,
)
from tools.model_registry import DatasetEntry, License, ModelEntry, Registry

APPROVAL = {
    "approved_by": "Quantum Seal legal",
    "approved_on": "2026-10-01",
    "license_url": "https://example.org/licence",
    "license_text_sha256": "a" * 64,
}


def model(**overrides: Any) -> ModelEntry:
    data: dict[str, Any] = {
        "id": "m1",
        "task": "llm",
        "version": "1",
        "origin": {"organization": "Org", "country": "SA"},
        "license": {"spdx": "Apache-2.0"},
        "languages": ["ar"],
    }
    data.update(overrides)
    return ModelEntry.model_validate(data)


def dataset(**overrides: Any) -> DatasetEntry:
    data: dict[str, Any] = {
        "id": "d1",
        "purpose": "evaluation",
        "consent_basis": "licensed",
        "source_kind": "third_party_licensed",
        "languages": ["ar"],
    }
    data.update(overrides)
    return DatasetEntry.model_validate(data)


@pytest.mark.parametrize("spdx", sorted(license_gate.ALLOWED_SPDX))
def test_allow_listed_spdx(spdx: str) -> None:
    assert evaluate_license(License(spdx=spdx))[0] is Verdict.ALLOW


@pytest.mark.parametrize(
    "lic",
    [
        {"spdx": "CC-BY-NC-4.0"},
        {"spdx": "CC-BY-NC-SA-4.0"},
        {"spdx": "cc-by-nc-nd-4.0"},
        {"name": "Non-Commercial Research License"},
        {"name": "Research only"},
        {"name": "Custom", "notes": "for research purposes"},
        {"name": "Academic use"},
        {"name": "Evaluation-only licence"},
        {"name": "Not for commercial use"},
        {"name": "Personal use only"},
        {"name": "NC"},
        # An approval can never override a non-commercial licence.
        {"spdx": "CC-BY-NC-4.0", "approval": APPROVAL},
    ],
)
def test_non_commercial_is_denied(lic: dict[str, Any]) -> None:
    verdict, reasons = evaluate_license(License.model_validate(lic))
    assert verdict is Verdict.DENY
    assert reasons


@pytest.mark.parametrize(
    "name", ["Llama Community License", "OpenRAIL-M", "Custom licence", "Apache 2.0 (Inc. terms)"]
)
def test_unknown_licences_need_review(name: str) -> None:
    assert evaluate_license(License(name=name))[0] is Verdict.NEEDS_REVIEW


def test_approval_record_allows_custom_licence() -> None:
    lic = License.model_validate({"name": "Custom licence", "approval": APPROVAL})
    assert evaluate_license(lic)[0] is Verdict.REVIEW_APPROVED


@given(
    st.sampled_from(["-NC-", " NC ", "NonCommercial", "non commercial", "research-only"]),
    st.text(alphabet="abcdefghij 0123456789", max_size=10),
)
def test_property_nc_marker_anywhere_denies(marker: str, filler: str) -> None:
    lic = License(name=f"{filler}{marker}{filler}".strip() or marker.strip())
    assert evaluate_license(lic)[0] is Verdict.DENY


def test_candidate_needs_review_is_not_blocking() -> None:
    finding = evaluate_model(model(license={"name": "Custom"}))
    assert finding.verdict is Verdict.NEEDS_REVIEW
    assert not finding.blocking
    assert any("not yet verified" in r for r in finding.reasons)


def _approved(**overrides: Any) -> ModelEntry:
    return model(
        status="approved",
        source={"repository": "org/model", "revision": "0" * 40},
        hardware_profiles=["h100-llm"],
        artefacts=[{"filename": "model.safetensors", "sha256": "b" * 64, "size_bytes": 10}],
        license={"spdx": "Apache-2.0", "verified_on": "2026-10-01"},
        **overrides,
    )


def test_approved_model_with_custom_licence_without_approval_blocks() -> None:
    entry = _approved().model_copy(
        update={"license": License(name="Custom", verified_on=date(2026, 10, 1))}
    )
    assert evaluate_model(entry).blocking


def test_approved_model_allow() -> None:
    finding = evaluate_model(_approved())
    assert finding.verdict is Verdict.ALLOW
    assert not finding.blocking
    assert finding.reasons == ()


@pytest.mark.parametrize(
    "filename",
    [
        "pytorch_model.bin",
        "model.pt",
        "weights.pkl",
        "model.gguf",
        "model.onnx",
        "model.ckpt",
        "model.h5",
        "model.SAFETENSORS.bin",
    ],
)
def test_non_safetensors_artefact_denied(filename: str) -> None:
    entry = model(artefacts=[{"filename": filename, "sha256": "c" * 64, "size_bytes": 1}])
    finding = evaluate_model(entry)
    assert finding.verdict is Verdict.DENY
    assert finding.blocking


@pytest.mark.parametrize(
    "filename",
    [
        "model-00001-of-00002.safetensors",
        "config.json",
        "tokenizer.model",
        "chat_template.jinja",
        "sub/tokenizer.json",
    ],
)
def test_allowed_artefacts(filename: str) -> None:
    entry = model(artefacts=[{"filename": filename, "sha256": "c" * 64, "size_bytes": 1}])
    assert evaluate_model(entry).verdict is Verdict.ALLOW


# --------------------------------------------------------------------- datasets


def test_licensed_and_consented_datasets_allowed() -> None:
    assert not evaluate_dataset(dataset()).blocking
    assert not evaluate_dataset(
        dataset(consent_basis="consented", source_kind="commissioned", purpose="fine_tuning")
    ).blocking


@pytest.mark.parametrize("basis", ["", "public", "scraped", "LICENSED", "unknown", "customer"])
def test_dataset_without_consent_tag_refused(basis: str) -> None:
    finding = evaluate_dataset(dataset(consent_basis=basis))
    assert finding.verdict is Verdict.DENY
    assert finding.blocking


def test_platform_traffic_always_refused() -> None:
    finding = evaluate_dataset(dataset(consent_basis="consented", source_kind="platform_traffic"))
    assert finding.blocking
    assert any("rule 2" in r for r in finding.reasons)


def test_dataset_licence_checked() -> None:
    assert evaluate_dataset(dataset(license={"spdx": "CC-BY-NC-4.0"})).verdict is Verdict.DENY
    assert evaluate_dataset(dataset(license={"name": "Custom"})).verdict is Verdict.NEEDS_REVIEW
    assert evaluate_dataset(dataset(license={"spdx": "MIT"})).verdict is Verdict.ALLOW
    # A denied dataset stays denied even if its licence only needs review.
    denied = evaluate_dataset(dataset(consent_basis="no", license={"name": "Custom"}))
    assert denied.verdict is Verdict.DENY


def test_check_dataset_allowed() -> None:
    registry = Registry(
        models=[model()],
        datasets=[dataset(), dataset(id="bad", consent_basis="scraped")],
    )
    check_dataset_allowed(registry, "d1")
    with pytest.raises(PermissionError, match="refused"):
        check_dataset_allowed(registry, "bad")
    with pytest.raises(PermissionError, match="not in the registry"):
        check_dataset_allowed(registry, "missing")


# --------------------------------------------------------------------- CLI


def _write(tmp_path: Path, registry: dict[str, Any]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(registry), "utf-8")
    return path


def test_cli_passes_on_repository_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert license_gate.main([]) == 0
    assert "none blocking" in capsys.readouterr().out


def test_cli_fails_on_denied_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    entry = model(license={"spdx": "CC-BY-NC-4.0"}).model_dump(mode="json")
    path = _write(tmp_path, {"schema_version": 1, "models": [entry]})
    assert license_gate.main(["--registry", str(path)]) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "blocking" in captured.err


def test_run_gate_covers_models_and_datasets() -> None:
    findings = run_gate(Registry(models=[model()], datasets=[dataset()]))
    assert [f.kind for f in findings] == ["model", "dataset"]
