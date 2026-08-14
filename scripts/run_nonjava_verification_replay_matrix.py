#!/usr/bin/env python3
"""Run a frozen verification-only diff replay matrix serially in Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


LANGUAGES = {"python", "c", "cpp"}
CCACHE_LANGUAGES = {"c", "cpp"}
CCACHE_MOUNT_TARGET = "/var/cache/refactoragent/ccache"
CCACHE_VOLUME_PREFIX = "smell-ccache"


def _docker_volume_component(value: str) -> str:
    component = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    if not component:
        raise ValueError("Docker cache volume component is empty")
    return component


def _ccache_volume_name(
    image: str,
    language: str,
    canonical_project_root: str,
) -> str:
    if language not in CCACHE_LANGUAGES:
        raise ValueError(f"Unsupported ccache language: {language}")
    project = PurePosixPath(canonical_project_root).name
    if not project:
        raise ValueError("Canonical project root has no project name")
    return "-".join(
        (
            CCACHE_VOLUME_PREFIX,
            _docker_volume_component(image),
            language,
            _docker_volume_component(project),
        )
    )


def _docker_runtime_args(
    *,
    image: str,
    language: str,
    canonical_project_root: str,
    cpuset: str,
    memory: str,
) -> list[str]:
    runtime_args = [
        "--network", "none",
        "--cpuset-cpus", cpuset,
        "--memory", memory,
    ]
    if language not in CCACHE_LANGUAGES:
        return runtime_args
    volume = _ccache_volume_name(image, language, canonical_project_root)
    return [
        *runtime_args,
        "--mount",
        f"type=volume,source={volume},target={CCACHE_MOUNT_TARGET}",
        "-e", f"CCACHE_DIR={CCACHE_MOUNT_TARGET}",
        "-e", "CCACHE_UMASK=000",
    ]


def _compiler_cache_manifest(
    image: str,
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scope": "ccache_objects_only",
        "mount_target": CCACHE_MOUNT_TARGET,
        "test_results_shared": False,
        "acceptance_shared": False,
        "volumes": sorted(
            {
                _ccache_volume_name(
                    image,
                    str(job["language"]),
                    str(job["canonical_project_root"]),
                )
                for job in jobs
                if str(job["language"]) in CCACHE_LANGUAGES
            }
        ),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SystemExit(f"invalid {label}: {value}")
    return str(path)


def _validate_job(raw: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SystemExit("each replay job must be an object")
    job = dict(raw)
    job_id = str(job.get("job_id") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", job_id) or job_id in seen:
        raise SystemExit(f"invalid or duplicate job_id: {job_id}")
    seen.add(job_id)
    language = str(job.get("language") or "")
    if language not in LANGUAGES:
        raise SystemExit(f"invalid language for {job_id}: {language}")
    canonical = str(job.get("canonical_project_root") or "")
    if not canonical.startswith("/opt/projects/") or ".." in PurePosixPath(canonical).parts:
        raise SystemExit(f"invalid canonical project root for {job_id}")
    commit = str(job.get("project_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"invalid project commit for {job_id}")
    history = job.get("history")
    if history is None:
        job["history"] = None
        return job
    if not isinstance(history, dict):
        raise SystemExit(f"invalid history record for {job_id}")
    patch_sha = str(history.get("patch_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", patch_sha):
        raise SystemExit(f"invalid historical patch SHA for {job_id}")
    history["sample"] = _safe_relative(str(history.get("sample") or ""), "sample path")
    history["patch"] = _safe_relative(str(history.get("patch") or ""), "patch path")
    return job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--cpuset", default="0-1")
    parser.add_argument("--memory", default="10g")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    history_root = Path(args.history).resolve()
    manifest_path = Path(args.manifest).resolve()
    output_root = Path(args.output_root).resolve()
    replay_script = source / "scripts/replay_nonjava_verification_diff.py"
    if not source.is_dir() or not history_root.is_dir() or not manifest_path.is_file():
        raise SystemExit("source, history, or manifest is missing")
    if not replay_script.is_file():
        raise SystemExit("verification replay script is missing from source")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "nonjava-verification-replay-matrix/v1":
        raise SystemExit("unsupported replay manifest")
    declared_jobs = manifest.get("jobs")
    if not isinstance(declared_jobs, list) or not declared_jobs:
        raise SystemExit("replay manifest has no jobs")
    seen: set[str] = set()
    jobs = [_validate_job(item, seen) for item in declared_jobs]

    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "results").mkdir()
    (output_root / "controller-logs").mkdir()
    frozen_manifest = dict(manifest)
    frozen_manifest.update(
        {
            "manifest_input_sha256": _sha256(manifest_path),
            "source": str(source),
            "history_root": str(history_root),
            "image": args.image,
            "network": "none",
            "concurrency": 1,
            "allow_test_changes": False,
            "model_invoked": False,
            "replay_script_sha256": _sha256(replay_script),
            "compiler_cache": _compiler_cache_manifest(args.image, jobs),
        }
    )
    _write_json(output_root / "manifest.json", frozen_manifest)
    completions = output_root / "completions.tsv"
    completions.write_text(
        "job_id\tlanguage\tsmell\tsample_id\tproject\tdocker_rc\tstatus\taccepted\ttrusted_verify\n",
        encoding="utf-8",
    )
    events = (output_root / "events.jsonl").open("a", encoding="utf-8", buffering=1)
    state = {
        "status": "running",
        "started_at": _utc_now(),
        "total": len(jobs),
        "terminal": 0,
        "trusted_verified": 0,
    }
    _write_json(output_root / "state.json", state)

    stopping = False

    def _request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    status_counts: dict[str, int] = {}
    for job in jobs:
        if stopping:
            break
        job_id = str(job["job_id"])
        result_dir = output_root / "results" / job_id
        events.write(json.dumps({"time": _utc_now(), "event": "job_start", **job}, sort_keys=True) + "\n")
        history = job.get("history")
        docker_rc = -1
        if history is None:
            result_dir.mkdir()
            result = {
                "schema": "nonjava-verification-diff-replay/v1",
                "success": False,
                "accepted": False,
                "status": "NO_HISTORICAL_DIFF",
                "trusted_verify": False,
                "reason": "No frozen historical final patch exists; no patch was synthesized.",
                "job": job,
            }
            _write_json(result_dir / "result.json", result)
        else:
            sample_host = history_root / str(history["sample"])
            patch_host = history_root / str(history["patch"])
            if not sample_host.is_file() or not patch_host.is_file():
                raise SystemExit(f"historical evidence is missing for {job_id}")
            if _sha256(patch_host) != history["patch_sha256"]:
                raise SystemExit(f"historical patch drift for {job_id}")
            container_name = f"verifyreplay-{job_id}"
            log_path = output_root / "controller-logs" / f"{job_id}.log"
            bootstrap = """
set -eu
git clone --quiet --no-hardlinks \"$CANONICAL_ROOT\" /tmp/replay-worktree
git -C /tmp/replay-worktree checkout --quiet --detach \"$PROJECT_COMMIT\"
exec python3 /agent-src/scripts/replay_nonjava_verification_diff.py \\
  --sample \"/history/$SAMPLE_REL\" \\
  --patch \"/history/$PATCH_REL\" \\
  --patch-sha256 \"$PATCH_SHA256\" \\
  --project-root /tmp/replay-worktree \\
  --canonical-project-root \"$CANONICAL_ROOT\" \\
  --project-commit \"$PROJECT_COMMIT\" \\
  --config /agent-src/runtime/python/smell_core/defaults/refactor.yaml \\
  --projects /opt/buildenv/projects.docker.yaml \\
  --output-root \"/run-output/results/$JOB_ID\" \\
  --timeout \"$REPLAY_TIMEOUT\"
""".strip()
            command = [
                "docker", "run", "--rm", "--pull", "never",
                "--name", container_name,
                *_docker_runtime_args(
                    image=args.image,
                    language=str(job["language"]),
                    canonical_project_root=str(job["canonical_project_root"]),
                    cpuset=args.cpuset,
                    memory=args.memory,
                ),
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "SMELL_BUILD_JOBS=1",
                "-e", f"JOB_ID={job_id}",
                "-e", f"CANONICAL_ROOT={job['canonical_project_root']}",
                "-e", f"PROJECT_COMMIT={job['project_commit']}",
                "-e", f"SAMPLE_REL={history['sample']}",
                "-e", f"PATCH_REL={history['patch']}",
                "-e", f"PATCH_SHA256={history['patch_sha256']}",
                "-e", f"REPLAY_TIMEOUT={args.timeout}",
                "-v", f"{source}:/agent-src:ro",
                "-v", f"{history_root}:/history:ro",
                "-v", f"{output_root}:/run-output",
                "--entrypoint", "bash",
                args.image,
                "-lc", bootstrap,
            ]
            with log_path.open("wb") as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
            docker_rc = process.returncode
            result_path = result_dir / "result.json"
            if result_path.is_file():
                result = json.loads(result_path.read_text(encoding="utf-8"))
            else:
                result_dir.mkdir(parents=True, exist_ok=True)
                result = {
                    "schema": "nonjava-verification-diff-replay/v1",
                    "success": False,
                    "accepted": False,
                    "status": "CONTAINER_RESULT_MISSING",
                    "trusted_verify": False,
                }
                _write_json(result_path, result)
            trusted = bool(
                (result_dir / "verify.json").is_file()
                and (result_dir / "baseline.json").is_file()
                and str(result.get("status") or "")
                not in {"REPLAY_SETUP_FAILED", "REPLAY_TIMEOUT", "CONTAINER_RESULT_MISSING"}
            )
            result["trusted_verify"] = trusted
            result["docker_rc"] = docker_rc
            _write_json(result_path, result)

        status = str(result.get("status") or "UNKNOWN")
        accepted = bool(result.get("accepted"))
        trusted = bool(result.get("trusted_verify"))
        with completions.open("a", encoding="utf-8") as stream:
            stream.write(
                "\t".join(
                    [
                        job_id,
                        str(job["language"]),
                        str(job["smell"]),
                        str(job["sample_id"]),
                        str(job["project"]),
                        str(docker_rc),
                        status,
                        str(accepted).lower(),
                        str(trusted).lower(),
                    ]
                )
                + "\n"
            )
        status_counts[status] = status_counts.get(status, 0) + 1
        state["terminal"] = int(state["terminal"]) + 1
        if trusted:
            state["trusted_verified"] = int(state["trusted_verified"]) + 1
        state["status_counts"] = dict(sorted(status_counts.items()))
        state["updated_at"] = _utc_now()
        _write_json(output_root / "state.json", state)
        events.write(
            json.dumps(
                {
                    "time": _utc_now(),
                    "event": "job_finish",
                    "job_id": job_id,
                    "docker_rc": docker_rc,
                    "status": status,
                    "accepted": accepted,
                    "trusted_verify": trusted,
                },
                sort_keys=True,
            )
            + "\n"
        )

    state["status"] = "stopped" if stopping else "finished"
    state["finished_at"] = _utc_now()
    _write_json(output_root / "state.json", state)
    events.close()
    return 0 if not stopping else 2


if __name__ == "__main__":
    raise SystemExit(main())
