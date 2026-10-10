"""Run the demo locally: ``uv run python -m triaxis_demo`` → http://127.0.0.1:8080.

Inference mode (ADR 0021) comes from the environment:

    TRIAXIS_INFERENCE_MODE=mock            # default: labelled simulated adapters
    TRIAXIS_INFERENCE_MODE=real            # OpenAI-compatible self-hosted endpoints
    TRIAXIS_LLM_URL=http://127.0.0.1:8000  # e.g. vLLM (in-cluster/loopback only)
    TRIAXIS_STT_URL=http://127.0.0.1:8001  # e.g. faster-whisper-server
    TRIAXIS_TTS_URL=http://127.0.0.1:8002  # OpenAI-compatible speech server

In real mode every model id selected by the tenant must be ``approved`` in
``models/registry.yaml`` with a verified commercial licence; otherwise
requests fail with ``model_unavailable`` (no fallback to mock).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

import httpx
import uvicorn

from triaxis_common.inference import (
    EndpointConfig,
    InferenceClients,
    InferenceSettings,
    build_real_clients,
)
from triaxis_common.logging import configure_logging
from triaxis_common.runtime import harden_process
from triaxis_common.taxonomy import InferenceMode
from triaxis_common.types import Environment
from triaxis_demo.app import DEFAULT_REPO, create_app


def inference_from_env(
    env: Mapping[str, str], repo: Path = DEFAULT_REPO
) -> InferenceClients | None:
    """Real adapters when ``TRIAXIS_INFERENCE_MODE=real``; ``None`` means mock."""
    mode = InferenceMode(env.get("TRIAXIS_INFERENCE_MODE", "mock"))
    if mode is InferenceMode.MOCK:
        return None

    def endpoint(name: str) -> EndpointConfig | None:
        url = env.get(f"TRIAXIS_{name}_URL")
        return EndpointConfig(base_url=url) if url else None

    settings = InferenceSettings(
        mode=mode, llm=endpoint("LLM"), stt=endpoint("STT"), tts=endpoint("TTS")
    )
    from tools.model_registry import RegistryApproval  # noqa: PLC0415 - repo tooling

    approval = RegistryApproval.from_file(repo / "models" / "registry.yaml")
    return build_real_clients(settings, httpx.AsyncClient(), approval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Triaxis client-demo prototype")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TRIAXIS_DEMO_PORT", "8080"))
    )
    args = parser.parse_args(argv)
    harden_process(Environment.DEV, Path(os.environ.get("TRIAXIS_TMPDIR", "/tmp/triaxis")))  # noqa: S108
    configure_logging("demo")
    backup_dir = os.environ.get("TRIAXIS_BACKUP_DIR")
    app = create_app(
        inference=inference_from_env(os.environ),
        backup_root=Path(backup_dir) if backup_dir else None,
    )
    # Loopback only: the demo is never exposed on a network interface.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
