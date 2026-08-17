#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.loop_policy import (  # noqa: E402
    LoopPolicy,
    parse_command_policy,
    resolve_command_payload,
    validate_transferable_command_loop_state,
)
from smell_core.verification_receipt import (  # noqa: E402
    validate_formal_verification_decision,
)
from smell_core.location import split_location_descriptors  # noqa: E402
from smell_core.feature_envy_target_contract import (  # noqa: E402
    FeatureEnvyTargetContractError,
    explicit_receiver_name,
)
from smell_core.target_context import parse_target_context_json  # noqa: E402
from smell_core.test_diagnostics import (  # noqa: E402
    build_test_details as _build_test_details,
    failed_build_test_steps as _failed_build_test_steps,
    failed_test_diagnostic_signature as _failed_test_diagnostic_signature,
    failed_test_diagnostics as _failed_test_diagnostics,
    step_diagnostic_text as _step_diagnostic_text,
    step_failed as _step_failed,
)
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
from bridge.smell_bridge import (  # noqa: E402
    _snapshot_project as _capture_candidate_snapshot,
    _summarize_command_result as _summarize_receipt_command_result,
)

OPENCODE_BATCH_API_KEY_ENV = "SMELL_OPENCODE_API_KEY"
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_PROVIDER_MODELS: dict[str, Any] = {
    "glm-4.7": {
        "name": "GLM-4.7",
        "limit": {"context": 200000, "output": 131072},
        "modalities": {"input": ["text"], "output": ["text"]},
        "tool_call": True,
        "reasoning": True,
        "temperature": True,
        "status": "active",
        "options": {"thinking": {"type": "disabled"}},
    },
    "glm-4.5-air": {
        "name": "GLM-4.5-Air",
        "limit": {"context": 98304, "output": 16384},
        "modalities": {"input": ["text"], "output": ["text"]},
        "tool_call": True,
        "reasoning": True,
        "temperature": True,
        "status": "active",
    },
}

FINAL_VERIFICATION_MODES = {"sample_optimized", "project_full"}
RUNNER_FINAL_RECEIPT_SCHEMA = "smell.runner-final-receipt/v1"
RUNNER_FINAL_VERIFY_MIN_REMAINING_SECONDS = 60.0
PROCESS_TERM_TIMEOUT_SECONDS = 2.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
PROCESS_DRAIN_TIMEOUT_SECONDS = 2.0

@dataclass(frozen=True)
class Sample:
    sample_id: str
    language: str
    smell: str
    project_name: str
    project_root: Path
    location: str
    evidence: str
    raw: dict[str, str]
    target_context: dict[str, Any] = field(default_factory=dict)
    test_location: str = ""
    test_command: str = ""
    build_command: str = ""
    project_test_command: str = ""
    verification_cwd: str = ""
    verification_command_source: str = ""
    verification_mode: str = ""
    canonical_project_root: Path | None = None
    sibling_revision_audit: tuple[dict[str, str], ...] = ()


def _run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    if timeout is None:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=stdout,
        stderr=stderr,
        start_new_session=os.name == "posix",
    )
    try:
        captured_stdout, captured_stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        shutdown = _terminate_process_tree(proc)
        final_stdout, final_stderr, drain = _bounded_process_communicate(proc)
        timeout_error = subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=final_stdout if final_stdout is not None else exc.stdout,
            stderr=final_stderr if final_stderr is not None else exc.stderr,
        )
        timeout_error.shutdown = {**shutdown, **drain}  # type: ignore[attr-defined]
        raise timeout_error from exc
    return subprocess.CompletedProcess(
        args,
        proc.returncode,
        stdout=captured_stdout,
        stderr=captured_stderr,
    )


def _process_tree_snapshot(root_pid: int) -> tuple[dict[int, tuple[int, int]], set[int]]:
    """Return one POSIX process-table snapshot and descendants of root_pid."""
    if os.name != "posix":  # pragma: no cover - delivery/runtime is POSIX
        return {}, set()
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        rows: dict[int, tuple[int, int]] = {}
        children: dict[int, list[int]] = {}
        for raw in listing.stdout.splitlines():
            fields = raw.split()
            if len(fields) != 3:
                continue
            pid, parent_pid, group_id = (int(value) for value in fields)
            rows[pid] = (parent_pid, group_id)
            children.setdefault(parent_pid, []).append(pid)
        pending = [root_pid]
        descendants: set[int] = set()
        while pending:
            parent_pid = pending.pop()
            for child_pid in children.get(parent_pid, []):
                if child_pid in descendants:
                    continue
                descendants.add(child_pid)
                pending.append(child_pid)
    except (OSError, ValueError):
        return {}, set()
    return rows, descendants


def _process_tree_groups(root_pid: int) -> list[int]:
    """Snapshot POSIX process groups owned by one runner child tree."""
    if os.name != "posix":  # pragma: no cover - delivery/runtime is POSIX
        return []
    rows, descendants = _process_tree_snapshot(root_pid)
    groups = {root_pid}
    groups.update(
        rows[pid][1]
        for pid in descendants
        if pid in rows and rows[pid][1] > 0
    )
    if root_pid in rows and rows[root_pid][1] > 0:
        groups.add(rows[root_pid][1])
    groups.discard(os.getpgrp())
    return sorted(groups, key=lambda group_id: group_id == root_pid)


def _process_tree_has_descendants(root_pid: int) -> bool:
    """Whether OpenCode currently owns an active tool child process."""
    _, descendants = _process_tree_snapshot(root_pid)
    return bool(descendants)


def _terminate_process_tree(
    proc: subprocess.Popen[str],
    *,
    term_timeout: float = PROCESS_TERM_TIMEOUT_SECONDS,
    kill_timeout: float = PROCESS_KILL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Bounded TERM/KILL closure for one runner-owned process tree."""
    started = time.monotonic()
    groups = _process_tree_groups(proc.pid) if os.name == "posix" else []

    term_started = time.monotonic()
    if os.name == "posix":  # pragma: no branch - delivery/runtime is POSIX
        for group_id in groups:
            try:
                os.killpg(group_id, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    elif proc.poll() is None:  # pragma: no cover - delivery/runtime is POSIX
        proc.terminate()
    term_signal_ms = round((time.monotonic() - term_started) * 1000, 3)

    term_wait_started = time.monotonic()
    term_reaped = proc.poll() is not None
    if not term_reaped:
        try:
            proc.wait(timeout=max(0.0, term_timeout))
            term_reaped = True
        except subprocess.TimeoutExpired:
            term_reaped = False
    term_wait_ms = round((time.monotonic() - term_wait_started) * 1000, 3)

    kill_started = time.monotonic()
    if os.name == "posix":  # pragma: no branch - delivery/runtime is POSIX
        # The root may have exited while a nested build group remains alive.
        for group_id in groups:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    kill_signal_ms = round((time.monotonic() - kill_started) * 1000, 3)

    kill_wait_started = time.monotonic()
    process_reaped = proc.poll() is not None
    if not process_reaped:
        try:
            proc.wait(timeout=max(0.0, kill_timeout))
            process_reaped = True
        except subprocess.TimeoutExpired:
            process_reaped = False
    kill_wait_ms = round((time.monotonic() - kill_wait_started) * 1000, 3)
    return {
        "schema_version": 1,
        "bounded": True,
        "term_timeout_seconds": term_timeout,
        "kill_timeout_seconds": kill_timeout,
        "term_signal_ms": term_signal_ms,
        "term_wait_ms": term_wait_ms,
        "term_reaped": term_reaped,
        "kill_signal_ms": kill_signal_ms,
        "kill_wait_ms": kill_wait_ms,
        "process_reaped": process_reaped,
        "total_ms": round((time.monotonic() - started) * 1000, 3),
    }


def _bounded_process_communicate(
    proc: subprocess.Popen[str],
    *,
    timeout: float = PROCESS_DRAIN_TIMEOUT_SECONDS,
) -> tuple[Any, Any, dict[str, Any]]:
    """Drain a terminated child's pipes without creating an unbounded tail."""
    started = time.monotonic()
    completed = False
    captured_stdout: Any = None
    captured_stderr: Any = None
    try:
        captured_stdout, captured_stderr = proc.communicate(timeout=max(0.0, timeout))
        completed = True
    except subprocess.TimeoutExpired as exc:
        captured_stdout = exc.stdout
        captured_stderr = exc.stderr
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
    return captured_stdout, captured_stderr, {
        "communicate_timeout_seconds": timeout,
        "communicate_completed": completed,
        "communicate_ms": round((time.monotonic() - started) * 1000, 3),
    }


def _git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-c", "safe.directory=*", *args], project_root)


def _idea_refactor_cli() -> str:
    return (
        os.environ.get("SMELL_IDEA_REFACTOR_CLI", "").strip()
        or os.environ.get("IDEA_REFACTOR_CLI", "").strip()
        or "idea-refactor"
    )


def _sanitize_idea_service_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    server = sanitized.get("server")
    if isinstance(server, dict):
        sanitized_server = dict(server)
        sanitized_server.pop("token", None)
        sanitized["server"] = sanitized_server
    sanitized.pop("token", None)
    return sanitized


def _close_idea_project(project_root: Path, sample_dir: Path) -> None:
    try:
        proc = _run(
            [_idea_refactor_cli(), "close-project", "--project-root", str(project_root)],
            project_root,
            timeout=60,
        )
        try:
            decoded = json.loads(proc.stdout)
            result = decoded if isinstance(decoded, dict) else {"status": "unknown"}
        except json.JSONDecodeError:
            result = {"status": "invalid_json"}
        payload = {
            "returncode": proc.returncode,
            "result": _sanitize_idea_service_payload(result),
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        payload = {"returncode": 124, "result": {"status": "timeout"}, "stderr": ""}
    (sample_dir / "idea-close-project.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def _dataset_evidence(row: dict[str, str | None]) -> str:
    """Preserve the dataset evidence verbatim as audit-only metadata."""
    return str(row.get("evidence") or "").strip()


def _dataset_target_context(row: dict[str, str | None]) -> dict[str, Any]:
    """Load explicit selector identity without consulting oracle evidence."""
    return parse_target_context_json(row.get("target_context_json"))


def _load_samples(dataset: Path) -> list[Sample]:
    with dataset.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"sample_id", "language", "smell_type", "project_name", "project_path", "location"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{dataset} is missing columns: {', '.join(sorted(missing))}")
        samples: list[Sample] = []
        for row in reader:
            smell = str(row["smell_type"] or "").strip()
            language = str(row["language"] or "java").strip().lower()
            location = str(row["location"] or "").strip()
            if smell in {
                "long_method",
                "long_parameter_list",
                "nested_complexity",
                "switch_statements",
            } and ":method=" not in location:
                raise ValueError(
                    f"{smell} dataset location must contain an explicit method selector: "
                    f"{location!r}"
                )
            target_context = _dataset_target_context(row)
            if language != "java" and smell == "feature_envy":
                try:
                    explicit_receiver_name(target_context)
                except FeatureEnvyTargetContractError as exc:
                    raise ValueError(
                        "non-Java feature_envy rows require an explicit receiver "
                        f"root in target_context_json.receiver_type: {exc}"
                    ) from exc
            if language != "java" and smell == "data_clumps":
                if not str(target_context.get("group") or "").strip():
                    raise ValueError(
                        "non-Java data_clumps rows require target_context_json.group"
                    )
                occurrence_locations = split_location_descriptors(location)
                if len(occurrence_locations) < 3:
                    raise ValueError(
                        "non-Java data_clumps rows require at least three explicit "
                        f"occurrence locations; got {len(occurrence_locations)}"
                    )
                if any(":method=" not in item for item in occurrence_locations):
                    raise ValueError(
                        "non-Java data_clumps occurrence locations require explicit "
                        "method selectors"
                    )
            if language != "java" and smell == "mysterious_name":
                if not str(target_context.get("symbol_kind") or "").strip():
                    raise ValueError(
                        "non-Java mysterious_name rows require target_context_json.symbol_kind"
                    )
                if not str(target_context.get("symbol_name") or "").strip():
                    raise ValueError(
                        "non-Java mysterious_name rows require target_context_json.symbol_name"
                    )
            verification_mode = str(row.get("verification_mode") or "").strip()
            build_command = str(row.get("build_command") or "").strip()
            project_test_command = str(
                row.get("project_test_command") or ""
            ).strip()
            verification_cwd = str(row.get("verification_cwd") or "").strip()
            if bool(build_command) != bool(project_test_command) or (
                verification_cwd and not build_command
            ):
                raise ValueError(
                    "DATASET_VERIFICATION_COMMAND_PAIR_REQUIRED: "
                    f"sample {row['sample_id']} must declare both build_command and "
                    "project_test_command when any project verification override is present"
                )
            samples.append(
                Sample(
                    sample_id=str(row["sample_id"]),
                    language=language,
                    smell=smell,
                    project_name=str(row["project_name"]),
                    project_root=Path(row["project_path"]).expanduser().resolve(),
                    location=location,
                    evidence=_dataset_evidence(row),
                    raw={str(k): str(v) for k, v in row.items()},
                    target_context=target_context,
                    test_location=str(row.get("test_file") or "").strip(),
                    test_command=str(row.get("test_command") or "").strip(),
                    build_command=build_command,
                    project_test_command=project_test_command,
                    verification_cwd=verification_cwd,
                    verification_command_source=(
                        "dataset"
                        if build_command or project_test_command or verification_cwd
                        else ""
                    ),
                    verification_mode=verification_mode,
                )
            )
    return samples


def _filter_samples(samples: list[Sample], args: argparse.Namespace) -> list[Sample]:
    sample_ids = set(args.sample_id or [])
    projects = set(args.project or [])
    selected: list[Sample] = []
    for sample in samples:
        if args.smell and sample.smell != args.smell:
            continue
        if projects and sample.project_name not in projects:
            continue
        if sample_ids and sample.sample_id not in sample_ids:
            continue
        selected.append(sample)
    if args.offset:
        selected = selected[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def _effective_verification_mode(sample: Sample, args: argparse.Namespace) -> str:
    cli_mode = str(getattr(args, "verification_mode", None) or "").strip()
    if cli_mode and cli_mode not in FINAL_VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification mode '{cli_mode}'. Expected one of: "
            f"{', '.join(sorted(FINAL_VERIFICATION_MODES))}."
        )
    sample_mode = str(sample.verification_mode or "").strip()
    requested = cli_mode or sample_mode or "project_full"
    # Once tests may change, a sample-only command can be edited together with
    # the implementation and is no longer an independent behavior oracle.
    # Use the project's complete frozen test command for every such task,
    # regardless of the dataset row's ordinary optimization hint.
    if getattr(args, "allow_test_changes", False):
        requested = "project_full"
    if requested == "sample_optimized" and not sample.test_location.strip():
        raise ValueError(
            "SAMPLE_ORACLE_TEST_FILE_MISSING: sample_optimized verification requires "
            f"sample {sample.sample_id} to declare test_file"
        )
    if requested == "sample_optimized" and not sample.test_command.strip():
        raise ValueError(
            "SAMPLE_ORACLE_TEST_COMMAND_MISSING: sample_optimized verification requires "
            f"sample {sample.sample_id} to declare test_command"
        )
    if requested not in FINAL_VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification mode '{requested}'. Expected one of: "
            f"{', '.join(sorted(FINAL_VERIFICATION_MODES))}."
        )
    return requested


def _normalized_verification_cwd(value: str, project_root: Path) -> str:
    raw = str(value or "").strip()
    path = Path(raw or ".").expanduser()
    if not path.is_absolute():
        path = project_root / path
    return str(path.resolve())


def _project_verification_identity(sample: Sample) -> tuple[str, str]:
    # The checkout revision is selected exclusively by project_name in the
    # authoritative revisions manifest. Dataset project_commit/test_commit
    # fields are provenance only and must not split this consistency group.
    return str(sample.project_root.resolve()), str(sample.project_name).strip()


def _resolve_verification_command_specs(
    samples: list[Sample], args: argparse.Namespace
) -> list[Sample]:
    """Resolve trusted project commands before any model process can start."""

    cli_build = str(getattr(args, "build_command", None) or "").strip()
    cli_test = str(getattr(args, "project_test_command", None) or "").strip()
    cli_cwd = str(getattr(args, "verification_cwd", None) or "").strip()
    if bool(cli_build) != bool(cli_test) or (cli_cwd and not cli_build):
        raise ValueError(
            "CLI_VERIFICATION_COMMAND_PAIR_REQUIRED: --build-command and "
            "--project-test-command must be provided together when any project "
            "verification override is present"
        )

    resolved: list[Sample] = []
    grouped_specs: dict[
        tuple[str, str],
        tuple[tuple[str, str, str], list[str]],
    ] = {}
    for sample in samples:
        row_build = str(sample.build_command or "").strip()
        row_test = str(sample.project_test_command or "").strip()
        row_cwd = str(sample.verification_cwd or "").strip()
        cli_spec_present = bool(cli_build)
        row_spec_present = bool(row_build)
        cli_spec = (
            cli_build,
            cli_test,
            _normalized_verification_cwd(cli_cwd, sample.project_root),
        )
        row_spec = (
            row_build,
            row_test,
            _normalized_verification_cwd(row_cwd, sample.project_root),
        )
        if cli_spec_present and row_spec_present and cli_spec != row_spec:
            raise ValueError(
                "VERIFICATION_COMMAND_SOURCE_CONFLICT: "
                f"sample {sample.sample_id} has a dataset build/project-test/cwd "
                "spec that differs from the explicit runner CLI spec"
            )

        effective_build = cli_build if cli_spec_present else row_build
        effective_test = cli_test if cli_spec_present else row_test
        effective_cwd = (
            (cli_cwd or ".")
            if cli_spec_present
            else (row_cwd or ".")
            if row_spec_present
            else ""
        )
        source = "cli" if cli_spec_present else sample.verification_command_source
        effective = replace(
            sample,
            build_command=effective_build,
            project_test_command=effective_test,
            verification_cwd=effective_cwd,
            verification_command_source=source,
        )
        resolved.append(effective)

        identity = _project_verification_identity(effective)
        spec = (
            effective_build,
            effective_test,
            _normalized_verification_cwd(effective_cwd, effective.project_root),
        )
        previous = grouped_specs.get(identity)
        if previous is None:
            grouped_specs[identity] = (spec, [effective.sample_id])
            continue
        previous_spec, sample_ids = previous
        if previous_spec != spec:
            raise ValueError(
                "PROJECT_VERIFICATION_SPEC_CONFLICT: project/revision "
                f"{identity[0]}@{identity[1]} has inconsistent build, project-test, "
                f"or cwd values across samples {', '.join([*sample_ids, effective.sample_id])}"
            )
        sample_ids.append(effective.sample_id)
    return resolved


def _append_verification_command_args(cmd: list[str], sample: Sample) -> None:
    if sample.build_command:
        cmd.extend(["--build-command", sample.build_command])
    if sample.project_test_command:
        cmd.extend(["--project-test-command", sample.project_test_command])
    if sample.verification_cwd:
        cmd.extend(["--verification-cwd", sample.verification_cwd])
    if sample.verification_command_source:
        cmd.extend(
            [
                "--verification-command-source",
                sample.verification_command_source,
            ]
        )
    if sample.test_command:
        cmd.extend(["--sample-test-source", "dataset"])


def _sanitize(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip("-") or "sample"


def _remap_text(value: str, source_root: Path, target_root: Path) -> str:
    if not value:
        return value
    source = str(source_root)
    target = str(target_root)
    return value.replace(source, target)


def _restore_worktree_build_wrappers(canonical_root: Path, worktree: Path) -> None:
    """Copy gitignored/untracked build-wrapper bootstrap files the worktree lacks.

    The isolated Git checkout only contains tracked files, so build wrappers
    whose bootstrap artifacts are gitignored (e.g. Maven Wrapper's
    ``.mvn/wrapper/maven-wrapper.jar`` or Gradle's
    ``gradle/wrapper/gradle-wrapper.jar``) are missing in the worktree and break
    offline builds. Restore them from the canonical working tree so the worktree
    is buildable. Idempotent: only copies files that are absent in the worktree.
    """
    for wrapper_rel in (".mvn/wrapper", "gradle/wrapper"):
        src_dir = canonical_root / wrapper_rel
        if not src_dir.is_dir():
            continue
        dst_dir = worktree / wrapper_rel
        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_dir)
            dst_file = dst_dir / rel
            if dst_file.exists():
                continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)


def _remove_worktree_checkout(canonical_root: Path, worktree: Path) -> None:
    """Remove either a legacy linked worktree or the current isolated clone."""
    if (worktree / ".git").is_file():
        _git(canonical_root, ["worktree", "remove", "--force", str(worktree)])
    shutil.rmtree(worktree, ignore_errors=True)


def _execution_checkout_run_dir(run_dir: Path) -> Path:
    """Keep executable Git metadata on the container's native filesystem."""
    return Path(tempfile.gettempdir()) / "opencode-refactor-worktrees" / run_dir.name


def _declared_sibling_projects(sample: Sample) -> list[str]:
    raw = str(sample.raw.get("checkout_sibling_projects") or "").strip()
    return [
        name.strip()
        for name in re.split(r"[|,;]", raw)
        if name.strip()
    ]


def _prepare_worktree(
    sample: Sample,
    run_dir: Path,
    *,
    target_commit: str,
    revisions: dict[str, dict[str, Any]] | None = None,
) -> Sample:
    """Create an isolated checkout pinned to ``target_commit``.

    ``target_commit`` is REQUIRED: there is no HEAD fallback. Callers (both the real
    refactor runner and baseline-check) must resolve the authoritative project_commit
    via :mod:`smell_core.project_revision` and pass it here.
    """
    if not target_commit:
        raise RuntimeError(
            "_prepare_worktree requires a non-empty target_commit; HEAD fallback is forbidden"
        )
    canonical_root = sample.project_root.resolve()

    worktree = run_dir / "worktrees" / f"sample-{_sanitize(sample.sample_id)}" / canonical_root.name
    if worktree.exists():
        _remove_worktree_checkout(canonical_root, worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    # Keep a regular .git directory in each isolated checkout. Some build
    # plugins (notably MyBatis' mycila license plugin through JGit) treat the
    # .git pointer file created by `git worktree` as a bare repository. A
    # shared local clone preserves isolation without duplicating Git objects
    # and works with both command-line Git and those plugins.
    proc = _git(canonical_root, ["clone", "--shared", "--no-checkout", str(canonical_root), str(worktree)])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"failed to create isolated checkout at {worktree}")
    # Pin to the authoritative target_commit. Verified against the manifest tree by the
    # caller via smell_core.project_revision after the worktree is materialized.
    verify_proc = _git(canonical_root, ["cat-file", "-e", f"{target_commit}^{{commit}}"])
    if verify_proc.returncode != 0:
        _remove_worktree_checkout(canonical_root, worktree)
        raise RuntimeError(
            f"target_commit {target_commit} does not exist in {canonical_root}: "
            f"{verify_proc.stderr or verify_proc.stdout}"
        )
    checkout_proc = _git(worktree, ["checkout", "--detach", target_commit])
    if checkout_proc.returncode != 0:
        _remove_worktree_checkout(canonical_root, worktree)
        raise RuntimeError(checkout_proc.stderr or checkout_proc.stdout or f"failed to check out {target_commit} at {worktree}")
    filemode_proc = _git(worktree, ["config", "core.filemode", "false"])
    if filemode_proc.returncode != 0:
        raise RuntimeError(filemode_proc.stderr or filemode_proc.stdout or "failed to configure worktree filemode")

    _restore_worktree_build_wrappers(canonical_root, worktree)

    sibling_audits: list[dict[str, str]] = []
    sibling_projects = _declared_sibling_projects(sample)
    if sibling_projects and revisions is None:
        raise RuntimeError("sibling project checkout requires the authoritative revisions manifest")
    for project_name in sibling_projects:
        sibling_root = canonical_root.parent / project_name
        if not sibling_root.is_dir():
            raise ProjectRevisionError(
                "SIBLING_PROJECT_ROOT_MISSING",
                f"declared sibling project {project_name} is missing beside {canonical_root}",
                project_name=project_name,
                project_root=str(sibling_root),
            )
        sibling_revision = resolve_revision(project_name, revisions or {}, "in-memory revisions")
        assert_commit_present(sibling_root, sibling_revision.project_commit)
        sibling_checkout = worktree.parent / sibling_root.name
        if sibling_checkout.exists():
            shutil.rmtree(sibling_checkout)
        clone_proc = _git(
            sibling_root,
            ["clone", "--shared", "--no-checkout", str(sibling_root), str(sibling_checkout)],
        )
        if clone_proc.returncode != 0:
            raise RuntimeError(
                clone_proc.stderr
                or clone_proc.stdout
                or f"failed to create sibling checkout at {sibling_checkout}"
            )
        checkout_proc = _git(
            sibling_checkout,
            ["checkout", "--detach", sibling_revision.project_commit],
        )
        if checkout_proc.returncode != 0:
            shutil.rmtree(sibling_checkout, ignore_errors=True)
            raise RuntimeError(
                checkout_proc.stderr
                or checkout_proc.stdout
                or f"failed to check out sibling {project_name}"
            )
        _restore_worktree_build_wrappers(sibling_root, sibling_checkout)
        sibling_audit = verify_checkout(sibling_checkout, sibling_revision)
        sibling_audits.append(
            {
                "project_name": project_name,
                "canonical_project_root": str(sibling_root),
                "execution_project_root": str(sibling_checkout),
                **sibling_audit,
            }
        )

    return replace(
        sample,
        project_root=worktree.resolve(),
        canonical_project_root=canonical_root,
        sibling_revision_audit=tuple(sibling_audits),
        location=_remap_text(sample.location, canonical_root, worktree.resolve()),
        evidence=_remap_text(sample.evidence, canonical_root, worktree.resolve()),
        test_location=_remap_text(sample.test_location, canonical_root, worktree.resolve()),
        test_command=_remap_text(sample.test_command, canonical_root, worktree.resolve()),
        build_command=_remap_text(
            sample.build_command, canonical_root, worktree.resolve()
        ),
        project_test_command=_remap_text(
            sample.project_test_command, canonical_root, worktree.resolve()
        ),
        verification_cwd=_remap_text(
            sample.verification_cwd, canonical_root, worktree.resolve()
        ),
    )


def _copy_tree_item(source: Path, target: Path) -> None:
    if source.is_dir():
        if target.exists() or target.is_symlink():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                link_target = os.readlink(item)
                try:
                    destination.symlink_to(link_target, target_is_directory=item.resolve().is_dir())
                except OSError:
                    resolved = item.resolve()
                    if resolved.is_file():
                        shutil.copyfile(resolved, destination)
            elif item.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _bootstrap_opencode(project_root: Path, sample_dir: Path) -> None:
    """Install the runner's .opencode config into the project root.

    Idempotent: if the target already has our plugin (smell.ts), skip the
    entire bootstrap. This prevents repeated calls (e.g. from retry attempts)
    from trying to move/overwrite an already-bootstrapped directory, which
    fails on read-only filesystems or permission-restricted volumes.
    """
    source = ROOT / ".opencode"
    target = project_root / ".opencode"
    if (target / "plugins" / "smell.ts").exists():
        # Already bootstrapped by a previous call — skip.
        return
    if target.exists() or target.is_symlink():
        backup = sample_dir / "original-opencode"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(target), str(backup))

    target.mkdir(parents=True, exist_ok=True)
    for name in ("agents", "commands", "plugins", "skills", "package.json", "package-lock.json", ".gitignore"):
        item = source / name
        if item.exists():
            _copy_tree_item(item, target / name)

    node_modules = source / "node_modules"
    if node_modules.exists():
        try:
            (target / "node_modules").symlink_to(node_modules, target_is_directory=True)
        except OSError:
            shutil.copytree(node_modules, target / "node_modules", symlinks=True)


def _provider_id_from_model(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _model_id(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _auth_json_paths(args: argparse.Namespace) -> list[Path]:
    raw = str(args.opencode_auth_json or "auto").strip()
    if raw in {"", "none", "disabled"}:
        return []
    if raw != "auto":
        return [Path(raw).expanduser()]
    paths: list[Path] = []
    if os.environ.get("OPENCODE_AUTH_JSON"):
        paths.append(Path(os.environ["OPENCODE_AUTH_JSON"]).expanduser())
    if os.environ.get("XDG_DATA_HOME"):
        paths.append(Path(os.environ["XDG_DATA_HOME"]).expanduser() / "opencode" / "auth.json")
    home = Path(os.environ.get("HOME", "~")).expanduser()
    paths.extend([home / ".local" / "share" / "opencode" / "auth.json", home / ".config" / "opencode" / "auth.json"])
    return list(dict.fromkeys(paths))


def _api_key_from_args(args: argparse.Namespace, provider_id: str) -> tuple[str, str, str]:
    if args.opencode_api_key:
        return args.opencode_api_key, OPENCODE_BATCH_API_KEY_ENV, "argument"
    if args.opencode_api_key_env and os.environ.get(args.opencode_api_key_env):
        return os.environ[args.opencode_api_key_env], args.opencode_api_key_env, "env"
    if provider_id:
        for path in _auth_json_paths(args):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            auth = data.get(provider_id) if isinstance(data, dict) else None
            if isinstance(auth, dict) and str(auth.get("key") or "").strip():
                return str(auth["key"]).strip(), OPENCODE_BATCH_API_KEY_ENV, f"auth_json:{path}"
    return "", "", ""


def _validate_model_auth(args: argparse.Namespace) -> None:
    """Fail before creating run artifacts when a model credential is absent."""
    if bool(args.dry_run) or bool(args.checkout_only):
        return
    provider_id = _provider_id_from_model(str(args.model or ""))
    if not provider_id:
        raise ValueError(
            "MODEL_PROVIDER_MISSING: --model must use provider/model-id format"
        )
    api_key, _, _ = _api_key_from_args(args, provider_id)
    if api_key:
        return
    requested_env = str(args.opencode_api_key_env or "").strip()
    env_hint = (
        f" Set and export {requested_env}."
        if requested_env
        else " Set --opencode-api-key-env or provide OPENCODE_AUTH_JSON."
    )
    raise ValueError(
        f"MODEL_AUTH_MISSING: no API key is configured for provider {provider_id!r}."
        f"{env_hint}"
    )


def _write_opencode_config(sample_dir: Path, args: argparse.Namespace) -> tuple[Path | None, dict[str, str], dict[str, Any]]:
    provider_id = _provider_id_from_model(args.model)
    api_key, api_key_env, api_key_source = _api_key_from_args(args, provider_id)
    base_url = args.opencode_base_url or (ZAI_DEFAULT_BASE_URL if provider_id == "zai" else "")
    metadata = {
        "provider": provider_id,
        "api_key_configured": bool(api_key),
        "api_key_env": api_key_env,
        "api_key_source": api_key_source,
        "base_url_configured": bool(base_url),
    }
    if not provider_id or (not api_key and not base_url):
        return None, {}, metadata

    model_id = _model_id(args.model)
    model_config = ZAI_PROVIDER_MODELS.get(model_id, {"name": model_id, "tool_call": True, "status": "active"})
    provider_config: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider_id.upper(),
        "options": {},
        "models": {model_id: model_config},
    }
    if api_key:
        provider_config["options"]["apiKey"] = f"{{env:{api_key_env}}}"
    if base_url:
        provider_config["options"]["baseURL"] = base_url

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": args.model,
        "enabled_providers": [provider_id],
        "provider": {provider_id: provider_config},
        # Batch runs are headless: a subagent (e.g. the built-in explore agent)
        # reading outside the session directory would otherwise hit an
        # external_directory "ask" that can never be answered, hanging the
        # session until the sample deadline. The primary agents already grant
        # this in their frontmatter; extend the same grant to every agent in
        # this isolated, per-sample runtime config.
        "permission": {"external_directory": "allow"},
    }
    path = sample_dir / "opencode.runtime.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime_env = {api_key_env: api_key} if api_key and api_key_env else {}
    metadata["config_path"] = str(path)
    return path, runtime_env, metadata


def _prepare_opencode_home(sample_dir: Path) -> dict[str, str]:
    # Use a fixed isolated home under sample_dir so the command session and its
    # native agent loop share one opencode.db without leaking cross-sample state.
    home = sample_dir / "opencode-home"
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".config" / "opencode"
    config.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", ".gitignore"):
        source = ROOT / ".opencode" / name
        if source.is_file():
            shutil.copyfile(source, config / name)
    node_modules = ROOT / ".opencode" / "node_modules"
    if node_modules.exists():
        target = config / "node_modules"
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        try:
            target.symlink_to(node_modules, target_is_directory=True)
        except OSError:
            shutil.copytree(node_modules, target, symlinks=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
    }


_NATIVE_DIAGNOSTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NATIVE_SEGMENTATION_FAULT", re.compile(r"\bsegmentation fault\b", re.IGNORECASE)),
    ("NATIVE_CORE_DUMP", re.compile(r"\bcore dumped\b", re.IGNORECASE)),
    (
        "NATIVE_COMPILER_KILLED",
        re.compile(
            r"(?:fatal error:\s*killed|killed signal terminated program)",
            re.IGNORECASE,
        ),
    ),
    ("NINJA_FAILED_EDGE", re.compile(r"\bFAILED:\s+\S", re.IGNORECASE)),
    (
        "NINJA_BUILD_STOPPED",
        re.compile(r"\bninja:\s+build stopped\b", re.IGNORECASE),
    ),
)


def _step_infra_kind(step: Any) -> str:
    """Return a conservative resource cause for one structured build/test step."""
    if not isinstance(step, dict) or not _step_failed(step):
        return ""
    status = str(step.get("status") or "").strip().casefold()
    returncode = step.get("returncode")
    text = _step_diagnostic_text(step)
    if (
        status in {"timeout", "timed_out"}
        or returncode == 124
        or re.search(r"\b(?:build|test|command) timed out after\b", text, re.IGNORECASE)
    ):
        return "timeout"
    if re.search(
        r"(?:\bout of memory\b|\boom[- ]kill(?:er|ed)?\b|cannot allocate memory|"
        r"fatal error:\s*killed|killed signal terminated program)",
        text,
        re.IGNORECASE,
    ):
        return "oom"
    if returncode in {-9, 137}:
        return "resource"
    return ""


def _native_failure_diagnostics(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract generic native build/test signals without copying raw log text."""
    diagnostics: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for step_name, step in _failed_build_test_steps(payload):
        text = _step_diagnostic_text(step)
        for category, pattern in _NATIVE_DIAGNOSTIC_PATTERNS:
            key = (step_name, category)
            if key in seen or not pattern.search(text):
                continue
            seen.add(key)
            diagnostics.append({"step": step_name, "category": category})
    return diagnostics


def _structured_failure_category(payload: dict[str, Any]) -> str:
    """Prefer structured build/test evidence over smell-only repair advice."""
    failed_steps = _failed_build_test_steps(payload)
    verify_status = str(payload.get("status") or "").strip()
    guard = payload.get("build_test_guard")
    reason = str(guard.get("reason") or "") if isinstance(guard, dict) else ""
    evidence_missing_reasons = {
        "test_not_executed",
        "test_evidence_missing",
        "no_tests_collected",
    }
    for step_name, step in failed_steps:
        if step_name != "build":
            continue
        infra_kind = _step_infra_kind(step)
        return {
            "timeout": "BUILD_TIMEOUT",
            "oom": "BUILD_OOM",
            "resource": "BUILD_RESOURCE_EXHAUSTED",
        }.get(infra_kind, "BUILD_COMPILE_ERROR")
    for step_name, step in failed_steps:
        if step_name not in {"test", "sample_test"}:
            continue
        step_status = str(step.get("status") or "").strip().casefold()
        if (
            verify_status == "TEST_EVIDENCE_MISSING"
            or step_status in evidence_missing_reasons
            or reason.strip().casefold() in evidence_missing_reasons
        ):
            return (
                "SAMPLE_TEST_EVIDENCE_MISSING"
                if step_name == "sample_test"
                else "TEST_EVIDENCE_MISSING"
            )
        infra_kind = _step_infra_kind(step)
        if infra_kind:
            return {
                "timeout": "TEST_TIMEOUT",
                "oom": "TEST_OOM",
                "resource": "TEST_RESOURCE_EXHAUSTED",
            }[infra_kind]
        return "TEST_BEHAVIOR_REGRESSION"

    if verify_status == "BUILD_FAILED":
        return "BUILD_COMPILE_ERROR"
    if verify_status in {"TEST_FAILED", "TEST_EVIDENCE_MISSING"}:
        if (
            verify_status == "TEST_EVIDENCE_MISSING"
            or reason.strip().casefold() in evidence_missing_reasons
        ):
            return "TEST_EVIDENCE_MISSING"
        return "TEST_BEHAVIOR_REGRESSION"
    return ""


def _failure_category_from_verify_payload(payload: dict[str, Any]) -> str:
    """Return a stage-aware failure category for runner trace and summaries.

    A structured build/test failure has higher repair priority than a stale or
    smell-only failure_pack category. The bridge payload remains unmodified.
    """
    if not isinstance(payload, dict):
        return ""
    structured = _structured_failure_category(payload)
    if structured:
        return structured
    pack = payload.get("failure_pack")
    if not isinstance(pack, dict):
        return ""
    return str(pack.get("failure_category") or "").strip()


def _verify_infra_failure_category(payload: dict[str, Any]) -> str:
    category = _structured_failure_category(payload)
    if category in {
        "BUILD_TIMEOUT",
        "BUILD_OOM",
        "BUILD_RESOURCE_EXHAUSTED",
        "TEST_TIMEOUT",
        "TEST_OOM",
        "TEST_RESOURCE_EXHAUSTED",
    }:
        return category
    return ""


def _verify_diff_path(payload: dict[str, Any]) -> Path | None:
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        candidates.append(artifacts.get("diff"))
    pack = payload.get("failure_pack")
    if isinstance(pack, dict) and isinstance(pack.get("artifact_paths"), dict):
        candidates.append(pack["artifact_paths"].get("diff"))
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("artifacts"), dict):
        candidates.append(snapshot["artifacts"].get("diff"))
    for candidate in candidates:
        if candidate:
            path = Path(str(candidate))
            if path.is_file():
                return path
    return None


def _verify_diff_sha256(payload: dict[str, Any]) -> str:
    path = _verify_diff_path(payload)
    if path is None:
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _accepted_verify_pass(payload: dict[str, Any], verify_returncode: int = 0) -> bool:
    return _compute_status(0, verify_returncode, payload) == "PASS"


def _compact_verify_attempt(
    payload: dict[str, Any],
    *,
    verify_source: str,
    verify_returncode: int,
) -> dict[str, Any]:
    status = _compute_status(0, verify_returncode, payload)
    failed_build_test_steps = [name for name, _ in _failed_build_test_steps(payload)]
    failed_test_diagnostics = _failed_test_diagnostics(payload)
    return {
        "verify_source": verify_source,
        "verify_returncode": verify_returncode,
        "verify_payload": payload,
        "reported_status": str(payload.get("status") or ""),
        "status": status,
        "success": payload.get("success") is True,
        "accepted": _accepted_verify_pass(payload, verify_returncode),
        "resolution": str(payload.get("resolution") or ""),
        "progress": bool(payload.get("progress")) or status == "PASS",
        "failure_category": _failure_category_from_verify_payload(payload),
        "failed_build_test_steps": failed_build_test_steps,
        "failed_test_cases": sorted(
            {item["test"] for item in failed_test_diagnostics}
        ),
        "failed_test_diagnostics": failed_test_diagnostics,
        "failed_test_signature": list(
            f'{item["test"]}|{item["exit_code"]}|{item["diagnostic_fingerprint"]}'
            for item in failed_test_diagnostics
        ),
        "diff_sha256": _verify_diff_sha256(payload),
        "native_diagnostics": _native_failure_diagnostics(payload),
    }


def _verification_attempt_history(
    agent_attempts: list[dict[str, Any]],
    final_attempt: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve every parsed agent verify before the runner-owned final decision."""
    return [*agent_attempts, final_attempt]


def _reconcile_final_verify_status(
    raw_status: str,
    verify_payload: dict[str, Any],
    agent_attempts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Reconcile only same-diff lifecycle contradictions around final verify.

    A final timeout/OOM after an agent PASS becomes an independent infra state.
    A final PASS immediately after a structured agent TEST_FAILED becomes flaky
    and requires confirmation. Neither exceptional state is accepted here.
    """
    infra_category = _verify_infra_failure_category(verify_payload)
    final_diff_sha256 = _verify_diff_sha256(verify_payload)
    last_agent = agent_attempts[-1] if agent_attempts else None
    last_agent_pass = bool(
        isinstance(last_agent, dict)
        and last_agent.get("verify_source") == "agent"
        and last_agent.get("status") == "PASS"
        and last_agent.get("accepted") is True
    )
    agent_diff_sha256 = (
        str(last_agent.get("diff_sha256") or "") if isinstance(last_agent, dict) else ""
    )
    same_diff = bool(
        last_agent_pass
        and final_diff_sha256
        and agent_diff_sha256
        and final_diff_sha256 == agent_diff_sha256
    )
    last_agent_same_diff_test_failure = bool(
        isinstance(last_agent, dict)
        and last_agent.get("verify_source") == "agent"
        and last_agent.get("reported_status") == "TEST_FAILED"
        and str(last_agent.get("failure_category") or "").startswith("TEST_")
        and any(
            step_name in {"test", "sample_test"}
            for step_name in (last_agent.get("failed_build_test_steps") or [])
        )
        and final_diff_sha256
        and str(last_agent.get("diff_sha256") or "") == final_diff_sha256
    )
    final_failed_test_signature = _failed_test_diagnostic_signature(verify_payload)
    same_diff_test_failure_signatures: set[tuple[str, ...]] = set()
    if (
        raw_status == "TEST_FAILED"
        and final_failed_test_signature
        and _failure_category_from_verify_payload(verify_payload).startswith("TEST_")
    ):
        same_diff_test_failure_signatures.add(final_failed_test_signature)
        for attempt in agent_attempts:
            if not isinstance(attempt, dict):
                continue
            signature = tuple(
                str(item) for item in (attempt.get("failed_test_signature") or [])
            )
            if (
                attempt.get("verify_source") == "agent"
                and attempt.get("reported_status") == "TEST_FAILED"
                and str(attempt.get("failure_category") or "").startswith("TEST_")
                and final_diff_sha256
                and str(attempt.get("diff_sha256") or "") == final_diff_sha256
                and signature
            ):
                same_diff_test_failure_signatures.add(signature)
    same_diff_test_failure_drift = len(same_diff_test_failure_signatures) >= 2
    audit = {
        "raw_status": raw_status,
        "infra_category": infra_category,
        "last_agent_pass": last_agent_pass,
        "same_diff_as_last_agent_pass": same_diff,
        "last_agent_same_diff_test_failure": last_agent_same_diff_test_failure,
        "same_diff_test_failure_drift": same_diff_test_failure_drift,
        "same_diff_test_failure_signatures": [
            list(signature) for signature in sorted(same_diff_test_failure_signatures)
        ],
        "final_diff_sha256": final_diff_sha256,
        "last_agent_diff_sha256": agent_diff_sha256,
        "confirmation_required": bool(
            same_diff_test_failure_drift
            or (raw_status == "PASS" and last_agent_same_diff_test_failure)
            or (
                raw_status in {"BUILD_FAILED", "TEST_FAILED", "VERIFY_FAILED"}
                and infra_category
                and same_diff
            )
        ),
    }
    if same_diff_test_failure_drift:
        return "FLAKY_TEST_INCONCLUSIVE", audit
    if raw_status == "PASS" and last_agent_same_diff_test_failure:
        return "FLAKY_TEST_INCONCLUSIVE", audit
    if raw_status in {"BUILD_FAILED", "TEST_FAILED", "VERIFY_FAILED"} and infra_category and same_diff:
        return "FINAL_VERIFY_INFRA_FAILED", audit
    return raw_status, audit


def _normalize_reconciled_final_failure(
    status: str,
    verify_payload: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Render a runner-owned terminal override as one canonical failure."""
    raw_pack = (
        verify_payload.get("failure_pack")
        if isinstance(verify_payload.get("failure_pack"), dict)
        else {}
    )
    raw_artifacts = (
        verify_payload.get("artifacts")
        if isinstance(verify_payload.get("artifacts"), dict)
        else {}
    )
    raw_artifact_paths = (
        raw_pack.get("artifact_paths")
        if isinstance(raw_pack.get("artifact_paths"), dict)
        else {
            key: value
            for key, value in raw_artifacts.items()
            if isinstance(value, str) and value
        }
    )
    raw_repair_contract = (
        raw_pack.get("repair_contract")
        if isinstance(raw_pack.get("repair_contract"), dict)
        else {}
    )
    if status == "FLAKY_TEST_INCONCLUSIVE":
        failure_category = "FLAKY_TEST_INCONCLUSIVE"
    elif status == "IDEA_PROTOCOL_FAILED":
        failure_category = "IDEA_PROTOCOL_FAILED"
    else:
        failure_category = str(
            audit.get("infra_category") or "FINAL_VERIFY_INFRA_FAILED"
        )
    failure_pack = {
        **raw_pack,
        "failure_category": failure_category,
        "failure_group": "",
        "retryable": False,
        "verify_status": status,
        "artifact_paths": raw_artifact_paths,
        "highlights": list(raw_pack.get("highlights") or []),
        "next_action": "",
        "recommendations": [],
        "repair_contract": {
            "repair_agent_may_edit": False,
            "prefer_narrow_fix": False,
            "must_rerun_smell_verify": False,
            "tests_may_change": raw_repair_contract.get("tests_may_change") is True,
        },
    }
    payload = {
        **verify_payload,
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "progress": False,
        "status": status,
        "resolution": "unresolved",
        "continue_hint": "",
        "failure_pack": failure_pack,
        "termination_reason": status,
        "reconciliation": {
            "raw_status": str(
                audit.get("raw_status") or verify_payload.get("status") or ""
            ),
            "raw_success": verify_payload.get("success") is True,
            "raw_accepted": verify_payload.get("accepted") is True,
            "raw_progress": verify_payload.get("progress") is True,
            "raw_resolution": str(verify_payload.get("resolution") or ""),
            "final_status": status,
            "failure_category": failure_category,
            "confirmation_required": audit.get("confirmation_required") is True,
            "same_diff_as_last_agent_pass": audit.get(
                "same_diff_as_last_agent_pass"
            )
            is True,
            "last_agent_same_diff_test_failure": audit.get(
                "last_agent_same_diff_test_failure"
            )
            is True,
            "same_diff_test_failure_drift": audit.get(
                "same_diff_test_failure_drift"
            )
            is True,
        },
    }
    for key, default in (
        ("smell_guard", None),
        ("build_test_guard", None),
        ("test_changes", None),
        ("snapshot", None),
        ("checkpoint", None),
        ("artifacts", {}),
        ("artifact_index", {}),
    ):
        payload.setdefault(key, default)
    checkpoint = payload.get("checkpoint")
    if isinstance(checkpoint, dict):
        payload["checkpoint"] = {
            **checkpoint,
            "accepted": False,
            "resolution": "unresolved",
            "verify_status": status,
        }
    payload.pop("failure_fingerprint", None)
    if status == "IDEA_PROTOCOL_FAILED" and isinstance(
        audit.get("idea_protocol"), dict
    ):
        payload["idea_protocol"] = audit["idea_protocol"]
    return payload


def _compute_status(opencode_returncode: int, verify_returncode: int, verify_payload: dict[str, Any]) -> str:
    """Return one authoritative terminal status for model and verification.

    A nonzero model process is an abnormal sample termination and takes
    precedence over the independent final verification. The latter remains
    diagnostic evidence but cannot turn an abnormal execution into PASS.
    """
    execution_failure = _opencode_failure_status(opencode_returncode)
    if execution_failure:
        return execution_failure
    verify_status = str(verify_payload.get("status") or "") if isinstance(verify_payload, dict) else ""
    if verify_status == "PASS":
        if (
            verify_returncode == 0
            and verify_payload.get("resolution") == "resolved"
            and verify_payload.get("success") is True
            and verify_payload.get("accepted") is True
        ):
            return "PASS"
        return "VERIFY_FAILED"
    return verify_status or "VERIFY_FAILED"


OPENCODE_FATAL_PROVIDER_RETURN_CODE = 86
SAMPLE_DEADLINE_EPOCH_MS_ENV = "SMELL_SAMPLE_DEADLINE_EPOCH_MS"


def _opencode_failure_status(returncode: int) -> str:
    if returncode == 0:
        return ""
    if returncode == 124:
        return "OPENCODE_TIMEOUT"
    if returncode == OPENCODE_FATAL_PROVIDER_RETURN_CODE:
        return "PROVIDER_QUOTA_FAILED"
    return "OPENCODE_FAILED"


def _fatal_provider_error(log_text: str) -> str:
    """Classify non-retryable provider quota failures found in OpenCode logs."""
    text = str(log_text or "")
    lowered = text.casefold()
    if "token plan 用量上限" in lowered or "已达到 token plan" in lowered:
        return "MINIMAX_TOKEN_PLAN_EXHAUSTED"
    if (
        "insufficient_quota" in lowered
        or "billing_hard_limit_reached" in lowered
        or "credit balance is too low" in lowered
    ):
        return "PROVIDER_INSUFFICIENT_QUOTA"
    return ""


def _opencode_timeout_seconds(sample_deadline: int) -> int:
    """Keep the runner hard stop equal to the one public sample budget."""
    return sample_deadline


def _deadline_epoch_ms(remaining_seconds: float) -> str:
    """Translate the runner's monotonic remainder for owned child processes."""
    return str(int((time.time() + max(0.0, remaining_seconds)) * 1000))


def _is_accepted_status(status: object) -> bool:
    return status == "PASS"


def _attempt_artifact_path(sample_dir: Path, name: str, attempt_suffix: str) -> Path:
    """Return the per-attempt artifact path. attempt_suffix is '' for attempt 0."""
    return sample_dir / f"{name}{attempt_suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _controller_context_manifest(
    command_loop_state: dict[str, Any] | None,
    refactoring_backend: str,
) -> dict[str, Any]:
    state = command_loop_state if isinstance(command_loop_state, dict) else {}
    policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    loop = policy.get("loop") if isinstance(policy.get("loop"), dict) else {}
    identity = policy.get("identity") if isinstance(policy.get("identity"), dict) else {}
    return {
        "schema_version": 1,
        "source": "controller_command_state",
        "identity": {
            key: identity.get(key)
            for key in (
                "project_root",
                "language",
                "smell",
                "location",
                "verification_command_source",
                "sample_test_source",
            )
        },
        "policy": {
            "verification_mode": policy.get("verification_mode"),
            "allow_test_changes": policy.get("allow_test_changes"),
            "refactoring_backend": refactoring_backend,
            "checkpoint_required": policy.get("checkpoint_required"),
            "loop_mode": loop.get("mode"),
            "max_smell_verify_cycles": loop.get("max_smell_verify_cycles"),
            "no_progress_limit": loop.get("no_progress_limit"),
            "allowed_failure_groups": loop.get("allowed_failure_groups"),
            "sample_deadline_seconds": loop.get("sample_deadline_seconds"),
        },
        "excluded_mutable_fields": [
            "smell_verify_cycle_count",
            "failure_category",
            "failure_pack",
            "loop_instruction",
            "next_action",
        ],
    }


def _append_synthetic_message_event(sample_dir: Path, event: dict[str, Any]) -> None:
    path = sample_dir / "synthetic-events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _task_prompt(sample: Sample) -> str:
    # Backend, verification, and test-change policy travel through command flags
    # and controller system context; the user task remains backend-neutral.
    target_count = len(split_location_descriptors(sample.location))
    lines = [
        f"Project root: {sample.project_root}",
        f"Language: {sample.language}",
        f"Smell type: {sample.smell}",
        f"Target location: {sample.location}",
    ]
    lines.append("")
    if target_count > 1:
        lines.append(
            f"Repair this grouped {sample.language} smell across all {target_count} listed "
            "target locations in one cohesive refactoring. Partial target removal is not accepted. "
            "Preserve behavior. Call smell_verify as the final acceptance gate."
        )
    else:
        lines.append(
            f"Repair this one {sample.language} smell from the dataset row. "
            "Preserve behavior. Call smell_verify as the final acceptance gate."
        )
    return "\n".join(lines)


def _command_arguments(task: str, args: argparse.Namespace, verification_mode: str) -> str:
    options = [
        f"--verification-mode={verification_mode}",
        f"--refactoring-backend={getattr(args, 'refactoring_backend', 'direct')}",
        f"--loop-mode={args.loop_mode}",
        f"--max-smell-verify-cycles={args.max_smell_verify_cycles}",
        f"--loop-no-progress-limit={args.loop_no_progress_limit}",
        f"--loop-on={args.loop_on}",
        f"--sample-deadline={args.sample_deadline}",
    ]
    if getattr(args, "allow_test_changes", False):
        options.append("--allow-test-changes")
    # The shared command parser intentionally consumes the free-form
    # instruction as the final option, so no controller flag may follow it.
    options.append(f"--loop-instruction={args.loop_instruction}")
    return " ".join(options) + " -- " + task


def _initial_command_loop_state(
    sample: Sample,
    args: argparse.Namespace,
    verification_mode: str,
    *,
    started_at_ms: int | None = None,
) -> dict[str, Any]:
    """Freeze trusted v7 state before the first OpenCode process starts.

    A verify-required reminder runs in a new OpenCode process.  The first
    model turn may have made no ``smell_verify`` call, so there may be no tool
    metadata from which to recover state.  Resolve it here through the same
    Python policy/identity authority used by the command hook.
    """

    task = _task_prompt(sample)
    payload = resolve_command_payload(
        _command_arguments(task, args, verification_mode),
        defaults={
            "project_root": str(sample.project_root),
            "project_override_root": (
                str(sample.canonical_project_root)
                if sample.canonical_project_root
                else None
            ),
            "language": sample.language,
            "smell": sample.smell,
            "location": sample.location,
            "target_context_json": (
                json.dumps(
                    sample.target_context,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if sample.target_context
                else None
            ),
            "sample_test_location": sample.test_location,
            "sample_test_command": sample.test_command,
            "sample_test_source": "dataset" if sample.test_command else "",
            "build_command": sample.build_command,
            "project_test_command": sample.project_test_command,
            "verification_cwd": sample.verification_cwd,
            "verification_command_source": sample.verification_command_source,
        },
        started_at_ms=started_at_ms,
    )
    state = payload.get("command_loop_state")
    if not isinstance(state, dict):
        raise ValueError("INVALID_COMMAND_LOOP_STATE: resolver returned no initial state")
    return state


def _copy_verify_artifacts(sample_dir: Path, verify_payload: dict[str, Any], attempt_suffix: str = "") -> None:
    artifacts = verify_payload.get("artifacts") if isinstance(verify_payload, dict) else None
    if not isinstance(artifacts, dict):
        return
    for key, filename in (("diff", "diff.patch"), ("diff_stat", "diff.stat")):
        source = artifacts.get(key)
        if source and Path(str(source)).is_file():
            dst = _attempt_artifact_path(sample_dir, filename, attempt_suffix)
            shutil.copyfile(str(source), str(dst))


def _persist_verify_payload(
    sample_dir: Path,
    verify_payload: dict[str, Any],
    attempt_suffix: str = "",
) -> None:
    verify_path = _attempt_artifact_path(sample_dir, "verify.json", attempt_suffix)
    verify_path.write_text(
        json.dumps(verify_payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _copy_verify_artifacts(sample_dir, verify_payload, attempt_suffix)


def _baseline_failure_status(returncode: int, payload: dict[str, Any]) -> str:
    """Return the precise setup status for a failed explicit c000 capture."""
    if returncode == 124 or payload.get("status") == "OPENCODE_TIMEOUT":
        return "OPENCODE_TIMEOUT"
    if (
        returncode == 0
        and payload.get("success") is True
        and payload.get("status") == "BASELINE_CAPTURED"
    ):
        return ""
    detail = str(payload.get("error") or payload.get("status") or "").strip()
    known = (
        "BASELINE_FINDING_NOT_FOUND",
        "TARGET_AMBIGUOUS",
        "DETECTOR_PROFILE_MISMATCH",
        "CHECKPOINT_RECAPTURE_REQUIRED",
        "CHECKPOINT_POLICY_MISMATCH",
        "CHECKPOINT_BASELINE_IDENTITY_MISMATCH",
        "CHECKPOINT_BASELINE_CAPTURE_FAILED",
        "CHECKPOINT_NOT_SUPPORTED",
    )
    for status in known:
        if status in detail:
            return status
    return "BASELINE_CAPTURE_FAILED"


def _run_capture_baseline(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    verification_mode: str,
    deadline_monotonic: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Freeze the Java product finding before the model can edit the checkout."""
    cmd = [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "capture-baseline",
        "--output-detail",
        "decision",
        "--project-root",
        str(sample.project_root),
        "--smell",
        sample.smell,
        "--location",
        sample.location,
        "--language",
        sample.language,
        "--verification-mode",
        verification_mode,
    ]
    canonical = sample.canonical_project_root
    if canonical and canonical != sample.project_root:
        cmd.extend(["--project-override-root", str(canonical)])
    if args.projects:
        cmd.extend(["--projects", args.projects])
    if sample.target_context:
        cmd.extend([
            "--target-context-json",
            json.dumps(sample.target_context, separators=(",", ":"), sort_keys=True),
        ])
    if sample.test_location:
        cmd.extend(["--sample-test-location", sample.test_location])
    if sample.test_command:
        cmd.extend(["--sample-test-command", sample.test_command])
    _append_verification_command_args(cmd, sample)
    if getattr(args, "allow_test_changes", False):
        cmd.append("--allow-test-changes")

    env = os.environ.copy()
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    remaining = (
        float(args.sample_deadline)
        if deadline_monotonic is None
        else deadline_monotonic - time.monotonic()
    )
    if remaining <= 0:
        returncode = 124
        stderr = ""
        payload = _sample_deadline_payload(args.sample_deadline, stage="baseline")
    else:
        env[SAMPLE_DEADLINE_EPOCH_MS_ENV] = _deadline_epoch_ms(remaining)
        try:
            proc = _run(cmd, ROOT, env=env, timeout=remaining)
        except subprocess.TimeoutExpired:
            returncode = 124
            stderr = ""
            payload = _sample_deadline_payload(args.sample_deadline, stage="baseline")
        else:
            returncode = proc.returncode
            stderr = proc.stderr
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = {
                    "success": False,
                    "error": "BASELINE_OUTPUT_PARSE_FAILED",
                    "stdout": proc.stdout,
                }
    artifact = {
        "returncode": returncode,
        "command": cmd,
        "payload": payload,
        "stderr": stderr,
    }
    (sample_dir / "baseline-capture.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return returncode, payload


def _sample_deadline_payload(
    sample_deadline: int,
    *,
    stage: str = "runner_final",
    termination_reason: str = "SAMPLE_DEADLINE_REACHED",
) -> dict[str, Any]:
    return {
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "progress": False,
        "status": "OPENCODE_TIMEOUT",
        "resolution": "unresolved",
        "project_full_executed": False,
        "termination_reason": termination_reason,
        "failure_pack": {
            "verify_status": "OPENCODE_TIMEOUT",
            "failure_category": "OPENCODE_TIMEOUT",
            "failure_group": "",
            "retryable": False,
            "next_action": "",
        },
        "timeout": {
            "scope": (
                "model-event-inactivity"
                if termination_reason == "MODEL_EVENT_INACTIVITY_TIMEOUT"
                else "sample"
            ),
            "stage": stage,
            "sample_deadline_seconds": sample_deadline,
        },
    }


def _normalize_sample_timeout(
    opencode_returncode: int,
    verify_returncode: int,
    verify_payload: dict[str, Any],
    sample_deadline: int,
    *,
    termination_reason: str = "SAMPLE_DEADLINE_REACHED",
) -> tuple[int, dict[str, Any]]:
    """Prevent a timed-out model turn from becoming an accepted final PASS."""
    if opencode_returncode != 124:
        return verify_returncode, verify_payload
    if (
        verify_payload.get("status") == "OPENCODE_TIMEOUT"
        and verify_payload.get("schema_version") == "smell.verify.decision/v1"
    ):
        return 124, verify_payload
    payload = _sample_deadline_payload(
        sample_deadline,
        stage="model",
        termination_reason=termination_reason,
    )
    observed_status = str(verify_payload.get("status") or "")
    if observed_status:
        payload["timeout"]["final_verify_observation"] = {
            "status": observed_status,
            "returncode": verify_returncode,
            "success": verify_payload.get("success") is True,
            "accepted": verify_payload.get("accepted") is True,
            "resolution": str(verify_payload.get("resolution") or ""),
            "decision": verify_payload,
        }
        payload["project_full_executed"] = (
            verify_payload.get("project_full_executed") is True
        )
    return 124, payload


def _normalize_opencode_failure(
    opencode_returncode: int,
    verify_returncode: int,
    verify_payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    status = _opencode_failure_status(opencode_returncode)
    if not status or status == "OPENCODE_TIMEOUT":
        return verify_returncode, verify_payload
    payload = {
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "progress": False,
        "status": status,
        "resolution": "unresolved",
        "project_full_executed": verify_payload.get("project_full_executed") is True,
        "termination_reason": status,
        "failure_pack": {
            "verify_status": status,
            "failure_category": status,
            "failure_group": "",
            "retryable": False,
            "next_action": "",
        },
        "execution_failure": {
            "stage": "model",
            "opencode_returncode": opencode_returncode,
            "final_verify_observation": {
                "status": str(verify_payload.get("status") or ""),
                "returncode": verify_returncode,
                "success": verify_payload.get("success") is True,
                "accepted": verify_payload.get("accepted") is True,
                "resolution": str(verify_payload.get("resolution") or ""),
                "decision": verify_payload,
            },
        },
    }
    return verify_returncode, payload


def _run_verify(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    verification_mode: str,
    attempt_suffix: str = "",
    baseline_seal: str = "",
    deadline_monotonic: float | None = None,
) -> tuple[int, dict[str, Any]]:
    cmd = [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "verify",
        "--output-detail",
        "decision",
        "--project-root",
        str(sample.project_root),
        "--smell",
        sample.smell,
        "--location",
        sample.location,
        "--language",
        sample.language,
        "--verification-mode",
        verification_mode,
        "--artifact-root",
        str(sample_dir / f"artifacts{attempt_suffix}"),
    ]
    canonical = sample.canonical_project_root
    if canonical and canonical != sample.project_root:
        cmd.extend(["--project-override-root", str(canonical)])
    if args.projects:
        cmd.extend(["--projects", args.projects])
    if sample.target_context:
        cmd.extend([
            "--target-context-json",
            json.dumps(sample.target_context, separators=(",", ":"), sort_keys=True),
        ])
    if sample.test_location:
        cmd.extend(["--sample-test-location", sample.test_location])
    if sample.test_command:
        cmd.extend(["--sample-test-command", sample.test_command])
    _append_verification_command_args(cmd, sample)
    if baseline_seal:
        cmd.extend(["--baseline-seal", baseline_seal])

    env = os.environ.copy()
    env["SMELL_REQUIRE_BUILD_TEST"] = "1"
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    remaining = (
        float(args.sample_deadline)
        if deadline_monotonic is None
        else deadline_monotonic - time.monotonic()
    )
    if remaining <= 0:
        payload = _sample_deadline_payload(args.sample_deadline)
        _persist_verify_payload(sample_dir, payload, attempt_suffix)
        return 124, payload
    env[SAMPLE_DEADLINE_EPOCH_MS_ENV] = _deadline_epoch_ms(remaining)
    try:
        proc = _run(cmd, ROOT, env=env, timeout=remaining)
    except subprocess.TimeoutExpired:
        payload = _sample_deadline_payload(args.sample_deadline)
        _persist_verify_payload(sample_dir, payload, attempt_suffix)
        return 124, payload
    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"success": False, "status": "VERIFY_OUTPUT_PARSE_FAILED", "stdout": proc.stdout, "stderr": proc.stderr}
    _persist_verify_payload(sample_dir, payload, attempt_suffix)
    return proc.returncode, payload


def _runner_final_receipt_audit(
    reason: str,
    *,
    reused: bool = False,
    agent_diff_sha256: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_FINAL_RECEIPT_SCHEMA,
        "reused": reused,
        "reason": reason,
        "source": "agent_smell_verify",
        "required_verification_mode": "project_full",
        "agent_diff_sha256": agent_diff_sha256,
    }


def _persist_runner_final_receipt_audit(
    sample_dir: Path,
    receipt_audit: dict[str, Any],
) -> None:
    (sample_dir / "runner-final-receipt.json").write_text(
        json.dumps(receipt_audit, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _receipt_artifacts_complete(
    payload: dict[str, Any],
    sample_dir: Path,
) -> bool:
    artifacts = payload.get("artifacts")
    artifact_index = payload.get("artifact_index")
    if not isinstance(artifacts, dict) or not isinstance(artifact_index, dict):
        return False
    required = {
        "guard_evidence",
        "build_test_guard",
        "build_result",
        "test_result",
        "build_log",
        "test_log",
        "snapshot",
        "diff",
        "diff_stat",
    }
    details = _build_test_details(payload)
    if isinstance(details.get("sample_test"), dict):
        required.update({"sample_test_result", "sample_test_log"})
    artifact_root = (sample_dir / "agent-artifacts").resolve()
    nonempty = required - {"build_log", "test_log", "sample_test_log"}
    for name in required:
        raw_path = artifacts.get(name)
        indexed = artifact_index.get(name)
        if not isinstance(raw_path, str) or not isinstance(indexed, dict):
            return False
        path = Path(raw_path)
        try:
            path.resolve().relative_to(artifact_root)
        except (OSError, ValueError):
            return False
        if str(indexed.get("path") or "") != raw_path:
            return False
        indexed_bytes = indexed.get("bytes")
        try:
            actual_bytes = path.stat().st_size
        except OSError:
            return False
        if not isinstance(indexed_bytes, int) or actual_bytes != indexed_bytes:
            return False
        if name in nonempty and indexed_bytes <= 0:
            return False
    return True


def _receipt_command_stage_complete(stage: Any) -> bool:
    return bool(
        isinstance(stage, dict)
        and stage.get("success") is True
        and stage.get("status") == "ok"
        and stage.get("returncode") == 0
        and (str(stage.get("command") or "").strip() or str(stage.get("script") or "").strip())
    )


def _receipt_command_stages_match(
    summary: Any,
    full_stage: Any,
    artifact_stage: Any,
) -> bool:
    return bool(
        _receipt_command_stage_complete(summary)
        and _receipt_command_stage_complete(full_stage)
        and _receipt_command_stage_complete(artifact_stage)
        and _summarize_receipt_command_result(full_stage) == summary
        and _summarize_receipt_command_result(artifact_stage) == summary
    )


def _read_receipt_json(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    artifacts = payload.get("artifacts")
    raw_path = artifacts.get(name) if isinstance(artifacts, dict) else None
    if not isinstance(raw_path, str):
        return None
    try:
        value = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _receipt_build_test_complete(payload: dict[str, Any], sample: Sample) -> bool:
    guard = payload.get("build_test_guard")
    if not isinstance(guard, dict) or guard.get("success") is not True:
        return False
    if (
        guard.get("verification_mode") != "project_full"
        or guard.get("project_full_executed") is not True
    ):
        return False
    focused_preflight = guard.get("focused_preflight")
    if not (
        isinstance(focused_preflight, dict)
        and focused_preflight.get("success") is True
        and focused_preflight.get("status") in {"READY", "NOT_APPLICABLE"}
        and focused_preflight.get("acceptance") is False
        and focused_preflight.get("project_full_executed") is False
    ):
        return False
    isolation = guard.get("verification_isolation")
    snapshot = payload.get("snapshot")
    isolation_contract = (
        isolation.get("contract_version"),
        isolation.get("mode"),
    ) if isinstance(isolation, dict) else (None, None)
    if not (
        isinstance(isolation, dict)
        and isinstance(snapshot, dict)
        and isolation_contract
        in {
            ("project-full-fresh-worktree/v1", "detached_git_worktree"),
            (
                "project-full-direct-output-cleanup/v1",
                "runner_checkout_with_output_cleanup",
            ),
        }
        and isolation.get("success") is True
        and isolation.get("stage") == "completed"
        and isolation.get("cleanup_success") is True
        and (
            isolation_contract
            != ("project-full-fresh-worktree/v1", "detached_git_worktree")
            or isolation.get("base_commit") == snapshot.get("base_commit")
        )
    ):
        return False
    full_guard = _read_receipt_json(payload, "build_test_guard")
    if not (
        isinstance(full_guard, dict)
        and full_guard.get("success") is True
        and full_guard.get("verification_mode") == "project_full"
        and full_guard.get("project_full_executed") is True
    ):
        return False
    full_isolation = full_guard.get("verification_isolation")
    if not (
        isinstance(full_isolation, dict)
        and full_isolation.get("contract_version")
        == isolation.get("contract_version")
        and full_isolation.get("mode") == isolation.get("mode")
        and full_isolation.get("success") is True
        and full_isolation.get("stage") == isolation.get("stage")
        and full_isolation.get("cleanup_success") is True
        and (
            isolation_contract
            != ("project-full-fresh-worktree/v1", "detached_git_worktree")
            or full_isolation.get("base_commit") == isolation.get("base_commit")
        )
    ):
        return False
    full_focused = full_guard.get("focused_preflight")
    if not (
        isinstance(full_focused, dict)
        and full_focused.get("success") is True
        and full_focused.get("status") in {"READY", "NOT_APPLICABLE"}
        and full_focused.get("status") == focused_preflight.get("status")
        and full_focused.get("acceptance") is False
        and full_focused.get("project_full_executed") is False
    ):
        return False
    details = _build_test_details(payload)
    full_details = full_guard.get("details")
    if not isinstance(full_details, dict):
        return False
    full_build = _read_receipt_json(payload, "build_result")
    full_test = _read_receipt_json(payload, "test_result")
    if not (
        _receipt_command_stages_match(
            details.get("build"), full_details.get("build"), full_build
        )
        and _receipt_command_stages_match(
            details.get("test"), full_details.get("test"), full_test
        )
        and isinstance(full_details["test"].get("execution_evidence"), dict)
        and full_details["test"]["execution_evidence"].get("success") is True
        and isinstance(full_test.get("execution_evidence"), dict)
        and full_test["execution_evidence"].get("success") is True
    ):
        return False
    sample_test = details.get("sample_test")
    full_sample_test_stage = full_details.get("sample_test")
    if bool(isinstance(sample_test, dict)) != bool(
        isinstance(full_sample_test_stage, dict)
    ):
        return False
    if sample.test_command.strip() and not isinstance(sample_test, dict):
        return False
    if isinstance(sample_test, dict):
        full_sample_test = _read_receipt_json(payload, "sample_test_result")
        if not (
            _receipt_command_stages_match(
                sample_test, full_sample_test_stage, full_sample_test
            )
            and isinstance(
                full_sample_test_stage.get("execution_evidence"), dict
            )
            and full_sample_test_stage["execution_evidence"].get("success") is True
            and isinstance(full_sample_test.get("execution_evidence"), dict)
            and full_sample_test["execution_evidence"].get("success") is True
        ):
            return False
    return True


def _receipt_guard_evidence_complete(payload: dict[str, Any]) -> bool:
    evidence = _read_receipt_json(payload, "guard_evidence")
    checkpoint = payload.get("checkpoint")
    evidence_checkpoint = evidence.get("checkpoint") if isinstance(evidence, dict) else None
    return bool(
        isinstance(evidence, dict)
        and evidence.get("success") is True
        and evidence.get("accepted") is True
        and evidence.get("status") == "PASS"
        and evidence.get("resolution") == "resolved"
        and isinstance(checkpoint, dict)
        and isinstance(evidence_checkpoint, dict)
        and evidence_checkpoint.get("accepted") == checkpoint.get("accepted")
        and evidence_checkpoint.get("resolution") == checkpoint.get("resolution")
        and evidence_checkpoint.get("verify_status") == checkpoint.get("verify_status")
        and evidence_checkpoint.get("build_test_success")
        == checkpoint.get("build_test_success")
        and evidence.get("smell_guard") == payload.get("smell_guard")
        and evidence.get("test_changes") == payload.get("test_changes")
    )


def _receipt_tests_unchanged(payload: dict[str, Any]) -> bool:
    test_changes = payload.get("test_changes")
    if not (
        isinstance(test_changes, dict)
        and test_changes.get("success") is True
        and test_changes.get("status") == "TEST_SOURCE_UNCHANGED"
    ):
        return False
    count_keys = (
        "added_count",
        "changed_count",
        "deleted_count",
        "verification_config_added_count",
        "verification_config_changed_count",
        "verification_config_deleted_count",
        "test_strength_violations_count",
    )
    return all(test_changes.get(key) in (None, 0) for key in count_keys)


def _receipt_matches_current_candidate(
    sample: Sample,
    payload: dict[str, Any],
    *,
    deadline_monotonic: float | None = None,
) -> bool:
    snapshot_summary = payload.get("snapshot")
    snapshot = _read_receipt_json(payload, "snapshot")
    diff_path = _verify_diff_path(payload)
    if not (
        isinstance(snapshot_summary, dict)
        and isinstance(snapshot, dict)
        and diff_path is not None
        and snapshot_summary.get("scope") == "full_worktree_pre_verification"
        and snapshot.get("scope") == "full_worktree_pre_verification"
    ):
        return False
    try:
        if Path(str(snapshot.get("project_root") or "")).resolve() != sample.project_root.resolve():
            return False
    except OSError:
        return False
    base_commit = str(snapshot.get("base_commit") or "").strip()
    change_audit = snapshot.get("change_audit")
    summary_change_audit = snapshot_summary.get("change_audit")
    saved_diff = snapshot.get("diff")
    if not (
        base_commit
        and snapshot_summary.get("base_commit") == base_commit
        and isinstance(change_audit, dict)
        and change_audit.get("success") is True
        and isinstance(change_audit.get("change_count"), int)
        and change_audit["change_count"] > 0
        and isinstance(summary_change_audit, dict)
        and summary_change_audit.get("success") is True
        and summary_change_audit.get("change_count") == change_audit["change_count"]
        and isinstance(saved_diff, dict)
        and saved_diff.get("returncode") == 0
        and isinstance(saved_diff.get("stdout"), str)
        and bool(saved_diff["stdout"])
    ):
        return False
    category_counts = change_audit.get("category_counts")
    if not isinstance(category_counts, dict) or int(category_counts.get("test") or 0):
        return False
    try:
        artifact_diff = diff_path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    if saved_diff["stdout"] != artifact_diff:
        return False
    declared_test_paths = [
        item.strip()
        for item in str(sample.test_location or "").split(";")
        if item.strip()
    ]
    try:
        current = _capture_candidate_snapshot(
            sample.project_root,
            declared_test_paths=declared_test_paths,
            base_commit=base_commit,
            deadline_monotonic=deadline_monotonic,
        )
    except (OSError, TypeError, ValueError, subprocess.TimeoutExpired):
        return False
    current_audit = current.get("change_audit")
    current_diff = current.get("diff")
    current_categories = (
        current_audit.get("category_counts") if isinstance(current_audit, dict) else None
    )
    return bool(
        isinstance(current_audit, dict)
        and current_audit.get("success") is True
        and isinstance(current_categories, dict)
        and int(current_categories.get("test") or 0) == 0
        and isinstance(current_diff, dict)
        and current_diff.get("returncode") == 0
        and current_diff.get("stdout") == artifact_diff
    )


def _agent_project_full_receipt(
    sample: Sample,
    sample_dir: Path,
    verification_mode: str,
    opencode_returncode: int,
    last_trace: dict[str, Any],
    agent_verification_history: list[dict[str, Any]],
    terminal_authorization: dict[str, Any],
    *,
    deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    def reject(reason: str, digest: str = "") -> tuple[None, dict[str, Any]]:
        return None, _runner_final_receipt_audit(reason, agent_diff_sha256=digest)

    if verification_mode != "project_full":
        return reject("NOT_PROJECT_FULL")
    if opencode_returncode != 0:
        return reject("OPENCODE_NONZERO")
    if terminal_authorization.get("authorized") is not True:
        return reject(
            str(terminal_authorization.get("reason") or "FORMAL_PASS_TERMINAL_MISSING")
        )
    completed_count = int(last_trace.get("tools_after_last_verify") or 0)
    completed_tools = last_trace.get("completed_tools_after_last_verify")
    if not isinstance(completed_tools, list):
        completed_tools = []
    if completed_count != len(completed_tools):
        return reject("TOOLS_AFTER_LAST_VERIFY_UNCLASSIFIED")
    read_only_tools = {"read", "grep", "glob", "list", "todowrite"}
    if any(str(tool) not in read_only_tools for tool in completed_tools):
        return reject("MUTATION_AFTER_LAST_VERIFY")
    if (
        last_trace.get("last_output_parsed") is not True
        or last_trace.get("last_loop_decision") != "stop"
    ):
        return reject("AGENT_VERIFY_NOT_TERMINAL")
    payload = last_trace.get("last_payload")
    last_attempt = agent_verification_history[-1] if agent_verification_history else None
    if not isinstance(payload, dict) or not isinstance(last_attempt, dict):
        return reject("AGENT_VERIFY_MISSING")
    digest = str(last_attempt.get("diff_sha256") or "")
    if not (
        payload.get("schema_version") == "smell.verify.decision/v1"
        and payload.get("project_full_executed") is True
        and _accepted_verify_pass(payload, int(last_attempt.get("verify_returncode") or 0))
        and last_attempt.get("verify_source") == "agent"
        and last_attempt.get("verify_returncode") == 0
        and last_attempt.get("status") == "PASS"
        and last_attempt.get("accepted") is True
        and not str(last_attempt.get("failure_category") or "")
        and not list(last_attempt.get("failed_build_test_steps") or [])
        and payload.get("failure_pack") in (None, {})
        and digest
        and digest == _verify_diff_sha256(payload)
    ):
        return reject("AGENT_FORMAL_PASS_REQUIRED", digest)
    smell_guard = payload.get("smell_guard")
    checkpoint = payload.get("checkpoint")
    if not (
        isinstance(smell_guard, dict)
        and smell_guard.get("success") is True
        and int(smell_guard.get("failure_count") or 0) == 0
        and isinstance(checkpoint, dict)
        and checkpoint.get("accepted") is True
        and checkpoint.get("resolution") == "resolved"
        and checkpoint.get("verify_status") == "PASS"
        and checkpoint.get("build_test_success") is True
    ):
        return reject("FORMAL_GUARD_EVIDENCE_INCOMPLETE", digest)
    if not _receipt_tests_unchanged(payload):
        return reject("TEST_SOURCE_CHANGED", digest)
    if not _receipt_artifacts_complete(payload, sample_dir):
        return reject("EVIDENCE_INCOMPLETE", digest)
    if not (
        _receipt_build_test_complete(payload, sample)
        and _receipt_guard_evidence_complete(payload)
    ):
        return reject("FRESH_ISOLATION_INCOMPLETE", digest)
    if any(
        str(attempt.get("diff_sha256") or "") == digest
        and (
            attempt.get("status") != "PASS"
            or attempt.get("accepted") is not True
            or attempt.get("success") is not True
        )
        for attempt in agent_verification_history[:-1]
        if isinstance(attempt, dict)
    ):
        return reject("SAME_DIFF_CONTRADICTION", digest)
    if not _receipt_matches_current_candidate(
        sample,
        payload,
        deadline_monotonic=deadline_monotonic,
    ):
        return reject("CURRENT_DIFF_MISMATCH", digest)
    if validate_formal_verification_decision(
        payload,
        require_project_full_pass=True,
    ) is None:
        return reject("AGENT_FORMAL_PASS_REQUIRED", digest)
    return payload, _runner_final_receipt_audit(
        "REUSED",
        reused=True,
        agent_diff_sha256=digest,
    )


def _runner_final_verify(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    verification_mode: str,
    *,
    baseline_seal: str,
    deadline_monotonic: float | None,
    opencode_returncode: int,
    last_trace: dict[str, Any],
    agent_verification_history: list[dict[str, Any]],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    authorization = _runner_terminal_authorization(last_trace, opencode_returncode)
    receipt_payload: dict[str, Any] | None = None
    if getattr(args, "refactoring_backend", "direct") == "idea":
        reuse_audit = _runner_final_receipt_audit("IDEA_REQUIRES_FRESH_VERIFY")
    elif deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        reuse_audit = _runner_final_receipt_audit("SAMPLE_DEADLINE_REACHED")
    else:
        receipt_payload, reuse_audit = _agent_project_full_receipt(
            sample,
            sample_dir,
            verification_mode,
            opencode_returncode,
            last_trace,
            agent_verification_history,
            authorization,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            receipt_payload = None
            reuse_audit = _runner_final_receipt_audit("SAMPLE_DEADLINE_REACHED")
    if receipt_payload is not None:
        receipt_audit = {
            **reuse_audit,
            "source": "agent_smell_verify",
            "promotion_authorized": True,
            "raw_observation": _runner_observation_summary(0, receipt_payload),
            "terminal_evidence": authorization,
            "canonical_status": "PASS",
            "canonical_accepted": True,
        }
        _persist_verify_payload(sample_dir, receipt_payload)
        _persist_runner_final_receipt_audit(sample_dir, receipt_audit)
        return 0, receipt_payload, receipt_audit
    remaining = (
        None
        if deadline_monotonic is None
        else deadline_monotonic - time.monotonic()
    )
    if (
        authorization.get("formal_terminal_valid") is True
        and authorization.get("authorized") is not True
        and remaining is not None
        and remaining < RUNNER_FINAL_VERIFY_MIN_REMAINING_SECONDS
    ):
        terminal_payload = last_trace.get("last_payload")
        if isinstance(terminal_payload, dict):
            last_attempt = (
                agent_verification_history[-1]
                if agent_verification_history
                else {}
            )
            terminal_returncode = int(last_attempt.get("verify_returncode") or 0)
            receipt_audit = {
                **_runner_final_receipt_audit(
                    "FRESH_VERIFY_SKIPPED_INSUFFICIENT_BUDGET"
                ),
                "source": "agent_formal_terminal",
                "promotion_authorized": False,
                "raw_observation": _runner_observation_summary(
                    terminal_returncode,
                    terminal_payload,
                ),
                "terminal_evidence": authorization,
                "canonical_status": str(terminal_payload.get("status") or ""),
                "canonical_accepted": False,
                "reuse_rejected_reason": str(reuse_audit.get("reason") or ""),
                "remaining_seconds": max(0.0, remaining),
            }
            _persist_verify_payload(sample_dir, terminal_payload)
            _persist_runner_final_receipt_audit(sample_dir, receipt_audit)
            return terminal_returncode, terminal_payload, receipt_audit
    verify_returncode, raw_verify_payload = _run_verify(
        sample,
        sample_dir,
        args,
        verification_mode,
        baseline_seal=baseline_seal,
        deadline_monotonic=deadline_monotonic,
    )
    (sample_dir / "verify.runner-observation.json").write_text(
        json.dumps(raw_verify_payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    verify_payload, confirmation = _apply_runner_confirmation(
        verify_returncode,
        raw_verify_payload,
        authorization,
        require_project_full_pass=verification_mode == "project_full",
    )
    receipt_audit = {
        **_runner_final_receipt_audit(str(confirmation["reason"])),
        "source": "fresh_runner_verify",
        "promotion_authorized": confirmation["promotion_authorized"],
        "raw_observation": confirmation["raw_observation"],
        "terminal_evidence": confirmation["terminal_evidence"],
        "canonical_status": str(verify_payload.get("status") or ""),
        "canonical_accepted": verify_payload.get("accepted") is True,
        "reuse_rejected_reason": str(reuse_audit.get("reason") or ""),
    }
    _persist_verify_payload(sample_dir, verify_payload)
    _persist_runner_final_receipt_audit(sample_dir, receipt_audit)
    return verify_returncode, verify_payload, receipt_audit


def _parse_session_id_from_json_events(events_text: str) -> str:
    """Extract the session id from a --format json stdout event stream.

    opencode run --format json emits one JSON object per line on stdout. Each
    event carries the session id at the TOP LEVEL as ``sessionID`` (not nested
    under properties). We scan every line and return the first non-empty
    sessionID found, so this works even if the session.created event is not the
    first one emitted.
    """
    for raw in (events_text or "").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("sessionID") or ev.get("session_id") or ""
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        # Fallback: nested form (SSE-style properties.sessionID or properties.info.id).
        props = ev.get("properties")
        if isinstance(props, dict):
            sid2 = props.get("sessionID") or ""
            if not sid2 and isinstance(props.get("info"), dict):
                sid2 = props["info"].get("id") or ""
            if isinstance(sid2, str) and sid2.strip():
                return sid2.strip()
    return ""


def _verification_trace(events_text: str) -> dict[str, Any]:
    """Summarize completed smell_verify calls from one OpenCode JSON stream."""
    calls = 0
    verification_history: list[dict[str, Any]] = []
    tools_after_last_verify = 0
    tool_attempts_after_last_verify = 0
    completed_tools_after_last_verify: list[str] = []
    attempted_tools_after_last_verify: list[str] = []
    last_payload: dict[str, Any] | None = None
    last_decision = ""
    last_termination_reason = ""
    last_status = ""
    last_command_loop_state: dict[str, Any] | None = None
    control_events: list[dict[str, Any]] = []
    tool_sequence: list[str] = []
    attempted_tool_sequence: list[str] = []
    direct_idea_cli_calls = 0
    direct_edit_calls = 0
    for raw in (events_text or "").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_use":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        state = part.get("state")
        if not isinstance(state, dict):
            continue
        tool_name = str(part.get("tool") or "")
        if tool_name:
            attempted_tool_sequence.append(tool_name)
        state_input = state.get("input")
        if tool_name == "bash" and isinstance(state_input, dict):
            command = str(state_input.get("command") or "")
            if re.search(r"(?:^|[\s/])idea-refactor(?:\s|$)", command):
                direct_idea_cli_calls += 1
        if tool_name in {"edit", "write", "patch", "apply_patch"}:
            direct_edit_calls += 1
        if calls and tool_name != "smell_verify":
            tool_attempts_after_last_verify += 1
            attempted_tools_after_last_verify.append(tool_name)
        if calls and tool_name == "smell_verify":
            # A newer verification attempt supersedes the older receipt even
            # when the tool itself errors or is cancelled before producing a
            # decision. Only a later completed, parsed call may become final.
            last_payload = None
            last_decision = ""
            last_termination_reason = ""
            last_status = ""
            last_command_loop_state = None
        if state.get("status") != "completed":
            continue
        if tool_name:
            tool_sequence.append(tool_name)
        if part.get("tool") != "smell_verify":
            if calls:
                tools_after_last_verify += 1
                completed_tools_after_last_verify.append(tool_name)
            continue
        calls += 1
        tools_after_last_verify = 0
        tool_attempts_after_last_verify = 0
        completed_tools_after_last_verify = []
        attempted_tools_after_last_verify = []
        # A malformed newer verify must not leave an older payload reusable.
        last_payload = None
        metadata = state.get("metadata")
        verify_returncode = 0
        event_command_loop_state: dict[str, Any] | None = None
        if isinstance(metadata, dict):
            metadata_exit_code = metadata.get("exitCode")
            if isinstance(metadata_exit_code, int):
                verify_returncode = metadata_exit_code
            meta_loop = metadata.get("loop")
            if isinstance(meta_loop, dict):
                last_decision = str(meta_loop.get("decision") or "")
                last_termination_reason = str(
                    meta_loop.get("termination_reason") or ""
                )
            command_loop_state = metadata.get("command_loop_state")
            if isinstance(command_loop_state, dict):
                last_command_loop_state = command_loop_state
                event_command_loop_state = command_loop_state
            auto = metadata.get("auto_continuation")
            if isinstance(auto, dict):
                last_status = str(auto.get("status") or "")
        output = state.get("output")
        if not isinstance(output, str):
            continue
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            last_payload = payload
            control_events.append(
                {
                    "command_loop_state": event_command_loop_state,
                    "loop": payload.get("loop"),
                }
            )
            verification_history.append(
                {
                    "verification_call": calls - 1,
                    "event_id": str(part.get("callID") or event.get("id") or ""),
                    "event_timestamp": event.get("timestamp"),
                    **_compact_verify_attempt(
                        payload,
                        verify_source="agent",
                        verify_returncode=verify_returncode,
                    ),
                }
            )
    loop = last_payload.get("loop") if isinstance(last_payload, dict) else None
    if not last_decision and isinstance(loop, dict):
        last_decision = str(loop.get("decision") or "")
        last_termination_reason = str(loop.get("termination_reason") or "")
    if not last_status and isinstance(last_payload, dict):
        last_status = str(last_payload.get("status") or "")
    return {
        "smell_verify_calls": calls,
        "verification_history": verification_history,
        "verification_history_count": len(verification_history),
        "tools_after_last_verify": tools_after_last_verify,
        "tool_attempts_after_last_verify": tool_attempts_after_last_verify,
        "completed_tools_after_last_verify": completed_tools_after_last_verify,
        "attempted_tools_after_last_verify": attempted_tools_after_last_verify,
        "last_loop_decision": last_decision,
        "last_loop_termination_reason": last_termination_reason,
        "last_status": last_status,
        "last_failure_category": _failure_category_from_verify_payload(last_payload or {}),
        "last_native_diagnostics": _native_failure_diagnostics(last_payload or {}),
        "last_output_parsed": last_payload is not None,
        "last_payload": last_payload,
        "command_loop_state": last_command_loop_state,
        "control_events": control_events,
        "tool_sequence": tool_sequence,
        "attempted_tool_sequence": attempted_tool_sequence,
        "idea_refactor_preview_calls": tool_sequence.count("idea_refactor_preview"),
        "idea_refactor_apply_calls": tool_sequence.count("idea_refactor_apply"),
        "idea_edit_calls": tool_sequence.count("idea_edit"),
        "direct_idea_cli_calls": direct_idea_cli_calls,
        "direct_edit_calls": direct_edit_calls,
    }


def _idea_protocol_contract(controller_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    sequence = [
        str(tool)
        for attempt in controller_attempts
        for tool in attempt.get("tool_sequence", [])
        if str(tool)
    ]
    attempted_sequence = [
        str(tool)
        for attempt in controller_attempts
        for tool in attempt.get("attempted_tool_sequence", [])
        if str(tool)
    ]
    preview_positions = [index for index, tool in enumerate(sequence) if tool == "idea_refactor_preview"]
    apply_positions = [index for index, tool in enumerate(sequence) if tool == "idea_refactor_apply"]
    verify_positions = [index for index, tool in enumerate(sequence) if tool == "smell_verify"]
    ordered = bool(
        preview_positions
        and apply_positions
        and verify_positions
        and min(preview_positions) < min(apply_positions) < max(verify_positions)
    )
    direct_idea_cli_calls = sum(int(attempt.get("direct_idea_cli_calls") or 0) for attempt in controller_attempts)
    direct_edit_calls = sum(int(attempt.get("direct_edit_calls") or 0) for attempt in controller_attempts)
    violations = []
    if not preview_positions:
        violations.append("IDEA_PREVIEW_MISSING")
    if not apply_positions:
        violations.append("IDEA_APPLY_MISSING")
    if not verify_positions:
        violations.append("SMELL_VERIFY_MISSING")
    if preview_positions and apply_positions and verify_positions and not ordered:
        violations.append("IDEA_PROTOCOL_ORDER_INVALID")
    if direct_idea_cli_calls:
        violations.append("DIRECT_IDEA_CLI_USED")
    if direct_edit_calls:
        violations.append("DIRECT_EDIT_USED")
    return {
        "success": not violations,
        "protocol": "idea-proposal-v1",
        "tool_sequence": sequence,
        "attempted_tool_sequence": attempted_sequence,
        "preview_calls": len(preview_positions),
        "apply_calls": len(apply_positions),
        "verify_calls": len(verify_positions),
        "idea_edit_calls": sequence.count("idea_edit"),
        "direct_idea_cli_calls": direct_idea_cli_calls,
        "direct_edit_calls": direct_edit_calls,
        "violations": violations,
    }


def _typed_command_control(state: Any) -> dict[str, Any] | None:
    """Read the v7 transport projection without interpreting smell policy."""
    validated = validate_transferable_command_loop_state(state)
    if validated is None:
        return None
    control = validated.get("control")
    if not isinstance(control, dict):
        return None
    generation = control.get("generation")
    decision = control.get("decision")
    instruction = control.get("instruction")
    termination_reason = control.get("termination_reason")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or decision not in {"verify_required", "continue", "stop"}
        or not isinstance(instruction, str)
        or not isinstance(termination_reason, str)
        or (decision == "verify_required" and (
            generation != 0
            or instruction != "Call smell_verify now using the frozen command identity."
            or termination_reason
        ))
        or (decision == "continue" and (not instruction or termination_reason))
        or (decision == "stop" and (instruction or not termination_reason))
    ):
        return None
    return {
        "generation": generation,
        "decision": decision,
        "instruction": instruction,
        "termination_reason": termination_reason,
    }


def _runner_transport_plan(
    trace: dict[str, Any],
    *,
    previous_state: dict[str, Any] | None,
    transported_generations: set[int],
) -> dict[str, Any]:
    """Validate one plugin-owned control transition and return transport only."""
    stopped = {
        "action": "stop",
        "generation": None,
        "instruction": "",
        "state": None,
        "reason": "CONTROL_EVIDENCE_INVALID",
    }
    previous = _typed_command_control(previous_state)
    if previous is None:
        return stopped
    verify_calls = int(trace.get("smell_verify_calls") or 0)
    if verify_calls == 0:
        if previous["decision"] != "verify_required":
            return {**stopped, "reason": "VERIFY_REQUIRED_CONTROL_MISSING"}
        generation = int(previous["generation"])
        if generation in transported_generations:
            return {**stopped, "reason": "CONTROL_GENERATION_ALREADY_TRANSPORTED"}
        return {
            "action": "verify_required",
            "generation": generation,
            "instruction": previous["instruction"],
            "state": previous_state,
            "reason": "",
        }

    raw_control_events = trace.get("control_events")
    if "control_events" in trace:
        if not isinstance(raw_control_events, list) or len(raw_control_events) != verify_calls:
            return {**stopped, "reason": "CONTROL_EVENT_CHAIN_INCOMPLETE"}
        control_events = raw_control_events
    else:
        payload = trace.get("last_payload")
        control_events = [
            {
                "command_loop_state": trace.get("command_loop_state"),
                "loop": payload.get("loop") if isinstance(payload, dict) else None,
            }
        ]
    current = previous
    state: dict[str, Any] | None = None
    for event in control_events:
        if not isinstance(event, dict):
            return stopped
        event_state = event.get("command_loop_state")
        event_control = _typed_command_control(event_state)
        loop = event.get("loop")
        if event_control is None or not isinstance(loop, dict):
            return stopped
        if event_control["decision"] not in {"continue", "stop"}:
            return {**stopped, "reason": "COMPLETED_VERIFY_CONTROL_INVALID"}
        if event_control["generation"] != current["generation"] + 1:
            return {**stopped, "reason": "CONTROL_GENERATION_INVALID"}
        for key in ("generation", "decision", "instruction", "termination_reason"):
            if loop.get(key) != event_control[key]:
                return {**stopped, "reason": "CONTROL_PAYLOAD_STATE_MISMATCH"}
        current = event_control
        state = event_state
    if state != trace.get("command_loop_state"):
        return {**stopped, "reason": "CONTROL_FINAL_STATE_MISMATCH"}
    generation = int(current["generation"])
    if current["decision"] == "continue" and generation in transported_generations:
        return {**stopped, "reason": "CONTROL_GENERATION_ALREADY_TRANSPORTED"}
    return {
        "action": current["decision"],
        "generation": generation,
        "instruction": current["instruction"],
        "state": state,
        "reason": "",
    }


def _runner_closure_action(
    trace: dict[str, Any],
    *,
    previous_state: dict[str, Any] | None,
    transported_generations: set[int],
) -> str:
    return str(
        _runner_transport_plan(
            trace,
            previous_state=previous_state,
            transported_generations=transported_generations,
        )["action"]
    )


def _runner_continuation_prompt(plan: dict[str, Any]) -> str:
    """Transport the plugin instruction verbatim; the runner adds no policy."""
    instruction = plan.get("instruction")
    return instruction if isinstance(instruction, str) else ""


def _runner_terminal_authorization(
    trace: dict[str, Any],
    opencode_returncode: int,
) -> dict[str, Any]:
    """Authorize only confirmation of one typed plugin formal PASS terminal."""
    evidence: dict[str, Any] = {
        "authorized": False,
        "formal_terminal_valid": False,
        "reason": "FORMAL_PASS_TERMINAL_MISSING",
        "control": None,
        "terminal_receipt": None,
    }
    if opencode_returncode != 0:
        return {**evidence, "reason": "OPENCODE_NONZERO"}
    if int(trace.get("smell_verify_calls") or 0) <= 0:
        return {**evidence, "reason": "ZERO_VERIFY"}
    state = trace.get("command_loop_state")
    control = _typed_command_control(state)
    payload = trace.get("last_payload")
    loop = payload.get("loop") if isinstance(payload, dict) else None
    formal_decision = validate_formal_verification_decision(payload)
    if (
        control is None
        or control["decision"] != "stop"
        or not isinstance(loop, dict)
        or formal_decision is None
    ):
        return {**evidence, "reason": "TERMINAL_CONTROL_INVALID"}
    if any(
        loop.get(key) != control[key]
        for key in ("generation", "decision", "instruction", "termination_reason")
    ):
        return {**evidence, "reason": "TERMINAL_PAYLOAD_STATE_MISMATCH"}
    control_plan = trace.get("runner_control_plan")
    if not (
        isinstance(control_plan, dict)
        and control_plan.get("action") == "stop"
        and control_plan.get("reason") == ""
        and control_plan.get("generation") == control["generation"]
        and control_plan.get("state") == state
    ):
        return {
            **evidence,
            "reason": "TERMINAL_CONTROL_TRANSITION_UNCONFIRMED",
            "control": control,
        }
    receipt = state.get("terminal_receipt") if isinstance(state, dict) else None
    receipt_loop = receipt.get("loop") if isinstance(receipt, dict) else None
    if not isinstance(receipt, dict) or not isinstance(receipt_loop, dict):
        return {**evidence, "reason": "TERMINAL_RECEIPT_INVALID", "control": control}
    if any(
        receipt_loop.get(key) != control[key]
        for key in ("generation", "decision", "instruction", "termination_reason")
    ):
        return {**evidence, "reason": "TERMINAL_RECEIPT_CONTROL_MISMATCH", "control": control}
    if receipt.get("formalVerificationReceipt") != formal_decision.get(
        "formal_verification_receipt"
    ):
        return {
            **evidence,
            "reason": "TERMINAL_FORMAL_RECEIPT_MISMATCH",
            "control": control,
        }
    typed_receipt = {
        "stage": receipt.get("stage"),
        "status": receipt.get("status"),
        "success": receipt.get("success"),
        "accepted": receipt.get("accepted"),
        "resolution": receipt.get("resolution"),
        "termination_reason": receipt.get("terminationReason"),
        "failure_category": receipt.get("failureCategory"),
        "failure_group": receipt.get("failureGroup"),
        "loop": receipt_loop,
    }
    authorized = bool(
        typed_receipt["stage"] == "formal_verify"
        and typed_receipt["status"] == "PASS"
        and typed_receipt["success"] is True
        and typed_receipt["accepted"] is True
        and typed_receipt["resolution"] == "resolved"
        and typed_receipt["termination_reason"] == "PASS"
        and control["termination_reason"] == "PASS"
    )
    return {
        "authorized": authorized,
        "formal_terminal_valid": typed_receipt["stage"] == "formal_verify",
        "reason": "FORMAL_PASS_TERMINAL" if authorized else "FORMAL_NONACCEPT_TERMINAL",
        "control": control,
        "terminal_receipt": typed_receipt,
    }


def _runner_observation_summary(
    verify_returncode: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "verify_returncode": verify_returncode,
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "success": payload.get("success") is True,
        "accepted": payload.get("accepted") is True,
        "resolution": payload.get("resolution"),
        "termination_reason": payload.get("termination_reason"),
    }


def _apply_runner_confirmation(
    verify_returncode: int,
    payload: dict[str, Any],
    authorization: dict[str, Any],
    *,
    require_project_full_pass: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Confirm or downgrade plugin closure; never create acceptance."""
    raw_observation = _runner_observation_summary(verify_returncode, payload)
    raw_fresh_pass = _accepted_verify_pass(payload, verify_returncode)
    formal_fresh_pass = bool(
        raw_fresh_pass
        and validate_formal_verification_decision(
            payload,
            require_project_full_pass=require_project_full_pass,
        )
    )
    if formal_fresh_pass and authorization.get("authorized") is True:
        return payload, {
            "reason": "FORMAL_PASS_CONFIRMED",
            "promotion_authorized": True,
            "raw_observation": raw_observation,
            "terminal_evidence": authorization,
        }
    if not raw_fresh_pass:
        return payload, {
            "reason": "FRESH_VERIFY_REJECTED",
            "promotion_authorized": authorization.get("authorized") is True,
            "raw_observation": raw_observation,
            "terminal_evidence": authorization,
        }
    status = (
        "FRESH_VERIFY_PROTOCOL_INVALID"
        if not formal_fresh_pass
        else "RUNNER_CONFIRMATION_NOT_AUTHORIZED"
    )
    canonical = {
        **payload,
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "status": status,
        "resolution": "rejected",
        "progress": False,
        "termination_reason": status,
        "failure_pack": {
            "failure_category": status,
            "failure_group": "controller",
            "retryable": False,
            "verify_status": status,
            "highlights": [
                (
                    "Fresh verification returned PASS without one complete formal decision receipt."
                    if status == "FRESH_VERIFY_PROTOCOL_INVALID"
                    else "Fresh verification passed, but no typed formal PASS terminal authorized acceptance."
                )
            ],
            "next_action": "",
            "recommendations": [],
        },
    }
    checkpoint = payload.get("checkpoint")
    if isinstance(checkpoint, dict):
        canonical["checkpoint"] = {
            **checkpoint,
            "accepted": False,
            "resolution": "rejected",
            "verify_status": status,
        }
    return canonical, {
        "reason": (
            "FRESH_VERIFY_PROTOCOL_INVALID"
            if status == "FRESH_VERIFY_PROTOCOL_INVALID"
            else "FRESH_PASS_NOT_AUTHORIZED"
        ),
        "promotion_authorized": False,
        "raw_observation": raw_observation,
        "terminal_evidence": authorization,
    }


def _termination_reasons(
    verify_payload: dict[str, Any],
    trace: dict[str, Any],
    runtime_reason: str,
) -> tuple[str, str, str]:
    """Keep controller closure distinct from final verification diagnosis."""
    loop_payload = verify_payload.get("loop")
    verification_reason = (
        str(loop_payload.get("termination_reason") or "")
        if isinstance(loop_payload, dict)
        else ""
    ) or str(verify_payload.get("termination_reason") or "")
    control_reason = str(runtime_reason or "") or str(
        trace.get("last_loop_termination_reason") or ""
    )
    return control_reason, verification_reason, verification_reason or control_reason


def _opencode_run_command(args: argparse.Namespace, agent: str, session_id: str = "") -> list[str]:
    cmd = [args.opencode_bin, "run"]
    if session_id:
        cmd.extend(["--session", session_id])
    else:
        command = {
            "java-refactor-agent": "java-refactor-run",
            "smell-refactor-agent": "smell-refactor-run",
        }[agent]
        cmd.extend(["--command", command])
    cmd.extend([
        "--agent", agent,
        "--model", args.model,
        "--dangerously-skip-permissions",
        "--format", "json",
        "--print-logs",
    ])
    return cmd


def _select_agent(sample: Sample, args: argparse.Namespace) -> str:
    if args.agent:
        return args.agent
    if sample.language == "java":
        return "java-refactor-agent"
    return "smell-refactor-agent"


def _run_opencode(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    agent: str,
    verification_mode: str,
    *,
    session_id: str = "",
    continuation_prompt: str = "",
    command_loop_state: dict[str, Any] | None = None,
    attempt_suffix: str = "",
    hard_timeout_seconds: int | None = None,
    baseline_seal: str = "",
) -> tuple[int, str, str]:
    """Run one initial or same-session OpenCode turn."""
    config_path, runtime_env, auth_meta = _write_opencode_config(sample_dir, args)
    task = _task_prompt(sample)
    command_arguments = _command_arguments(task, args, verification_mode)
    stdin_payload = continuation_prompt if session_id else command_arguments
    task_path = _attempt_artifact_path(sample_dir, "task.txt", attempt_suffix)
    task_path.write_text(stdin_payload + "\n", encoding="utf-8")
    raw_input_path = _attempt_artifact_path(sample_dir, "raw-user-input.txt", attempt_suffix)
    raw_input_path.write_text(stdin_payload, encoding="utf-8")
    controller_source_path = _attempt_artifact_path(
        sample_dir, "controller-context.json", attempt_suffix
    )
    controller_source_path.write_text(
        json.dumps(
            _controller_context_manifest(
                command_loop_state,
                getattr(args, "refactoring_backend", "direct"),
            ),
            indent=2,
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    controller_system_path = _attempt_artifact_path(
        sample_dir, "controller-system.txt", attempt_suffix
    )
    message_manifest_path = _attempt_artifact_path(
        sample_dir, "message-manifest.json", attempt_suffix
    )

    env = os.environ.copy()
    env.update(_prepare_opencode_home(sample_dir))
    env.update(runtime_env)
    if config_path:
        env["OPENCODE_CONFIG"] = str(config_path)
    # The custom command owns the native in-session loop. Disable the legacy
    # session.idle mechanism so there is exactly one controller.
    env["SMELL_BATCH_RUN"] = "1"
    if session_id and command_loop_state:
        env["SMELL_COMMAND_LOOP_STATE_JSON"] = json.dumps(
            command_loop_state, separators=(",", ":"), sort_keys=True
        )
    else:
        env.pop("SMELL_COMMAND_LOOP_STATE_JSON", None)
    env["SMELL_BRIDGE_FILE"] = str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py")
    env["SMELL_PROJECT_ROOT"] = str(sample.project_root)
    if sample.canonical_project_root:
        env["SMELL_CANONICAL_PROJECT_ROOT"] = str(sample.canonical_project_root)
    env["SMELL_LANGUAGE"] = sample.language
    env["SMELL_SMELL"] = sample.smell
    env["SMELL_LOCATION"] = sample.location
    env.pop("SMELL_EVIDENCE", None)
    if sample.target_context:
        env["SMELL_TARGET_CONTEXT_JSON"] = json.dumps(
            sample.target_context, separators=(",", ":"), sort_keys=True
        )
    else:
        env.pop("SMELL_TARGET_CONTEXT_JSON", None)
    env["SMELL_VERIFICATION_MODE"] = verification_mode
    env["SMELL_SAMPLE_TEST_LOCATION"] = sample.test_location
    env["SMELL_SAMPLE_TEST_COMMAND"] = sample.test_command
    env["SMELL_SAMPLE_TEST_SOURCE"] = "dataset" if sample.test_command else ""
    env["SMELL_BUILD_COMMAND"] = sample.build_command
    env["SMELL_PROJECT_TEST_COMMAND"] = sample.project_test_command
    env["SMELL_VERIFICATION_CWD"] = sample.verification_cwd
    env["SMELL_VERIFICATION_COMMAND_SOURCE"] = (
        sample.verification_command_source
    )
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    refactoring_backend = getattr(args, "refactoring_backend", "direct")
    baseline_context_path = sample_dir / "baseline-capture.json"
    if baseline_context_path.is_file():
        env["SMELL_BASELINE_CONTEXT_FILE"] = str(baseline_context_path)
    else:
        env.pop("SMELL_BASELINE_CONTEXT_FILE", None)
    env["SMELL_CONTROLLER_CONTEXT_AUDIT_FILE"] = str(controller_system_path)
    # Backend authority is the frozen command policy transported through the
    # same CLI surface used by an interactive command.  Do not add runner-only
    # environment switches that a manual `opencode run` cannot reproduce.
    for legacy_backend_env in (
        "SMELL_REFACTORING_BACKEND",
        "SMELL_ENABLE_IDEA_TOOLS",
        "SMELL_IDEA_PREPARED",
        "SMELL_IDEA_PROJECT_ROOT",
    ):
        env.pop(legacy_backend_env, None)
    if baseline_seal:
        env["SMELL_BASELINE_SEAL"] = baseline_seal
    else:
        env.pop("SMELL_BASELINE_SEAL", None)
    # Agent-triggered verifies are loop feedback. After the model exits, the
    # runner either validates one exact project_full receipt as its final
    # decision or performs the existing fresh bridge verify.
    agent_artifact_root = sample_dir / "agent-artifacts"
    agent_artifact_root.mkdir(parents=True, exist_ok=True)
    env["SMELL_ARTIFACT_ROOT"] = str(agent_artifact_root)
    if args.projects:
        env["SMELL_PROJECTS"] = args.projects
    env["SMELL_REQUIRE_BUILD_TEST"] = "1"
    turn_timeout_seconds = float(
        hard_timeout_seconds or _opencode_timeout_seconds(args.sample_deadline)
    )
    model_event_inactivity_timeout = float(
        getattr(args, "model_event_inactivity_timeout", 300)
    )
    env[SAMPLE_DEADLINE_EPOCH_MS_ENV] = _deadline_epoch_ms(turn_timeout_seconds)

    # --format json: raw JSON events on stdout (for session-id parsing).
    # --print-logs: human-readable logs on stderr (written to run.log).
    cmd = _opencode_run_command(args, agent, session_id)
    command_payload = {
        "cmd": cmd,
        "command_arguments_transport": "stdin",
        "command_arguments_length": len(stdin_payload),
        "session_id_requested": session_id,
        "is_continuation": bool(session_id),
        "cwd": str(sample.project_root),
        "agent": agent,
        "auth": {**auth_meta, "api_key_source": "configured" if auth_meta.get("api_key_configured") else ""},
        "verification_mode": verification_mode,
        "verification_commands": {
            "build_command": sample.build_command,
            "project_test_command": sample.project_test_command,
            "verification_cwd": sample.verification_cwd,
            "source": sample.verification_command_source,
            "sample_test_source": "dataset" if sample.test_command else "",
        },
        "loop_policy": parse_command_policy(command_arguments).loop.to_dict(),
        "time_budget": {
            "source": "sample-deadline",
            "scope": "baseline-model-continuations-and-runner-final",
            "sample_deadline_seconds": args.sample_deadline,
            "opencode_hard_timeout_seconds": hard_timeout_seconds or _opencode_timeout_seconds(args.sample_deadline),
            "final_verify_budget": "remaining-sample-budget",
            "final_verify_mode": "runner_final",
            "idle_watchdog_enabled": True,
            "model_event_inactivity_timeout_seconds": model_event_inactivity_timeout,
            "idle_watchdog_suspends_for_child_processes": True,
        },
    }
    command_path = _attempt_artifact_path(sample_dir, "command.json", attempt_suffix)
    command_path.write_text(json.dumps(command_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    log_path = _attempt_artifact_path(sample_dir, "run.log", attempt_suffix)
    events_path = _attempt_artifact_path(sample_dir, "run.events.jsonl", attempt_suffix)
    with log_path.open("w", encoding="utf-8") as log, events_path.open("w", encoding="utf-8") as events_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(sample.project_root),
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
        )
        # OpenCode already supports reading the message from stdin. Keep the
        # command arguments as one exact string so yargs cannot coerce numeric
        # evidence tokens into numbers before run.ts formats the message.
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_payload)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
        # Read stdout (JSON events) incrementally, write to events file, and
        # detect session id.
        detected_sid = ""
        last_event_at = time.monotonic()

        def _drain_stdout():
            nonlocal detected_sid, last_event_at
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    last_event_at = time.monotonic()
                    events_file.write(line)
                    events_file.flush()
                    if not detected_sid:
                        sid = _parse_session_id_from_json_events(line)
                        if sid:
                            detected_sid = sid
            except (OSError, ValueError):
                # The bounded shutdown path may close stdout to release this
                # daemon reader after its drain allowance expires.
                return

        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + turn_timeout_seconds
        timeout_code = 0
        termination_reason = ""
        shutdown: dict[str, Any] = {}
        provider_failure = ""
        log_scan_offset = 0
        log_scan_tail = ""
        while proc.poll() is None:
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as log_reader:
                    log_reader.seek(log_scan_offset)
                    log_chunk = log_reader.read()
                    log_scan_offset = log_reader.tell()
            except OSError:
                log_chunk = ""
            provider_failure = _fatal_provider_error(log_scan_tail + log_chunk)
            log_scan_tail = (log_scan_tail + log_chunk)[-256:]
            if provider_failure:
                shutdown = _terminate_process_tree(proc)
                timeout_code = OPENCODE_FATAL_PROVIDER_RETURN_CODE
                break
            now = time.monotonic()
            if now > deadline:
                shutdown = _terminate_process_tree(proc)
                timeout_code = 124
                termination_reason = "SAMPLE_DEADLINE_REACHED"
                break
            if now - last_event_at > model_event_inactivity_timeout:
                if _process_tree_has_descendants(proc.pid):
                    # A bridge/build/test tool can be quiet for minutes. Give
                    # the model a fresh inactivity window after that child
                    # work completes instead of charging tool time as silence.
                    last_event_at = now
                else:
                    shutdown = _terminate_process_tree(proc)
                    timeout_code = 124
                    termination_reason = "MODEL_EVENT_INACTIVITY_TIMEOUT"
                    break
            time.sleep(1)
        drain_started = time.monotonic()
        reader.join(timeout=PROCESS_DRAIN_TIMEOUT_SECONDS)
        if reader.is_alive() and proc.stdout is not None:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            reader.join(timeout=PROCESS_DRAIN_TIMEOUT_SECONDS)
        stdout_drain_ms = round((time.monotonic() - drain_started) * 1000, 3)
        log_drain_started = time.monotonic()
        events_file.flush()
        log.flush()
        log_drain_ms = round((time.monotonic() - log_drain_started) * 1000, 3)
        if shutdown:
            shutdown.update(
                {
                    "stdout_drain_timeout_seconds": PROCESS_DRAIN_TIMEOUT_SECONDS,
                    "stdout_drain_completed": not reader.is_alive(),
                    "stdout_drain_ms": stdout_drain_ms,
                    "log_drain_ms": log_drain_ms,
                }
            )
        if provider_failure:
            provider_failure_path = _attempt_artifact_path(
                sample_dir, "provider.failure.json", attempt_suffix
            )
            provider_failure_path.write_text(
                json.dumps(
                    {
                        "failure_category": "PROVIDER_QUOTA_FAILED",
                        "provider_failure": provider_failure,
                        "retryable": False,
                        "shutdown": shutdown,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if termination_reason:
            termination_path = _attempt_artifact_path(
                sample_dir, "opencode-termination.json", attempt_suffix
            )
            termination_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "OPENCODE_TIMEOUT",
                        "termination_reason": termination_reason,
                        "model_event_inactivity_timeout_seconds": (
                            model_event_inactivity_timeout
                        ),
                        "shutdown": shutdown,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        rc = timeout_code if timeout_code else int(proc.returncode or 0)
        if not detected_sid:
            # Fallback: re-parse the full events file (thread may have set it
            # after the poll loop checked).
            try:
                detected_sid = _parse_session_id_from_json_events(events_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        message_manifest = {
            "schema_version": 1,
            "provenance": "controller_resume" if session_id else "user_command",
            "session_id_requested": session_id,
            "session_id_observed": detected_sid or session_id,
            "user_parts_mutated_by_plugin": False,
            "raw_user_input": {
                "path": str(raw_input_path),
                "bytes": raw_input_path.stat().st_size,
                "sha256": _sha256_file(raw_input_path),
            },
            "controller_context_source": {
                "path": str(controller_source_path),
                "sha256": _sha256_file(controller_source_path),
            },
            "controller_system_context": {
                "path": str(controller_system_path),
                "captured": controller_system_path.is_file(),
                "sha256": (
                    _sha256_file(controller_system_path)
                    if controller_system_path.is_file()
                    else ""
                ),
            },
        }
        message_manifest_path.write_text(
            json.dumps(message_manifest, indent=2, ensure_ascii=True, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return rc, detected_sid or session_id, termination_reason


def _append_result(results_path: Path, row: dict[str, Any]) -> None:
    fieldnames = [
        "sample_id",
        "smell",
        "project_name",
        "project_root",
        "execution_project_root",
        "location",
        "verification_mode",
        "verification_command_source",
        "agent",
        "status",
        "resolution",
        "accepted",
        "progress",
        "termination_reason",
        "control_termination_reason",
        "verification_termination_reason",
        "opencode_returncode",
        "opencode_timed_out",
        "opencode_failure_category",
        "verify_returncode",
        "duration_seconds",
        "setup_duration_seconds",
        "sample_budget_elapsed_seconds",
        "sample_dir",
        "note",
    ]
    exists = results_path.is_file()
    with results_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def _sample_timing_evidence(
    run_started_monotonic: float,
    sample_budget_started_monotonic: float | None = None,
) -> dict[str, str]:
    """Separate checkout/setup time from the command-owned sample budget."""

    now = time.monotonic()
    total = max(0.0, now - run_started_monotonic)
    if sample_budget_started_monotonic is None:
        return {
            "duration_seconds": f"{total:.1f}",
            "setup_duration_seconds": f"{total:.1f}",
            "sample_budget_elapsed_seconds": "",
        }
    return {
        "duration_seconds": f"{total:.1f}",
        "setup_duration_seconds": (
            f"{max(0.0, sample_budget_started_monotonic - run_started_monotonic):.1f}"
        ),
        "sample_budget_elapsed_seconds": (
            f"{max(0.0, now - sample_budget_started_monotonic):.1f}"
        ),
    }


def _checkout_only_sample(sample: Sample, run_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Diagnostic mode: create a REAL isolated checkout pinned to project_commit, verify it
    against the manifest, and record the revision audit — but do NOT invoke opencode/verify.

    Used to prove the real refactor entry pins project_commit (no HEAD fallback) without
    calling the model or performing a refactor.
    """
    started = time.time()
    sample_dir = run_dir / "samples" / f"sample-{_sanitize(sample.sample_id)}-{_sanitize(sample.project_name)}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    canonical_root = sample.project_root.resolve()
    row: dict[str, Any] = {
        "sample_id": sample.sample_id,
        "smell": sample.smell,
        "project_name": sample.project_name,
        "project_root": str(sample.project_root),
    }
    try:
        revisions = load_revisions(args.project_revisions)
        rev = resolve_revision(sample.project_name, revisions, args.project_revisions)
        assert_commit_present(canonical_root, rev.project_commit)
        prepared = _prepare_worktree(
            sample,
            _execution_checkout_run_dir(run_dir),
            target_commit=rev.project_commit,
            revisions=revisions,
        )
        audit = verify_checkout(prepared.project_root, rev)
        audit.update(
            audit_test_commit(
                canonical_root,
                sample.raw.get("test_commit", ""),
                rev.project_commit,
            )
        )
        if prepared.sibling_revision_audit:
            audit["sibling_checkouts"] = list(prepared.sibling_revision_audit)
        audit.update(
            verify_test_oracle(
                prepared.project_root,
                prepared.test_location,
                sample.raw.get("test_oracle_sha256", ""),
            )
        )
        row.update({
            "status": "CHECKOUT_OK",
            "execution_project_root": str(prepared.project_root),
            **audit,
        })
        _remove_worktree_checkout(canonical_root, prepared.project_root)
        for sibling in prepared.sibling_revision_audit:
            sibling_checkout = str(sibling.get("execution_project_root") or "").strip()
            if sibling_checkout:
                shutil.rmtree(sibling_checkout, ignore_errors=True)
    except ProjectRevisionError as exc:
        row.update({
            "status": exc.status,
            "execution_project_root": "",
            "requested_project_commit": exc.extra.get("project_commit", "") or (rev.project_commit if "rev" in dir() else ""),
            "actual_commit": "",
            "expected_tree_hash": rev.expected_tree_hash if "rev" in dir() else "",
            "actual_tree_hash": "",
            "project_revision_alignment": exc.status,
            "project_revisions_path": args.project_revisions,
            "error_message": exc.message,
        })
    except Exception as exc:  # noqa: BLE001
        row.update({"status": "CHECKOUT_ERROR", "execution_project_root": "",
                    "project_revision_alignment": "CHECKOUT_ERROR", "error_message": str(exc)})
    row["duration_seconds"] = f"{time.time() - started:.1f}"
    (sample_dir / "checkout_audit.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return row


def _run_sample(sample: Sample, run_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_started_monotonic = time.monotonic()
    dataset_audit = {
        "evidence": sample.evidence,
        "target_context": sample.target_context,
        "sample_test_source": "dataset" if sample.test_command else "",
        "verification_command_source": sample.verification_command_source,
    }
    sample_dir = run_dir / "samples" / f"sample-{_sanitize(sample.sample_id)}-{_sanitize(sample.project_name)}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    # Clean any stale opencode-home from a previous run of the same sample to
    # avoid opencode.db session residue / corruption. The home is recreated by
    # _prepare_opencode_home on the first _run_opencode call.
    stale_home = sample_dir / "opencode-home"
    if stale_home.exists():
        shutil.rmtree(stale_home, ignore_errors=True)
    agent = _select_agent(sample, args)
    verification_mode = _effective_verification_mode(sample, args)

    # One isolated checkout is used for the complete command-owned native loop.
    # The checkout is ALWAYS pinned to the authoritative project_commit from
    # project-revisions.json (resolved via smell_core.project_revision). There is no
    # HEAD fallback: if the manifest entry / commit / tree cannot be honored, the
    # sample fails fast with an explicit PROJECT_* status.
    revision_audit: dict[str, Any] = {}
    revisions_path = getattr(args, "project_revisions", DEFAULT_REVISIONS_PATH)
    try:
        revisions = load_revisions(revisions_path)
        rev = resolve_revision(sample.project_name, revisions, revisions_path)
        assert_commit_present(sample.project_root.resolve(), rev.project_commit)
        execution_sample = (
            _prepare_worktree(
                sample,
                _execution_checkout_run_dir(run_dir),
                target_commit=rev.project_commit,
                revisions=revisions,
            )
            if args.worktree
            else replace(sample, canonical_project_root=sample.project_root)
        )
        if args.worktree:
            revision_audit = verify_checkout(execution_sample.project_root, rev)
            if execution_sample.sibling_revision_audit:
                revision_audit["sibling_checkouts"] = list(
                    execution_sample.sibling_revision_audit
                )
        revision_audit.update(
            audit_test_commit(
                sample.project_root.resolve(),
                sample.raw.get("test_commit", ""),
                rev.project_commit,
            )
        )
        revision_audit.update(
            verify_test_oracle(
                execution_sample.project_root,
                execution_sample.test_location,
                sample.raw.get("test_oracle_sha256", ""),
            )
        )
    except ProjectRevisionError as exc:
        # Fail fast: record the deviation and abort this sample without running
        # opencode/verify or touching runtime HEAD.
        revision_audit = {
            "requested_project_commit": rev.project_commit if "rev" in dir() else "",
            "actual_commit": "",
            "expected_tree_hash": rev.expected_tree_hash if "rev" in dir() else "",
            "actual_tree_hash": "",
            "project_revision_alignment": exc.status,
            "project_revisions_path": revisions_path,
        }
        row = {
            "sample_id": sample.sample_id,
            "smell": sample.smell,
            "project_name": sample.project_name,
            "project_root": str(sample.project_root),
            "execution_project_root": "",
            "location": sample.location,
            "verification_mode": verification_mode,
            "verification_command_source": sample.verification_command_source,
            "agent": agent,
            "status": exc.status,
            "opencode_returncode": -1,
            "verify_returncode": -1,
            **_sample_timing_evidence(run_started_monotonic),
            "sample_dir": str(sample_dir),
            "note": f"project_revision_error: {exc.status}: {exc.message}",
        }
        result_summary = {
            **row,
            "attempts": [],
            "revision_audit": revision_audit,
            "dataset_audit": dataset_audit,
        }
        (sample_dir / "result.json").write_text(
            json.dumps(result_summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        return row
    (sample_dir / "sample.json").write_text(
        json.dumps(
            {
                **sample.raw,
                "execution_project_root": str(execution_sample.project_root),
                "effective_verification_commands": {
                    "build_command": execution_sample.build_command,
                    "project_test_command": execution_sample.project_test_command,
                    "verification_cwd": execution_sample.verification_cwd,
                    "source": execution_sample.verification_command_source,
                    "sample_test_source": (
                        "dataset" if execution_sample.test_command else ""
                    ),
                },
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sample_deadline_started_at_ms = int(time.time() * 1000)
    sample_budget_started_monotonic = time.monotonic()
    sample_deadline_monotonic = (
        sample_budget_started_monotonic
        + _opencode_timeout_seconds(args.sample_deadline)
    )

    baseline_capture: dict[str, Any] | None = None
    baseline_seal = ""
    if execution_sample.language == "java":
        baseline_returncode, baseline_capture = _run_capture_baseline(
            execution_sample,
            sample_dir,
            args,
            verification_mode,
            deadline_monotonic=sample_deadline_monotonic,
        )
        baseline_status = _baseline_failure_status(
            baseline_returncode,
            baseline_capture,
        )
        if baseline_status == "OPENCODE_TIMEOUT":
            timeout_payload = (
                baseline_capture
                if baseline_capture.get("schema_version") == "smell.verify.decision/v1"
                else _sample_deadline_payload(args.sample_deadline, stage="baseline")
            )
            _persist_verify_payload(sample_dir, timeout_payload)
            timeout_attempt = {
                **_compact_verify_attempt(
                    timeout_payload,
                    verify_source="runner_deadline",
                    verify_returncode=124,
                ),
                "attempt": 0,
                "controller_attempt": 0,
                "opencode_returncode": 124,
                "status": "OPENCODE_TIMEOUT",
                "reported_status": "OPENCODE_TIMEOUT",
                "accepted": False,
                "resolution": "unresolved",
                "session_id": "",
                "is_continuation": False,
                "opencode_timed_out": True,
                "opencode_failure_category": "OPENCODE_TIMEOUT",
            }
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "verification_command_source": sample.verification_command_source,
                "allow_test_changes": bool(getattr(args, "allow_test_changes", False)),
                "refactoring_backend": getattr(args, "refactoring_backend", "direct"),
                "agent": agent,
                "status": "OPENCODE_TIMEOUT",
                "failure_category": "OPENCODE_TIMEOUT",
                "resolution": "unresolved",
                "accepted": False,
                "progress": False,
                "termination_reason": "SAMPLE_DEADLINE_REACHED",
                "control_termination_reason": "SAMPLE_DEADLINE_REACHED",
                "verification_termination_reason": str(
                    timeout_payload.get("termination_reason") or ""
                ),
                "opencode_returncode": 124,
                "opencode_timed_out": True,
                "opencode_failure_category": "OPENCODE_TIMEOUT",
                "verify_returncode": 124,
                **_sample_timing_evidence(
                    run_started_monotonic,
                    sample_budget_started_monotonic,
                ),
                "sample_dir": str(sample_dir),
                "note": "sample_deadline_reached_during_baseline_capture",
            }
            (sample_dir / "result.json").write_text(
                json.dumps(
                    {
                        **row,
                        "attempts": [timeout_attempt],
                        "controller_attempts": [],
                        "revision_audit": revision_audit,
                        "dataset_audit": dataset_audit,
                        "baseline_capture": baseline_capture,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return row
        if baseline_status:
            baseline_error = str(
                baseline_capture.get("error")
                or baseline_capture.get("status")
                or "baseline capture failed"
            )
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "verification_command_source": sample.verification_command_source,
                "agent": agent,
                "status": baseline_status,
                "opencode_returncode": -1,
                "verify_returncode": -1,
                **_sample_timing_evidence(
                    run_started_monotonic,
                    sample_budget_started_monotonic,
                ),
                "sample_dir": str(sample_dir),
                "note": f"baseline_capture_failed: {baseline_error}",
            }
            (sample_dir / "result.json").write_text(
                json.dumps(
                    {
                        **row,
                        "attempts": [],
                        "controller_attempts": [],
                        "revision_audit": revision_audit,
                        "dataset_audit": dataset_audit,
                        "baseline_capture": baseline_capture,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return row
        baseline_seal = str(baseline_capture.get("baseline_seal") or "").strip()
        if not baseline_seal:
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "verification_command_source": sample.verification_command_source,
                "agent": agent,
                "status": "BASELINE_SEAL_MISSING",
                "opencode_returncode": -1,
                "verify_returncode": -1,
                **_sample_timing_evidence(
                    run_started_monotonic,
                    sample_budget_started_monotonic,
                ),
                "sample_dir": str(sample_dir),
                "note": "baseline_capture_failed: controller baseline seal missing",
            }
            (sample_dir / "result.json").write_text(
                json.dumps({**row, "attempts": [], "baseline_capture": baseline_capture}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            return row

    # Bootstrap .opencode once before starting the command-owned loop.
    _bootstrap_opencode(execution_sample.project_root, sample_dir)

    # Batch `opencode run` exits as the session becomes idle, so a fire-and-forget
    # plugin promptAsync cannot reliably create another turn. The plugin still
    # owns every decision; the runner only transports each validated v7 control
    # generation into the same OpenCode session once.
    controller_attempts: list[dict[str, Any]] = []
    agent_verification_history: list[dict[str, Any]] = []
    seen_agent_verification_ids: set[str] = set()
    session_id = ""
    continuation_prompt = ""
    command_loop_state: dict[str, Any] | None = _initial_command_loop_state(
        execution_sample,
        args,
        verification_mode,
        started_at_ms=sample_deadline_started_at_ms,
    )
    continuations_dispatched = 0
    transported_control_generations: set[int] = set()
    attempt_index = 0
    opencode_returncode = 0
    opencode_termination_reason = ""
    last_trace: dict[str, Any] = _verification_trace("")
    while True:
        remaining = int(sample_deadline_monotonic - time.monotonic())
        if remaining <= 0:
            opencode_returncode = 124
            break
        attempt_suffix = "" if attempt_index == 0 else f".continue-{attempt_index}"
        (
            opencode_returncode,
            detected_session_id,
            attempt_termination_reason,
        ) = _run_opencode(
            execution_sample,
            sample_dir,
            args,
            agent,
            verification_mode,
            session_id=session_id,
            continuation_prompt=continuation_prompt,
            command_loop_state=command_loop_state,
            attempt_suffix=attempt_suffix,
            hard_timeout_seconds=remaining,
            baseline_seal=baseline_seal,
        )
        if attempt_termination_reason:
            opencode_termination_reason = attempt_termination_reason
        if detected_session_id:
            session_id = detected_session_id
        events_path = _attempt_artifact_path(sample_dir, "run.events.jsonl", attempt_suffix)
        try:
            trace = _verification_trace(events_path.read_text(encoding="utf-8"))
        except OSError:
            trace = _verification_trace("")
        last_trace = trace
        trace_history = trace.get("verification_history")
        if isinstance(trace_history, list):
            for record in trace_history:
                if not isinstance(record, dict):
                    continue
                event_id = str(record.get("event_id") or "")
                if event_id and event_id in seen_agent_verification_ids:
                    continue
                if event_id:
                    seen_agent_verification_ids.add(event_id)
                agent_verification_history.append(
                    {
                        "attempt": len(agent_verification_history),
                        "controller_attempt": attempt_index,
                        "controller_suffix": attempt_suffix,
                        "opencode_returncode": opencode_returncode,
                        "session_id": session_id,
                        **record,
                    }
                )
        trace_summary = {
            key: value
            for key, value in trace.items()
            if key not in {"last_payload", "verification_history", "control_events"}
        }
        controller_attempts.append(
            {
                "attempt": attempt_index,
                "suffix": attempt_suffix,
                "opencode_returncode": opencode_returncode,
                "termination_reason": attempt_termination_reason,
                "session_id": session_id,
                **trace_summary,
            }
        )
        if opencode_returncode != 0 or not session_id:
            break
        transport_plan = _runner_transport_plan(
            trace,
            previous_state=command_loop_state,
            transported_generations=transported_control_generations,
        )
        last_trace["runner_control_plan"] = transport_plan
        controller_attempts[-1]["runner_control"] = {
            key: transport_plan.get(key)
            for key in ("action", "generation", "reason")
        }
        action = str(transport_plan["action"])
        if action == "stop":
            break
        generation = transport_plan.get("generation")
        if not isinstance(generation, int):
            break
        transported_control_generations.add(generation)
        continuations_dispatched += 1
        next_state = transport_plan.get("state")
        if isinstance(next_state, dict):
            command_loop_state = next_state
        continuation_prompt = _runner_continuation_prompt(transport_plan)
        _append_synthetic_message_event(
            sample_dir,
            {
                "schema_version": 1,
                "source": "batch_runner",
                "provenance": "controller_resume",
                "action": action,
                "session_id": session_id,
                "from_attempt": attempt_index,
                "to_attempt": attempt_index + 1,
                "continuation": continuations_dispatched,
                "control_generation": generation,
                "details_source": "command_loop_state.control",
            },
        )
        attempt_index += 1

    verify_returncode, verify_payload, runner_final_receipt = _runner_final_verify(
        execution_sample,
        sample_dir,
        args,
        verification_mode,
        baseline_seal=baseline_seal,
        deadline_monotonic=sample_deadline_monotonic,
        opencode_returncode=opencode_returncode,
        last_trace=last_trace,
        agent_verification_history=agent_verification_history,
    )
    if (
        opencode_returncode == 0
        and verify_returncode == 124
        and verify_payload.get("status") == "OPENCODE_TIMEOUT"
    ):
        opencode_returncode = 124
    original_verify_payload = verify_payload
    verify_returncode, verify_payload = _normalize_sample_timeout(
        opencode_returncode,
        verify_returncode,
        verify_payload,
        args.sample_deadline,
        termination_reason=(
            opencode_termination_reason or "SAMPLE_DEADLINE_REACHED"
        ),
    )
    verify_returncode, verify_payload = _normalize_opencode_failure(
        opencode_returncode,
        verify_returncode,
        verify_payload,
    )
    if verify_payload is not original_verify_payload:
        _persist_verify_payload(sample_dir, verify_payload)
    if getattr(args, "refactoring_backend", "direct") == "idea":
        _close_idea_project(execution_sample.project_root, sample_dir)
    final_verify_source = "runner_final"
    final_verify_execution = (
        "agent_project_full_receipt"
        if runner_final_receipt.get("reused") is True
        else "fresh_runner_verify"
    )
    opencode_failure_category = (
        "PROVIDER_QUOTA_FAILED"
        if opencode_returncode == OPENCODE_FATAL_PROVIDER_RETURN_CODE
        else (
            "OPENCODE_TIMEOUT"
            if opencode_returncode == 124
            else ("OPENCODE_FAILED" if opencode_returncode else "")
        )
    )
    final_status = _compute_status(opencode_returncode, verify_returncode, verify_payload)
    idea_protocol = (
        {
            **_idea_protocol_contract(controller_attempts),
            "authority": "audit_only",
        }
        if getattr(args, "refactoring_backend", "direct") == "idea"
        else None
    )
    final_verify_raw_status = final_status
    final_status, final_verify_audit = _reconcile_final_verify_status(
        final_verify_raw_status,
        verify_payload,
        agent_verification_history,
    )
    if isinstance(idea_protocol, dict):
        final_verify_audit["idea_protocol"] = idea_protocol
    raw_reconciled_verify_payload: dict[str, Any] | None = None
    if final_status in {
        "FINAL_VERIFY_INFRA_FAILED",
        "FLAKY_TEST_INCONCLUSIVE",
        "IDEA_PROTOCOL_FAILED",
    }:
        raw_reconciled_verify_payload = verify_payload
        verify_payload = _normalize_reconciled_final_failure(
            final_status,
            raw_reconciled_verify_payload,
            final_verify_audit,
        )
        _persist_verify_payload(sample_dir, verify_payload)
    accepted = _is_accepted_status(final_status)
    if (
        str(verify_payload.get("status") or "") != final_status
        or (verify_payload.get("accepted") is True) != accepted
    ):
        verify_payload = {
            **verify_payload,
            "status": final_status,
            "accepted": accepted,
            "success": accepted,
            "resolution": "resolved" if accepted else "rejected",
        }
        _persist_verify_payload(sample_dir, verify_payload)
    resolution = str(verify_payload.get("resolution") or "")
    progress = bool(verify_payload.get("progress")) or accepted
    runner_final_receipt["canonical_status"] = final_status
    runner_final_receipt["canonical_accepted"] = accepted
    _persist_runner_final_receipt_audit(sample_dir, runner_final_receipt)
    (
        control_termination_reason,
        verification_termination_reason,
        termination_reason,
    ) = _termination_reasons(
        verify_payload,
        last_trace,
        opencode_termination_reason,
    )
    build_test_guard = verify_payload.get("build_test_guard")
    verification_command_source = (
        str(build_test_guard.get("verification_command_source") or "")
        if isinstance(build_test_guard, dict)
        else ""
    ) or sample.verification_command_source
    last = {
        **_compact_verify_attempt(
            verify_payload,
            verify_source=final_verify_source,
            verify_returncode=verify_returncode,
        ),
        "attempt": len(agent_verification_history),
        "controller_attempt": attempt_index,
        "opencode_returncode": opencode_returncode,
        "status": final_status,
        "reported_status": str(verify_payload.get("status") or ""),
        "accepted": accepted,
        "resolution": resolution,
        "failure_category": (
            "FLAKY_TEST_INCONCLUSIVE"
            if final_status == "FLAKY_TEST_INCONCLUSIVE"
            else (
                str(final_verify_audit.get("infra_category") or "")
                if final_status == "FINAL_VERIFY_INFRA_FAILED"
                else _failure_category_from_verify_payload(verify_payload)
            )
        ),
        "session_id": session_id,
        "is_continuation": attempt_index > 0,
        "opencode_timed_out": opencode_returncode == 124,
        "opencode_failure_category": opencode_failure_category,
        "raw_status": final_verify_raw_status,
        "final_verify_audit": final_verify_audit,
        "runner_final_receipt": runner_final_receipt,
    }
    if raw_reconciled_verify_payload is not None:
        last["raw_verify_payload"] = raw_reconciled_verify_payload
        last["raw_reported_status"] = final_verify_raw_status
    attempts = _verification_attempt_history(agent_verification_history, last)
    note = (
        f"loop_policy={args.loop_mode}:{args.max_smell_verify_cycles};"
        f"runner_transports={continuations_dispatched};"
        f"opencode_timed_out={str(opencode_returncode == 124).lower()};"
        f"final_verify_source={final_verify_source};"
        f"final_verify_execution={final_verify_execution};"
        f"final_verify_raw_status={final_verify_raw_status};"
        f"final_verify_infra_category={final_verify_audit.get('infra_category') or ''};"
        f"agent_verifications={len(agent_verification_history)}"
    )

    row = {
        "sample_id": sample.sample_id,
        "smell": sample.smell,
        "project_name": sample.project_name,
        "project_root": str(sample.project_root),
        "execution_project_root": str(execution_sample.project_root),
        "location": execution_sample.location,
        "verification_mode": verification_mode,
        "verification_command_source": verification_command_source,
        "allow_test_changes": bool(getattr(args, "allow_test_changes", False)),
        "refactoring_backend": getattr(args, "refactoring_backend", "direct"),
        "agent": agent,
        "status": final_status,
        "failure_category": last["failure_category"],
        "resolution": resolution,
        "accepted": accepted,
        "progress": progress,
        "termination_reason": termination_reason,
        "control_termination_reason": control_termination_reason,
        "verification_termination_reason": verification_termination_reason,
        "opencode_returncode": last["opencode_returncode"],
        "opencode_timed_out": opencode_returncode == 124,
        "opencode_failure_category": opencode_failure_category,
        "verify_returncode": last["verify_returncode"],
        "final_verify_raw_status": final_verify_raw_status,
        "final_verify_infra_category": final_verify_audit.get("infra_category") or "",
        "final_verify_execution": final_verify_execution,
        "runner_final_receipt_reused": runner_final_receipt.get("reused") is True,
        "runner_final_receipt_reason": str(runner_final_receipt.get("reason") or ""),
        "same_diff_as_last_agent_pass": final_verify_audit.get(
            "same_diff_as_last_agent_pass"
        )
        is True,
        "last_agent_same_diff_test_failure": final_verify_audit.get(
            "last_agent_same_diff_test_failure"
        )
        is True,
        **_sample_timing_evidence(
            run_started_monotonic,
            sample_budget_started_monotonic,
        ),
        "sample_dir": str(sample_dir),
        "note": note,
    }
    result_summary = {
        **row,
        "attempts": attempts,
        "controller_attempts": controller_attempts,
        "agent_verification_count": len(agent_verification_history),
        "final_verify_audit": final_verify_audit,
        "runner_final_receipt": runner_final_receipt,
        "revision_audit": revision_audit,
        "dataset_audit": dataset_audit,
        "baseline_capture": baseline_capture,
        "idea_protocol": idea_protocol,
    }
    (sample_dir / "result.json").write_text(json.dumps(result_summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run smell dataset rows through the mounted-source OpenCode refactor agent.")
    parser.add_argument("--dataset", required=True, help="Path to one supported-language smell dataset CSV file.")
    parser.add_argument("--model", default="zai/glm-4.7")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-api-key", default="")
    parser.add_argument("--opencode-api-key-env", default="")
    parser.add_argument("--opencode-auth-json", default="auto")
    parser.add_argument("--opencode-base-url", default="")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--run-name", default="")
    parser.add_argument("--projects", default=os.environ.get("SMELL_PROJECTS", ""))
    parser.add_argument(
        "--build-command",
        default=None,
        help="Trusted build command for every selected project revision; requires --project-test-command.",
    )
    parser.add_argument(
        "--project-test-command",
        default=None,
        help="Trusted project-level test command for every selected project revision; requires --build-command.",
    )
    parser.add_argument(
        "--verification-cwd",
        default=None,
        help="Working directory for the explicit build/project-test pair; relative paths resolve from project_path.",
    )
    parser.add_argument("--smell", default="")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--project", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--loop-mode", choices=["off", "verify-failure"], default="verify-failure")
    parser.add_argument("--max-smell-verify-cycles", type=int, choices=range(0, 11), default=10)
    parser.add_argument("--loop-no-progress-limit", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--loop-on", default="smell,compile,test")
    parser.add_argument("--loop-instruction", default=LoopPolicy().instruction)
    parser.add_argument(
        "--sample-deadline",
        type=int,
        default=1800,
        help="Single shared time budget for baseline capture, model turns, continuations, and final verification.",
    )
    parser.add_argument(
        "--model-event-inactivity-timeout",
        type=int,
        default=300,
        help=(
            "Terminate an OpenCode model turn after this many seconds without a new "
            "JSON event; active bridge/build/test child processes suspend the watchdog."
        ),
    )
    parser.add_argument(
        "--verification-mode",
        choices=sorted(FINAL_VERIFICATION_MODES),
        default=None,
        help="Explicit run-wide override. Otherwise each CSV row is used, then project_full.",
    )
    parser.add_argument(
        "--allow-test-changes",
        action="store_true",
        help="Explicitly allow model edits under test-source roots. The controller still freezes verification configuration and requires the full build/test contract.",
    )
    parser.add_argument(
        "--refactoring-backend",
        choices=["direct", "idea"],
        default="direct",
        help="Java edit backend. 'idea' exposes the proposal wrapper while retaining the shared java-refactor-run command and guard state.",
    )
    parser.add_argument(
        "--agent",
        choices=["smell-refactor-agent", "java-refactor-agent"],
        default="",
    )
    parser.add_argument("--no-worktree", dest="worktree", action="store_false", help="Mutate project_path directly. Default is one isolated Git checkout per sample.")
    parser.set_defaults(worktree=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--checkout-only",
        action="store_true",
        help="Diagnostic: create a real isolated checkout pinned to project_commit and verify "
        "it against the manifest, but do NOT invoke opencode/verify. Proves the real refactor "
        "entry pins project_commit without calling the model.",
    )
    parser.add_argument(
        "--project-revisions",
        default=os.environ.get("PROJECT_REVISIONS", DEFAULT_REVISIONS_PATH),
        help="JSON manifest mapping project_name -> {project_commit, tree_hash, ...}. "
        "The real refactor runner pins every checkout to project_commit; HEAD is never used.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The runner owns normalization of dataset/CLI optimization hints. Once
    # test migration is enabled, only the complete project verification
    # contract is an independent behavior gate.
    if args.refactoring_backend == "idea" and args.agent == "smell-refactor-agent":
        parser.error("--refactoring-backend=idea requires the Java refactor agent")
    if args.model_event_inactivity_timeout <= 0:
        parser.error("--model-event-inactivity-timeout must be positive")
    # Validate the runner flags through the same parser used by the OpenCode
    # command hook, so batch and direct command invocations cannot drift.
    policy_validation_mode = (
        "project_full"
        if args.allow_test_changes
        else args.verification_mode or "project_full"
    )
    parse_command_policy(
        _command_arguments("validation task", args, policy_validation_mode)
    )
    dataset = Path(args.dataset).expanduser().resolve()
    try:
        samples = _resolve_verification_command_specs(
            _filter_samples(_load_samples(dataset), args),
            args,
        )
        effective_verification_modes = sorted(
            {_effective_verification_mode(sample, args) for sample in samples}
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.refactoring_backend == "idea" and any(sample.language != "java" for sample in samples):
        parser.error("--refactoring-backend=idea supports Java samples only")
    try:
        _validate_model_auth(args)
    except ValueError as exc:
        parser.error(str(exc))
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = args.run_name or f"smell-refactor-{dataset.stem}-{timestamp}"
    run_dir = Path(args.runs_root).expanduser().resolve() / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset": str(dataset),
        "model": args.model,
        "selected_count": len(samples),
        "run_dir": str(run_dir),
        "execution_checkout_root": str(_execution_checkout_run_dir(run_dir)),
        "verification_mode": (
            effective_verification_modes[0]
            if len(effective_verification_modes) == 1
            else "per_sample"
        ),
        "verification_modes": effective_verification_modes,
        "verification_mode_override": args.verification_mode,
        "verification_mode_forced_by_test_changes": bool(args.allow_test_changes),
        "verification_mode_default": "project_full",
        "verification_command_cli": {
            "build_command": str(args.build_command or ""),
            "project_test_command": str(args.project_test_command or ""),
            "verification_cwd": str(args.verification_cwd or ""),
            "source": (
                "cli"
                if args.build_command or args.project_test_command or args.verification_cwd
                else ""
            ),
        },
        "refactoring_backend": args.refactoring_backend,
        "loop_policy": parse_command_policy(
            _command_arguments("validation task", args, policy_validation_mode)
        ).loop.to_dict(),
        "time_budget": {
            "source": "sample-deadline",
            "scope": "baseline-model-continuations-and-runner-final",
            "sample_deadline_seconds": args.sample_deadline,
            "opencode_hard_timeout_seconds": _opencode_timeout_seconds(args.sample_deadline),
            "final_verify_budget": "remaining-sample-budget",
            "final_verify_mode": "runner_final",
            "idle_watchdog_enabled": True,
            "model_event_inactivity_timeout_seconds": (
                args.model_event_inactivity_timeout
            ),
            "idle_watchdog_suspends_for_child_processes": True,
        },
        "dry_run": args.dry_run,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        for sample in samples:
            print(f"{sample.sample_id}\t{sample.smell}\t{sample.project_name}\t{sample.location}")
        return 0

    if args.checkout_only:
        checkout_rows = []
        for index, sample in enumerate(samples, start=1):
            print(f"[checkout-only {index}/{len(samples)}] {sample.sample_id} {sample.project_name}", flush=True)
            row = _checkout_only_sample(sample, run_dir, args)
            checkout_rows.append(row)
            print(f"  -> status={row.get('status')} actual_commit={(row.get('actual_commit') or '')[:12]} "
                  f"actual_tree={(row.get('actual_tree_hash') or '')[:12]} "
                  f"alignment={row.get('project_revision_alignment')}", flush=True)
        (run_dir / "checkout_audit.csv").write_text(
            json.dumps(checkout_rows, indent=2) + "\n", encoding="utf-8"
        )
        return 0 if all(r.get("status") == "CHECKOUT_OK" for r in checkout_rows) else 1

    results_path = run_dir / "results.csv"
    failures = 0
    for index, sample in enumerate(samples, start=1):
        print(f"[{index}/{len(samples)}] {sample.sample_id} {sample.project_name} {sample.smell} {sample.location}", flush=True)
        try:
            row = _run_sample(sample, run_dir, args)
        except Exception as exc:  # keep batch artifacts for the failed row
            # Do not increment failures here: the status-based check below
            # counts RUNNER_FAILED once. Previously this double-counted.
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": "",
                "location": sample.location,
                "verification_mode": _effective_verification_mode(sample, args),
                "verification_command_source": sample.verification_command_source,
                "agent": _select_agent(sample, args),
                "status": "RUNNER_FAILED",
                "opencode_returncode": "",
                "verify_returncode": "",
                "duration_seconds": "",
                "sample_dir": "",
                "note": str(exc),
            }
        if not _is_accepted_status(row.get("status")):
            failures += 1
        _append_result(results_path, row)
        print(f"  -> {row.get('status')} {row.get('sample_dir')}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
