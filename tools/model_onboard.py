"""Evidence-based model onboarding (ADR 0009, 0011, 0021).

Run on the model-preparation machine (or any trusted machine with access to the
model hub) — **never** from a serving pod. It reads the model card and file
metadata of an exact revision, and only then updates ``models/registry.yaml``:

    python -m tools.model_onboard --model-id whisper-large-v3 \
        --repo openai/whisper-large-v3 --task stt          # dry run: prints evidence
    python -m tools.model_onboard ... --approve --approved-by "Name"   # writes registry

Rules enforced:

- the licence comes from the model card of the pinned revision; only SPDX
  allow-listed licences (Apache-2.0, MIT, BSD, ISC) can be approved here —
  anything else stays a candidate and needs a human approval record (ADR 0009);
- the revision is pinned to the commit SHA the hub reports;
- weights must be ``.safetensors``; each one is recorded with its sha256 and
  size (taken from the hub's LFS metadata, no download needed);
- pickle-style weight files are never recorded (and must not be downloaded:
  fetch with ``--include "*.safetensors" "*.json" "*.txt" "*.model"``).

The hub URL defaults to https://huggingface.co; set ``HF_ENDPOINT`` to use an
internal mirror.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

import httpx
import yaml

from tools.license_gate import NON_COMMERCIAL
from tools.model_registry import DEFAULT_REGISTRY, ModelEntry, load_registry

DEFAULT_ENDPOINT: Final = "https://huggingface.co"
SPDX_BY_HUB_ID: Final = {
    "apache-2.0": "Apache-2.0",
    "mit": "MIT",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "isc": "ISC",
}
PICKLE_SUFFIXES: Final = frozenset({".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle"})
HEADER_MARK: Final = "# Triaxis model and dataset registry"


class OnboardError(Exception):
    """Evidence is missing or the model cannot be approved. Message is safe to print."""


def fetch_metadata(
    repo: str, revision: str | None, client: httpx.Client, endpoint: str
) -> dict[str, Any]:
    path = f"/api/models/{repo}" + (f"/revision/{revision}" if revision else "")
    try:
        resp = client.get(endpoint.rstrip("/") + path, params={"blobs": "true"}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OnboardError(f"cannot read model metadata ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("sha"), str):
        raise OnboardError("model metadata has no revision sha")
    return data


def licence_from(data: dict[str, Any]) -> tuple[str | None, str]:
    """(SPDX id if allow-listed, raw hub licence string)."""
    card = data.get("cardData") or {}
    raw = card.get("license") if isinstance(card, dict) else None
    if not isinstance(raw, str) or not raw:
        raise OnboardError("model card declares no licence")
    return SPDX_BY_HUB_ID.get(raw.lower()), raw


def artefacts_from(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """safetensors artefacts with sha256/size, and the pickle-style files ignored."""
    weights: list[dict[str, Any]] = []
    ignored: list[str] = []
    for sibling in data.get("siblings", []):
        name = sibling.get("rfilename", "")
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in PICKLE_SUFFIXES:
            ignored.append(name)
        elif suffix == ".safetensors":
            lfs = sibling.get("lfs") or {}
            sha, size = lfs.get("sha256"), lfs.get("size") or sibling.get("size")
            if not isinstance(sha, str) or len(sha) != 64 or not isinstance(size, int):
                raise OnboardError(f"no sha256/size for {name}")
            weights.append({"filename": name, "sha256": sha, "size_bytes": size})
    if not weights:
        raise OnboardError("no .safetensors weights in this revision (ADR 0011)")
    return sorted(weights, key=lambda a: a["filename"]), sorted(ignored)


def build_entry(
    existing: ModelEntry, repo: str, data: dict[str, Any], *, approve: bool, today: date
) -> tuple[dict[str, Any], list[str]]:
    """Return the updated registry entry (as a dict) and human-readable findings."""
    spdx, raw_licence = licence_from(data)
    if NON_COMMERCIAL.search(raw_licence):
        raise OnboardError(f"licence '{raw_licence}' is non-commercial/research-only: denied")
    weights, ignored = artefacts_from(data)
    findings = [
        f"revision {data['sha']}",
        f"licence on model card: {raw_licence}"
        + (
            ""
            if spdx
            else " (not on the SPDX allow-list: stays a candidate, needs approval record)"
        ),
        f"{len(weights)} safetensors file(s), {sum(w['size_bytes'] for w in weights) / 1e9:.1f} GB",
    ]
    if ignored:
        findings.append(f"pickle-style files present and NOT recorded: {', '.join(ignored)}")
    entry = existing.model_dump(mode="json", exclude_none=True)
    entry["source"] = {"repository": repo, "revision": data["sha"]}
    entry["version"] = data["sha"][:12]
    entry["artefacts"] = weights
    if spdx:
        entry["license"] = {
            **entry["license"],
            "spdx": spdx,
            "verified_on": today.isoformat(),
            "notes": f"Verified from the model card of revision {data['sha'][:12]}.",
        }
        if approve:
            entry["status"] = "approved"
    elif approve:
        raise OnboardError(f"cannot approve: licence '{raw_licence}' needs a human approval record")
    return entry, findings


def write_registry(path: Path, entry: dict[str, Any]) -> None:
    text = path.read_text("utf-8")
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    raw = yaml.safe_load(text)
    raw["models"] = [entry if m["id"] == entry["id"] else m for m in raw["models"]]
    body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=100)
    path.write_text((header + "\n" if header else "") + body, "utf-8")
    load_registry(path)  # must still validate; raises otherwise


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--model-id", required=True, help="registry id (also the vLLM served model name)"
    )
    parser.add_argument(
        "--repo", required=True, help="hub repository, e.g. openai/whisper-large-v3"
    )
    parser.add_argument(
        "--revision", help="commit sha or tag; default: current main (then pinned to its sha)"
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--approve", action="store_true", help="write status=approved to the registry"
    )
    parser.add_argument(
        "--approved-by",
        help="name recorded in the commit message you make (required with --approve)",
    )
    args = parser.parse_args(argv)

    registry = load_registry(args.registry)
    existing = registry.model(args.model_id)
    if existing is None:
        print(
            f"onboard: '{args.model_id}' is not in the registry; add a candidate entry first",
            file=sys.stderr,
        )
        return 2
    if args.approve and not args.approved_by:
        print("onboard: --approved-by is required with --approve", file=sys.stderr)
        return 2
    endpoint = os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT)
    with client or httpx.Client() as http:
        try:
            data = fetch_metadata(args.repo, args.revision, http, endpoint)
            entry, findings = build_entry(
                existing, args.repo, data, approve=args.approve, today=datetime.now(UTC).date()
            )
        except OnboardError as exc:
            print(f"onboard: {exc}", file=sys.stderr)
            return 1
    for line in findings:
        print(f"onboard: {line}")
    if not args.approve:
        print(
            "onboard: dry run; nothing written. "
            "Re-run with --approve --approved-by NAME to record approval."
        )
        return 0
    write_registry(args.registry, entry)
    print(
        f"onboard: {args.model_id} approved in {args.registry} (by {args.approved_by}). "
        "Run the licence gate, then commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
