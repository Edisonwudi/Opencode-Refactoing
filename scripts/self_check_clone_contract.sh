#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required=(
  .opencode/agents/java-refactor-agent.md
  .opencode/plugins/smell.ts
  runtime/python/bridge/smell_bridge.py
  scripts/run_smell_dataset.py
  docker/java-refactor-delivery/entrypoint.sh
  docker/mounted-source/entrypoint.sh
  package.json
  package-lock.json
  opencode.json
)
for path in "${required[@]}"; do
  [[ -e "$repo_root/$path" ]] || { echo "Missing cloned agent source: $path" >&2; exit 1; }
done

for forbidden in \
  delta_patch \
  docker/java-refactor-delivery/Dockerfile \
  docker/java-refactor-environment \
  runtime/python/smell_core/defaults/projects.java.docker.yaml; do
  [[ ! -e "$repo_root/$forbidden" ]] || { echo "Obsolete environment build content remains: $forbidden" >&2; exit 1; }
done

bash -n "$repo_root/docker/java-refactor-delivery/entrypoint.sh"
bash -n "$repo_root/docker/mounted-source/entrypoint.sh"
echo "Cloned agent source contract self-check passed"
