#!/usr/bin/env python3
"""Run build then test for every bundled Java sample in an isolated checkout."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.config import (  # noqa: E402
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.guards import run_build_test_guard  # noqa: E402
from smell_core.project_revision import (  # noqa: E402
    DEFAULT_REVISIONS_PATH,
    ProjectRevisionError,
    assert_commit_present,
    load_revisions,
    resolve_revision,
    verify_checkout,
)
from run_smell_dataset import (  # noqa: E402
    Sample as RunnerSample,
    _prepare_worktree,
    _remove_worktree_checkout,
)


@dataclass(frozen=True)
class Sample:
    csv_name: str
    sample_id: str
    smell: str
    project_name: str
    project_path: str
    location: str
    test_file: str
    test_command: str
    target_method: str
    legacy_test_commit: str = ""

    @property
    def key(self) -> str:
        return f"{self.csv_name}:{self.sample_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify every bundled sample in runtime order: create one isolated checkout, "
            "run its configured build, then run its sample test command."
        )
    )
    parser.add_argument(
        "--dataset-root",
        default=os.environ.get("DATASET_ROOT", "/opt/dataset/java/delivery_schema"),
    )
    parser.add_argument("--config", default=os.environ.get("SMELL_CONFIG", ""))
    parser.add_argument("--projects", default=os.environ.get("SMELL_PROJECTS", ""))
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--smell", action="append", default=[])
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--limit-samples", type=int, default=0)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--report", default="runs/baseline-preflight.json")
    parser.add_argument(
        "--commit-policy",
        choices=("audit", "strict"),
        default=os.environ.get("COMMIT_POLICY", "audit"),
        help=(
            "audit: checkout project_commit (pinned), record legacy test_commit status but do "
            "not block build/test. strict: additionally fail TEST_COMMIT_MISSING when a non-empty "
            "legacy test_commit is absent from the repo."
        ),
    )
    parser.add_argument(
        "--project-revisions",
        default=os.environ.get(
            "PROJECT_REVISIONS", "/opt/opencode-refactor/project-revisions.json"
        ),
        help="JSON mapping project_name -> {project_commit, tree_hash, ...} used to pin checkouts.",
    )
    return parser.parse_args()


def load_samples(
    dataset_root: Path,
    projects: set[str],
    smells: set[str],
    sample_ids: set[str],
) -> list[Sample]:
    samples: list[Sample] = []
    csv_paths = sorted(dataset_root.glob("*.csv"))
    if not csv_paths:
        raise ValueError(f"No dataset CSV files found under {dataset_root}")
    required = {"sample_id", "smell_type", "project_name", "project_path", "location", "test_file", "test_command"}
    for csv_path in csv_paths:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = sorted(required - set(reader.fieldnames or []))
            if missing:
                raise ValueError(f"{csv_path} is missing required columns: {', '.join(missing)}")
            for row in reader:
                sample = Sample(
                    csv_name=csv_path.name,
                    sample_id=str(row.get("sample_id") or "").strip(),
                    smell=str(row.get("smell_type") or csv_path.stem).strip(),
                    project_name=str(row.get("project_name") or "").strip(),
                    project_path=str(row.get("project_path") or "").strip(),
                    location=str(row.get("location") or "").strip(),
                    test_file=str(row.get("test_file") or "").strip(),
                    test_command=str(row.get("test_command") or "").strip(),
                    target_method=target_method(row.get("group_occurrences")),
                    legacy_test_commit=str(row.get("test_commit") or "").strip(),
                )
                if projects and sample.project_name not in projects:
                    continue
                if smells and sample.smell not in smells:
                    continue
                if sample_ids and sample.sample_id not in sample_ids:
                    continue
                missing_values = [
                    name
                    for name in ("sample_id", "smell", "project_name", "project_path", "location", "test_command")
                    if not getattr(sample, name)
                ]
                if missing_values:
                    raise ValueError(f"{sample.key} has empty required values: {', '.join(missing_values)}")
                if not Path(sample.project_path).is_dir():
                    raise ValueError(f"{sample.key} project path does not exist: {sample.project_path}")
                validate_test_files(sample)
                samples.append(sample)
    if not samples:
        raise ValueError("No samples matched the requested filters")
    return samples


def validate_test_files(sample: Sample) -> None:
    """Fail when a declared test artifact is absent or not part of project HEAD."""
    project_root = Path(sample.project_path)
    for declared_path in sample.test_file.split(";"):
        relative_path = declared_path.strip()
        if not relative_path:
            continue
        test_path = Path(relative_path)
        if test_path.is_absolute() or ".." in test_path.parts:
            raise ValueError(f"{sample.key} test_file must stay inside the project: {relative_path}")
        if not (project_root / test_path).is_file():
            raise ValueError(f"{sample.key} test_file does not exist: {relative_path}")
        content = (project_root / test_path).read_text(encoding="utf-8", errors="replace")
        if "missing source anchor" in content:
            raise ValueError(
                f"{sample.key} test_file asserts source text instead of behavior: {relative_path}"
            )
        if sample.smell == "long_parameter_list" and sample.target_method:
            method = re.escape(sample.target_method)
            reflection = re.compile(
                rf"get(?:Declared)?Method\(\s*[\"']{method}[\"']\s*,"
            )
            if reflection.search(content):
                raise ValueError(
                    f"{sample.key} test_file binds the refactored method signature: {relative_path}"
                )
        if test_path.as_posix() not in tracked_files(project_root):
            raise ValueError(
                f"{sample.key} test_file is not synchronized in project HEAD: {relative_path}"
            )


def target_method(raw_group_occurrences: object) -> str:
    try:
        parsed = json.loads(str(raw_group_occurrences or "{}"))
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("method") or "").strip()
    return ""


@lru_cache(maxsize=None)
def tracked_files(project_root: Path) -> frozenset[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={project_root}",
            "-C",
            str(project_root),
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Unable to inspect project HEAD at {project_root}: {result.stderr.strip()}"
        )
    return frozenset(result.stdout.splitlines())


def count_distinct_tests(samples: list[Sample]) -> int:
    return len({(str(Path(sample.project_path).resolve()), sample.test_command) for sample in samples})


def resolve(
    sample: Sample,
    refactor_config: Any,
    project_overrides: list[Any],
    *,
    project_override_root: str | None = None,
):
    resolved = resolve_run_config(
        refactor_config=refactor_config,
        project_overrides=project_overrides,
        project_root=sample.project_path,
        project_override_root=project_override_root,
        smell=sample.smell,
        location=sample.location,
        cli_language="java",
        verification_mode="sample_optimized",
        sample_test_location=sample.test_file,
        sample_test_command=sample.test_command,
    )
    if "JAVA_HOME" not in sample.test_command:
        java_home = str(resolved.env.get("JAVA_HOME") or "").strip()
        path = str(resolved.env.get("PATH") or "").strip()
        if not java_home:
            raise ValueError(f"{sample.key} project config does not provide JAVA_HOME")
        if not path.startswith(f"{java_home}/bin:"):
            raise ValueError(f"{sample.key} project PATH does not start with JAVA_HOME/bin")
    return resolved


@contextmanager
def isolated_worktree(sample: Sample, checkout_id: str, *, target_commit: str | None = None):
    canonical_root = Path(sample.project_path).resolve()
    runner_sample = RunnerSample(
        sample_id=checkout_id[:16],
        language="java",
        smell=sample.smell,
        project_name=sample.project_name,
        project_root=canonical_root,
        location=sample.location,
        evidence="",
        raw={},
        test_location=sample.test_file,
        test_command=sample.test_command,
        verification_mode="sample_optimized",
    )
    temp_root = Path(tempfile.mkdtemp(prefix="baseline-preflight-"))
    prepared = _prepare_worktree(runner_sample, temp_root, target_commit=target_commit)
    isolated = Sample(
        csv_name=sample.csv_name,
        sample_id=sample.sample_id,
        smell=sample.smell,
        project_name=sample.project_name,
        project_path=str(prepared.project_root),
        location=prepared.location,
        test_file=prepared.test_location,
        test_command=prepared.test_command,
        target_method=sample.target_method,
    )
    try:
        validate_test_files(isolated)
        yield isolated, str(canonical_root)
    finally:
        _remove_worktree_checkout(canonical_root, prepared.project_root)
        shutil.rmtree(temp_root, ignore_errors=True)


def concise_phase(result: dict[str, Any] | None, phase: str) -> dict[str, Any] | None:
    if result is None:
        return None
    details = result.get("details") or {}
    item = details.get(phase)
    if item is None:
        return None
    return {
        "success": bool(item.get("success")),
        "status": item.get("status"),
        "returncode": item.get("returncode"),
        "command": item.get("command") or "",
        "script": item.get("script") or "",
        "cwd": item.get("cwd") or "",
        "summary_text": item.get("summary_text") or "",
        "failure_highlights": item.get("failure_highlights") or [],
        "diagnostics": item.get("diagnostics") or [],
        "tail": item.get("tail") or [],
        "output_tail": str(item.get("output") or "")[-20000:],
    }


# Project revision pinning is delegated to the shared module
# ``smell_core.project_revision`` so baseline-check and the real refactor runner use
# the identical load/resolve/checkout/verify path. The helpers below are thin wrappers
# kept only to minimize churn at the call sites.

def _legacy_status(sample: "Sample", canonical_root: Path, legacy: str) -> str:
    if not legacy:
        return "LEGACY_EMPTY"
    rc = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(canonical_root),
         "cat-file", "-e", f"{legacy}^{{commit}}"],
        capture_output=True,
    ).returncode
    return "LEGACY_PRESENT" if rc == 0 else "LEGACY_MISSING"


def main() -> int:
    args = parse_args()
    started = time.time()
    samples = load_samples(
        Path(args.dataset_root),
        set(args.project),
        set(args.smell),
        set(args.sample_id),
    )
    if args.limit_samples:
        samples = samples[: args.limit_samples]
    distinct_test_count = count_distinct_tests(samples)

    project_samples: dict[str, Sample] = {}
    for sample in samples:
        project_samples.setdefault(str(Path(sample.project_path).resolve()), sample)

    summary = {
        "success": None,
        "list_only": bool(args.list_only),
        "dataset_root": str(Path(args.dataset_root).resolve()),
        "sample_count": len(samples),
        "project_count": len(project_samples),
        "distinct_test_count": distinct_test_count,
        "execution_order": "sample_build_then_test",
        "projects": [],
        "tests": [],
        "failed_build_sample_keys": [],
        "failed_test_sample_keys": [],
        "failed_sample_keys": [],
    }
    print(
        f"baseline-preflight samples={len(samples)} projects={len(project_samples)} "
        f"distinct_tests={distinct_test_count} execution_order=sample_build_then_test "
        f"list_only={args.list_only}",
        flush=True,
    )
    if args.list_only:
        summary["success"] = True
        _write_report(Path(args.report), summary, started)
        return 0

    refactor_config = load_refactor_config(args.config or None)
    project_overrides = load_project_overrides(args.projects or None)
    try:
        revisions = load_revisions(args.project_revisions)
    except ProjectRevisionError as exc:
        # Manifest-level errors are fatal and fail-fast. Record a single-entry report and exit non-zero.
        summary = {
            "success": False, "list_only": bool(args.list_only),
            "dataset_root": str(Path(args.dataset_root).resolve()),
            "sample_count": len(samples), "project_count": len(project_samples),
            "tests": [], "projects": [], "failed_build_sample_keys": [],
            "failed_test_sample_keys": [], "failed_sample_keys": [],
            "manifest_error": exc.status, "manifest_message": exc.message,
        }
        _write_report(Path(args.report), summary, started)
        print(f"baseline-preflight manifest_error={exc.status}: {exc.message} report={args.report}", flush=True)
        return 1
    project_stats: dict[str, dict[str, Any]] = {}
    for project_path, sample in project_samples.items():
        project_stats[project_path] = {
            "project_name": sample.project_name,
            "project_path": project_path,
            "sample_count": 0,
            "build_pass_count": 0,
            "build_fail_count": 0,
            "test_pass_count": 0,
            "test_fail_count": 0,
        }

    failed_build_sample_keys: set[str] = set()
    failed_test_sample_keys: set[str] = set()
    failed_sample_keys: set[str] = set()
    for index, sample in enumerate(samples, 1):
        project_path = str(Path(sample.project_path).resolve())
        command_hash = hashlib.sha256(sample.test_command.encode("utf-8")).hexdigest()
        checkout_id = hashlib.sha256(f"{sample.key}\0{sample.test_command}".encode("utf-8")).hexdigest()
        canonical_root = Path(sample.project_path).resolve()
        legacy = sample.legacy_test_commit
        legacy_status = _legacy_status(sample, canonical_root, legacy)

        # Resolve the authoritative project_commit via the shared module (no HEAD fallback).
        revision_audit: dict[str, str] = {}
        commit_error = ""
        try:
            rev = resolve_revision(sample.project_name, revisions, args.project_revisions)
            assert_commit_present(canonical_root, rev.project_commit)
            project_commit = rev.project_commit
        except ProjectRevisionError as exc:
            commit_error = exc.status
            project_commit = ""
            revision_audit = {
                "requested_project_commit": "",
                "actual_commit": "",
                "expected_tree_hash": "",
                "actual_tree_hash": "",
                "project_revision_alignment": exc.status,
                "project_revisions_path": args.project_revisions,
            }
        # strict policy: a non-empty legacy test_commit that is missing must fail explicitly.
        if not commit_error and args.commit_policy == "strict" and legacy and legacy_status == "LEGACY_MISSING":
            commit_error = "TEST_COMMIT_MISSING"

        print(
            f"SAMPLE {index}/{len(samples)} key={sample.key} project={sample.project_name} "
            f"hash={command_hash[:12]} policy={args.commit_policy} "
            f"project_commit={project_commit[:12] if project_commit else 'NONE'} "
            f"legacy_test_commit_status={legacy_status} BUILD->TEST",
            flush=True,
        )

        result: dict[str, Any] = {}
        actual_commit = ""
        if commit_error:
            # Do NOT run build/test when the pinned commit is unresolvable; record the failure.
            status = "commit_error"
            build_phase = None
            test_phase = None
            build_success = False
            test_success = False
            success = False
            result = {"success": False, "message": f"commit policy error: {commit_error}"}
        else:
            try:
                with isolated_worktree(sample, checkout_id, target_commit=project_commit) as (isolated, _canonical):
                    # Verify actual commit + tree against the manifest via the shared module.
                    revision_audit = verify_checkout(Path(isolated.project_path), rev)
                    actual_commit = revision_audit.get("actual_commit", "")
                    resolved = copy.deepcopy(
                        resolve(
                            isolated,
                            refactor_config,
                            project_overrides,
                            project_override_root=_canonical,
                        )
                    )
                    resolved.defaults.run_build = True
                    resolved.defaults.run_tests = True
                    result = run_build_test_guard(resolved)
            except ProjectRevisionError as exc:
                commit_error = exc.status
                status = "commit_error"
                build_phase = None
                test_phase = None
                build_success = False
                test_success = False
                success = False
                result = {"success": False, "message": f"{exc.status}: {exc.message}"}
                revision_audit = revision_audit or {
                    "requested_project_commit": rev.project_commit,
                    "actual_commit": "",
                    "expected_tree_hash": rev.expected_tree_hash,
                    "actual_tree_hash": "",
                    "project_revision_alignment": exc.status,
                    "project_revisions_path": args.project_revisions,
                }
            except Exception as exc:  # noqa: BLE001
                status = "checkout_error"
                build_phase = None
                test_phase = None
                build_success = False
                test_success = False
                success = False
                result = {"success": False, "message": f"checkout error: {exc}"}
                commit_error = commit_error or "CHECKOUT_ERROR"

        if not commit_error:
            build_phase = concise_phase(result, "build")
            test_phase = concise_phase(result, "test")
            build_success = bool(
                build_phase and build_phase.get("success") and build_phase.get("status") == "ok"
            )
            test_success = bool(build_success and test_phase and test_phase.get("success"))
            success = bool(result.get("success")) and build_success and test_success
            status = "pass" if success else ("build_failed" if not build_success else "test_failed")
        stats = project_stats[project_path]
        stats["sample_count"] += 1
        if commit_error:
            stats["build_fail_count"] += 1
            failed_build_sample_keys.add(sample.key)
            failed_sample_keys.add(sample.key)
        else:
            stats["build_pass_count" if build_success else "build_fail_count"] += 1
            if build_success:
                stats["test_pass_count" if test_success else "test_fail_count"] += 1
            if not build_success:
                failed_build_sample_keys.add(sample.key)
            elif not test_success:
                failed_test_sample_keys.add(sample.key)
            if not success:
                failed_sample_keys.add(sample.key)
        print(
            f"SAMPLE {'PASS' if success else 'FAIL'} key={sample.key} "
            f"build={'PASS' if build_success else 'FAIL'} "
            f"test={'PASS' if test_success else ('SKIP' if not build_success else 'FAIL')} "
            f"actual_commit={actual_commit[:12] if actual_commit else '-'} "
            f"commit_error={commit_error or '-'}",
            flush=True,
        )
        summary["tests"].append(
            {
                "sample_key": sample.key,
                "project_name": sample.project_name,
                "project_path": project_path,
                "command_hash": command_hash,
                "sample_keys": [sample.key],
                "status": status,
                "success": success,
                "message": result.get("message") or "",
                "build": build_phase,
                "test": test_phase,
                "legacy_test_commit": legacy,
                "legacy_test_commit_status": legacy_status,
                "project_commit": project_commit,
                "actual_commit": revision_audit.get("actual_commit", ""),
                "expected_tree_hash": revision_audit.get("expected_tree_hash", ""),
                "actual_tree_hash": revision_audit.get("actual_tree_hash", ""),
                "commit_policy": args.commit_policy,
                "project_revision_alignment": revision_audit.get("project_revision_alignment", ""),
                "project_revisions_path": revision_audit.get("project_revisions_path", ""),
                "source_image_id": revision_audit.get("source_image_id", ""),
                "source_image_tag": revision_audit.get("source_image_tag", ""),
                "delivery_image_tag": revision_audit.get("delivery_image_tag", ""),
                "delivery_image_id_source": revision_audit.get("delivery_image_id_source", "external_attestation"),
                "commit_error": commit_error,
            }
        )

    summary["projects"] = [project_stats[path] for path in sorted(project_stats)]
    summary["failed_build_sample_keys"] = sorted(failed_build_sample_keys)
    summary["failed_test_sample_keys"] = sorted(failed_test_sample_keys)
    summary["failed_sample_keys"] = sorted(failed_sample_keys)
    summary["success"] = not failed_sample_keys
    _write_report(Path(args.report), summary, started)
    print(
        f"baseline-preflight success={summary['success']} "
        f"failed_build_samples={len(failed_build_sample_keys)} "
        f"failed_test_samples={len(failed_test_sample_keys)} "
        f"failed_samples={len(failed_sample_keys)} report={args.report}",
        flush=True,
    )
    return 0 if summary["success"] else 1


def _write_report(path: Path, summary: dict[str, Any], started: float) -> None:
    summary["elapsed_seconds"] = round(time.time() - started, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
