#!/bin/sh
set -eu

destination="${1:-artifacts/sbom}"
mkdir -p "$destination"
destination="$(cd "$destination" && pwd)"
repo="$(pwd)"

if command -v syft >/dev/null 2>&1; then
  syft "dir:$repo" -o "spdx-json=$destination/source.spdx.json"
else
  docker run --rm \
    -v "$repo:/src:ro" \
    -v "$destination:/out" \
    anchore/syft:v1.18.1 dir:/src -o spdx-json=/out/source.spdx.json
fi

if command -v cyclonedx-py >/dev/null 2>&1 && [ -f backend/requirements.txt ]; then
  cyclonedx-py requirements backend/requirements.txt -o "$destination/python.cdx.json"
fi

echo "SBOM written to $destination"
