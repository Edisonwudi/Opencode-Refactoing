#!/usr/bin/env python3
"""Adversarial checks for exact-candidate project_full receipt reuse."""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402
from bridge.smell_bridge import _snapshot_project  # noqa: E402


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
        },
        "artifacts": artifacts,
        "artifact_index": artifact_index,
        "loop": {"decision": "stop"},
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="runner-final-receipt-") as raw:
        root = Path(raw)
        sample, sample_dir, payload, trace, history = _fixture(root)
        args = argparse.Namespace(
            projects="",
            sample_deadline=60,
            allow_test_changes=True,
        )

        original_run_verify = R._run_verify
        calls: list[str] = []

        def unexpected_verify(*_args, **_kwargs):
            calls.append("fresh")
            raise AssertionError("trusted exact-candidate receipt must skip duplicate project_full")

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
        assert rc == 0 and reused["status"] == "PASS", (rc, reused)
        assert audit["reused"] is True and audit["reason"] == "REUSED", audit
        assert calls == [], calls
        persisted = json.loads((sample_dir / "verify.json").read_text(encoding="utf-8"))
        assert persisted == payload, persisted
        persisted_audit = json.loads(
            (sample_dir / "runner-final-receipt.json").read_text(encoding="utf-8")
        )
        assert persisted_audit["reused"] is True, persisted_audit

        read_only_trace = {
            **trace,
            "tools_after_last_verify": 1,
            "tool_attempts_after_last_verify": 1,
            "completed_tools_after_last_verify": ["todowrite"],
            "attempted_tools_after_last_verify": ["todowrite"],
        }
        read_only_payload, read_only_audit = R._agent_project_full_receipt(
            sample,
            sample_dir,
            "project_full",
            0,
            read_only_trace,
            history,
        )
        assert read_only_payload is payload, read_only_payload
        assert read_only_audit["reused"] is True, read_only_audit

        expired_rc, expired, expired_audit = R._runner_final_verify(
            sample,
            sample_dir,
            args,
            "project_full",
            baseline_seal="c000-seal",
            deadline_monotonic=time.monotonic() - 1,
            opencode_returncode=0,
            last_trace=trace,
            agent_verification_history=history,
        )
        assert expired_rc == 124 and expired["status"] == "OPENCODE_TIMEOUT", expired
        assert expired_audit["reason"] == "SAMPLE_DEADLINE_REACHED", expired_audit
        assert "runner_final_receipt" not in expired, expired

        fallback_payload = copy.deepcopy(payload)
        fallback_payload["status"] = "SMELL_GUARD_FAILED"
        fallback_payload["success"] = False
        fallback_payload["accepted"] = False
        fallback_payload["resolution"] = "unresolved"
        fallback_calls: list[str] = []

        def fallback_verify(*_args, **_kwargs):
            fallback_calls.append("fresh")
            return 1, fallback_payload

        def assert_fallback(
            reason: str,
            *,
            changed_sample: R.Sample | None = None,
            changed_trace: dict[str, object] | None = None,
            changed_history: list[dict[str, object]] | None = None,
            opencode_returncode: int = 0,
        ) -> None:
            fallback_calls.clear()
            R._run_verify = fallback_verify
            try:
                _rc, decision, receipt = R._runner_final_verify(
                    changed_sample or sample,
                    sample_dir,
                    args,
                    "project_full",
                    baseline_seal="c000-seal",
                    deadline_monotonic=None,
                    opencode_returncode=opencode_returncode,
                    last_trace=changed_trace or trace,
                    agent_verification_history=changed_history or history,
                )
            finally:
                R._run_verify = original_run_verify
            assert fallback_calls == ["fresh"], (reason, fallback_calls)
            assert decision["status"] == "SMELL_GUARD_FAILED", decision
            assert receipt["reused"] is False and receipt["reason"] == reason, receipt

        after_tool = {**trace, "tools_after_last_verify": 1}
        assert_fallback("TOOLS_AFTER_LAST_VERIFY", changed_trace=after_tool)
        attempted_tool = {**trace, "tool_attempts_after_last_verify": 1}
        assert_fallback(
            "TOOL_ATTEMPT_AFTER_LAST_VERIFY", changed_trace=attempted_tool
        )
        assert_fallback("OPENCODE_NONZERO", opencode_returncode=7)

        modified_tests = copy.deepcopy(payload)
        modified_tests["test_changes"]["status"] = "TEST_SOURCE_CHANGE_ALLOWED"
        modified_tests["test_changes"]["changed_count"] = 1
        modified_attempt = R._compact_verify_attempt(
            modified_tests, verify_source="agent", verify_returncode=0
        )
        assert_fallback(
            "TEST_SOURCE_CHANGED",
            changed_trace={**trace, "last_payload": modified_tests},
            changed_history=[modified_attempt],
        )

        missing_evidence = copy.deepcopy(payload)
        del missing_evidence["artifacts"]["test_result"]
        del missing_evidence["artifact_index"]["test_result"]
        missing_attempt = R._compact_verify_attempt(
            missing_evidence, verify_source="agent", verify_returncode=0
        )
        assert_fallback(
            "EVIDENCE_INCOMPLETE",
            changed_trace={**trace, "last_payload": missing_evidence},
            changed_history=[missing_attempt],
        )

        no_cleanup = copy.deepcopy(payload)
        no_cleanup["build_test_guard"]["verification_isolation"]["cleanup_success"] = False
        cleanup_attempt = R._compact_verify_attempt(
            no_cleanup, verify_source="agent", verify_returncode=0
        )
        assert_fallback(
            "FRESH_ISOLATION_INCOMPLETE",
            changed_trace={**trace, "last_payload": no_cleanup},
            changed_history=[cleanup_attempt],
        )

        failed_preflight = copy.deepcopy(payload)
        failed_preflight["build_test_guard"]["focused_preflight"].update(
            {"success": False, "status": "FAILED"}
        )
        failed_preflight_attempt = R._compact_verify_attempt(
            failed_preflight, verify_source="agent", verify_returncode=0
        )
        assert_fallback(
            "FRESH_ISOLATION_INCOMPLETE",
            changed_trace={**trace, "last_payload": failed_preflight},
            changed_history=[failed_preflight_attempt],
        )

        missing_preflight = copy.deepcopy(payload)
        del missing_preflight["build_test_guard"]["focused_preflight"]
        missing_preflight_attempt = R._compact_verify_attempt(
            missing_preflight, verify_source="agent", verify_returncode=0
        )
        assert_fallback(
            "FRESH_ISOLATION_INCOMPLETE",
            changed_trace={**trace, "last_payload": missing_preflight},
            changed_history=[missing_preflight_attempt],
        )

        assert_fallback(
            "FRESH_ISOLATION_INCOMPLETE",
            changed_sample=replace(sample, test_command="./focused-sample-test"),
        )

        build_guard_path = Path(payload["artifacts"]["build_test_guard"])
        original_build_guard = build_guard_path.read_text(encoding="utf-8")

        sample_mismatch_guard = json.loads(original_build_guard)
        sample_mismatch_guard["details"]["sample_test"] = {
            "label": "sample_test",
            "success": False,
            "status": "fail",
            "returncode": 1,
            "command": "./focused-sample-test",
            "script": "",
            "cwd": str(sample.project_root),
            "source": "dataset",
        }
        _write_json(build_guard_path, sample_mismatch_guard)
        sample_mismatch_payload = copy.deepcopy(payload)
        sample_mismatch_payload["artifact_index"]["build_test_guard"]["bytes"] = (
            build_guard_path.stat().st_size
        )
        sample_mismatch_attempt = R._compact_verify_attempt(
            sample_mismatch_payload, verify_source="agent", verify_returncode=0
        )
        try:
            assert_fallback(
                "FRESH_ISOLATION_INCOMPLETE",
                changed_trace={**trace, "last_payload": sample_mismatch_payload},
                changed_history=[sample_mismatch_attempt],
            )
        finally:
            build_guard_path.write_text(original_build_guard, encoding="utf-8")

        build_result_path = Path(payload["artifacts"]["build_result"])
        original_build_result = build_result_path.read_text(encoding="utf-8")
        forged_guard = json.loads(original_build_guard)
        forged_build = json.loads(original_build_result)
        forged_guard["details"]["build"]["command"] = "true # forged build"
        forged_build["command"] = "true # forged build"
        _write_json(build_guard_path, forged_guard)
        _write_json(build_result_path, forged_build)
        forged_stage_payload = copy.deepcopy(payload)
        forged_stage_payload["artifact_index"]["build_test_guard"]["bytes"] = (
            build_guard_path.stat().st_size
        )
        forged_stage_payload["artifact_index"]["build_result"]["bytes"] = (
            build_result_path.stat().st_size
        )
        forged_stage_attempt = R._compact_verify_attempt(
            forged_stage_payload, verify_source="agent", verify_returncode=0
        )
        try:
            assert_fallback(
                "FRESH_ISOLATION_INCOMPLETE",
                changed_trace={**trace, "last_payload": forged_stage_payload},
                changed_history=[forged_stage_attempt],
            )
        finally:
            build_guard_path.write_text(original_build_guard, encoding="utf-8")
            build_result_path.write_text(original_build_result, encoding="utf-8")

        corrupted_build_guard = json.loads(original_build_guard)
        corrupted_build_guard.update(
            {"success": False, "project_full_executed": False}
        )
        _write_json(build_guard_path, corrupted_build_guard)
        corrupted_build_payload = copy.deepcopy(payload)
        corrupted_build_payload["artifact_index"]["build_test_guard"]["bytes"] = (
            build_guard_path.stat().st_size
        )
        corrupted_build_attempt = R._compact_verify_attempt(
            corrupted_build_payload, verify_source="agent", verify_returncode=0
        )
        try:
            assert_fallback(
                "FRESH_ISOLATION_INCOMPLETE",
                changed_trace={**trace, "last_payload": corrupted_build_payload},
                changed_history=[corrupted_build_attempt],
            )
        finally:
            build_guard_path.write_text(original_build_guard, encoding="utf-8")

        guard_evidence_path = Path(payload["artifacts"]["guard_evidence"])
        original_guard_evidence = guard_evidence_path.read_text(encoding="utf-8")

        changed_test_evidence = json.loads(original_guard_evidence)
        changed_test_evidence["test_changes"].update(
            {"status": "TEST_SOURCE_CHANGE_ALLOWED", "changed_count": 1}
        )
        _write_json(guard_evidence_path, changed_test_evidence)
        changed_test_evidence_payload = copy.deepcopy(payload)
        changed_test_evidence_payload["artifact_index"]["guard_evidence"]["bytes"] = (
            guard_evidence_path.stat().st_size
        )
        changed_test_evidence_attempt = R._compact_verify_attempt(
            changed_test_evidence_payload, verify_source="agent", verify_returncode=0
        )
        try:
            assert_fallback(
                "FRESH_ISOLATION_INCOMPLETE",
                changed_trace={**trace, "last_payload": changed_test_evidence_payload},
                changed_history=[changed_test_evidence_attempt],
            )
        finally:
            guard_evidence_path.write_text(original_guard_evidence, encoding="utf-8")

        corrupted_guard_evidence = json.loads(original_guard_evidence)
        corrupted_guard_evidence.update(
            {"success": False, "accepted": False, "status": "SMELL_GUARD_FAILED"}
        )
        _write_json(guard_evidence_path, corrupted_guard_evidence)
        corrupted_evidence_payload = copy.deepcopy(payload)
        corrupted_evidence_payload["artifact_index"]["guard_evidence"]["bytes"] = (
            guard_evidence_path.stat().st_size
        )
        corrupted_evidence_attempt = R._compact_verify_attempt(
            corrupted_evidence_payload, verify_source="agent", verify_returncode=0
        )
        try:
            assert_fallback(
                "FRESH_ISOLATION_INCOMPLETE",
                changed_trace={**trace, "last_payload": corrupted_evidence_payload},
                changed_history=[corrupted_evidence_attempt],
            )
        finally:
            guard_evidence_path.write_text(original_guard_evidence, encoding="utf-8")

        contradiction = copy.deepcopy(history[0])
        contradiction.update({
            "status": "TEST_FAILED",
            "reported_status": "TEST_FAILED",
            "accepted": False,
            "success": False,
            "failure_category": "TEST_BEHAVIOR_REGRESSION",
        })
        assert_fallback(
            "SAME_DIFF_CONTRADICTION",
            changed_history=[contradiction, history[0]],
        )

        source = sample.project_root / "sample.c"
        original = source.read_text(encoding="utf-8")
        source.write_text(original + "int later(void) { return 2; }\n", encoding="utf-8")
        try:
            assert_fallback("CURRENT_DIFF_MISMATCH")
        finally:
            source.write_text(original, encoding="utf-8")

    print(
        "runner final receipt self-check passed: exact agent project_full reused; "
        "deadline, post-verify tools, rc, test edits, missing evidence, isolation, "
        "contradictions, and diff drift fall back"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
