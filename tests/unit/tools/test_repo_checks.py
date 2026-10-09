"""Tests for check_no_weights, check_ci_sync and export_schemas."""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from tools import check_ci_sync, check_no_weights, export_schemas
from tools.check_no_weights import check_file

REPO = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------- no weights


@pytest.mark.parametrize(
    "name",
    [
        "model.safetensors",
        "pytorch_model.bin",
        "x.PT",
        "w.pkl",
        "m.gguf",
        "m.onnx",
        "m.npz",
        "archive.zip",
    ],
)
def test_denied_suffixes(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    assert check_file(path) is not None


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (b"\x80\x04\x95" + b"\x00" * 20, "pickle"),
        (b"\x80\x02" + b"\x00" * 20, "pickle"),
        (struct.pack("<Q", 20) + b'{"weight": {}}' + b"\x00" * 20, "safetensors"),
        (b"PK\x03\x04" + b"\x00" * 20, "zip"),
        (b"GGUF" + b"\x00" * 20, "GGUF"),
        (b"\x89HDF\r\n\x1a\n" + b"\x00" * 8, "HDF5"),
        (b"\x93NUMPY" + b"\x00" * 10, "NumPy"),
    ],
)
def test_magic_bytes_detected_despite_innocent_name(
    tmp_path: Path, content: bytes, label: str
) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(content)
    reason = check_file(path)
    assert reason is not None
    assert label in reason


def test_large_files_denied(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"a" * (check_no_weights.MAX_BYTES + 1))
    assert "larger" in (check_file(path) or "")


def test_ordinary_files_pass(tmp_path: Path) -> None:
    path = tmp_path / "ok.py"
    path.write_text("print('hi')\n", "utf-8")
    assert check_file(path) is None
    tiny = tmp_path / "tiny"
    tiny.write_bytes(b"\x80")
    assert check_file(tiny) is None
    assert check_file(tmp_path / "missing") is None


def test_no_weights_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "a.py"
    good.write_text("x = 1\n", "utf-8")
    bad = tmp_path / "b.bin"
    bad.write_bytes(b"x")
    assert check_no_weights.main([str(good)]) == 0
    assert check_no_weights.main([str(good), str(bad)]) == 1
    assert "b.bin" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_no_weights_all_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ok.txt").write_text("fine\n", "utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "ok.txt"], check=True)
    monkeypatch.chdir(tmp_path)
    assert check_no_weights.main(["--all"]) == 0
    files = check_no_weights.tracked_files(tmp_path)
    assert [f.name for f in files] == ["ok.txt"]


# --------------------------------------------------------------------- CI sync


def _layout(root: Path, content: str = "name: ci\n") -> None:
    for d in (".ci/workflows", ".github/workflows", ".forgejo/workflows"):
        (root / d).mkdir(parents=True)
        (root / d / "ci.yml").write_text(content, "utf-8")


def test_ci_sync_repository_is_in_sync() -> None:
    assert check_ci_sync.find_drift(REPO) == []


def test_ci_sync_detects_drift(tmp_path: Path) -> None:
    _layout(tmp_path)
    assert check_ci_sync.find_drift(tmp_path) == []
    (tmp_path / ".github/workflows/ci.yml").write_text("changed\n", "utf-8")
    (tmp_path / ".forgejo/workflows/ci.yml").unlink()
    (tmp_path / ".forgejo/workflows/extra.yml").write_text("x\n", "utf-8")
    problems = check_ci_sync.find_drift(tmp_path)
    assert any("differs" in p for p in problems)
    assert any("missing copy" in p for p in problems)
    assert any("not present" in p for p in problems)


def test_ci_sync_no_canonical(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".ci/workflows").mkdir(parents=True)
    assert check_ci_sync.main(["--root", str(tmp_path)]) == 1
    assert "no workflows" in capsys.readouterr().err
    assert check_ci_sync.main(["--root", str(REPO)]) == 0


# --------------------------------------------------------------------- schemas


def test_committed_schemas_are_current() -> None:
    assert export_schemas.stale(REPO) == []


def test_export_and_check(tmp_path: Path) -> None:
    assert export_schemas.main(["--check", "--root", str(tmp_path)]) == 1
    assert export_schemas.main(["--root", str(tmp_path)]) == 0
    assert export_schemas.main(["--check", "--root", str(tmp_path)]) == 0
