from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from tools import model_onboard
from tools.license_gate import run_gate
from tools.model_registry import RegistryApproval, load_registry
from triaxis_common.inference import ModelTask

REPO = Path(__file__).resolve().parents[3]
SHA = "a" * 40
HASH = "b" * 64


def hub(
    licence: str | None = "apache-2.0", files: list[dict[str, Any]] | None = None
) -> httpx.Client:
    siblings = (
        files
        if files is not None
        else [
            {
                "rfilename": "model-00001-of-00002.safetensors",
                "size": 5,
                "lfs": {"sha256": HASH, "size": 5},
            },
            {
                "rfilename": "model-00002-of-00002.safetensors",
                "size": 7,
                "lfs": {"sha256": "c" * 64, "size": 7},
            },
            {"rfilename": "config.json", "size": 100},
        ]
    )
    body: dict[str, Any] = {"sha": SHA, "siblings": siblings}
    if licence is not None:
        body["cardData"] = {"license": licence}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["blobs"] == "true"
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.yaml"
    shutil.copy(REPO / "models/registry.yaml", path)
    return path


def run(registry: Path, client: httpx.Client, *extra: str) -> int:
    return model_onboard.main(
        [
            "--model-id",
            "qwen3.5-27b",
            "--repo",
            "Qwen/Qwen3.5-27B",
            "--registry",
            str(registry),
            *extra,
        ],
        client,
    )


def test_dry_run_prints_evidence_and_writes_nothing(
    registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = registry.read_text()
    assert run(registry, hub()) == 0
    out = capsys.readouterr().out
    assert f"revision {SHA}" in out
    assert "2 safetensors file(s)" in out
    assert registry.read_text() == before


def test_approve_pins_revision_hashes_and_unlocks_the_model_gate(registry: Path) -> None:
    assert run(registry, hub(), "--approve", "--approved-by", "Ahmed") == 0
    reg = load_registry(registry)
    entry = reg.model("qwen3.5-27b")
    assert entry is not None
    assert entry.status.value == "approved"
    assert entry.source.revision == SHA
    assert [a.sha256 for a in entry.artefacts] == [HASH, "c" * 64]
    assert entry.license.spdx == "Apache-2.0"
    assert entry.license.verified_on is not None
    assert not [f for f in run_gate(reg) if f.blocking]
    assert RegistryApproval(reg)("qwen3.5-27b", ModelTask.LLM).approved
    assert registry.read_text().startswith("# Triaxis model and dataset registry")


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (hub(licence="cc-by-nc-4.0"), "non-commercial"),
        (hub(licence=None), "no licence"),
        (hub(files=[{"rfilename": "pytorch_model.bin"}]), "no .safetensors"),
        (hub(files=[{"rfilename": "m.safetensors", "size": 1}]), "no sha256"),
    ],
)
def test_refuses_without_evidence(
    registry: Path, client: httpx.Client, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = registry.read_text()
    assert run(registry, client, "--approve", "--approved-by", "x") == 1
    assert message in capsys.readouterr().err
    assert registry.read_text() == before


def test_custom_licence_cannot_be_approved_here(
    registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(registry, hub(licence="other"), "--approve", "--approved-by", "x") == 1
    assert "human approval record" in capsys.readouterr().err
    assert run(registry, hub(licence="other")) == 0  # dry run still reports it
    assert "needs approval record" in capsys.readouterr().out


def test_pickle_files_reported_not_recorded(
    registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = [
        {"rfilename": "m.safetensors", "size": 3, "lfs": {"sha256": HASH, "size": 3}},
        {"rfilename": "pytorch_model.bin", "size": 9},
    ]
    assert run(registry, hub(files=files)) == 0
    assert "NOT recorded: pytorch_model.bin" in capsys.readouterr().out


def test_usage_errors(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        model_onboard.main(
            ["--model-id", "nope", "--repo", "a/b", "--registry", str(registry)], hub()
        )
        == 2
    )
    assert run(registry, hub(), "--approve") == 2
    assert "--approved-by" in capsys.readouterr().err


def test_hub_errors(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    down = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403)))
    assert run(registry, down) == 1
    assert "cannot read model metadata" in capsys.readouterr().err
    nosha = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"x": 1}))
    )
    assert run(registry, nosha) == 1
