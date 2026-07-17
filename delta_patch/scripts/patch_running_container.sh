#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <running-container-name-or-id>" >&2
  exit 64
fi

CONTAINER="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

docker exec "$CONTAINER" sh -lc '
  set -e
  command -v node >/dev/null
  test "$(node --version)" = "v18.19.1"
  command -v npm >/dev/null
  test "$(opencode --version)" = "1.17.8"
'

docker exec "$CONTAINER" sh -lc 'mkdir -p /opt/opencode-refactor /usr/local/bin'
docker cp "$PATCH_DIR/payload/opencode-refactor/." "$CONTAINER:/opt/opencode-refactor/"
docker cp "$PATCH_DIR/payload/opencode-refactor/docker/java-refactor-delivery/entrypoint.sh" "$CONTAINER:/usr/local/bin/run-java-refactor-delivery"

docker exec "$CONTAINER" sh -lc '
  set -e
  cd /opt/opencode-refactor
  npm ci
  cd /opt/opencode-refactor/.opencode
  npm ci
  cd /opt/opencode-refactor
  chmod +x /usr/local/bin/run-java-refactor-delivery
  chmod +x scripts/run_smell_dataset.py
  chmod +x scripts/self_check_smell_verify.mjs
  python3 -m py_compile runtime/python/bridge/smell_bridge.py scripts/run_smell_dataset.py
  node node_modules/typescript/bin/tsc --noEmit --skipLibCheck --module NodeNext --moduleResolution NodeNext --target ES2022 --types node .opencode/plugins/smell.ts
  python3 runtime/python/bridge/smell_bridge.py verify --help >/dev/null
  node scripts/self_check_smell_verify.mjs >/tmp/smell-verify-self-check.json
'

echo "Patched running container: $CONTAINER"
