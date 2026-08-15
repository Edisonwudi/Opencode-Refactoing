#!/usr/bin/env python3
"""Keep the Python bridge limited to the three product entry points."""
from __future__ import annotations

import argparse
import sys
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

    print(
        "bridge-surface self-check: PASS commands=3 legacy_context_commands=0 "
        "structured_status_precedes_text build_precedes_smell project_full=fresh-tests "
        "timeout_classification=nonrepairable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
