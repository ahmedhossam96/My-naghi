"""Zero-retention runtime guards (ADR 0006).

``harden_process()`` runs first thing at service start-up:

- core dumps disabled (``RLIMIT_CORE = 0`` and, on Linux, not dumpable);
- ``TMPDIR`` and ``tempfile.tempdir`` pointed at a tmpfs directory;
- in staging/prod, start-up fails if that directory is not on a tmpfs.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from triaxis_common.types import Environment

PR_SET_DUMPABLE: Final = 4
MOUNTINFO: Final = Path("/proc/self/mountinfo")
_MEMORY_FILESYSTEMS: Final = frozenset({"tmpfs", "ramfs"})


class RuntimeGuardError(RuntimeError):
    """A zero-retention guard could not be established. The process must exit."""


@dataclass(frozen=True, slots=True)
class HardeningReport:
    core_dumps_disabled: bool
    not_dumpable: bool
    tmpdir: Path
    tmpdir_is_memory: bool


def disable_core_dumps() -> bool:
    """Set the core-file size limit to zero. Returns True when applied."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    return soft == 0 and hard == 0


def set_not_dumpable() -> bool:
    """Linux only: ``prctl(PR_SET_DUMPABLE, 0)``. Returns True when applied."""
    if not sys.platform.startswith("linux"):
        return False
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        return False
    libc = ctypes.CDLL(libc_name, use_errno=True)
    return int(libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)) == 0


def _unescape_mount_path(raw: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as octal (\040 etc.).
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 4 <= len(raw) and raw[i + 1 : i + 4].isdigit():
            out.append(chr(int(raw[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def filesystem_type(path: Path, mountinfo: str | None = None) -> str | None:
    """Filesystem type of the mount containing ``path`` (Linux mountinfo)."""
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO.read_text("utf-8")
        except OSError:
            return None
    target = str(path.resolve())
    best_mount = ""
    best_type: str | None = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        right_fields = right.split()
        if len(fields) < 5 or not right_fields:
            continue
        mount_point = _unescape_mount_path(fields[4])
        prefix = mount_point.rstrip("/") + "/"
        covers = target == mount_point or target.startswith(prefix)
        if covers and len(mount_point) >= len(best_mount):
            best_mount = mount_point
            best_type = right_fields[0]
    return best_type


def is_memory_filesystem(path: Path, mountinfo: str | None = None) -> bool:
    return filesystem_type(path, mountinfo) in _MEMORY_FILESYSTEMS


def configure_tmpdir(path: Path, *, require_memory: bool, mountinfo: str | None = None) -> bool:
    """Point ``TMPDIR``/``tempfile.tempdir`` at ``path``. Returns whether it is tmpfs."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    in_memory = is_memory_filesystem(path, mountinfo)
    if require_memory and not in_memory:
        raise RuntimeGuardError("TMPDIR is not on a memory filesystem (tmpfs)")
    os.environ["TMPDIR"] = str(path)
    tempfile.tempdir = str(path)
    return in_memory


def harden_process(
    environment: Environment, tmpdir: Path, *, mountinfo: str | None = None
) -> HardeningReport:
    """Apply all guards. Fails closed in staging/prod."""
    strict = environment.is_production_like
    core = disable_core_dumps()
    dumpable_off = set_not_dumpable()
    if strict and not (core and dumpable_off):
        raise RuntimeGuardError("core dumps could not be disabled")
    in_memory = configure_tmpdir(tmpdir, require_memory=strict, mountinfo=mountinfo)
    return HardeningReport(
        core_dumps_disabled=core,
        not_dumpable=dumpable_off,
        tmpdir=tmpdir,
        tmpdir_is_memory=in_memory,
    )
