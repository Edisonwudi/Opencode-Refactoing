#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entrypoint="$repo_root/docker/mounted-source/entrypoint.sh"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/mounted-source-contract.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/deps/node_modules" "$tmp/deps/opencode-node_modules"
root_hash="$(shasum -a 256 "$repo_root/package-lock.json" | awk '{print $1}')"
opencode_hash="$(shasum -a 256 "$repo_root/.opencode/package-lock.json" | awk '{print $1}')"

AGENT_SOURCE_REQUIRE_READONLY=0 \
MOUNTED_SOURCE_CONTRACT_CHECK_ONLY=1 \
OPENCODE_AGENT_SOURCE="$repo_root" \
OPENCODE_RUNTIME_DEPS="$tmp/deps" \
AGENT_ROOT_LOCK_SHA256="$root_hash" \
AGENT_OPENCODE_LOCK_SHA256="$opencode_hash" \
bash "$entrypoint" >"$tmp/pass.log"
grep -q 'Mounted source dependency contract passed' "$tmp/pass.log"

set +e
AGENT_SOURCE_REQUIRE_READONLY=0 \
MOUNTED_SOURCE_CONTRACT_CHECK_ONLY=1 \
OPENCODE_AGENT_SOURCE="$repo_root" \
OPENCODE_RUNTIME_DEPS="$tmp/deps" \
AGENT_ROOT_LOCK_SHA256="$(printf '0%.0s' {1..64})" \
AGENT_OPENCODE_LOCK_SHA256="$opencode_hash" \
bash "$entrypoint" >"$tmp/mismatch.stdout" 2>"$tmp/mismatch.stderr"
status=$?
set -e
[[ "$status" == "78" ]] || { echo "Expected dependency mismatch exit 78, got $status" >&2; exit 1; }
grep -q 'AGENT_DEPENDENCY_MISMATCH: package-lock.json' "$tmp/mismatch.stderr"

echo "Mounted source contract self-check passed"
