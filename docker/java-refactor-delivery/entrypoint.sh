#!/usr/bin/env bash
set -euo pipefail

DATASET_ROOT="${DATASET_ROOT:-/opt/dataset/java/delivery_schema}"
RUNS_ROOT="${RUNS_ROOT:-/runs}"
RUN_AS_USER="${RUN_AS_USER:-smell}"
MODEL_EGRESS_ONLY="${MODEL_EGRESS_ONLY:-1}"
MAVEN_OFFLINE_REPOSITORY="${MAVEN_OFFLINE_REPOSITORY:-/opt/buildenv/offline-home/.m2/repository}"
MAVEN_OFFLINE_REPOSITORY_ID="${MAVEN_OFFLINE_REPOSITORY_ID:-local-all}"
MAVEN_OFFLINE_SETTINGS="${MAVEN_OFFLINE_SETTINGS:-/opt/buildenv/maven-offline-settings.xml}"
MAVEN_GLOBAL_SETTINGS="${MAVEN_GLOBAL_SETTINGS:-/opt/buildenv/maven-global-settings.xml}"
MAVEN_USER_SETTINGS="${MAVEN_USER_SETTINGS:-/opt/buildenv/offline-home/.m2/settings.xml}"

check_maven_offline_settings() {
  local candidate
  for candidate in "$MAVEN_GLOBAL_SETTINGS" "$MAVEN_USER_SETTINGS"; do
    if [[ ! -f "$candidate" ]] || ! cmp -s "$MAVEN_OFFLINE_SETTINGS" "$candidate"; then
      echo "Maven settings are not pinned to the bundled offline mirror: $candidate" >&2
      return 1
    fi
  done
}

check_maven_offline_repository() {
  python3 /opt/opencode-refactor/scripts/normalize_maven_offline_repo.py \
    --check \
    --repository "$MAVEN_OFFLINE_REPOSITORY" \
    --repository-id "$MAVEN_OFFLINE_REPOSITORY_ID"
}

ensure_local_hostname() {
  local current_hostname
  current_hostname="$(hostname)"
  if getent hosts "$current_hostname" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$(id -u)" != "0" ]]; then
    echo "Container hostname is not locally resolvable: $current_hostname" >&2
    return 69
  fi
  printf '127.0.0.1\t%s\n' "$current_hostname" >> /etc/hosts
  getent hosts "$current_hostname" >/dev/null 2>&1
}

ensure_local_hostname
check_maven_offline_settings
check_maven_offline_repository

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

if [[ "${1:-}" == "baseline-check" ]]; then
  shift
  mkdir -p "$RUNS_ROOT"
  if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
    chown -R "$RUN_AS_USER:$RUN_AS_USER" "$RUNS_ROOT"
    exec runuser -u "$RUN_AS_USER" -- python3 /opt/opencode-refactor/scripts/self_check_java_baselines.py \
      --report "$RUNS_ROOT/baseline-preflight.json" \
      "$@"
  fi
  exec python3 /opt/opencode-refactor/scripts/self_check_java_baselines.py \
    --report "$RUNS_ROOT/baseline-preflight.json" \
    "$@"
fi

if [[ "${1:-}" == "dependency-audit" ]]; then
  shift
  mkdir -p "$RUNS_ROOT"
  if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
    chown -R "$RUN_AS_USER:$RUN_AS_USER" "$RUNS_ROOT"
    exec runuser -u "$RUN_AS_USER" -- \
      python3 /opt/opencode-refactor/scripts/audit_java_image_dependencies.py "$@"
  fi
  exec python3 /opt/opencode-refactor/scripts/audit_java_image_dependencies.py "$@"
fi

if [[ "$MODEL_EGRESS_ONLY" != "1" ]]; then
  echo "MODEL_EGRESS_ONLY must remain enabled for this delivery image." >&2
  exit 64
fi

if [[ ! -d "$DATASET_ROOT" ]]; then
  echo "Dataset directory not found: $DATASET_ROOT" >&2
  exit 66
fi

if [[ "${1:-}" == "benchmark-worker" ]]; then
  shift
  if [[ "$(id -u)" != "0" ]]; then
    echo "benchmark-worker must start as root so the delivery entrypoint can prepare permissions and drop privileges." >&2
    exit 77
  fi
  if [[ "$RUN_AS_USER" == "root" ]] || ! id "$RUN_AS_USER" >/dev/null 2>&1; then
    echo "benchmark-worker requires a valid non-root RUN_AS_USER; got: $RUN_AS_USER" >&2
    exit 64
  fi

  benchmark_runner="${BENCHMARK_WORKER_SCRIPT:-/control/run_worker.py}"
  benchmark_results_root=""
  benchmark_secret_source=""
  benchmark_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --results-root)
        [[ $# -ge 2 ]] || { echo "--results-root requires a value" >&2; exit 64; }
        benchmark_results_root="$2"
        benchmark_args+=("$1" "$2")
        shift 2
        ;;
      --secret-file)
        [[ $# -ge 2 ]] || { echo "--secret-file requires a value" >&2; exit 64; }
        benchmark_secret_source="$2"
        shift 2
        ;;
      *)
        benchmark_args+=("$1")
        shift
        ;;
    esac
  done

  [[ -f "$benchmark_runner" ]] || { echo "Benchmark worker not found: $benchmark_runner" >&2; exit 66; }
  [[ -n "$benchmark_results_root" ]] || { echo "benchmark-worker requires --results-root" >&2; exit 64; }
  [[ -s "$benchmark_secret_source" ]] || { echo "Benchmark secret file is missing or empty" >&2; exit 66; }
  benchmark_artifact_root="$benchmark_results_root/artifacts"

  benchmark_secret_target="/dev/shm/minimax-api-key.$$.secret"
  install -m 400 -o "$RUN_AS_USER" -g "$RUN_AS_USER" \
    "$benchmark_secret_source" "$benchmark_secret_target"
  benchmark_args+=("--secret-file" "$benchmark_secret_target")

  mkdir -p /tmp/opencode-refactor-worktrees \
    /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data \
    /tmp/idea-cache /tmp/idea-state /tmp/idea-runtime
  if ! mkdir -p "$benchmark_results_root" "$benchmark_artifact_root"; then
    echo "Cannot create benchmark artifact directory: $benchmark_artifact_root" >&2
    exit 73
  fi
  if ! chown -R "$RUN_AS_USER:$RUN_AS_USER" "$benchmark_results_root"; then
    echo "Cannot assign benchmark results to $RUN_AS_USER: $benchmark_results_root" >&2
    exit 73
  fi
  chown -R "$RUN_AS_USER:$RUN_AS_USER" \
    /tmp/opencode-refactor-worktrees \
    /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data \
    /tmp/idea-cache /tmp/idea-state /tmp/idea-runtime
  if ! runuser -u "$RUN_AS_USER" -- test -w "$benchmark_artifact_root"; then
    echo "Benchmark artifact directory is not writable by $RUN_AS_USER: $benchmark_artifact_root" >&2
    exit 73
  fi

  exec runuser -u "$RUN_AS_USER" -- \
    env SMELL_ARTIFACT_ROOT="$benchmark_artifact_root" \
    python3 "$benchmark_runner" "${benchmark_args[@]}"
fi

mkdir -p "$RUNS_ROOT" \
  /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data \
  /tmp/idea-cache /tmp/idea-state /tmp/idea-runtime
if [[ "$(id -u)" == "0" && "$RUN_AS_USER" != "root" ]]; then
  chown -R "$RUN_AS_USER:$RUN_AS_USER" \
    "$RUNS_ROOT" \
    /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data \
    /tmp/idea-cache /tmp/idea-state /tmp/idea-runtime
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
echo "       run-java-refactor-delivery benchmark-worker --plan <plan.json> --results-root <dir> --secret-file <file>" >&2
echo "Available datasets:" >&2
find "$DATASET_ROOT" -maxdepth 1 -name '*.csv' -print | sort >&2
exit 64
