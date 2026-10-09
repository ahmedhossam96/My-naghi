"""Block model weights, pickles and other large binaries from commits (ADR 0011).

    python -m tools.check_no_weights FILE...    # pre-commit passes staged files
    python -m tools.check_no_weights --all      # every file tracked by git

A file is rejected if it has a weight/pickle suffix, starts with a known weight
or serialisation magic number, or is larger than the size cap.
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path
from typing import Final

MAX_BYTES: Final = 5 * 1024 * 1024
DENIED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".safetensors",
        ".bin",
        ".pt",
        ".pth",
        ".ckpt",
        ".pkl",
        ".pickle",
        ".joblib",
        ".gguf",
        ".ggml",
        ".onnx",
        ".h5",
        ".hdf5",
        ".keras",
        ".npy",
        ".npz",
        ".tflite",
        ".pb",
        ".mlmodel",
        ".msgpack",
        ".jit",
        ".torchscript",
        ".nemo",
        ".zip",
    }
)
_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"PK\x03\x04", "zip archive (PyTorch checkpoint format)"),
    (b"GGUF", "GGUF weights"),
    (b"\x89HDF\r\n\x1a\n", "HDF5 weights"),
    (b"\x93NUMPY", "NumPy array"),
)


def _looks_like_pickle(head: bytes) -> bool:
    return len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5


def _looks_like_safetensors(head: bytes, size: int) -> bool:
    if len(head) < 10:
        return False
    (header_len,) = struct.unpack("<Q", head[:8])
    return 0 < header_len < size and head[8:10] == b'{"'


def check_file(path: Path) -> str | None:  # noqa: PLR0911 - one return per rule reads best
    """Return a reason if the file must not be committed, else None."""
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    if suffix in DENIED_SUFFIXES:
        return f"weight/serialisation suffix '{suffix}'"
    size = path.stat().st_size
    if size > MAX_BYTES:
        return f"larger than {MAX_BYTES // (1024 * 1024)} MiB"
    with path.open("rb") as fh:
        head = fh.read(16)
    if _looks_like_pickle(head):
        return "pickle data"
    if _looks_like_safetensors(head, size):
        return "safetensors data"
    for magic, label in _MAGIC:
        if head.startswith(magic):
            return label
    return None


def tracked_files(root: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - git from PATH is the developer's own
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [root / name for name in output.decode("utf-8").split("\0") if name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", help="check every tracked file")
    args = parser.parse_args(argv)
    files: list[Path] = tracked_files(Path()) if args.all else args.files
    problems = [(p, reason) for p in files if (reason := check_file(p)) is not None]
    for path, reason in problems:
        print(f"no-weights: {path}: {reason}", file=sys.stderr)
    if not problems:
        print(f"no-weights: {len(files)} files OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
