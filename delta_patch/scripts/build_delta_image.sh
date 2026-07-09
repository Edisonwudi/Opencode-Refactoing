#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <existing-base-image> [new-image-tag]" >&2
  exit 64
fi

BASE_IMAGE="$1"
NEW_IMAGE="${2:-opencode-java-refactor-delivery:0.1.1-patch}"
NODE_VERSION="${NODE_VERSION:-22.22.2}"
NODE_DIST_BASE="${NODE_DIST_BASE:-https://nodejs.org/dist}"
NO_CACHE="${NO_CACHE:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cache_args=()
if [[ "$NO_CACHE" == "1" || "$NO_CACHE" == "true" ]]; then
  cache_args+=(--no-cache)
fi

cd "$PATCH_DIR"

docker build \
  "${cache_args[@]}" \
  -f Dockerfile.delta \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "NODE_VERSION=$NODE_VERSION" \
  --build-arg "NODE_DIST_BASE=$NODE_DIST_BASE" \
  -t "$NEW_IMAGE" \
  .

echo "Built delta image: $NEW_IMAGE"
