#!/usr/bin/env python3
"""Focused self-check for runner verification-history lifecycle decisions."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402


failures: list[str] = []


def check(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{name}: expected {expected!r}, got {actual!r}")
        print(f"FAIL {name}: expected {expected!r}, got {actual!r}")
    else:
        print(f"ok   {name}")


def step(
    *,
    status: str,
    returncode: int,
    summary: str,
    success: bool = False,
) -> dict[str, object]:
    return {
        "status": status,
        "returncode": returncode,
        "success": success,
        "summary_text": summary,
        "failure_highlights": [summary] if summary else [],
    }


def payload(
    status: str,
    diff_path: Path,
    *,
    build: dict[str, object] | None = None,
    test: dict[str, object] | None = None,
    failure_category: str = "",
) -> dict[str, object]:
    passed = status == "PASS"
    details: dict[str, object] = {}
    if build is not None:
        details["build"] = build
    if test is not None:
        details["test"] = test
    return {
        "status": status,
        "success": passed,
        "accepted": passed,
        "resolution": "resolved" if passed else "unresolved",
        "progress": passed,
        "artifacts": {"diff": str(diff_path)},
        "smell_guard": {"success": status != "SMELL_GUARD_FAILED"},
        "build_test_guard": {
            "success": passed,
            "details": details,
        },
        "failure_pack": {"failure_category": failure_category},
    }


def verify_event(verify_payload: dict[str, object], exit_code: int) -> str:
    return json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "smell_verify",
                "state": {
                    "status": "completed",
                    "output": json.dumps(verify_payload),
                    "metadata": {"exitCode": exit_code},
                },
            },
        }
    )


with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    same_diff = temp / "same.patch"
    same_diff.write_text("diff --git a/source.c b/source.c\n", encoding="utf-8")
    different_diff = temp / "different.patch"
    different_diff.write_text("diff --git a/other.c b/other.c\n", encoding="utf-8")

    passing = payload(
        "PASS",
        same_diff,
        build=step(status="ok", returncode=0, summary="", success=True),
        test=step(status="ok", returncode=0, summary="", success=True),
    )
    test_failure = payload(
        "TEST_FAILED",
        same_diff,
        build=step(status="ok", returncode=0, summary="", success=True),
        test=step(status="fail", returncode=1, summary="assertion failed"),
        failure_category="SMELL_GUARD_FAILED",
    )
    events = "\n".join(
        [verify_event(test_failure, 1), verify_event(passing, 0)]
    )
    trace = R._verification_trace(events)
    history = trace["verification_history"]
    check("all_agent_verifies_preserved", len(history), 2)
    check("first_agent_status_preserved", history[0]["reported_status"], "TEST_FAILED")
    check("last_agent_status_preserved", history[1]["status"], "PASS")
    check("agent_diff_hash_preserved", bool(history[1]["diff_sha256"]), True)
    persisted_attempts = R._verification_attempt_history(
        history,
        {"verify_source": "runner_final", "status": "PASS"},
    )
    check("result_attempts_keep_agent_history", len(persisted_attempts), 3)
    check("result_attempts_end_with_runner_final", persisted_attempts[-1]["verify_source"], "runner_final")

    flaky_status, flaky_audit = R._reconcile_final_verify_status(
        "PASS", passing, [history[0]]
    )
    check("same_diff_red_then_green_is_inconclusive", flaky_status, "FLAKY_TEST_INCONCLUSIVE")
    check("flaky_result_not_accepted", R._is_accepted_status(flaky_status), False)
    check(
        "flaky_rule_is_behavior_triggered",
        flaky_audit["last_agent_same_diff_test_failure"],
        True,
    )
    check("flaky_requires_confirmation", flaky_audit["confirmation_required"], True)
    confirmed_status, _ = R._reconcile_final_verify_status(
        "PASS", passing, history
    )
    check("later_same_diff_agent_pass_confirms_prior_red", confirmed_status, "PASS")

    changed_pass = payload(
        "PASS",
        different_diff,
        build=step(status="ok", returncode=0, summary="", success=True),
        test=step(status="ok", returncode=0, summary="", success=True),
    )
    changed_status, _ = R._reconcile_final_verify_status(
        "PASS", changed_pass, [history[0]]
    )
    check("different_diff_pass_needs_no_repeat", changed_status, "PASS")
    ordinary_pass_status, _ = R._reconcile_final_verify_status("PASS", passing, [])
    check("ordinary_pass_needs_no_repeat", ordinary_pass_status, "PASS")

    timeout_failure = payload(
        "BUILD_FAILED",
        same_diff,
        build=step(
            status="timeout",
            returncode=124,
            summary="Build timed out after 600 seconds.",
        ),
        failure_category="SMELL_GUARD_FAILED",
    )
    infra_status, infra_audit = R._reconcile_final_verify_status(
        "BUILD_FAILED", timeout_failure, [history[1]]
    )
    check("same_diff_pass_then_timeout_is_infra", infra_status, "FINAL_VERIFY_INFRA_FAILED")
    check("infra_result_not_accepted", R._is_accepted_status(infra_status), False)
    check("structured_timeout_category", infra_audit["infra_category"], "BUILD_TIMEOUT")
    check("infra_requires_confirmation", infra_audit["confirmation_required"], True)

    changed_timeout = payload(
        "BUILD_FAILED",
        different_diff,
        build=step(
            status="timeout",
            returncode=124,
            summary="Build timed out after 600 seconds.",
        ),
    )
    changed_infra_status, _ = R._reconcile_final_verify_status(
        "BUILD_FAILED", changed_timeout, [history[1]]
    )
    check("different_diff_timeout_stays_failed", changed_infra_status, "BUILD_FAILED")

    smell_and_build = payload(
        "SMELL_GUARD_FAILED",
        same_diff,
        build=step(status="fail", returncode=1, summary="compile error"),
        failure_category="SMELL_GUARD_FAILED",
    )
    check(
        "build_failure_precedes_smell_advice",
        R._failure_category_from_verify_payload(smell_and_build),
        "BUILD_COMPILE_ERROR",
    )
    prioritized_prompt = R._runner_continuation_prompt(
        "continue",
        1,
        3,
        "repair",
        failure_category="BUILD_COMPILE_ERROR",
    )
    check(
        "continuation_reads_build_test_priority_from_latest_tool_result",
        "latest smell_verify tool result" in prioritized_prompt
        and "BUILD_COMPILE_ERROR" not in prioritized_prompt
        and "smell-only repair advice" not in prioritized_prompt,
        True,
    )

    ordinary_timeout_name = payload(
        "TEST_FAILED",
        same_diff,
        test=step(
            status="fail",
            returncode=1,
            summary="FAILED tests/test_timeout_behavior.py::test_retry",
        ),
    )
    check(
        "timeout_word_without_timeout_state_is_test_failure",
        R._failure_category_from_verify_payload(ordinary_timeout_name),
        "TEST_BEHAVIOR_REGRESSION",
    )

    missing_test_evidence = payload(
        "TEST_EVIDENCE_MISSING",
        same_diff,
        test=step(
            status="test_not_executed",
            returncode=0,
            summary="No fresh non-skipped tests were observed.",
        ),
    )
    check(
        "missing_test_evidence_is_not_behavior_regression",
        R._failure_category_from_verify_payload(missing_test_evidence),
        "TEST_EVIDENCE_MISSING",
    )

    oom_failure = payload(
        "BUILD_FAILED",
        same_diff,
        build=step(
            status="fail",
            returncode=1,
            summary="c++: fatal error: Killed signal terminated program cc1plus",
        ),
    )
    check("compiler_kill_is_oom", R._failure_category_from_verify_payload(oom_failure), "BUILD_OOM")

    native_failure = payload(
        "BUILD_FAILED",
        same_diff,
        build=step(
            status="fail",
            returncode=1,
            summary=(
                "Segmentation fault (core dumped)\n"
                "FAILED: generated/output\n"
                "ninja: build stopped: subcommand failed."
            ),
        ),
    )
    native_categories = {
        item["category"] for item in R._native_failure_diagnostics(native_failure)
    }
    check("segfault_category", "NATIVE_SEGMENTATION_FAULT" in native_categories, True)
    check("core_dump_category", "NATIVE_CORE_DUMP" in native_categories, True)
    check("ninja_failed_edge_category", "NINJA_FAILED_EDGE" in native_categories, True)
    check("ninja_stopped_category", "NINJA_BUILD_STOPPED" in native_categories, True)
    check(
        "segfault_is_not_resource_failure",
        R._failure_category_from_verify_payload(native_failure),
        "BUILD_COMPILE_ERROR",
    )


if failures:
    print("\nverification lifecycle self-check failed:")
    for failure in failures:
        print(f"- {failure}")
    raise SystemExit(1)

print("\nverification lifecycle self-check passed")
