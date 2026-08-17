#!/usr/bin/env python3
"""Adversarial checks for runner confirm-only final verification."""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402
from bridge.smell_bridge import _snapshot_project  # noqa: E402
from smell_core.verification_receipt import (  # noqa: E402
    validate_formal_verification_decision,
)


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _fixture(root: Path) -> tuple[R.Sample, Path, dict[str, object], dict[str, object], list[dict[str, object]]]:
    project = root / "project"
    sample_dir = root / "sample"
    artifact_dir = sample_dir / "agent-artifacts" / "verify-receipt"
    project.mkdir()
    artifact_dir.mkdir(parents=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "self-check@example.invalid")
    _git(project, "config", "user.name", "Self Check")
    source = project / "sample.c"
    source.write_text("int target(void) { return 1; }\n", encoding="utf-8")
    _git(project, "add", "sample.c")
    _git(project, "commit", "-qm", "baseline")
    base_commit = _git(project, "rev-parse", "HEAD")
    source.write_text("int helper(void) { return 1; }\nint target(void) { return helper(); }\n", encoding="utf-8")

    snapshot = _snapshot_project(project, base_commit=base_commit)
    build = {
        "label": "build",
        "success": True,
        "status": "ok",
        "returncode": 0,
        "command": "cc -c sample.c",
        "script": "",
        "cwd": str(project),
        "source": "project",
        "output": "",
    }
    test = {
        "label": "test",
        "success": True,
        "status": "ok",
        "returncode": 0,
        "command": "./project-tests",
        "script": "",
        "cwd": str(project),
        "source": "project",
        "output": "1 test passed\n",
        "execution_evidence": {"success": True, "status": "fresh"},
    }
    isolation = {
        "contract_version": "project-full-fresh-worktree/v1",
        "mode": "detached_git_worktree",
        "success": True,
        "stage": "completed",
        "base_commit": base_commit,
        "snapshot_change_count": 1,
        "cleanup_success": True,
    }
    build_test = {
        "type": "build_test",
        "success": True,
        "verification_mode": "project_full",
        "project_full_executed": True,
        "focused_preflight": {
            "schema_version": "smell.focused-preflight/v1",
            "success": True,
            "status": "NOT_APPLICABLE",
            "acceptance": False,
            "project_full_executed": False,
        },
        "verification_isolation": isolation,
        "details": {"build": build, "test": test, "sample_test": None},
    }

    paths = {
        "build_test_guard": artifact_dir / "build-test-guard.full.json",
        "build_result": artifact_dir / "build.full.json",
        "test_result": artifact_dir / "test.full.json",
        "build_log": artifact_dir / "build.log",
        "test_log": artifact_dir / "test.log",
        "snapshot": artifact_dir / "snapshot.full.json",
        "diff": artifact_dir / "diff.patch",
        "diff_stat": artifact_dir / "diff.stat",
        "guard_evidence": artifact_dir / "guard-evidence.json",
    }
    _write_json(paths["build_test_guard"], build_test)
    _write_json(paths["build_result"], build)
    _write_json(paths["test_result"], test)
    paths["build_log"].write_text("", encoding="utf-8")
    paths["test_log"].write_text("1 test passed\n", encoding="utf-8")
    _write_json(paths["snapshot"], snapshot)
    paths["diff"].write_text(snapshot["diff"]["stdout"], encoding="utf-8")
    paths["diff_stat"].write_text(snapshot["diff_stat"]["stdout"], encoding="utf-8")
    test_changes = {
        "contract_version": "worktree-change-audit/v1",
        "success": True,
        "status": "TEST_SOURCE_UNCHANGED",
        "added_count": 0,
        "changed_count": 0,
        "deleted_count": 0,
        "verification_config_added_count": 0,
        "verification_config_changed_count": 0,
        "verification_config_deleted_count": 0,
    }
    _write_json(
        paths["guard_evidence"],
        {
            "success": True,
            "accepted": True,
            "progress": True,
            "status": "PASS",
            "resolution": "resolved",
            "checkpoint": {
                "accepted": True,
                "resolution": "resolved",
                "verify_status": "PASS",
                "build_test_success": True,
            },
            "smell_guard": {"success": True, "failure_count": 0, "results": []},
            "test_changes": test_changes,
        },
    )
    artifacts = {name: str(path) for name, path in paths.items()}
    artifact_index = {
        name: {"path": str(path), "bytes": path.stat().st_size}
        for name, path in paths.items()
    }
    payload: dict[str, object] = {
        "schema_version": "smell.verify.decision/v1",
        "success": True,
        "accepted": True,
        "progress": True,
        "status": "PASS",
        "resolution": "resolved",
        "project_full_executed": True,
        "smell_guard": {"success": True, "failure_count": 0, "results": []},
        "build_test_guard": {
            **build_test,
            "details": {
                "build": {key: build[key] for key in ("label", "success", "status", "returncode", "command", "script", "cwd", "source")},
                "test": {key: test[key] for key in ("label", "success", "status", "returncode", "command", "script", "cwd", "source")},
                "sample_test": None,
            },
        },
        "test_changes": test_changes,
        "snapshot": {
            "project_root": str(project),
            "scope": "full_worktree_pre_verification",
            "base_commit": base_commit,
            "change_audit": {"success": True, "change_count": 1},
            "artifacts": {
                "snapshot": str(paths["snapshot"]),
                "diff": str(paths["diff"]),
                "diff_stat": str(paths["diff_stat"]),
            },
        },
        "checkpoint": {
            "accepted": True,
            "resolution": "resolved",
            "verify_status": "PASS",
            "build_test_success": True,
            "baseline_project_commit": base_commit,
            "baseline_tree_hash": "",
            "production_diff_hash": "fixture-production-diff",
            "test_changes": {
                "current_tree_sha256": "",
                "current_verification_config_tree_sha256": "",
            },
        },
        "artifacts": artifacts,
        "artifact_index": artifact_index,
        "loop": {"decision": "stop"},
    }
    payload["formal_verification_receipt"] = {
        "schema_version": "smell.formal-verification-receipt/v1",
        "terminal_stage": "formal_verify",
        "status": "PASS",
        "success": True,
        "accepted": True,
        "resolution": "resolved",
        "candidate_identity": {
            "baseline_revision": base_commit,
            "baseline_tree": "",
            "production_diff": "fixture-production-diff",
            "test_tree": "",
            "verification_config_tree": "",
        },
        "outcome": "pass",
        "diagnostic_signature": "PASS",
        "guard": {
            "success": True,
            "failure_count": 0,
            "artifact_ref": str(paths["guard_evidence"]),
        },
        "build_test": {
            "success": True,
            "reason": "",
            "project_full_executed": True,
            "build_status": "ok",
            "test_status": "ok",
            "sample_test_status": "",
        },
        "fresh_isolation": isolation,
        "artifact_refs": artifacts,
    }
    attempt = R._compact_verify_attempt(payload, verify_source="agent", verify_returncode=0)
    trace: dict[str, object] = {
        "last_payload": payload,
        "last_output_parsed": True,
        "last_loop_decision": "stop",
        "tools_after_last_verify": 0,
        "tool_attempts_after_last_verify": 0,
    }
    sample = R.Sample(
        sample_id="receipt",
        language="c",
        smell="long_method",
        project_name="fixture",
        project_root=project,
        location="sample.c:method=target|line=2",
        evidence="",
        raw={},
    )
    return sample, sample_dir, payload, trace, [attempt]


def _state(
    *,
    generation: int,
    decision: str,
    instruction: str,
    termination_reason: str,
    stage: str | None = None,
    status: str = "",
    success: bool = False,
    accepted: bool = False,
    resolution: str = "rejected",
    formal_receipt: dict[str, object] | None = None,
    language: str = "python",
) -> dict[str, object]:
    control = {
        "generation": generation,
        "decision": decision,
        "instruction": instruction,
        "termination_reason": termination_reason,
    }
    receipt = None
    if stage is not None:
        receipt = {
            "stage": stage,
            "status": status,
            "success": success,
            "accepted": accepted,
            "resolution": resolution,
            "terminationReason": termination_reason,
            "failureCategory": "",
            "failureGroup": "",
            "formalVerificationReceipt": (
                formal_receipt if stage == "formal_verify" else None
            ),
            "ideaProtocolReceipt": None,
            "loop": control,
        }
    candidate = (
        formal_receipt.get("candidate_identity")
        if isinstance(formal_receipt, dict)
        else None
    )
    return {
        "schema_version": 7,
        "policy": {
            "task": "Continue the current smell refactoring task.",
            "verification_mode": "project_full",
            "refactoring_backend": "direct",
            "allow_test_changes": False,
            "checkpoint_required": True,
            "identity": {
                "project_root": "/tmp/project",
                "smell": "long_method",
                "location": "sample.py:method=target|line=1",
                "verification_mode": "project_full",
                "project_override_root": "",
                "language": language,
                "target_context_json": "",
                "sample_test_location": "",
                "sample_test_command": "",
                "build_command": "",
                "project_test_command": "",
                "verification_cwd": "",
                "verification_command_source": "",
                "sample_test_source": "",
            },
            "loop": {
                "mode": "verify-failure",
                "max_smell_verify_cycles": 5,
                "no_progress_limit": 1,
                "allowed_failure_groups": ["smell", "compile", "test"],
                "instruction": "repair narrowly",
                "sample_deadline_seconds": 1800,
            },
        },
        "target_identity_context": "",
        "started_at": 1,
        "control": control,
        "smell_verify_cycle_count": 0,
        "no_progress_count": 0,
        "last_failure_fingerprint": "",
        "best_metric_deficit": None,
        "best_structural_failure_count": None,
        "last_blocker_codes": [],
        "seen_structural_states": [],
        "formal_candidate_state": {
            "candidate_identity": candidate,
            "outcome": (
                formal_receipt.get("outcome")
                if isinstance(formal_receipt, dict)
                else ""
            ),
            "diagnostic_signature": (
                formal_receipt.get("diagnostic_signature")
                if isinstance(formal_receipt, dict)
                else ""
            ),
            "confirmation_required": False,
        },
        "idea_protocol_state": {
            "active_proposal": None,
            "proposal_blocker": None,
            "mutation_generation": 0,
            "verified_generation": 0,
            "mutation_route": "",
            "mutation_proposal_id": "",
            "revertible_apply_generation": None,
        },
        "terminal_receipt": receipt,
    }


def _trace(state: dict[str, object] | None, payload: dict[str, object] | None) -> dict[str, object]:
    trace: dict[str, object] = {
        "smell_verify_calls": 1 if payload is not None else 0,
        "last_output_parsed": payload is not None,
        "last_payload": payload,
        "command_loop_state": state,
    }
    if state is not None and payload is not None:
        initial = _state(
            generation=0,
            decision="verify_required",
            instruction="Call smell_verify now using the frozen command identity.",
            termination_reason="",
        )
        trace["runner_control_plan"] = R._runner_transport_plan(
            trace,
            previous_state=initial,
            transported_generations=set(),
        )
    return trace


def _pass_payload(*, language: str = "python") -> dict[str, object]:
    java = language == "java"
    isolation = {
        "contract_version": (
            "project-full-direct-output-cleanup/v1"
            if java
            else "project-full-fresh-worktree/v1"
        ),
        "mode": (
            "runner_checkout_with_output_cleanup"
            if java
            else "detached_git_worktree"
        ),
        "success": True,
        "stage": "completed",
        "cleanup_success": True,
    }
    artifacts = {
        "guard_evidence": "/tmp/guard.json",
        "build_result": "/tmp/build.json",
        "test_result": "/tmp/test.json",
        "diff": "/tmp/diff.patch",
    }
    receipt = {
        "schema_version": "smell.formal-verification-receipt/v1",
        "terminal_stage": "formal_verify",
        "status": "PASS",
        "success": True,
        "accepted": True,
        "resolution": "resolved",
        "candidate_identity": {
            "baseline_revision": "base-revision",
            "baseline_tree": "base-tree" if java else "",
            "production_diff": "production-diff",
            "test_tree": "test-tree" if java else "",
            "verification_config_tree": "verification-config-tree" if java else "",
        },
        "outcome": "pass",
        "diagnostic_signature": "PASS",
        "guard": {
            "success": True,
            "failure_count": 0,
            "artifact_ref": artifacts["guard_evidence"],
        },
        "build_test": {
            "success": True,
            "reason": "",
            "project_full_executed": True,
            "build_status": "ok",
            "test_status": "ok",
            "sample_test_status": "",
        },
        "fresh_isolation": isolation,
        "artifact_refs": artifacts,
    }
    return {
        "schema_version": "smell.verify.decision/v1",
        "success": True,
        "accepted": True,
        "status": "PASS",
        "resolution": "resolved",
        "termination_reason": "PASS",
        "project_full_executed": True,
        "smell_guard": {"success": True, "failure_count": 0, "results": []},
        "build_test_guard": {
            "success": True,
            "verification_mode": "project_full",
            "project_full_executed": True,
        },
        "test_changes": {"success": True, "status": "TEST_SOURCE_UNCHANGED"},
        "checkpoint": {
            "accepted": True,
            "resolution": "resolved",
            "verify_status": "PASS",
            "build_test_success": True,
        },
        "artifacts": artifacts,
        "artifact_index": {
            name: {"path": path, "bytes": 1}
            for name, path in artifacts.items()
        },
        "formal_verification_receipt": receipt,
    }


def _authorized_receipt_fixture(
    root: Path,
    *,
    language: str = "c",
) -> tuple[R.Sample, Path, dict[str, object], dict[str, object], list[dict[str, object]]]:
    sample, sample_dir, payload, trace, _history = _fixture(root)
    if language == "java":
        sample = R.Sample(**{**sample.__dict__, "language": "java"})
        isolation = payload["build_test_guard"]["verification_isolation"]
        isolation.update(
            {
                "contract_version": "project-full-direct-output-cleanup/v1",
                "mode": "runner_checkout_with_output_cleanup",
            }
        )
        full_guard_path = Path(payload["artifacts"]["build_test_guard"])
        full_guard = json.loads(full_guard_path.read_text(encoding="utf-8"))
        full_guard["verification_isolation"].update(
            {
                "contract_version": "project-full-direct-output-cleanup/v1",
                "mode": "runner_checkout_with_output_cleanup",
            }
        )
        _write_json(full_guard_path, full_guard)
        payload["artifact_index"]["build_test_guard"]["bytes"] = (
            full_guard_path.stat().st_size
        )
        candidate = payload["formal_verification_receipt"]["candidate_identity"]
        candidate["test_tree"] = "java-test-tree"
        candidate["verification_config_tree"] = "java-verification-config-tree"
        payload["checkpoint"]["test_changes"].update(
            {
                "current_tree_sha256": "java-test-tree",
                "current_verification_config_tree_sha256": (
                    "java-verification-config-tree"
                ),
            }
        )
    loop = {
        "generation": 1,
        "decision": "stop",
        "instruction": "",
        "termination_reason": "PASS",
    }
    state = _state(
        generation=1,
        decision="stop",
        instruction="",
        termination_reason="PASS",
        stage="formal_verify",
        status="PASS",
        success=True,
        accepted=True,
        resolution="resolved",
        formal_receipt=payload["formal_verification_receipt"],
        language=language,
    )
    payload["loop"] = loop
    trace.update(
        {
            "smell_verify_calls": 1,
            "last_payload": payload,
            "last_output_parsed": True,
            "last_loop_decision": "stop",
            "command_loop_state": state,
        }
    )
    initial = _state(
        generation=0,
        decision="verify_required",
        instruction="Call smell_verify now using the frozen command identity.",
        termination_reason="",
        language=language,
    )
    trace["runner_control_plan"] = R._runner_transport_plan(
        trace,
        previous_state=initial,
        transported_generations=set(),
    )
    history = [R._compact_verify_attempt(payload, verify_source="agent", verify_returncode=0)]
    return sample, sample_dir, payload, trace, history


def _check_receipt_reuse(root: Path) -> None:
    sample, sample_dir, payload, trace, history = _authorized_receipt_fixture(root)
    args = argparse.Namespace(projects="", sample_deadline=60, allow_test_changes=True)
    original_run_verify = R._run_verify
    calls: list[str] = []

    def unexpected_verify(*_args, **_kwargs):
        calls.append("fresh")
        raise AssertionError("qualified formal project_full receipt must skip duplicate full verification")

    R._run_verify = unexpected_verify
    try:
        rc, reused, audit = R._runner_final_verify(
            sample,
            sample_dir,
            args,
            "project_full",
            baseline_seal="c000-seal",
            deadline_monotonic=None,
            opencode_returncode=0,
            last_trace=trace,
            agent_verification_history=history,
        )
    finally:
        R._run_verify = original_run_verify
    assert rc == 0 and reused["status"] == "PASS" and reused["accepted"] is True, reused
    assert audit["reused"] is True and audit["reason"] == "REUSED", audit
    assert audit["terminal_evidence"]["authorized"] is True, audit
    assert calls == [], calls

    # Receipt reuse re-snapshots the live candidate.  That read-only Git work
    # must share the sample deadline; once it expires, reuse is rejected and
    # the canonical final result is the existing typed timeout.
    original_snapshot = R._capture_candidate_snapshot
    original_run_verify = R._run_verify
    snapshot_deadlines: list[float | None] = []
    verify_deadlines: list[float | None] = []
    deadline = time.monotonic() + 0.05

    def blocked_snapshot(*_args, deadline_monotonic=None, **_kwargs):
        snapshot_deadlines.append(deadline_monotonic)
        assert deadline_monotonic == deadline, deadline_monotonic
        while time.monotonic() < deadline_monotonic:
            time.sleep(0.001)
        raise subprocess.TimeoutExpired(["git", "diff"], timeout=0.05)

    def deadline_verify(*_args, deadline_monotonic=None, **_kwargs):
        verify_deadlines.append(deadline_monotonic)
        return 124, {
            "schema_version": "smell.verify.decision/v1",
            "success": False,
            "accepted": False,
            "status": "OPENCODE_TIMEOUT",
            "resolution": "rejected",
        }

    R._capture_candidate_snapshot = blocked_snapshot
    R._run_verify = deadline_verify
    try:
        timeout_rc, timeout_payload, timeout_audit = R._runner_final_verify(
            sample,
            sample_dir,
            args,
            "project_full",
            baseline_seal="c000-seal",
            deadline_monotonic=deadline,
            opencode_returncode=0,
            last_trace=trace,
            agent_verification_history=history,
        )
    finally:
        R._capture_candidate_snapshot = original_snapshot
        R._run_verify = original_run_verify
    assert snapshot_deadlines == [deadline], snapshot_deadlines
    assert verify_deadlines == [deadline], verify_deadlines
    assert timeout_rc == 124 and timeout_payload["accepted"] is False, timeout_payload
    assert timeout_audit["reuse_rejected_reason"] == "SAMPLE_DEADLINE_REACHED", timeout_audit

    java_root = root / "java-direct-output-cleanup"
    java_root.mkdir()
    (
        java_sample,
        java_sample_dir,
        _java_payload,
        java_trace,
        java_history,
    ) = _authorized_receipt_fixture(java_root, language="java")
    R._run_verify = unexpected_verify
    try:
        java_rc, java_reused, java_audit = R._runner_final_verify(
            java_sample,
            java_sample_dir,
            args,
            "project_full",
            baseline_seal="c000-seal",
            deadline_monotonic=None,
            opencode_returncode=0,
            last_trace=java_trace,
            agent_verification_history=java_history,
        )
    finally:
        R._run_verify = original_run_verify
    assert java_rc == 0 and java_reused["accepted"] is True, java_reused
    assert java_audit["reused"] is True and java_audit["reason"] == "REUSED", (
        java_audit
    )
    assert calls == [], calls

    fresh_pass = _pass_payload()

    def trace_for_payload(changed_payload: dict[str, object]) -> dict[str, object]:
        changed_state = copy.deepcopy(trace["command_loop_state"])
        changed_receipt = changed_payload.get("formal_verification_receipt")
        changed_state["terminal_receipt"]["formalVerificationReceipt"] = changed_receipt
        if isinstance(changed_receipt, dict):
            changed_state["formal_candidate_state"] = {
                "candidate_identity": changed_receipt.get("candidate_identity"),
                "outcome": changed_receipt.get("outcome"),
                "diagnostic_signature": changed_receipt.get("diagnostic_signature"),
                "confirmation_required": False,
            }
        changed_trace = {
            **trace,
            "last_payload": changed_payload,
            "command_loop_state": changed_state,
        }
        initial = _state(
            generation=0,
            decision="verify_required",
            instruction="Call smell_verify now using the frozen command identity.",
            termination_reason="",
        )
        changed_trace["runner_control_plan"] = R._runner_transport_plan(
            changed_trace,
            previous_state=initial,
            transported_generations=set(),
        )
        return changed_trace

    def assert_fresh(
        reason: str,
        *,
        changed_args: argparse.Namespace | None = None,
        changed_sample: R.Sample | None = None,
        changed_trace: dict[str, object] | None = None,
        changed_history: list[dict[str, object]] | None = None,
        opencode_returncode: int = 0,
        deadline_monotonic: float | None = None,
        fresh_payload: dict[str, object] | None = None,
        fresh_returncode: int = 0,
    ) -> tuple[dict[str, object], dict[str, object]]:
        calls.clear()
        observation = fresh_payload or fresh_pass

        def fresh(*_args, **_kwargs):
            calls.append("fresh")
            return fresh_returncode, copy.deepcopy(observation)

        R._run_verify = fresh
        try:
            _rc, decision, receipt = R._runner_final_verify(
                changed_sample or sample,
                sample_dir,
                changed_args or args,
                "project_full",
                baseline_seal="c000-seal",
                deadline_monotonic=deadline_monotonic,
                opencode_returncode=opencode_returncode,
                last_trace=changed_trace or trace,
                agent_verification_history=changed_history or history,
            )
        finally:
            R._run_verify = original_run_verify
        assert calls == ["fresh"], (reason, calls)
        assert receipt["reused"] is False, (reason, receipt)
        assert receipt["reuse_rejected_reason"] == reason, (reason, receipt)
        return decision, receipt

    assert_fresh(
        "IDEA_REQUIRES_FRESH_VERIFY",
        changed_args=argparse.Namespace(
            **{**vars(args), "refactoring_backend": "idea"}
        ),
    )

    read_only_after = {
        **trace,
        "tools_after_last_verify": 1,
        "tool_attempts_after_last_verify": 1,
        "completed_tools_after_last_verify": ["todowrite"],
        "attempted_tools_after_last_verify": ["todowrite"],
    }
    calls.clear()
    R._run_verify = unexpected_verify
    try:
        read_only_rc, read_only_payload, read_only_audit = R._runner_final_verify(
            sample,
            sample_dir,
            args,
            "project_full",
            baseline_seal="c000-seal",
            deadline_monotonic=None,
            opencode_returncode=0,
            last_trace=read_only_after,
            agent_verification_history=history,
        )
    finally:
        R._run_verify = original_run_verify
    assert read_only_rc == 0 and read_only_payload["accepted"] is True, read_only_payload
    assert read_only_audit["reused"] is True, read_only_audit
    assert calls == [], calls

    mutating_after = {
        **trace,
        "tools_after_last_verify": 1,
        "tool_attempts_after_last_verify": 1,
        "completed_tools_after_last_verify": ["edit"],
        "attempted_tools_after_last_verify": ["edit"],
    }
    assert_fresh("MUTATION_AFTER_LAST_VERIFY", changed_trace=mutating_after)

    changed_tests = copy.deepcopy(payload)
    changed_tests["test_changes"]["status"] = "TEST_SOURCE_CHANGE_ALLOWED"
    changed_tests["test_changes"]["changed_count"] = 1
    assert_fresh(
        "TEST_SOURCE_CHANGED",
        changed_trace=trace_for_payload(changed_tests),
        changed_history=[R._compact_verify_attempt(changed_tests, verify_source="agent", verify_returncode=0)],
    )

    missing_artifact = copy.deepcopy(payload)
    del missing_artifact["artifacts"]["test_result"]
    del missing_artifact["artifact_index"]["test_result"]
    assert_fresh(
        "EVIDENCE_INCOMPLETE",
        changed_trace=trace_for_payload(missing_artifact),
        changed_history=[R._compact_verify_attempt(missing_artifact, verify_source="agent", verify_returncode=0)],
    )

    no_cleanup = copy.deepcopy(payload)
    no_cleanup["build_test_guard"]["verification_isolation"]["cleanup_success"] = False
    assert_fresh(
        "FRESH_ISOLATION_INCOMPLETE",
        changed_trace=trace_for_payload(no_cleanup),
        changed_history=[R._compact_verify_attempt(no_cleanup, verify_source="agent", verify_returncode=0)],
    )

    failed_focused = copy.deepcopy(payload)
    failed_focused["build_test_guard"]["focused_preflight"].update(
        {"success": False, "status": "FAILED"}
    )
    assert_fresh(
        "FRESH_ISOLATION_INCOMPLETE",
        changed_trace=trace_for_payload(failed_focused),
        changed_history=[R._compact_verify_attempt(failed_focused, verify_source="agent", verify_returncode=0)],
    )
    assert_fresh(
        "FRESH_ISOLATION_INCOMPLETE",
        changed_sample=R.Sample(
            **{**sample.__dict__, "test_command": "./focused-sample-test"}
        ),
    )

    contradiction = copy.deepcopy(history[0])
    contradiction.update(
        {
            "status": "TEST_FAILED",
            "accepted": False,
            "success": False,
            "failure_category": "TEST_BEHAVIOR_REGRESSION",
        }
    )
    assert_fresh(
        "SAME_DIFF_CONTRADICTION",
        changed_history=[contradiction, history[0]],
    )

    source = sample.project_root / "sample.c"
    original_source = source.read_text(encoding="utf-8")
    source.write_text(original_source + "int later(void) { return 2; }\n", encoding="utf-8")
    try:
        assert_fresh("CURRENT_DIFF_MISMATCH")
    finally:
        source.write_text(original_source, encoding="utf-8")

    timeout_payload = {
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "status": "OPENCODE_TIMEOUT",
        "resolution": "rejected",
    }
    deadline_decision, _ = assert_fresh(
        "SAMPLE_DEADLINE_REACHED",
        deadline_monotonic=time.monotonic() - 1,
        fresh_payload=timeout_payload,
        fresh_returncode=124,
    )
    assert deadline_decision["accepted"] is False, deadline_decision

    runtime_decision, _ = assert_fresh(
        "OPENCODE_NONZERO",
        opencode_returncode=124,
    )
    assert runtime_decision["accepted"] is False, runtime_decision


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="runner-confirm-only-") as raw:
        root = Path(raw)
        reuse_root = root / "reuse"
        reuse_root.mkdir()
        _check_receipt_reuse(reuse_root)
        project = root / "project"
        sample_dir = root / "sample"
        project.mkdir()
        sample_dir.mkdir()
        sample = R.Sample(
            sample_id="confirm-only",
            language="python",
            smell="long_method",
            project_name="fixture",
            project_root=project,
            location="sample.py:function=target|line=1",
            evidence="",
            raw={},
        )
        args = argparse.Namespace(projects="", sample_deadline=60, allow_test_changes=False)

        formal_loop = {
            "generation": 1,
            "decision": "stop",
            "instruction": "",
            "termination_reason": "PASS",
        }
        formal_payload = {**_pass_payload(), "loop": formal_loop}
        formal_state = _state(
            generation=1,
            decision="stop",
            instruction="",
            termination_reason="PASS",
            stage="formal_verify",
            status="PASS",
            success=True,
            accepted=True,
            resolution="resolved",
            formal_receipt=formal_payload["formal_verification_receipt"],
        )
        formal_trace = _trace(formal_state, formal_payload)

        original_run_verify = R._run_verify
        calls: list[str] = []

        def run_case(
            name: str,
            trace: dict[str, object],
            fresh_payload: dict[str, object],
            *,
            fresh_returncode: int = 0,
            opencode_returncode: int = 0,
        ) -> tuple[dict[str, object], dict[str, object]]:
            calls.clear()

            def fresh(*_args, **_kwargs):
                calls.append("fresh")
                return fresh_returncode, copy.deepcopy(fresh_payload)

            R._run_verify = fresh
            try:
                _rc, decision, audit = R._runner_final_verify(
                    sample,
                    sample_dir,
                    args,
                    "project_full",
                    baseline_seal="c000-seal",
                    deadline_monotonic=None,
                    opencode_returncode=opencode_returncode,
                    last_trace=trace,
                    agent_verification_history=[],
                )
            finally:
                R._run_verify = original_run_verify
            assert calls == ["fresh"], (name, calls)
            raw_observation = json.loads(
                (sample_dir / "verify.runner-observation.json").read_text(encoding="utf-8")
            )
            assert raw_observation == fresh_payload, (name, raw_observation)
            persisted = json.loads((sample_dir / "verify.json").read_text(encoding="utf-8"))
            receipt = json.loads(
                (sample_dir / "runner-final-receipt.json").read_text(encoding="utf-8")
            )
            assert persisted == decision, (name, persisted, decision)
            assert receipt == audit, (name, receipt, audit)
            assert audit["canonical_status"] == decision["status"], (name, audit, decision)
            assert audit["canonical_accepted"] is (decision.get("accepted") is True), (name, audit, decision)
            return decision, audit

        confirmed, confirmed_audit = run_case("formal-pass", formal_trace, _pass_payload())
        assert confirmed["status"] == "PASS" and confirmed["accepted"] is True, confirmed
        assert confirmed_audit["reason"] == "FORMAL_PASS_CONFIRMED", confirmed_audit
        assert confirmed_audit["reused"] is False, confirmed_audit
        assert validate_formal_verification_decision(
            confirmed,
            require_project_full_pass=True,
        ), confirmed

        java_payload = {
            **_pass_payload(language="java"),
            "loop": formal_loop,
        }
        java_state = _state(
            generation=1,
            decision="stop",
            instruction="",
            termination_reason="PASS",
            stage="formal_verify",
            status="PASS",
            success=True,
            accepted=True,
            resolution="resolved",
            formal_receipt=java_payload["formal_verification_receipt"],
            language="java",
        )
        java_trace = _trace(java_state, java_payload)
        java_confirmed, java_audit = run_case(
            "java-direct-output-cleanup-pass",
            java_trace,
            _pass_payload(language="java"),
        )
        assert java_confirmed["accepted"] is True, java_confirmed
        assert java_audit["reason"] == "FORMAL_PASS_CONFIRMED", java_audit
        assert validate_formal_verification_decision(
            java_confirmed,
            require_project_full_pass=True,
        ), java_confirmed

        invalid_java_isolation = _pass_payload(language="java")
        invalid_java_isolation["formal_verification_receipt"]["fresh_isolation"][
            "mode"
        ] = "detached_git_worktree"
        invalid_java, invalid_java_audit = run_case(
            "java-mismatched-isolation-contract",
            java_trace,
            invalid_java_isolation,
        )
        assert invalid_java["status"] == "FRESH_VERIFY_PROTOCOL_INVALID", invalid_java
        assert invalid_java["accepted"] is False, invalid_java
        assert invalid_java_audit["reason"] == "FRESH_VERIFY_PROTOCOL_INVALID", (
            invalid_java_audit
        )

        zero_decision, _ = run_case("zero-verify", _trace(None, None), _pass_payload())
        assert zero_decision["status"] == "RUNNER_CONFIRMATION_NOT_AUTHORIZED", zero_decision

        continue_loop = {
            "generation": 1,
            "decision": "continue",
            "instruction": "Repair and verify again.",
            "termination_reason": "",
        }
        continue_state = _state(
            generation=1,
            decision="continue",
            instruction=continue_loop["instruction"],
            termination_reason="",
        )
        continue_decision, _ = run_case(
            "continue-open",
            _trace(continue_state, {"status": "SMELL_GUARD_FAILED", "loop": continue_loop}),
            _pass_payload(),
        )
        assert continue_decision["accepted"] is False, continue_decision

        for stage in ("cheap_guard", "protocol"):
            state = _state(
                generation=1,
                decision="stop",
                instruction="",
                termination_reason="PASS",
                stage=stage,
                status="PASS",
                success=True,
                accepted=True,
                resolution="resolved",
            )
            decision, _ = run_case(stage, _trace(state, formal_payload), _pass_payload())
            assert decision["accepted"] is False, (stage, decision)

        improved_loop = {**formal_loop, "termination_reason": "IMPROVED"}
        improved_state = _state(
            generation=1,
            decision="stop",
            instruction="",
            termination_reason="IMPROVED",
            stage="formal_verify",
            status="IMPROVED",
            success=True,
            accepted=False,
            resolution="improved",
        )
        improved_decision, _ = run_case(
            "formal-improved",
            _trace(improved_state, {"status": "IMPROVED", "loop": improved_loop}),
            _pass_payload(),
        )
        assert improved_decision["accepted"] is False, improved_decision

        formal_reject_loop = {**formal_loop, "termination_reason": "SMELL_GUARD_FAILED"}
        formal_reject_state = _state(
            generation=1,
            decision="stop",
            instruction="",
            termination_reason="SMELL_GUARD_FAILED",
            stage="formal_verify",
            status="SMELL_GUARD_FAILED",
            success=False,
            accepted=False,
            resolution="rejected",
        )
        formal_reject, _ = run_case(
            "formal-nonaccept",
            _trace(
                formal_reject_state,
                {"status": "SMELL_GUARD_FAILED", "loop": formal_reject_loop},
            ),
            _pass_payload(),
        )
        assert formal_reject["accepted"] is False, formal_reject

        typed_nonaccept = copy.deepcopy(formal_payload)
        typed_nonaccept.update({
            "status": "TEST_FAILED",
            "success": False,
            "accepted": False,
            "resolution": "unresolved",
            "loop": {
                **formal_loop,
                "termination_reason": "TEST_FAILED",
            },
        })
        typed_nonaccept_receipt = typed_nonaccept["formal_verification_receipt"]
        typed_nonaccept_receipt.update({
            "status": "TEST_FAILED",
            "success": False,
            "accepted": False,
            "resolution": "unresolved",
            "outcome": "test_failed",
            "diagnostic_signature": "TEST_FAILED",
        })
        typed_nonaccept_receipt["build_test"].update({
            "success": False,
            "reason": "TEST_FAILED",
            "test_status": "failed",
        })
        typed_nonaccept_state = _state(
            generation=1,
            decision="stop",
            instruction="",
            termination_reason="TEST_FAILED",
            stage="formal_verify",
            status="TEST_FAILED",
            success=False,
            accepted=False,
            resolution="unresolved",
            formal_receipt=typed_nonaccept_receipt,
        )
        typed_nonaccept_trace = _trace(typed_nonaccept_state, typed_nonaccept)
        calls.clear()

        def unexpected_fresh(*_args, **_kwargs):
            calls.append("fresh")
            raise AssertionError("a typed nonaccept terminal must not start a fresh verify with no budget")

        R._run_verify = unexpected_fresh
        try:
            nonaccept_rc, nonaccept_decision, nonaccept_audit = R._runner_final_verify(
                sample,
                sample_dir,
                args,
                "project_full",
                baseline_seal="c000-seal",
                deadline_monotonic=time.monotonic() + 1,
                opencode_returncode=0,
                last_trace=typed_nonaccept_trace,
                agent_verification_history=[],
            )
        finally:
            R._run_verify = original_run_verify
        assert calls == [], calls
        assert nonaccept_rc == 0, nonaccept_rc
        assert nonaccept_decision["status"] == "TEST_FAILED", nonaccept_decision
        assert nonaccept_decision["accepted"] is False, nonaccept_decision
        assert nonaccept_audit["reason"] == "FRESH_VERIFY_SKIPPED_INSUFFICIENT_BUDGET", nonaccept_audit
        assert nonaccept_audit["source"] == "agent_formal_terminal", nonaccept_audit

        stale_state = _state(
            generation=2,
            decision="stop",
            instruction="",
            termination_reason="PASS",
            stage="formal_verify",
            status="PASS",
            success=True,
            accepted=True,
            resolution="resolved",
            formal_receipt=formal_payload["formal_verification_receipt"],
        )
        stale_loop = {**formal_loop, "generation": 2}
        stale, stale_audit = run_case(
            "stale-generation",
            _trace(stale_state, {**formal_payload, "loop": stale_loop}),
            _pass_payload(),
        )
        assert stale["accepted"] is False, stale
        assert (
            stale_audit["terminal_evidence"]["reason"]
            == "TERMINAL_CONTROL_TRANSITION_UNCONFIRMED"
        ), stale_audit

        contradictory_payload = {**formal_payload, "loop": {**formal_loop, "termination_reason": "OTHER"}}
        contradiction, contradiction_audit = run_case(
            "terminal-payload-state-mismatch",
            _trace(formal_state, contradictory_payload),
            _pass_payload(),
        )
        assert contradiction["accepted"] is False, contradiction
        assert (
            contradiction_audit["terminal_evidence"]["reason"]
            == "TERMINAL_PAYLOAD_STATE_MISMATCH"
        ), contradiction_audit

        aborted, aborted_audit = run_case(
            "runtime-abort",
            formal_trace,
            _pass_payload(),
            opencode_returncode=124,
        )
        assert aborted["accepted"] is False, aborted
        assert aborted_audit["terminal_evidence"]["reason"] == "OPENCODE_NONZERO", aborted_audit

        fresh_failure = {
            "schema_version": "smell.verify.decision/v1",
            "success": False,
            "accepted": False,
            "status": "SMELL_GUARD_FAILED",
            "resolution": "unresolved",
        }
        rejected, rejected_audit = run_case(
            "fresh-downgrade",
            formal_trace,
            fresh_failure,
            fresh_returncode=1,
        )
        assert rejected["status"] == "SMELL_GUARD_FAILED" and rejected["accepted"] is False, rejected
        assert rejected_audit["reason"] == "FRESH_VERIFY_REJECTED", rejected_audit

    print(
        "runner final receipt self-check passed: a complete typed formal project_full receipt is reused; "
        "otherwise fresh verification only confirms or downgrades and invalid terminal/runtime states reject promotion"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
