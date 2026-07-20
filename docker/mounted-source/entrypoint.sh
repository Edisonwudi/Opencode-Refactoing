#!/usr/bin/env bash
set -euo pipefail

source_root="${OPENCODE_AGENT_SOURCE:-/agent-src}"
runtime_root="${OPENCODE_AGENT_ROOT:-/opt/opencode-refactor}"
deps_root="${OPENCODE_RUNTIME_DEPS:-/opt/opencode-runtime}"

die() {
  printf '%s\n' "$1" >&2
  exit "${2:-70}"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "No SHA-256 implementation is available" 69
  fi
}

require_lock_match() {
  local file="$1"
  local expected="$2"
  local label="$3"
  [[ -n "$expected" ]] || die "Missing expected lockfile hash: $label" 70
  [[ -f "$file" ]] || die "Mounted agent source is missing lockfile: $file" 66
  local actual
  actual="$(sha256_file "$file")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'AGENT_DEPENDENCY_MISMATCH: %s expected=%s actual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 78
  fi
}

[[ -d "$source_root" ]] || die "Mounted agent source directory not found: $source_root" 66

required_dirs=(.opencode runtime scripts docker/java-refactor-delivery)
required_files=(package.json package-lock.json opencode.json .opencode/package-lock.json)
for path in "${required_dirs[@]}"; do
  [[ -d "$source_root/$path" ]] || die "Mounted agent source is missing directory: $path" 66
done
for path in "${required_files[@]}"; do
  [[ -f "$source_root/$path" ]] || die "Mounted agent source is missing file: $path" 66
done

if [[ "${AGENT_SOURCE_REQUIRE_READONLY:-1}" == "1" ]]; then
  command -v findmnt >/dev/null 2>&1 || die "findmnt is required to verify the read-only source mount" 69
  mount_options="$(findmnt -n -o OPTIONS -T "$source_root" 2>/dev/null || true)"
  [[ ",$mount_options," == *,ro,* ]] || die "Mounted agent source must be read-only: $source_root" 77
fi

require_lock_match \
  "$source_root/package-lock.json" \
  "${AGENT_ROOT_LOCK_SHA256:-}" \
  "package-lock.json"
require_lock_match \
  "$source_root/.opencode/package-lock.json" \
  "${AGENT_OPENCODE_LOCK_SHA256:-}" \
  ".opencode/package-lock.json"

[[ -d "$deps_root/node_modules" ]] || die "Runtime dependency directory is missing: $deps_root/node_modules" 70
[[ -d "$deps_root/opencode-node_modules" ]] || die "OpenCode dependency directory is missing: $deps_root/opencode-node_modules" 70

if [[ "${MOUNTED_SOURCE_CONTRACT_CHECK_ONLY:-0}" == "1" ]]; then
  echo "Mounted source dependency contract passed"
  exit 0
fi

cd /
rm -rf "$runtime_root"
install -d "$runtime_root/.opencode" "$runtime_root/docker"

for path in runtime scripts docker/java-refactor-delivery; do
  cp -a "$source_root/$path" "$runtime_root/$(dirname "$path")/"
done
for path in agents commands plugins skills package.json package-lock.json .gitignore; do
  if [[ -e "$source_root/.opencode/$path" ]]; then
    cp -a "$source_root/.opencode/$path" "$runtime_root/.opencode/$path"
  fi
done
for path in package.json package-lock.json opencode.json; do
  cp "$source_root/$path" "$runtime_root/$path"
done

ln -s "$deps_root/node_modules" "$runtime_root/node_modules"
ln -s "$deps_root/opencode-node_modules" "$runtime_root/.opencode/node_modules"

export PROJECT_REVISIONS="${PROJECT_REVISIONS:-$deps_root/project-revisions.json}"
export SMELL_PROJECTS="${SMELL_PROJECTS_OVERRIDE:-${SMELL_PROJECTS:-$deps_root/projects.docker.yaml}}"
[[ -f "$PROJECT_REVISIONS" ]] || die "Project revision manifest is missing: $PROJECT_REVISIONS" 70
[[ -f "$SMELL_PROJECTS" ]] || die "Project environment config is missing: $SMELL_PROJECTS" 70

cd "$runtime_root"
exec "$runtime_root/docker/java-refactor-delivery/entrypoint.sh" "$@"
