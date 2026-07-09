#!/usr/bin/env bash
set -euo pipefail

DATASET_ROOT="${DATASET_ROOT:-/opt/dataset/java/delivery_schema}"
RUNS_ROOT="${RUNS_ROOT:-/runs}"
RUN_AS_USER="${RUN_AS_USER:-smell}"
MODEL_EGRESS_ONLY="${MODEL_EGRESS_ONLY:-1}"

prepare_opencode_auth_json_for_run_user() {
  local source="${OPENCODE_AUTH_JSON:-}"
  if [[ -z "$source" ]]; then
    source="/opt/buildenv/offline-home/.local/share/opencode/auth.json"
  fi
  if [[ ! -f "$source" ]]; then
    return 0
  fi
  if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
    local target_dir="/tmp/opencode-auth/$RUN_AS_USER"
    local target="$target_dir/auth.json"
    install -d -m 700 -o "$RUN_AS_USER" -g "$RUN_AS_USER" "$target_dir"
    cp "$source" "$target"
    chown "$RUN_AS_USER:$RUN_AS_USER" "$target"
    chmod 600 "$target"
    export OPENCODE_AUTH_JSON="$target"
    return 0
  fi
  export OPENCODE_AUTH_JSON="$source"
}

if [[ "${1:-}" == "bash" || "${1:-}" == "sh" ]]; then
  exec "$@"
fi

if [[ "${1:-}" == "self-check" || "${1:-}" == "smell-verify-self-check" ]]; then
  shift
  exec node /opt/opencode-refactor/scripts/self_check_smell_verify.mjs --require-dataset "$@"
fi

if [[ "$MODEL_EGRESS_ONLY" != "1" ]]; then
  echo "MODEL_EGRESS_ONLY must remain enabled for this delivery image." >&2
  exit 64
fi

if [[ ! -d "$DATASET_ROOT" ]]; then
  echo "Dataset directory not found: $DATASET_ROOT" >&2
  exit 66
fi

mkdir -p "$RUNS_ROOT" /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data
if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
  chown -R "$RUN_AS_USER:$RUN_AS_USER" "$RUNS_ROOT" /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data
elif [[ ! -w "$RUNS_ROOT" ]]; then
  echo "Runs directory is not writable: $RUNS_ROOT" >&2
  exit 73
fi

prepare_opencode_auth_json_for_run_user

if [[ $# -gt 0 ]]; then
  if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
    exec runuser -u "$RUN_AS_USER" -- python3 /opt/opencode-refactor/scripts/run_smell_dataset.py \
      --runs-root "$RUNS_ROOT" \
      --idea-refactor-cli "${IDEA_REFACTOR_CLI:-/opt/idea-refactoring/bin/idea-refactor}" \
      "$@"
  fi
  exec python3 /opt/opencode-refactor/scripts/run_smell_dataset.py \
    --runs-root "$RUNS_ROOT" \
    --idea-refactor-cli "${IDEA_REFACTOR_CLI:-/opt/idea-refactoring/bin/idea-refactor}" \
    "$@"
fi

echo "Usage: run-java-refactor-delivery --dataset /opt/dataset/java/delivery_schema/<smell>.csv --model <provider/model> [filters]" >&2
echo "Available datasets:" >&2
find "$DATASET_ROOT" -maxdepth 1 -name '*.csv' -print | sort >&2
exit 64
