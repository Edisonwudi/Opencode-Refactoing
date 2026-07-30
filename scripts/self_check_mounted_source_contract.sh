#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entrypoint="$repo_root/docker/mounted-source/entrypoint.sh"
dockerfile="$repo_root/docker/java-refactor-delivery/Dockerfile.mounted-source"
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

grep -q '^FROM ${DEPENDENCY_SOURCE_IMAGE} AS dependency_source$' "$dockerfile"
grep -q '^FROM ${DEPENDENCY_CLOSURE_IMAGE} AS dependency_closure$' "$dockerfile"
grep -q '^FROM ${BASE_ENV_IMAGE}$' "$dockerfile"
grep -q '^ARG BASE_ENV_IMAGE=opencode-smell-opencode:0.1.0-amd64$' "$dockerfile"
grep -q 'COPY docker/mounted-source/entrypoint.sh /usr/local/bin/run-mounted-opencode-agent' "$dockerfile"
grep -q 'org.opencontainers.refactor.agent-source-mode="mounted-readonly"' "$dockerfile"
grep -q 'org.opencontainers.refactor.idea-support="absent"' "$dockerfile"
grep -q 'SMELL_PROJECTS=/opt/opencode-runtime/projects.docker.yaml' "$dockerfile"
grep -q 'COPY --from=dependency_source /etc/gitconfig /etc/gitconfig' "$dockerfile"
grep -q 'COPY --from=dependency_closure /opt/buildenv/ /opt/buildenv/' "$dockerfile"
grep -q 'COPY --from=dependency_source /opt/projects/ /opt/projects/' "$dockerfile"
grep -q 'test ! -e /opt/opencode-refactor' "$dockerfile"
grep -q 'test ! -e /opt/idea' "$dockerfile"
grep -q 'test ! -e /opt/idea-refactoring' "$dockerfile"
if grep -Eq '^COPY (\.opencode|runtime/python|scripts|docker/java-refactor-delivery/entrypoint\.sh)' "$dockerfile"; then
  echo "Environment image must not copy Agent source" >&2
  exit 1
fi
if grep -Fqx 'ENTRYPOINT ["/usr/local/bin/run-java-refactor-delivery"]' "$dockerfile"; then
  echo "Environment image must enter through the mounted-source contract" >&2
  exit 1
fi

echo "Mounted source contract self-check passed"
