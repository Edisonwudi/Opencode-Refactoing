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
    interpolate_command_text,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.guards import run_build_test_guard  # noqa: E402
from smell_core.project_revision import (  # noqa: E402
    DEFAULT_REVISIONS_PATH,
    ProjectRevisionError,
    audit_test_commit,
    assert_commit_present,
    load_revisions,
    resolve_revision,
    verify_checkout,
    verify_test_oracle,
)
from run_smell_dataset import (  # noqa: E402
    Sample as RunnerSample,
    _prepare_worktree,
    _remove_worktree_checkout,
)

# Dedup-execution-plan support: shared paths and constants.
MAVEN_OFFLINE_SETTINGS = "/opt/buildenv/maven-offline-settings.xml"
GRADLE_INIT_RETENTION = "/opt/buildenv/gradle-cache-retention.init.gradle"
EXECUTION_PLAN_SCHEMA_VERSION = 1


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
    test_oracle_sha256: str = ""

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
    parser.add_argument(
        "--list-execution-plans",
        action="store_true",
        help=(
            "Dedup mode: materialize every sample into an authoritative execution plan (keyed by "
            "commit/tree/build/test scripts/env/maven-settings/jdk, NOT the legacy command_hash), "
            "deduplicate, and write execution_plan_manifest.json. Does NOT execute build/test."
        ),
    )
    parser.add_argument(
        "--deduplicate-execution-plans",
        action="store_true",
        help=(
            "Dedup mode: materialize the manifest, then execute exactly one unique plan (the one "
            "selected by --execution-id) once, first-pass, writing full build/test evidence."
        ),
    )
    parser.add_argument(
        "--execution-id",
        default="",
        help="With --deduplicate-execution-plans, select the single plan to execute.",
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
                    test_oracle_sha256=str(row.get("test_oracle_sha256") or "").strip(),
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


def _sha256_file(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _java_version_fingerprint(java_home: str) -> str:
    """Stable hash of the JDK identity at JAVA_HOME (path + `java -version`)."""
    parts: list[str] = [f"JAVA_HOME={java_home}"]
    java_bin = Path(java_home) / "bin" / "java"
    if java_bin.is_file():
        try:
            proc = subprocess.run(
                [str(java_bin), "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
            )
            parts.append(f"java -version rc={proc.returncode}\n{proc.stdout}")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"java -version UNAVAILABLE: {exc}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _normalize_path_relative(text: str, project_root: str, canonical_root: str) -> str:
    """Collapse absolute project/worktree paths to a project-relative placeholder.

    The materialized build/test commands embed the isolated worktree's absolute
    path. Two samples sharing the same logical plan would otherwise hash
    differently because their worktrees live at different temp paths. Rewrite any
    known absolute root back to ``${PROJECT_ROOT}`` so the key is stable.
    """
    out = text
    for anchor in (project_root, canonical_root):
        if anchor:
            out = out.replace(anchor, "${PROJECT_ROOT}")
    return out


@dataclass(frozen=True)
class ExecutionPlan:
    """A fully-materialized, authoritative build/test execution plan.

    The ``execution_id`` is a SHA256 over the stable-ordered serialization of
    every input that can change execution semantics (commit/tree/cwd/build/test
    scripts/env/maven-settings/jdk). It deliberately does NOT include random
    worktree temp paths — those are normalized to ``${PROJECT_ROOT}``.
    """

    execution_id: str
    project_name: str
    project_commit: str
    tree_hash: str
    delivery_image_id: str
    verification_mode: str
    build_command: str
    build_script: str
    test_command: str
    test_script: str
    normalized_cwd: str
    environment: dict[str, str]
    environment_fingerprint: str
    maven_settings_sha256: str
    gradle_init_sha256: str
    jdk_fingerprint: str
    build_source: str
    test_source: str
    sample_keys: list[str]

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "project_name": self.project_name,
            "project_commit": self.project_commit,
            "tree_hash": self.tree_hash,
            "delivery_image_id": self.delivery_image_id,
            "verification_mode": self.verification_mode,
            "build": {"command": self.build_command, "script": self.build_script},
            "test": {"command": self.test_command, "script": self.test_script},
            "normalized_cwd": self.normalized_cwd,
            "environment": dict(self.environment),
            "environment_fingerprint": self.environment_fingerprint,
            "maven_settings_sha256": self.maven_settings_sha256,
            "gradle_init_sha256": self.gradle_init_sha256,
            "jdk_fingerprint": self.jdk_fingerprint,
            "build_source": self.build_source,
            "test_source": self.test_source,
            "sample_keys": list(self.sample_keys),
        }


def materialize_execution_plan(
    sample: Sample,
    rev,
    resolved,
    canonical_root: str,
) -> ExecutionPlan:
    """Build the authoritative, materialized execution plan for one sample.

    Takes the ALREADY-resolved run config (post sample_optimized resolution, so
    test = dataset test_command when present) and normalizes away unstable
    absolute paths so equivalent plans hash identically.
    """
    project_root = str(resolved.project_root)
    build_command = interpolate_command_text(resolved.build.command or "", resolved.project_root)
    build_script = interpolate_command_text(resolved.build.script or "", resolved.project_root)
    test_command = interpolate_command_text(resolved.test.command or "", resolved.project_root)
    test_script = interpolate_command_text(resolved.test.script or "", resolved.project_root)
    # Normalize absolute roots out of every textual field that participates in the key.
    build_command = _normalize_path_relative(build_command, project_root, canonical_root)
    build_script = _normalize_path_relative(build_script, project_root, canonical_root)
    test_command = _normalize_path_relative(test_command, project_root, canonical_root)
    test_script = _normalize_path_relative(test_script, project_root, canonical_root)
    cwd = _normalize_path_relative(str(resolved.cwd), project_root, canonical_root)

    java_home = str(resolved.env.get("JAVA_HOME") or "").strip()
    # Stable, sorted serialization of the environment that affects execution.
    env_for_hash = {k: str(resolved.env.get(k, "")) for k in sorted(resolved.env)}
    environment_fingerprint = hashlib.sha256(
        json.dumps(env_for_hash, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    maven_sha = _sha256_file(MAVEN_OFFLINE_SETTINGS)
    gradle_sha = _sha256_file(GRADLE_INIT_RETENTION)
    jdk_fp = _java_version_fingerprint(java_home)

    blob = {
        "schema": "execution-plan-v1",
        "delivery_image_id": rev.source_image_id,
        "project_name": sample.project_name,
        "actual_commit": rev.project_commit,
        "actual_tree_hash": rev.expected_tree_hash,
        "normalized_cwd": cwd,
        "verification_mode": resolved.verification_mode,
        "build_command": build_command,
        "build_script": build_script,
        "test_command": test_command,
        "test_script": test_script,
        "environment_fingerprint": environment_fingerprint,
        "maven_settings_sha256": maven_sha,
        "gradle_init_sha256": gradle_sha,
        "jdk_fingerprint": jdk_fp,
    }
    execution_id = hashlib.sha256(
        json.dumps(blob, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return ExecutionPlan(
        execution_id=execution_id,
        project_name=sample.project_name,
        project_commit=rev.project_commit,
        tree_hash=rev.expected_tree_hash,
        delivery_image_id=rev.source_image_id,
        verification_mode=resolved.verification_mode,
        build_command=build_command,
        build_script=build_script,
        test_command=test_command,
        test_script=test_script,
        normalized_cwd=cwd,
        environment=dict(env_for_hash),
        environment_fingerprint=environment_fingerprint,
        maven_settings_sha256=maven_sha,
        gradle_init_sha256=gradle_sha,
        jdk_fingerprint=jdk_fp,
        build_source=resolved.build_source,
        test_source=resolved.test_source,
        sample_keys=[sample.key],
    )


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


def build_execution_plan_manifest(
    samples: list[Sample],
    refactor_config: Any,
    project_overrides: list[Any],
    revisions_path: str,
) -> tuple[dict[str, Any], dict[str, ExecutionPlan], list[dict[str, Any]]]:
    """Materialize every sample into an authoritative plan and deduplicate.

    Returns ``(manifest_dict, plans_by_id, materialization_errors)``. The first
    pass ONLY materializes text/env (no build/test execution) so equivalent plans
    collapse regardless of which sample surfaced them.
    """
    revisions = load_revisions(revisions_path)
    plans_by_id: dict[str, ExecutionPlan] = {}
    sample_to_plan: dict[str, str] = {}
    materialization_errors: list[dict[str, Any]] = []
    for sample in samples:
        canonical_root = str(Path(sample.project_path).resolve())
        try:
            rev = resolve_revision(sample.project_name, revisions, revisions_path)
            assert_commit_present(Path(canonical_root), rev.project_commit)
            resolved = resolve(
                sample,
                refactor_config,
                project_overrides,
                project_override_root=canonical_root,
            )
            plan = materialize_execution_plan(sample, rev, resolved, canonical_root)
        except ProjectRevisionError as exc:
            materialization_errors.append(
                {"sample_key": sample.key, "status": exc.status, "message": exc.message}
            )
            continue
        except Exception as exc:  # noqa: BLE001
            materialization_errors.append(
                {"sample_key": sample.key, "status": "MATERIALIZE_ERROR", "message": str(exc)}
            )
            continue
        sample_to_plan[sample.key] = plan.execution_id
        existing = plans_by_id.get(plan.execution_id)
        if existing is None:
            plans_by_id[plan.execution_id] = plan
        else:
            # Same plan already seen: attach this sample key (preserve first-seen plan).
            merged_keys = list(existing.sample_keys) + [sample.key]
            plans_by_id[plan.execution_id] = ExecutionPlan(
                execution_id=existing.execution_id,
                project_name=existing.project_name,
                project_commit=existing.project_commit,
                tree_hash=existing.tree_hash,
                delivery_image_id=existing.delivery_image_id,
                verification_mode=existing.verification_mode,
                build_command=existing.build_command,
                build_script=existing.build_script,
                test_command=existing.test_command,
                test_script=existing.test_script,
                normalized_cwd=existing.normalized_cwd,
                environment=existing.environment,
                environment_fingerprint=existing.environment_fingerprint,
                maven_settings_sha256=existing.maven_settings_sha256,
                gradle_init_sha256=existing.gradle_init_sha256,
                jdk_fingerprint=existing.jdk_fingerprint,
                build_source=existing.build_source,
                test_source=existing.test_source,
                sample_keys=merged_keys,
            )
    delivery_image_id = ""
    if plans_by_id:
        delivery_image_id = next(iter(plans_by_id.values())).delivery_image_id
    manifest = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "delivery_image_id": delivery_image_id,
        "sample_count": len(samples),
        "unique_plan_count": len(plans_by_id),
        "sample_to_plan": sample_to_plan,
        "plans": [p.to_manifest_entry() for p in plans_by_id.values()],
    }
    return manifest, plans_by_id, materialization_errors


def _plan_by_id(plans_by_id: dict[str, ExecutionPlan], execution_id: str) -> ExecutionPlan:
    plan = plans_by_id.get(execution_id)
    if plan is None:
        raise SystemExit(f"execution_id {execution_id} not found in manifest")
    return plan


def _find_representative_sample(samples: list[Sample], plan: ExecutionPlan) -> Sample:
    """Return the first sample in dataset order that belongs to this plan."""
    wanted = set(plan.sample_keys)
    for sample in samples:
        if sample.key in wanted:
            return sample
    raise SystemExit(
        f"no sample found for execution_id {plan.execution_id} "
        f"(sample_keys={plan.sample_keys[:3]}...)"
    )


def execute_single_plan(
    samples: list[Sample],
    plan: ExecutionPlan,
    refactor_config: Any,
    project_overrides: list[Any],
    revisions_path: str,
    report_path: Path,
) -> dict[str, Any]:
    """Execute exactly one unique plan once (first-pass, no retry).

    Creates an isolated worktree pinned to the manifest commit, verifies the
    tree, runs build then test via the shared guard, and writes a full-evidence
    ``plan_result.json`` (complete build/test stdout/stderr, not just tails).
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    sample = _find_representative_sample(samples, plan)
    canonical_root = Path(sample.project_path).resolve()
    started = time.time()
    result: dict[str, Any] = {
        "execution_id": plan.execution_id,
        "project_name": plan.project_name,
        "sample_keys": list(plan.sample_keys),
        "representative_sample_key": sample.key,
        "status": "running",
        "attempt": 1,
        "first_pass": False,
    }
    revisions = load_revisions(revisions_path)
    try:
        rev = resolve_revision(sample.project_name, revisions, revisions_path)
        assert_commit_present(canonical_root, rev.project_commit)
        # Sanity: the manifest plan must match what we resolve now.
        if rev.project_commit != plan.project_commit or rev.expected_tree_hash != plan.tree_hash:
            raise RuntimeError(
                "manifest/revisions drift: "
                f"commit {rev.project_commit[:12]} vs plan {plan.project_commit[:12]}, "
                f"tree {rev.expected_tree_hash[:12]} vs plan {plan.tree_hash[:12]}"
            )
        checkout_id = hashlib.sha256(
            f"plan\0{plan.execution_id}".encode("utf-8")
        ).hexdigest()
        with isolated_worktree(sample, checkout_id, target_commit=rev.project_commit) as (isolated, _canonical):
            revision_audit = verify_checkout(Path(isolated.project_path), rev)
            revision_audit.update(
                verify_test_oracle(
                    Path(isolated.project_path),
                    sample.test_file,
                    sample.test_oracle_sha256,
                )
            )
            resolved = copy.deepcopy(
                resolve(
                    isolated,
                    refactor_config,
                    project_overrides,
                    project_override_root=str(canonical_root),
                )
            )
            resolved.defaults.run_build = True
            resolved.defaults.run_tests = True
            guard_result = run_build_test_guard(resolved)
    except ProjectRevisionError as exc:
        result.update(
            status="commit_error",
            success=False,
            build_success=False,
            test_success=False,
            commit_error=exc.status,
            message=f"{exc.status}: {exc.message}",
            revision_audit={
                "requested_project_commit": exc.extra.get("project_commit") or rev.project_commit,
                "actual_commit": "",
                "expected_tree_hash": rev.expected_tree_hash,
                "actual_tree_hash": "",
                "project_revision_alignment": exc.status,
            },
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return result
    except Exception as exc:  # noqa: BLE001
        result.update(
            status="checkout_error",
            success=False,
            build_success=False,
            test_success=False,
            commit_error="CHECKOUT_ERROR",
            message=str(exc),
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return result

    build_phase = concise_phase(guard_result, "build")
    test_phase = concise_phase(guard_result, "test")
    build_success = bool(
        build_phase and build_phase.get("success") and build_phase.get("status") == "ok"
    )
    test_success = bool(build_success and test_phase and test_phase.get("success"))
    first_pass = bool(build_success and test_success)
    # Preserve COMPLETE build/test output (full stdout/stderr), not just tails.
    details = (guard_result or {}).get("details") or {}
    build_full = details.get("build") or {}
    test_full = details.get("test") or {}
    result.update(
        status="pass" if first_pass else ("build_failed" if not build_success else "test_failed"),
        success=first_pass,
        first_pass=first_pass,
        build_success=build_success,
        test_success=test_success,
        build=build_phase,
        test=test_phase,
        revision_audit=revision_audit,
        build_output_chars=len(str(build_full.get("output") or "")),
        test_output_chars=len(str(test_full.get("output") or "")),
    )
    # Write full logs next to the result file.
    logs_dir = report_path.parent
    if isinstance(build_full.get("output"), str) and build_full["output"]:
        (logs_dir / "build.log").write_text(build_full["output"], encoding="utf-8", errors="replace")
    if isinstance(test_full.get("output"), str) and test_full["output"]:
        (logs_dir / "test.log").write_text(test_full["output"], encoding="utf-8", errors="replace")
    result["elapsed_seconds"] = round(time.time() - started, 3)
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return result




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

    if args.list_execution_plans or args.deduplicate_execution_plans:
        refactor_config = load_refactor_config(args.config or None)
        project_overrides = load_project_overrides(args.projects or None)
        manifest, plans_by_id, merrors = build_execution_plan_manifest(
            samples, refactor_config, project_overrides, args.project_revisions
        )
        if merrors:
            manifest["materialization_errors"] = merrors
            print(
                f"execution-plan materialization_errors={len(merrors)} "
                f"(first: {merrors[0] if merrors else ''})",
                flush=True,
            )
            _write_report(Path(args.report), manifest, started)
            return 1
        if args.list_execution_plans:
            _write_report(Path(args.report), manifest, started)
            print(
                f"execution-plan sample_count={manifest['sample_count']} "
                f"unique_plan_count={manifest['unique_plan_count']} report={args.report}",
                flush=True,
            )
            return 0
        # --deduplicate-execution-plans: execute the single selected plan once.
        if not args.execution_id:
            print("deduplicate-execution-plans requires --execution-id", flush=True)
            return 2
        plan = _plan_by_id(plans_by_id, args.execution_id)
        result = execute_single_plan(
            samples,
            plan,
            refactor_config,
            project_overrides,
            args.project_revisions,
            Path(args.report),
        )
        print(
            f"plan execution_id={plan.execution_id[:12]} project={plan.project_name} "
            f"sample_keys={len(plan.sample_keys)} status={result.get('status')} "
            f"first_pass={result.get('first_pass')} report={args.report}",
            flush=True,
        )
        return 0 if result.get("first_pass") else 1

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

        # Resolve the authoritative project_commit via the shared module (no HEAD fallback).
        revision_audit: dict[str, str] = {}
        commit_error = ""
        try:
            rev = resolve_revision(sample.project_name, revisions, args.project_revisions)
            assert_commit_present(canonical_root, rev.project_commit)
            project_commit = rev.project_commit
            provenance_audit = audit_test_commit(
                canonical_root,
                legacy,
                project_commit,
            )
            revision_audit.update(provenance_audit)
            legacy_status = provenance_audit["test_commit_provenance"]
        except ProjectRevisionError as exc:
            commit_error = exc.status
            project_commit = ""
            legacy_status = "NOT_AUDITED"
            revision_audit = {
                "requested_project_commit": "",
                "actual_commit": "",
                "expected_tree_hash": "",
                "actual_tree_hash": "",
                "project_revision_alignment": exc.status,
                "project_revisions_path": args.project_revisions,
            }
        # strict policy: a non-empty legacy test_commit that is missing must fail explicitly.
        if (
            not commit_error
            and args.commit_policy == "strict"
            and legacy_status == "MISSING_FROM_REPOSITORY"
        ):
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
                    revision_audit.update(
                        verify_test_oracle(
                            Path(isolated.project_path),
                            sample.test_file,
                            sample.test_oracle_sha256,
                        )
                    )
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
