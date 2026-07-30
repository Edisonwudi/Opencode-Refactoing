#!/usr/bin/env python3
"""Audit a Java delivery image's dependency closure without calling a model.

This is intentionally an orchestration layer over ``self_check_java_baselines.py``.
The baseline checker remains the single source of truth for dataset selection,
project revisions, build commands, test commands, JDKs, and verification modes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE_SCRIPT = ROOT / "scripts" / "self_check_java_baselines.py"

OFFLINE_MISSING_PATTERNS = (
    re.compile(r"no cached version(?: of .+)? available for offline mode", re.IGNORECASE),
    re.compile(r"cannot access .+ in offline mode", re.IGNORECASE),
    re.compile(r"has not been downloaded from it before", re.IGNORECASE),
    re.compile(r"was not found in .+ during a previous attempt", re.IGNORECASE),
    re.compile(r"not available in offline mode", re.IGNORECASE),
)
RESOLUTION_FAILURE_PATTERNS = (
    re.compile(
        r"could not resolve (?:all )?(?:files|artifacts|dependencies|plugin)",
        re.IGNORECASE,
    ),
    re.compile(r"could not find artifact", re.IGNORECASE),
    re.compile(r"pluginresolutionexception", re.IGNORECASE),
    re.compile(r"dependencyresolutionexception", re.IGNORECASE),
    re.compile(r"plugin .+ was not found in any of the following sources", re.IGNORECASE),
)
TOOLCHAIN_MISSING_PATTERNS = (
    re.compile(r"misconfigured toolchains", re.IGNORECASE),
    re.compile(r"non-existing JDK home configuration", re.IGNORECASE),
    re.compile(r"cannot find matching toolchain", re.IGNORECASE),
    re.compile(r"no matching toolchains found", re.IGNORECASE),
    re.compile(r"toolchain.+(?:jdk|java).+(?:missing|not found|does not exist)", re.IGNORECASE),
)
COORDINATE_PATTERN = re.compile(
    r"(?<![\w/])"
    r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+"
    r"(?::[A-Za-z0-9_.+-]+){0,2}"
    r"(?![\w/])"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute every unique Java build/test plan without a model and report "
            "offline dependency gaps separately from build or test failures."
        )
    )
    parser.add_argument(
        "--dataset-root",
        default="/opt/dataset/java/delivery_schema",
        help="Dataset directory consumed by the delivery image.",
    )
    parser.add_argument("--config", default="")
    parser.add_argument("--projects", default="")
    parser.add_argument(
        "--project-revisions",
        default=os.environ.get(
            "PROJECT_REVISIONS", "/opt/opencode-refactor/project-revisions.json"
        ),
    )
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--smell", action="append", default=[])
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--limit-samples", type=int, default=0)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of unique plans to execute concurrently. Default: 1.",
    )
    parser.add_argument(
        "--plan-timeout-seconds",
        type=int,
        default=1800,
        help="Hard timeout for one unique build/test plan. Default: 1800.",
    )
    parser.add_argument(
        "--output",
        default="/runs/image-dependency-audit",
        help="Audit artifact directory.",
    )
    parser.add_argument(
        "--baseline-script",
        default=str(DEFAULT_BASELINE_SCRIPT),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only materialize and list unique execution plans.",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.plan_timeout_seconds < 1:
        parser.error("--plan-timeout-seconds must be at least 1")
    return args


def _common_baseline_args(args: argparse.Namespace) -> list[str]:
    result = [
        "--dataset-root",
        args.dataset_root,
        "--project-revisions",
        args.project_revisions,
    ]
    for option in ("config", "projects"):
        value = str(getattr(args, option) or "")
        if value:
            result.extend((f"--{option}", value))
    for option in ("project", "smell", "sample_id"):
        for value in getattr(args, option):
            result.extend((f"--{option.replace('_', '-')}", str(value)))
    if args.limit_samples:
        result.extend(("--limit-samples", str(args.limit_samples)))
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _collect_text(result: dict[str, Any], plan_dir: Path) -> str:
    chunks: list[str] = []
    for key in ("message", "stdout", "stderr"):
        value = result.get(key)
        if value:
            chunks.append(str(value))
    status = str(result.get("status") or "")
    if status == "build_failed":
        failed_phases = ("build",)
    elif status == "test_failed":
        failed_phases = ("test",)
    else:
        failed_phases = ()
    for phase_name in failed_phases:
        phase = result.get(phase_name)
        if not isinstance(phase, dict):
            continue
        for key in ("summary_text", "output_tail"):
            value = phase.get(key)
            if value:
                chunks.append(str(value))
        for key in ("failure_highlights", "diagnostics", "tail"):
            value = phase.get(key)
            if isinstance(value, list):
                chunks.extend(str(item) for item in value)
    log_names = ["baseline.stdout.log", "baseline.stderr.log"]
    log_names.extend(f"{phase}.log" for phase in failed_phases)
    for log_name in log_names:
        log_path = plan_dir / log_name
        if log_path.is_file():
            chunks.append(log_path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _matching_evidence(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        if any(pattern.search(line) for pattern in patterns):
            seen.add(line)
            evidence.append(line[:1000])
            if len(evidence) == 8:
                break
    return evidence


def classify_dependency_failure(
    result: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Classify one plan conservatively, preserving non-dependency failures."""
    coordinates = sorted(set(COORDINATE_PATTERN.findall(text)))[:20]
    offline_evidence = _matching_evidence(text, OFFLINE_MISSING_PATTERNS)
    resolution_evidence = _matching_evidence(text, RESOLUTION_FAILURE_PATTERNS)
    toolchain_evidence = _matching_evidence(text, TOOLCHAIN_MISSING_PATTERNS)

    infrastructure_statuses = {
        "audit_worker_failed",
        "baseline_runner_failed",
        "checkout_error",
        "commit_error",
        "timeout",
    }
    status = str(result.get("status") or "")
    if result.get("first_pass") is True:
        category = "PASS"
        confidence = "high"
        evidence: list[str] = []
    elif status in infrastructure_statuses:
        category = "INFRA_FAILED"
        confidence = "high"
        evidence = []
    elif offline_evidence:
        category = "OFFLINE_DEPENDENCY_MISSING"
        confidence = "high"
        evidence = offline_evidence + [
            line for line in resolution_evidence if line not in set(offline_evidence)
        ]
        evidence = evidence[:8]
    elif toolchain_evidence:
        category = "BUILD_TOOLCHAIN_MISSING"
        confidence = "high"
        evidence = toolchain_evidence
    elif resolution_evidence:
        category = "DEPENDENCY_RESOLUTION_FAILED"
        confidence = "medium"
        evidence = resolution_evidence
    elif status == "build_failed":
        category = "BUILD_FAILED"
        confidence = "high"
        evidence = []
    elif status == "test_failed":
        category = "TEST_FAILED"
        confidence = "high"
        evidence = []
    else:
        category = "INFRA_FAILED"
        confidence = "medium"
        evidence = []

    if category not in {
        "OFFLINE_DEPENDENCY_MISSING",
        "DEPENDENCY_RESOLUTION_FAILED",
    }:
        coordinates = []
    return {
        "category": category,
        "confidence": confidence,
        "coordinates": coordinates,
        "evidence": evidence,
    }


def _execute_plan(
    *,
    baseline_script: Path,
    common_args: list[str],
    plan: dict[str, Any],
    output_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    execution_id = str(plan["execution_id"])
    plan_dir = output_root / "plans" / execution_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    result_path = plan_dir / "result.json"
    for stale_name in (
        "result.json",
        "build.log",
        "test.log",
        "baseline.stdout.log",
        "baseline.stderr.log",
    ):
        stale_path = plan_dir / stale_name
        if stale_path.is_file():
            stale_path.unlink()
    command = [
        sys.executable,
        str(baseline_script),
        *common_args,
        "--deduplicate-execution-plans",
        "--execution-id",
        execution_id,
        "--report",
        str(result_path),
    ]
    started = time.time()
    try:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
        (plan_dir / "baseline.stdout.log").write_text(
            stdout, encoding="utf-8", errors="replace"
        )
        (plan_dir / "baseline.stderr.log").write_text(
            stderr, encoding="utf-8", errors="replace"
        )
        if timed_out:
            result = {
                "execution_id": execution_id,
                "project_name": plan.get("project_name", ""),
                "sample_keys": plan.get("sample_keys", []),
                "status": "timeout",
                "first_pass": False,
                "message": f"plan exceeded {timeout_seconds} seconds",
                "baseline_returncode": process.returncode,
            }
            _write_json(result_path, result)
        elif result_path.is_file():
            result = _read_json(result_path)
        else:
            result = {
                "execution_id": execution_id,
                "project_name": plan.get("project_name", ""),
                "sample_keys": plan.get("sample_keys", []),
                "status": "baseline_runner_failed",
                "first_pass": False,
                "message": "baseline checker did not write its result",
            }
        result["baseline_returncode"] = process.returncode
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "execution_id": execution_id,
            "project_name": plan.get("project_name", ""),
            "sample_keys": plan.get("sample_keys", []),
            "status": "baseline_runner_failed",
            "first_pass": False,
            "message": f"baseline checker execution failed: {exc}",
            "baseline_returncode": None,
        }
        _write_json(result_path, result)

    text = _collect_text(result, plan_dir)
    classification = classify_dependency_failure(result, text)
    return {
        "execution_id": execution_id,
        "project_name": result.get("project_name") or plan.get("project_name", ""),
        "sample_keys": result.get("sample_keys") or plan.get("sample_keys", []),
        "status": result.get("status", ""),
        "first_pass": bool(result.get("first_pass")),
        "elapsed_seconds": round(time.time() - started, 3),
        "baseline_returncode": result.get("baseline_returncode"),
        **classification,
        "artifact_dir": str(plan_dir),
    }


def summarize_results(
    plans: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        category = str(result.get("category") or "INFRA_FAILED")
        counts[category] = counts.get(category, 0) + 1
    confirmed_missing_count = counts.get("OFFLINE_DEPENDENCY_MISSING", 0)
    resolution_failure_count = counts.get("DEPENDENCY_RESOLUTION_FAILED", 0)
    toolchain_missing_count = counts.get("BUILD_TOOLCHAIN_MISSING", 0)
    return {
        "success": len(results) == len(plans) and counts.get("PASS", 0) == len(plans),
        "plan_count": len(plans),
        "completed_plan_count": len(results),
        "confirmed_missing_count": confirmed_missing_count,
        "resolution_failure_count": resolution_failure_count,
        "toolchain_missing_count": toolchain_missing_count,
        "dependency_related_failure_count": (
            confirmed_missing_count
            + resolution_failure_count
            + toolchain_missing_count
        ),
        "category_counts": dict(sorted(counts.items())),
        "results": sorted(results, key=lambda item: str(item.get("execution_id", ""))),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()
    baseline_script = Path(args.baseline_script).resolve()
    if not baseline_script.is_file():
        print(
            f"dependency-audit error=baseline script not found: {baseline_script}",
            file=sys.stderr,
        )
        return 2

    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    plan_manifest_path = output_root / "execution_plan_manifest.json"
    report_path = output_root / "report.json"
    for stale_name in (
        "execution_plan_manifest.json",
        "plan.stdout.log",
        "plan.stderr.log",
        "report.json",
    ):
        stale_path = output_root / stale_name
        if stale_path.is_file():
            stale_path.unlink()
    common_args = _common_baseline_args(args)
    list_command = [
        sys.executable,
        str(baseline_script),
        *common_args,
        "--list-execution-plans",
        "--report",
        str(plan_manifest_path),
    ]
    listed = subprocess.run(list_command, text=True, capture_output=True, check=False)
    (output_root / "plan.stdout.log").write_text(
        listed.stdout, encoding="utf-8", errors="replace"
    )
    (output_root / "plan.stderr.log").write_text(
        listed.stderr, encoding="utf-8", errors="replace"
    )
    if listed.returncode != 0 or not plan_manifest_path.is_file():
        _write_json(
            report_path,
            {
                "schema_version": 1,
                "success": False,
                "complete": False,
                "category": "MATERIALIZATION_FAILED",
                "baseline_returncode": listed.returncode,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        print(
            f"dependency-audit error=execution plan materialization failed "
            f"returncode={listed.returncode} artifacts={output_root}",
            file=sys.stderr,
        )
        return 2

    manifest = _read_json(plan_manifest_path)
    plans = manifest.get("plans")
    if not isinstance(plans, list):
        _write_json(
            report_path,
            {
                "schema_version": 1,
                "success": False,
                "complete": False,
                "category": "INVALID_PLAN_MANIFEST",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        print("dependency-audit error=execution plan manifest has no plans array", file=sys.stderr)
        return 2
    if not plans:
        _write_json(
            report_path,
            {
                "schema_version": 1,
                "success": False,
                "complete": False,
                "category": "EMPTY_SELECTION",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        print(
            "dependency-audit error=selection produced zero execution plans; "
            "refusing to certify an empty dependency closure",
            file=sys.stderr,
        )
        return 2
    if args.list_only:
        print(
            f"dependency-audit list-only samples={manifest.get('sample_count', 0)} "
            f"unique_plans={len(plans)} manifest={plan_manifest_path}"
        )
        return 0

    completed_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                _execute_plan,
                baseline_script=baseline_script,
                common_args=common_args,
                plan=plan,
                output_root=output_root,
                timeout_seconds=args.plan_timeout_seconds,
            ): plan
            for plan in plans
        }
        for future in concurrent.futures.as_completed(futures):
            plan = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "execution_id": str(plan.get("execution_id", "")),
                    "project_name": str(plan.get("project_name", "")),
                    "sample_keys": plan.get("sample_keys", []),
                    "status": "audit_worker_failed",
                    "first_pass": False,
                    "category": "INFRA_FAILED",
                    "confidence": "high",
                    "coordinates": [],
                    "evidence": [str(exc)],
                    "artifact_dir": "",
                }
            completed_results.append(result)
            partial = summarize_results(plans, completed_results)
            partial.update(
                {
                    "schema_version": 1,
                    "complete": len(completed_results) == len(plans),
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "execution_plan_manifest": str(plan_manifest_path),
                }
            )
            _write_json(report_path, partial)
            print(
                f"[{len(completed_results)}/{len(plans)}] "
                f"{result['execution_id'][:12]} project={result['project_name']} "
                f"category={result['category']}",
                flush=True,
            )

    summary = summarize_results(plans, completed_results)
    summary.update(
        {
            "schema_version": 1,
            "complete": True,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 3),
            "jobs": args.jobs,
            "execution_plan_manifest": str(plan_manifest_path),
            "execution_plan_manifest_sha256": hashlib.sha256(
                plan_manifest_path.read_bytes()
            ).hexdigest(),
        }
    )
    _write_json(report_path, summary)
    print(
        f"dependency-audit success={summary['success']} plans={len(plans)} "
        f"confirmed_missing={summary['confirmed_missing_count']} "
        f"resolution_failures={summary['resolution_failure_count']} "
        f"categories={json.dumps(summary['category_counts'], sort_keys=True)} "
        f"report={report_path}"
    )
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
