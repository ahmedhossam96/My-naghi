#!/usr/bin/env bash
# Sign an image or model artefact with cosign and attach its SBOM as an attestation.
#   tools/sign.sh image <registry/repo@sha256:...> <sbom.cdx.json>
#   tools/sign.sh blob  <path/to/artefact>          <sbom.cdx.json>
#
# The signing key never leaves OpenBao/HSM: COSIGN_KEY_REF must be a KMS reference
# (e.g. hashivault://triaxis-release, served by OpenBao's Transit engine).
# Images must be referenced by digest, never by tag. Transparency-log upload is
# disabled because the platform is air-gapped (no public Rekor).
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 image|blob <ref> <sbom.cdx.json>" >&2
  exit 2
fi
kind="$1"; ref="$2"; sbom="$3"
: "${COSIGN_KEY_REF:?COSIGN_KEY_REF must point at the OpenBao/HSM-held key (e.g. hashivault://triaxis-release)}"

case "$kind" in
  image)
    if [[ "$ref" != *@sha256:* ]]; then
      echo "sign: refusing to sign a tag; pass an image digest (repo@sha256:...)" >&2
      exit 2
    fi
    cosign sign --key "$COSIGN_KEY_REF" --tlog-upload=false --yes "$ref"
    cosign attest --key "$COSIGN_KEY_REF" --tlog-upload=false --yes \
      --type cyclonedx --predicate "$sbom" "$ref"
    ;;
  blob)
    cosign sign-blob --key "$COSIGN_KEY_REF" --tlog-upload=false --yes \
      --bundle "$ref.cosign.bundle" "$ref"
    cosign attest-blob --key "$COSIGN_KEY_REF" --tlog-upload=false --yes \
      --type cyclonedx --predicate "$sbom" --bundle "$ref.sbom.cosign.bundle" "$ref"
    ;;
  *)
    echo "sign: unknown kind '$kind' (expected image or blob)" >&2
    exit 2
    ;;
esac
echo "sign: signed $kind $ref"
