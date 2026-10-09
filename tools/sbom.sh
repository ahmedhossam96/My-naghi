#!/usr/bin/env bash
# Generate a CycloneDX SBOM with syft.
#   tools/sbom.sh <source> <output.cdx.json>
#   source: dir:<path> | registry:<image@digest> | oci-dir:<path>
# Uses a local `syft` if present, otherwise the pinned container image in $SYFT_IMAGE.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <source> <output.cdx.json>" >&2
  exit 2
fi
source_ref="$1"
output="$2"
SYFT_IMAGE="${SYFT_IMAGE:-docker.io/anchore/syft:v1.18.1}"

if command -v syft >/dev/null 2>&1; then
  syft scan "$source_ref" -o "cyclonedx-json=$output"
else
  out_dir="$(cd "$(dirname "$output")" && pwd)"
  out_name="$(basename "$output")"
  # Mount the repository read-only; only the output directory is writable.
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$PWD:/src:ro" -v "$out_dir:/out" -w /src \
    "$SYFT_IMAGE" scan "$source_ref" -o "cyclonedx-json=/out/$out_name"
fi
echo "sbom: wrote $output"
