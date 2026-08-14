#!/usr/bin/env python3
"""Re-verify one frozen historical production diff without invoking a model.

The caller provides a clean checkout at the frozen project revision, the
historical sample descriptor, and the byte-frozen patch.  This script captures
the current c000 contract before applying the patch and then runs the current
project_full Guard.  It deliberately has no patch-repair or test fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime/python/bridge/smell_bridge.py"


class ReplayError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if process.returncode not in allowed:
        raise ReplayError(
            f"command failed with rc={process.returncode}: {' '.join(command)}\n"
            f"{process.stderr[-4000:]}"
        )
    return process


def _git(project_root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=project_root).stdout.strip()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _bridge_command(
    action: str,
    *,
    sample: dict[str, Any],
    project_root: Path,
    canonical_project_root: str,
    config: Path,
    projects: Path,
    artifact_root: Path,
    baseline_seal: str = "",
) -> list[str]:
    frozen_location = str(sample["location"])
    canonical_prefix = canonical_project_root.rstrip("/")
    if canonical_prefix not in frozen_location:
        raise ReplayError("sample location does not contain the canonical project root")
    replay_location = frozen_location.replace(canonical_prefix, str(project_root))
    command = [
        sys.executable,
        str(BRIDGE),
        action,
        "--output-detail",
        "audit",
        "--project-root",
        str(project_root),
        "--project-override-root",
        canonical_project_root,
        "--config",
        str(config),
        "--projects",
        str(projects),
        "--smell",
        str(sample["smell_type"]),
        "--location",
        replay_location,
        "--language",
        str(sample["language"]),
        "--verification-mode",
        "project_full",
    ]
    evidence = str(sample.get("evidence") or "")
    if evidence:
        command.extend(["--smell-evidence", evidence])
    target_context = str(sample.get("target_context_json") or "").strip()
    if target_context:
        parsed = json.loads(target_context)
        command.extend(
            [
                "--target-context-json",
                json.dumps(parsed, sort_keys=True, separators=(",", ":")),
            ]
        )
    if action == "verify":
        command.extend(["--artifact-root", str(artifact_root)])
        if baseline_seal:
            command.extend(["--baseline-seal", baseline_seal])
    return command


def _parse_bridge_output(process: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{label} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReplayError(f"{label} returned a non-object payload")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--patch-sha256", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--canonical-project-root", required=True)
    parser.add_argument("--project-commit", required=True)
    parser.add_argument(
        "--config",
        default=str(ROOT / "runtime/python/smell_core/defaults/refactor.yaml"),
    )
    parser.add_argument("--projects", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    sample_path = Path(args.sample).resolve()
    patch_path = Path(args.patch).resolve()
    project_root = Path(args.project_root).resolve()
    config = Path(args.config).resolve()
    projects = Path(args.projects).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=False)

    started_at = _utc_now()
    result: dict[str, Any] = {
        "schema": "nonjava-verification-diff-replay/v1",
        "started_at": started_at,
        "success": False,
        "accepted": False,
        "status": "REPLAY_SETUP_FAILED",
        "sample_path": str(sample_path),
        "patch_path": str(patch_path),
        "project_root": str(project_root),
        "canonical_project_root": args.canonical_project_root,
        "project_commit": args.project_commit,
    }
    try:
        if not all(path.is_file() for path in (sample_path, patch_path, config, projects)):
            raise ReplayError("sample, patch, config, or projects file is missing")
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        if not isinstance(sample, dict):
            raise ReplayError("sample descriptor is not an object")
        patch_sha256 = _sha256(patch_path)
        if patch_sha256 != args.patch_sha256:
            raise ReplayError(
                f"historical patch SHA mismatch: expected {args.patch_sha256}, got {patch_sha256}"
            )
        head = _git(project_root, "rev-parse", "HEAD")
        if head != args.project_commit:
            raise ReplayError(f"project commit mismatch: expected {args.project_commit}, got {head}")
        dirty = _git(project_root, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise ReplayError("project checkout is not clean before c000 capture")

        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMELL_ALLOW_TEST_CHANGES": "0",
                "SMELL_REQUIRE_BUILD_TEST": "1",
                "SMELL_CHECKPOINT_ROOT": str(output_root / "checkpoints"),
            }
        )
        baseline_command = _bridge_command(
            "capture-baseline",
            sample=sample,
            project_root=project_root,
            canonical_project_root=args.canonical_project_root,
            config=config,
            projects=projects,
            artifact_root=output_root / "artifacts",
        )
        baseline_process = _run(
            baseline_command,
            cwd=project_root,
            env=environment,
            timeout=args.timeout,
            allowed=(0, 1),
        )
        baseline = _parse_bridge_output(baseline_process, "capture-baseline")
        _write_json(
            output_root / "baseline.json",
            {
                "returncode": baseline_process.returncode,
                "stderr": baseline_process.stderr,
                "payload": baseline,
            },
        )
        baseline_seal = str(baseline.get("baseline_seal") or "")
        if baseline_process.returncode != 0 or not baseline.get("success") or not baseline_seal:
            raise ReplayError(f"baseline capture failed: {baseline.get('status') or baseline}")

        _run(["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)], cwd=project_root)
        _run(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=project_root)
        changed = _git(project_root, "status", "--porcelain=v1", "--untracked-files=all")
        if not changed:
            raise ReplayError("historical patch produced no worktree change")
        (output_root / "applied.status").write_text(changed + "\n", encoding="utf-8")
        current_patch = _run(
            [
                "git",
                "-c",
                "core.quotePath=false",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--diff-algorithm=myers",
                "--no-indent-heuristic",
                "--unified=3",
                "--inter-hunk-context=0",
                args.project_commit,
                "--",
            ],
            cwd=project_root,
        ).stdout
        (output_root / "applied.patch").write_text(
            current_patch,
            encoding="utf-8",
            errors="surrogateescape",
        )

        verify_command = _bridge_command(
            "verify",
            sample=sample,
            project_root=project_root,
            canonical_project_root=args.canonical_project_root,
            config=config,
            projects=projects,
            artifact_root=output_root / "artifacts",
            baseline_seal=baseline_seal,
        )
        verify_process = _run(
            verify_command,
            cwd=project_root,
            env=environment,
            timeout=args.timeout,
            allowed=(0, 1),
        )
        verify = _parse_bridge_output(verify_process, "verify")
        _write_json(output_root / "verify.json", verify)
        (output_root / "verify.stderr.log").write_text(
            verify_process.stderr,
            encoding="utf-8",
            errors="surrogateescape",
        )
        result.update(
            {
                "success": bool(verify.get("success")),
                "accepted": bool(verify.get("accepted")),
                "status": str(verify.get("status") or "VERIFY_FAILED"),
                "bridge_returncode": verify_process.returncode,
                "historical_patch_sha256": patch_sha256,
                "applied_patch_sha256": _sha256(output_root / "applied.patch"),
                "baseline_seal": baseline_seal,
                "sample": {
                    "sample_id": str(sample.get("sample_id") or ""),
                    "language": str(sample.get("language") or ""),
                    "smell_type": str(sample.get("smell_type") or ""),
                    "project_name": str(sample.get("project_name") or ""),
                    "location": str(sample.get("location") or ""),
                },
            }
        )
    except (ReplayError, subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = str(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            result["status"] = "REPLAY_TIMEOUT"
    finally:
        result["finished_at"] = _utc_now()
        _write_json(output_root / "result.json", result)

    return 0 if result.get("accepted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
