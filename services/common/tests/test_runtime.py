from __future__ import annotations

import contextlib
import os
import resource
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from triaxis_common import runtime
from triaxis_common.runtime import (
    RuntimeGuardError,
    configure_tmpdir,
    disable_core_dumps,
    filesystem_type,
    harden_process,
    is_memory_filesystem,
)
from triaxis_common.types import Environment

MOUNTINFO = """\
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
23 22 0:21 / /proc rw,nosuid shared:2 - proc proc rw
24 22 0:22 / /tmp/triaxis rw,nosuid,nodev shared:3 - tmpfs tmpfs rw,size=1048576k
25 22 0:23 / /var/lib/with\\040space rw shared:4 - tmpfs tmpfs rw
26 22 8:2 / /data rw shared:5 - xfs /dev/sdb1 rw
garbage line without separator
27 22 8:3 / rw -
"""


@pytest.fixture
def restore_process_state() -> Iterator[None]:
    saved_env = os.environ.get("TMPDIR")
    saved_tempdir = tempfile.tempdir
    saved_core = resource.getrlimit(resource.RLIMIT_CORE)
    yield
    if saved_env is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = saved_env
    tempfile.tempdir = saved_tempdir
    # Raising the hard limit back needs privileges we may not have; soft is enough.
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_CORE, saved_core)


@pytest.mark.parametrize(
    ("path", "fstype"),
    [
        ("/tmp/triaxis", "tmpfs"),
        ("/tmp/triaxis/sub/dir", "tmpfs"),
        ("/tmp/triaxisX", "ext4"),  # prefix match must respect path components
        ("/var/lib/with space/x", "tmpfs"),  # octal-escaped mount point
        ("/data/models", "xfs"),
        ("/home", "ext4"),
    ],
)
def test_filesystem_type_from_mountinfo(path: str, fstype: str) -> None:
    assert filesystem_type(Path(path), MOUNTINFO) == fstype


def test_memory_filesystem_detection() -> None:
    assert is_memory_filesystem(Path("/tmp/triaxis"), MOUNTINFO)
    assert not is_memory_filesystem(Path("/data"), MOUNTINFO)


def test_filesystem_type_without_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime, "MOUNTINFO", tmp_path / "missing")
    assert filesystem_type(tmp_path) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_filesystem_type_live_proc() -> None:
    assert filesystem_type(Path("/")) is not None


def test_configure_tmpdir_on_tmpfs(
    tmp_path: Path, restore_process_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "scratch"
    mountinfo = f"1 0 0:1 / {tmp_path} rw - tmpfs tmpfs rw\n"
    assert configure_tmpdir(target, require_memory=True, mountinfo=mountinfo)
    assert os.environ["TMPDIR"] == str(target)
    assert tempfile.gettempdir() == str(target)
    assert target.stat().st_mode & 0o777 == 0o700


def test_configure_tmpdir_refuses_disk_when_required(
    tmp_path: Path, restore_process_state: None
) -> None:
    mountinfo = f"1 0 0:1 / {tmp_path} rw - ext4 /dev/sda1 rw\n"
    with pytest.raises(RuntimeGuardError, match="tmpfs"):
        configure_tmpdir(tmp_path / "scratch", require_memory=True, mountinfo=mountinfo)


def test_disable_core_dumps(restore_process_state: None) -> None:
    assert disable_core_dumps()
    assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)


def test_harden_process_dev_tolerates_disk(tmp_path: Path, restore_process_state: None) -> None:
    mountinfo = f"1 0 0:1 / {tmp_path} rw - ext4 /dev/sda1 rw\n"
    report = harden_process(Environment.DEV, tmp_path / "t", mountinfo=mountinfo)
    assert report.core_dumps_disabled
    assert not report.tmpdir_is_memory


def test_harden_process_prod_fails_closed_on_disk(
    tmp_path: Path, restore_process_state: None
) -> None:
    mountinfo = f"1 0 0:1 / {tmp_path} rw - ext4 /dev/sda1 rw\n"
    with pytest.raises(RuntimeGuardError):
        harden_process(Environment.PROD, tmp_path / "t", mountinfo=mountinfo)


def test_harden_process_prod_on_tmpfs(tmp_path: Path, restore_process_state: None) -> None:
    mountinfo = f"1 0 0:1 / {tmp_path} rw - tmpfs tmpfs rw\n"
    report = harden_process(Environment.STAGING, tmp_path / "t", mountinfo=mountinfo)
    assert report.tmpdir_is_memory
    if sys.platform.startswith("linux"):
        assert report.not_dumpable


def test_harden_process_prod_fails_if_dumpable_cannot_be_cleared(
    tmp_path: Path, restore_process_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "set_not_dumpable", lambda: False)
    mountinfo = f"1 0 0:1 / {tmp_path} rw - tmpfs tmpfs rw\n"
    with pytest.raises(RuntimeGuardError, match="core dumps"):
        harden_process(Environment.PROD, tmp_path / "t", mountinfo=mountinfo)


def test_set_not_dumpable_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    assert not runtime.set_not_dumpable()


def test_set_not_dumpable_without_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("ctypes.util.find_library", lambda _name: None)
    assert not runtime.set_not_dumpable()
