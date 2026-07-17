#!/usr/bin/env python3
"""Inline self-test for command-owned loop policy and runner helpers.

Does NOT run subprocesses or hit models. Validates the pure decision helpers
(_compute_status, command policy parsing, session-id parsing, and task shaping).

Run: python3 scripts/self_check_runner_continue.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402
from smell_core.loop_policy import parse_command_policy  # noqa: E402

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
check("opencode_fail_verify_pass", R._compute_status(1, 0, make_payload("PASS")), "OPENCODE_FAILED")
check("opencode_timeout_verify_pass", R._compute_status(124, 0, make_payload("PASS")), "PASS_AFTER_OPENCODE_TIMEOUT")
check("opencode_timeout_verify_fail", R._compute_status(124, 0, make_payload("SMELL_GUARD_FAILED")), "OPENCODE_FAILED")

print("== single time budget ==")
check("opencode_timeout_derived", R._opencode_timeout_seconds(1800), 1860)
check("pass_is_accepted", R._is_accepted_status("PASS"), True)
check("timeout_pass_is_accepted", R._is_accepted_status("PASS_AFTER_OPENCODE_TIMEOUT"), True)
check("opencode_failure_not_accepted", R._is_accepted_status("OPENCODE_FAILED"), False)
parser = R.build_parser()
parsed = parser.parse_args(["--dataset", "/tmp/input.csv", "--sample-deadline", "2400"])
check("sample_deadline_public_entry", parsed.sample_deadline, 2400)
for removed_flag in ("--timeout", "--verify-timeout", "--opencode-log-idle-timeout"):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--dataset", "/tmp/input.csv", removed_flag, "1"])
    except SystemExit:
        pass
    else:
        raise AssertionError(f"removed flag still accepted: {removed_flag}")

print("== command policy parser ==")
resolved = parse_command_policy('--verification-mode=sample_optimized --loop-max=2 --loop-on=smell,test --loop-instruction="Use the pack" -- Project root: /tmp/p')
check("policy_mode", resolved.verification_mode, "sample_optimized")
check("policy_max", resolved.loop.max_continuations, 2)
check("policy_groups", resolved.loop.allowed_failure_groups, ("smell", "test"))
check("policy_instruction", resolved.loop.instruction, "Use the pack")
quoted_argv = parse_command_policy("--loop-max=2 '--loop-instruction=修复最新 failure pack 并保持行为不变' -- task")
check("policy_instruction_quoted_argv", quoted_argv.loop.instruction, "修复最新 failure pack 并保持行为不变")
check_true("policy_allows_guard", resolved.loop.allows("SMELL_GUARD_FAILED"))
check("policy_blocks_compile", resolved.loop.allows("BUILD_COMPILE_ERROR"), False)
check("policy_task", resolved.task, "Project root: /tmp/p")
disabled = parse_command_policy('--loop-max=0 -- Project root: /tmp/p')
check("zero_disables", disabled.loop.mode, "off")
for name, raw in [
    ("missing_delimiter", "--loop-max=2 task"),
    ("bad_max", "--loop-max=9 -- task"),
    ("bad_group", "--loop-on=unknown -- task"),
]:
    try:
        parse_command_policy(raw)
        failures.append(f"{name}: expected ValueError")
    except ValueError as exc:
        check_true(name, str(exc).startswith("INVALID_LOOP_POLICY:"))

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

print("== _task_prompt ==")
# Build a minimal fake sample/args to call _task_prompt
import argparse
from run_smell_dataset import Sample
sample = Sample(
    sample_id="1", language="java", smell="long_method", project_name="p",
    project_root=Path("/tmp/p"), location="Foo.java:1", evidence="", raw={},
)
args = argparse.Namespace(
    idea_refactor_cli="",
    loop_mode="verify-failure",
    loop_max=2,
    loop_no_progress_limit=1,
    loop_on="smell,compile,test",
    loop_instruction="Repair from the latest failure pack",
    sample_deadline=1800,
)
prompt_plain = R._task_prompt(sample, args, "local", "java-refactor-agent")
check_true("prompt_has_base", "Repair this one Java smell" in prompt_plain)
roundtrip = parse_command_policy(R._command_arguments(prompt_plain, args, "local"))
check("command_roundtrip_instruction", roundtrip.loop.instruction, args.loop_instruction)
check_true("command_roundtrip_task", "Repair this one Java smell" in roundtrip.task)

print()
if failures:
    print(f"FAILED: {len(failures)} checks")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
