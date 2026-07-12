#!/usr/bin/env python3
"""Inline self-test for run_smell_dataset.py retry-loop pure functions.

Does NOT run subprocesses or hit models. Validates the pure decision helpers
(_should_retry, _compute_status, _failure_category_from_verify_payload,
_build_failure_context) and simulates the full retry loop's stopping behavior.

Run: python3 scripts/self_check_runner_continue.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402

failures: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual != expected:
        failures.append(f"{name}: expected {expected!r}, got {actual!r}")
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok   {name}")


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


def make_payload(status: str, category: str = "", **extra) -> dict:
    pack = {"failure_category": category, "verify_status": status}
    pack.update(extra.get("pack_extra", {}))
    return {"success": status == "PASS", "status": status, "failure_pack": pack, **{k: v for k, v in extra.items() if k != "pack_extra"}}


def simulate_loop(first_status: str, first_category: str, subsequent=None) -> tuple[int, str]:
    """Replay the _run_sample retry decision logic without subprocesses.

    subsequent: list of (status, category) for attempts 1,2... Returns
    (total_attempts, final_status).
    """
    subsequent = subsequent or []
    attempts = 0
    attempt_idx = 0
    # Build the sequence of (status, payload) the loop would see.
    seq = [(first_status, first_category)] + subsequent
    final_status = first_status
    for status, category in seq:
        payload = make_payload(status, category)
        attempts += 1
        final_status = status
        if not R._should_retry(status, payload, attempt_idx):
            break
        attempt_idx += 1
    return attempts, final_status


print("== _failure_category_from_verify_payload ==")
check("empty payload", R._failure_category_from_verify_payload({}), "")
check("no failure_pack", R._failure_category_from_verify_payload({"status": "X"}), "")
check("with category", R._failure_category_from_verify_payload(make_payload("X", "SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("non-dict pack", R._failure_category_from_verify_payload({"failure_pack": "nope"}), "")

print("== _compute_status ==")
check("pass", R._compute_status(0, 0, make_payload("PASS")), "PASS")
check("verify_fail_rc", R._compute_status(0, 1, make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("verify_fail_nostatus", R._compute_status(0, 1, {"status": ""}), "VERIFY_FAILED")
check("both_fail", R._compute_status(1, 1, make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")), "OPENCODE_FAILED")

print("== _should_retry ==")
# PASS never retries
check("pass_no_retry", R._should_retry("PASS", make_payload("PASS"), 0), False)
# repairable + budget left -> retry
check("guard_retry_att0", R._should_retry("SMELL_GUARD_FAILED", make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"), 0), True)
check("compile_retry_att0", R._should_retry("BUILD_FAILED", make_payload("BUILD_FAILED", "BUILD_COMPILE_ERROR"), 0), True)
check("test_regression_retry", R._should_retry("TEST_FAILED", make_payload("TEST_FAILED", "TEST_BEHAVIOR_REGRESSION"), 0), True)
# non-repairable -> no retry
for cat in ["BUILD_DEPENDENCY_RESOLUTION", "TIMEOUT_OR_MODAL_SUSPECTED", "BUILD_TEST_REQUIRED", "UNKNOWN_VERIFY_FAILURE", "OPENCODE_FAILED", ""]:
    check(f"no_retry_{cat or 'empty'}", R._should_retry("X", make_payload("X", cat), 0), False)
# budget exhausted -> no retry even if repairable
check("budget_exhausted_att2", R._should_retry("SMELL_GUARD_FAILED", make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"), 2), False)
check("budget_exhausted_att1_retry", R._should_retry("SMELL_GUARD_FAILED", make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"), 1), True)

print("== _parse_session_id_from_json_events ==")
# Real --format json event format: sessionID is a TOP-LEVEL field.
check("toplevel_sid", R._parse_session_id_from_json_events('{"type":"error","timestamp":1,"sessionID":"ses_abc123","error":{}}'), "ses_abc123")
# Multiple lines: first one with sessionID wins
multi = '{"type":"server.connected","sessionID":"ses_xyz"}\n{"type":"message.updated","sessionID":"ses_xyz"}'
check("multi_line_sid", R._parse_session_id_from_json_events(multi), "ses_xyz")
# Nested SSE-style fallback (properties.sessionID)
check("nested_sid", R._parse_session_id_from_json_events('{"type":"session.idle","properties":{"sessionID":"ses_nest"}}'), "ses_nest")
# No sessionID -> empty
check("no_sid", R._parse_session_id_from_json_events('{"type":"server.connected"}'), "")
check("empty_input", R._parse_session_id_from_json_events(""), "")
# Non-JSON lines ignored
check("non_json_ignored", R._parse_session_id_from_json_events('not json\n{"sessionID":"ses_ok"}'), "ses_ok")
# Whitespace in sessionID trimmed
check("sid_trimmed", R._parse_session_id_from_json_events('{"sessionID":"  ses_trim  "}'), "ses_trim")

print("== _build_continuation_nudge ==")
nudge = R._build_continuation_nudge(make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"), 1)
check_true("nudge_has_attempt", "Attempt 1" in nudge)
check_true("nudge_has_category", "SMELL_GUARD_FAILED" in nudge)
check_true("nudge_has_instruction", "narrow corrective edit" in nudge)
# Shorter than full failure_context (no highlights block)
full = R._build_failure_context(make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED", pack_extra={"highlights": ["x" * 200], "recommendations": ["y"]}), 1)
check_true("nudge_shorter_than_full", len(nudge) < len(full))
# No failure_pack -> still produces a nudge (points at smell_verify), just without category
nudge_no_pack = R._build_continuation_nudge({"status": "X"}, 1)
check_true("nudge_no_pack_has_attempt", "Attempt 1" in nudge_no_pack)
check_true("nudge_no_pack_has_instruction", "narrow corrective edit" in nudge_no_pack)

print("== retry loop simulation ==")
# 1. first PASS -> 1 attempt
n, s = simulate_loop("PASS", "")
check("first_pass_attempts", n, 1)
check("first_pass_status", s, "PASS")
# 2. first repairable fail, then PASS -> 2 attempts
n, s = simulate_loop("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED", [("PASS", "")])
check("fail_then_pass_attempts", n, 2)
check("fail_then_pass_status", s, "PASS")
# 3. non-repairable first fail -> 1 attempt, no retry
n, s = simulate_loop("BUILD_FAILED", "BUILD_DEPENDENCY_RESOLUTION")
check("nonrepairable_attempts", n, 1)
check("nonrepairable_status", s, "BUILD_FAILED")
# 4. repairable fail x3 (exhaust budget) -> 3 attempts (0,1,2), stop at 2
n, s = simulate_loop("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED", [("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"), ("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")])
check("exhaust_budget_attempts", n, 3)
check("exhaust_budget_status", s, "SMELL_GUARD_FAILED")
# 5. repairable then non-repairable -> 2 attempts
n, s = simulate_loop("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED", [("BUILD_FAILED", "BUILD_DEPENDENCY_RESOLUTION")])
check("repairable_then_non_attempts", n, 2)
check("repairable_then_non_status", s, "BUILD_FAILED")

print("== _build_failure_context ==")
ctx = R._build_failure_context(make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED", pack_extra={"highlights": ["cannot find symbol Foo"], "recommendations": ["fix it"]}), 1)
check_true("ctx_has_attempt", "attempt 1" in ctx)
check_true("ctx_has_category", "SMELL_GUARD_FAILED" in ctx)
check_true("ctx_has_highlight", "cannot find symbol Foo" in ctx)
check_true("ctx_has_recommendation", "fix it" in ctx)
check_true("ctx_has_instruction", "narrow corrective edit" in ctx)
# empty when no failure_pack
check("ctx_empty_no_pack", R._build_failure_context({"status": "X"}, 1), "")
# highlights truncated
long_h = "x" * 500
ctx2 = R._build_failure_context(make_payload("X", "X", pack_extra={"highlights": [long_h]}), 2)
check_true("ctx_truncates", "..." in ctx2 and len(ctx2) < 600)

print("== _task_prompt includes failure_context ==")
# Build a minimal fake sample/args to call _task_prompt
import argparse
from run_smell_dataset import Sample
sample = Sample(
    sample_id="1", language="java", smell="long_method", project_name="p",
    project_root=Path("/tmp/p"), location="Foo.java:1", evidence="", raw={},
)
args = argparse.Namespace(idea_refactor_cli="")
prompt_plain = R._task_prompt(sample, args, "local", "java-refactor-agent")
prompt_with_ctx = R._task_prompt(sample, args, "local", "java-refactor-agent", failure_context="PREV FAILED CONTEXT")
check_true("prompt_has_base", "Repair this one Java smell" in prompt_plain)
check_true("prompt_no_ctx_by_default", "PREV FAILED CONTEXT" not in prompt_plain)
check_true("prompt_has_ctx_when_given", "PREV FAILED CONTEXT" in prompt_with_ctx)

print("== constants alignment ==")
check("max_attempts_value", R.MAX_RUNNER_CONTINUE_ATTEMPTS, 2)
check("repairable_count", len(R.REPAIRABLE_FAILURE_CATEGORIES), 5)
check_true("repairable_has_guard", "SMELL_GUARD_FAILED" in R.REPAIRABLE_FAILURE_CATEGORIES)
check_true("repairable_has_compile", "BUILD_COMPILE_ERROR" in R.REPAIRABLE_FAILURE_CATEGORIES)
check_true("repairable_has_test_regression", "TEST_BEHAVIOR_REGRESSION" in R.REPAIRABLE_FAILURE_CATEGORIES)
check_true("repairable_has_test_stale", "TEST_REFLECTION_ENTRY_STALE" in R.REPAIRABLE_FAILURE_CATEGORIES)
check_true("repairable_has_sample_test", "SAMPLE_TEST_FAILED" in R.REPAIRABLE_FAILURE_CATEGORIES)

print()
if failures:
    print(f"FAILED: {len(failures)} checks")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
