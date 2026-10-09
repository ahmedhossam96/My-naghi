"""Run the demo locally: ``uv run python -m triaxis_demo`` → http://127.0.0.1:8080."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from triaxis_common.logging import configure_logging
from triaxis_common.runtime import harden_process
from triaxis_common.types import Environment
from triaxis_demo.app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Triaxis client-demo prototype")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TRIAXIS_DEMO_PORT", "8080"))
    )
    args = parser.parse_args(argv)
    harden_process(Environment.DEV, Path(os.environ.get("TRIAXIS_TMPDIR", "/tmp/triaxis")))  # noqa: S108
    configure_logging("demo")
    # Loopback only: the demo is never exposed on a network interface.
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
