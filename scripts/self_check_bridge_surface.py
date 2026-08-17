#!/usr/bin/env python3
"""Keep the Python bridge limited to the three product entry points."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python" / "bridge"))

import smell_bridge  # noqa: E402


def main() -> int:
    parser = smell_bridge.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)
    expected = {"resolve-command", "capture-baseline", "verify"}
    assert commands == expected, (commands, expected)
    progress_args = parser.parse_args([
        "verify",
        "--project-root", "/tmp/project",
        "--language", "python",
        "--smell", "long_method",
        "--location", "sample.py:1",
        "--guard-progress-only",
    ])
    assert progress_args.func is smell_bridge.cmd_verify, progress_args
    assert progress_args.guard_progress_only is True, progress_args
    focused_args = parser.parse_args([
        "verify",
        "--project-root", "/tmp/project",
        "--language", "cpp",
        "--smell", "code_clone_type_1",
        "--location", "sample.cc:1",
        "--focused-preflight-only",
    ])
    assert focused_args.func is smell_bridge.cmd_verify, focused_args
    assert focused_args.focused_preflight_only is True, focused_args
    explicit_verification_args = parser.parse_args([
        "verify",
        "--project-root", "/tmp/project",
        "--language", "java",
        "--smell", "long_method",
        "--location", "Sample.java:1",
        "--build-command", "./gradlew classes",
        "--project-test-command", "./gradlew test",
        "--verification-cwd", ".",
        "--verification-command-source", "command",
        "--sample-test-command", "./gradlew focusedTest",
        "--sample-test-source", "command",
    ])
    assert explicit_verification_args.build_command == "./gradlew classes"
    assert explicit_verification_args.project_test_command == "./gradlew test"
    assert explicit_verification_args.verification_cwd == "."
    assert explicit_verification_args.verification_command_source == "command"
    assert explicit_verification_args.sample_test_source == "command"
    mixed_cli_inputs = smell_bridge._resolved_command_inputs(
        SimpleNamespace(
            build_command="cli-build",
            project_test_command=None,
            verification_cwd=None,
            verification_command_source=None,
            sample_test_location=None,
            sample_test_command="cli-sample",
            sample_test_source=None,
        ),
        {
            "SMELL_PROJECT_ROOT": "/tmp/project",
            "SMELL_SMELL": "long_method",
            "SMELL_LOCATION": "Sample.java:1",
            "SMELL_BUILD_COMMAND": "env-build",
            "SMELL_PROJECT_TEST_COMMAND": "env-test",
            "SMELL_VERIFICATION_CWD": "env-cwd",
            "SMELL_VERIFICATION_COMMAND_SOURCE": "dataset",
            "SMELL_SAMPLE_TEST_COMMAND": "env-sample",
            "SMELL_SAMPLE_TEST_SOURCE": "dataset",
        },
    )
    assert mixed_cli_inputs["build_command"] == "cli-build"
    assert mixed_cli_inputs["project_test_command"] == ""
    assert mixed_cli_inputs["verification_cwd"] == ""
    assert mixed_cli_inputs["verification_command_source"] == ""
    assert mixed_cli_inputs["sample_test_command"] == "cli-sample"
    assert mixed_cli_inputs["sample_test_source"] == ""

    build_timeout = {
        "status": "BUILD_FAILED",
        "build_test_guard": {
            "details": {"build": {"success": False, "status": "timeout"}}
        },
    }
    sample_test_timeout = {
        "status": "SAMPLE_TEST_FAILED",
        "build_test_guard": {
            "details": {"test": {"success": False, "status": "timeout"}}
        },
    }
    for payload in (build_timeout, sample_test_timeout):
        category, _ = smell_bridge._classify_failure_pack(payload, "")
        assert category == "TIMEOUT_OR_MODAL_SUSPECTED", category
        failure_pack = smell_bridge._build_failure_pack(payload, {})
        assert failure_pack["failure_group"] == "", failure_pack
        assert failure_pack["retryable"] is False, failure_pack
        assert failure_pack["repair_contract"]["repair_agent_may_edit"] is False

    structured_test_failure = {
        "status": "TEST_FAILED",
        "build_test_guard": {
            "details": {
                "test": {
                    "success": False,
                    "status": "failed",
                    "returncode": 1,
                }
            }
        },
    }
    category, _ = smell_bridge._classify_failure_pack(
        structured_test_failure,
        "FAILED test_timeout_header_is_preserved",
    )
    assert category == "TEST_BEHAVIOR_REGRESSION", category

    build_and_smell_failure = {
        "status": "BUILD_FAILED",
        "smell_guard": {"success": False},
        "build_test_guard": {
            "details": {
                "build": {
                    "success": False,
                    "status": "failed",
                    "returncode": 1,
                }
            }
        },
    }
    category, _ = smell_bridge._classify_failure_pack(
        build_and_smell_failure,
        "error: helper has not been declared",
    )
    assert category == "BUILD_COMPILE_ERROR", category
    native_pack = smell_bridge._build_failure_pack(
        build_and_smell_failure,
        {},
    )
    # Inline payloads have no native log here; the pattern contract is checked
    # directly so future artifact-backed traces retain these universal signals.
    native_patterns = [
        "Segmentation fault",
        "core dumped",
        "fatal error: Killed",
        "ninja: build stopped",
    ]
    native_text = "\n".join(native_patterns)
    highlights = smell_bridge._highlight_patterns(
        native_text,
        native_patterns,
        context=0,
        limit=len(native_patterns),
    )
    assert len(highlights) == len(native_patterns), (native_pack, highlights)

    test_not_executed = {
        "status": "TEST_EVIDENCE_MISSING",
        "build_test_guard": {
            "details": {
                "test": {
                    "success": False,
                    "status": "test_not_executed",
                    "returncode": 0,
                }
            }
        },
    }
    category, _ = smell_bridge._classify_failure_pack(
        test_not_executed,
        "command returned 0",
    )
    assert category == "TEST_EVIDENCE_MISSING", category
    failure_pack = smell_bridge._build_failure_pack(test_not_executed, {})
    assert failure_pack["failure_group"] == "", failure_pack
    assert failure_pack["repair_contract"]["repair_agent_may_edit"] is False

    assert smell_bridge._verify_status(
        False,
        {"success": True},
        test_not_executed["build_test_guard"] | {
            "success": False,
            "verification_mode": "project_full",
        },
    ) == "TEST_EVIDENCE_MISSING"

    assert smell_bridge._requires_fresh_test_execution(
        SimpleNamespace(verification_mode="project_full"),
        test_changes={},
        exact_dead_code_deletion=False,
    ) is True
    assert smell_bridge._requires_fresh_test_execution(
        SimpleNamespace(verification_mode="auto"),
        test_changes={},
        exact_dead_code_deletion=False,
    ) is False

    receipt_artifacts = {
        "guard_evidence": "/tmp/artifacts/guard-evidence.json",
        "build_result": "/tmp/artifacts/build.full.json",
        "test_result": "/tmp/artifacts/test.full.json",
        "diff": "/tmp/artifacts/diff.patch",
    }
    pass_decision = smell_bridge._verify_decision_payload(
        {
            "success": True,
            "accepted": True,
            "progress": True,
            "status": "PASS",
            "resolution": "resolved",
            "project_full_executed": True,
            "smell_guard": {
                "success": True,
                "failure_count": 0,
                "results": [],
            },
            "build_test_guard": {
                "success": True,
                "project_full_executed": True,
                "details": {
                    "build": {"success": True, "status": "passed"},
                    "test": {"success": True, "status": "passed"},
                    "sample_test": None,
                },
                "verification_isolation": {
                    "contract_version": "project-full-fresh-worktree/v1",
                    "mode": "detached_git_worktree",
                    "success": True,
                    "stage": "completed",
                    "base_commit": "base-revision",
                    "snapshot_change_count": 2,
                    "cleanup_success": True,
                },
            },
            "checkpoint": {
                "baseline_project_commit": "base-revision",
                "baseline_tree_hash": "base-tree",
                "production_diff_hash": "existing-production-diff-id",
                "test_changes": {
                    "current_tree_sha256": "existing-test-tree-id",
                    "current_verification_config_tree_sha256": "existing-verification-config-tree-id",
                },
            },
        },
        receipt_artifacts,
    )
    pass_receipt = pass_decision["formal_verification_receipt"]
    assert pass_receipt["schema_version"] == (
        "smell.formal-verification-receipt/v1"
    ), pass_receipt
    assert pass_receipt["terminal_stage"] == "formal_verify", pass_receipt
    assert pass_receipt["candidate_identity"] == {
        "baseline_revision": "base-revision",
        "baseline_tree": "base-tree",
        "production_diff": "existing-production-diff-id",
        "test_tree": "existing-test-tree-id",
        "verification_config_tree": "existing-verification-config-tree-id",
    }, pass_receipt
    assert pass_receipt["outcome"] == "pass", pass_receipt
    assert pass_receipt["diagnostic_signature"] == "PASS", pass_receipt
    assert pass_receipt["guard"]["success"] is True, pass_receipt
    assert pass_receipt["build_test"]["test_status"] == "passed", pass_receipt
    assert pass_receipt["fresh_isolation"]["cleanup_success"] is True, (
        pass_receipt
    )
    assert pass_receipt["artifact_refs"] == receipt_artifacts, pass_receipt

    failed_decision = smell_bridge._verify_decision_payload(
        {
            "success": False,
            "accepted": False,
            "progress": False,
            "status": "TEST_FAILED",
            "resolution": "unresolved",
            "project_full_executed": True,
            "smell_guard": {
                "success": True,
                "failure_count": 0,
                "results": [],
            },
            "build_test_guard": {
                "success": False,
                "project_full_executed": True,
                "details": {
                    "build": {"success": True, "status": "passed"},
                    "test": {"success": False, "status": "failed"},
                    "sample_test": None,
                },
            },
            "checkpoint": {
                "baseline_project_commit": "base-revision",
                "production_diff_hash": "existing-production-diff-id",
                "test_changes": {
                    "current_tree_sha256": "existing-test-tree-id",
                    "current_verification_config_tree_sha256": "existing-verification-config-tree-id",
                },
                "delta": {"reason": "NO_STRUCTURAL_PROGRESS"},
            },
            "failure_pack": {
                "failure_category": "TEST_BEHAVIOR_REGRESSION",
                "failure_group": "test",
                "retryable": True,
                "verify_status": "TEST_FAILED",
                "next_action": "run one fresh confirmation",
            },
        },
        receipt_artifacts,
    )
    failed_receipt = failed_decision["formal_verification_receipt"]
    assert failed_receipt["outcome"] == "test_failed", failed_receipt
    assert failed_receipt["diagnostic_signature"].startswith("tests=1:test@1:"), (
        failed_decision
    )

    def failed_test_decision(
        test_name: str,
        *,
        named_case: bool = True,
    ) -> dict[str, object]:
        diagnostic = (
            f"FAIL {test_name}: exit 1\n[FAIL] assertion mismatch\n"
            if named_case
            else f"pytest failed: {test_name}\n"
        )
        payload = {
            "success": False,
            "accepted": False,
            "progress": False,
            "status": "TEST_FAILED",
            "resolution": "unresolved",
            "project_full_executed": True,
            "smell_guard": {
                "success": True,
                "failure_count": 0,
                "results": [],
            },
            "build_test_guard": {
                "success": False,
                "project_full_executed": True,
                "details": {
                    "build": {"success": True, "status": "passed"},
                    "test": {
                        "success": False,
                        "status": "failed",
                        "returncode": 1,
                        "stdout": diagnostic,
                    },
                    "sample_test": None,
                },
            },
            "checkpoint": {
                "baseline_project_commit": "base-revision",
                "production_diff_hash": "same-production-diff-id",
                "test_changes": {
                    "current_tree_sha256": "same-test-tree-id",
                    "current_verification_config_tree_sha256": (
                        "same-verification-config-tree-id"
                    ),
                },
                "delta": {"reason": "NO_STRUCTURAL_PROGRESS"},
            },
            "failure_pack": {
                "failure_category": "TEST_BEHAVIOR_REGRESSION",
                "failure_group": "test",
                "retryable": True,
                "verify_status": "TEST_FAILED",
                "next_action": "repair the behavior regression",
            },
        }
        return smell_bridge._verify_decision_payload(payload, receipt_artifacts)

    first_failed_test = failed_test_decision("first-case.sh")
    second_failed_test = failed_test_decision("second-case.sh")
    assert (
        first_failed_test["formal_verification_receipt"]["candidate_identity"]
        == second_failed_test["formal_verification_receipt"]["candidate_identity"]
    )
    assert (
        first_failed_test["formal_verification_receipt"]["diagnostic_signature"]
        != second_failed_test["formal_verification_receipt"]["diagnostic_signature"]
    ), (first_failed_test, second_failed_test)
    first_generic_failure = failed_test_decision(
        "first_generic_case",
        named_case=False,
    )
    second_generic_failure = failed_test_decision(
        "second_generic_case",
        named_case=False,
    )
    assert (
        first_generic_failure["formal_verification_receipt"]["diagnostic_signature"]
        != second_generic_failure["formal_verification_receipt"]["diagnostic_signature"]
    ), (first_generic_failure, second_generic_failure)
    changed_test_tree_payload = smell_bridge._verify_decision_payload(
        {
            "success": True,
            "accepted": True,
            "progress": True,
            "status": "PASS",
            "resolution": "resolved",
            "smell_guard": {"success": True, "failure_count": 0, "results": []},
            "build_test_guard": {"success": True, "details": {}},
            "checkpoint": {
                "baseline_project_commit": "base-revision",
                "baseline_tree_hash": "base-tree",
                "production_diff_hash": "existing-production-diff-id",
                "test_changes": {
                    "current_tree_sha256": "changed-test-tree-id",
                    "current_verification_config_tree_sha256": "existing-verification-config-tree-id",
                },
            },
        },
        receipt_artifacts,
    )
    assert (
        changed_test_tree_payload["formal_verification_receipt"]["candidate_identity"]
        != pass_receipt["candidate_identity"]
    ), changed_test_tree_payload
    assert len(json.dumps(pass_decision).encode("utf-8")) < (
        smell_bridge.DECISION_MAX_BYTES
    )

    # Every Git command nested under snapshot capture must consume the same
    # absolute sample deadline.  A blocked Git command may not outlive the
    # manual/plugin verification budget.
    original_run_git = smell_bridge._run_git
    observed_deadlines: list[float | None] = []

    def timed_out_git(
        args: list[str],
        cwd: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        observed_deadlines.append(deadline_monotonic)
        raise subprocess.TimeoutExpired(["git", *args], timeout=0.01)

    smell_bridge._run_git = timed_out_git
    snapshot_deadline = time.monotonic() + 0.1
    try:
        try:
            smell_bridge._snapshot_project(
                Path("/tmp/project"),
                deadline_monotonic=snapshot_deadline,
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("snapshot Git timeout must fail closed")
    finally:
        smell_bridge._run_git = original_run_git
    assert observed_deadlines == [snapshot_deadline], observed_deadlines

    original_subprocess_run = smell_bridge.subprocess.run
    observed_timeouts: list[float | None] = []

    def blocked_git_process(*args, **kwargs):
        observed_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args[0], timeout=kwargs.get("timeout"))

    smell_bridge.subprocess.run = blocked_git_process
    try:
        explicit_deadline = time.monotonic() + 0.1
        try:
            smell_bridge._run_git(
                ["status", "--short"],
                Path("/tmp/project"),
                deadline_monotonic=explicit_deadline,
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("bounded Git timeout must propagate")
    finally:
        smell_bridge.subprocess.run = original_subprocess_run
    assert len(observed_timeouts) == 1, observed_timeouts
    assert observed_timeouts[0] is not None and 0 < observed_timeouts[0] <= 0.1

    print(
        "bridge-surface self-check: PASS commands=3 legacy_context_commands=0 "
        "structured_status_precedes_text build_precedes_smell project_full=fresh-tests "
        "timeout_classification=nonrepairable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
