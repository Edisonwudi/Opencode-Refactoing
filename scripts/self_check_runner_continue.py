#!/usr/bin/env python3
"""Inline self-test for command-owned loop policy and runner helpers.

Does NOT run subprocesses or hit models. Validates the pure decision helpers
(_compute_status, command policy parsing, session-id parsing, and task shaping).

Run: python3 scripts/self_check_runner_continue.py
"""
from __future__ import annotations

import contextlib
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
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

print("== dataset evidence identity ==")
god_row = {"smell_type": "god_class", "class": "Configuration", "evidence": "nom=143;wmc=162"}
check(
    "god_class_appends_class",
    R._dataset_evidence(god_row),
    "nom=143;wmc=162;class=Configuration",
)
check(
    "god_class_preserves_existing_class",
    R._dataset_evidence({**god_row, "evidence": "nom=143;class=Configuration"}),
    "nom=143;class=Configuration",
)
check(
    "other_smell_unchanged",
    R._dataset_evidence({"smell_type": "feature_envy", "class": "Configuration", "evidence": "far=8"}),
    "far=8",
)

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

print("== synchronous verification closure ==")
def verify_event(payload: dict, status: str = "completed") -> str:
    return json.dumps({
        "type": "tool_use",
        "sessionID": "ses_loop",
        "part": {
            "tool": "smell_verify",
            "state": {"status": status, "output": json.dumps(payload)},
        },
    })

empty_trace = R._verification_trace('{"type":"text","part":{"text":"done"}}')
check("trace_empty_calls", empty_trace["smell_verify_calls"], 0)
check(
    "initial_missing_verify_reminder",
    R._runner_closure_action(empty_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
    "verify_required",
)
check(
    "missing_verify_one_shot",
    R._runner_closure_action(empty_trace, reminder_used=True, continuations_dispatched=0, max_continuations=2),
    "stop",
)
continue_trace = R._verification_trace(verify_event({"status": "SMELL_GUARD_FAILED", "loop": {"decision": "continue"}}))
check("trace_verify_calls", continue_trace["smell_verify_calls"], 1)
check("trace_decision", continue_trace["last_loop_decision"], "continue")
check(
    "continue_with_budget",
    R._runner_closure_action(continue_trace, reminder_used=False, continuations_dispatched=1, max_continuations=2),
    "continue",
)
check(
    "continue_cap_shared",
    R._runner_closure_action(continue_trace, reminder_used=False, continuations_dispatched=2, max_continuations=2),
    "stop",
)
pass_trace = R._verification_trace(verify_event({"status": "PASS", "loop": {"decision": "stop"}}))
check(
    "pass_stops",
    R._runner_closure_action(pass_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
    "stop",
)
malformed = json.dumps({"type": "tool_use", "part": {"tool": "smell_verify", "state": {"status": "completed", "output": "truncated"}}})
malformed_trace = R._verification_trace(malformed)
check("malformed_still_counts_verify", malformed_trace["smell_verify_calls"], 1)
check("malformed_has_no_decision", malformed_trace["last_loop_decision"], "")
truncated_with_metadata = json.dumps({
    "type": "tool_use",
    "part": {
        "tool": "smell_verify",
        "state": {
            "status": "completed",
            "output": "{truncated",
            "metadata": {
                "loop": {"decision": "continue"},
                "auto_continuation": {"status": "SMELL_GUARD_FAILED"},
            },
        },
    },
})
metadata_trace = R._verification_trace(truncated_with_metadata)
check("metadata_survives_truncated_output", metadata_trace["last_loop_decision"], "continue")
check("metadata_status", metadata_trace["last_status"], "SMELL_GUARD_FAILED")
cap_payload = {
    "status": "SMELL_GUARD_FAILED",
    "failure_pack": {"retryable": True},
    "checkpoint": {"delta": {"metric_progress": True}},
    "loop": {"decision": "stop", "termination_reason": "MAX_CONTINUATIONS_REACHED"},
}
cap_trace = R._verification_trace(verify_event(cap_payload))
check("cap_termination_reason", cap_trace["last_termination_reason"], "MAX_CONTINUATIONS_REACHED")
check("cap_retryable", cap_trace["last_failure_retryable"], True)
check("cap_metric_progress", cap_trace["last_metric_progress"], True)
check(
    "cap_progress_resumes_same_session",
    R._runner_closure_action(cap_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
    "continue_after_internal_cap",
)
check(
    "cap_progress_obeys_outer_budget",
    R._runner_closure_action(cap_trace, reminder_used=False, continuations_dispatched=2, max_continuations=2),
    "stop",
)
compile_cap_payload = {
    "status": "BUILD_FAILED",
    "failure_pack": {"retryable": True},
    "checkpoint": {"delta": {"metric_progress": False}},
    "snapshot": {"diff_stat": {"stdout": " Foo.java | 2 +-\n 1 file changed"}},
    "loop": {"decision": "stop", "termination_reason": "MAX_CONTINUATIONS_REACHED"},
}
compile_cap_trace = R._verification_trace(verify_event(compile_cap_payload))
check("compile_cap_has_diff", compile_cap_trace["last_has_production_diff"], True)
check(
    "compile_cap_nonempty_diff_resumes",
    R._runner_closure_action(
        compile_cap_trace,
        reminder_used=False,
        continuations_dispatched=0,
        max_continuations=2,
    ),
    "continue_after_internal_cap",
)
compile_cap_no_diff = {
    **compile_cap_payload,
    "snapshot": {"diff_stat": {"stdout": ""}},
}
check(
    "compile_cap_empty_diff_stops",
    R._runner_closure_action(
        R._verification_trace(verify_event(compile_cap_no_diff)),
        reminder_used=False,
        continuations_dispatched=0,
        max_continuations=2,
    ),
    "stop",
)
for name, mutation in (
    ("cap_without_progress_stops", {"checkpoint": {"delta": {"metric_progress": False}}}),
    ("cap_non_retryable_stops", {"failure_pack": {"retryable": False}}),
    ("non_cap_stop_stops", {"loop": {"decision": "stop", "termination_reason": "NO_PROGRESS"}}),
):
    payload = {**cap_payload, **mutation}
    trace = R._verification_trace(verify_event(payload))
    check(
        name,
        R._runner_closure_action(trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
        "stop",
    )
check_true("verify_prompt_marker", "verify-required" in R._runner_continuation_prompt("verify_required", 0, 2, "repair"))
check_true("continue_prompt_marker", "continue 1/2" in R._runner_continuation_prompt("continue", 1, 2, "repair"))
check_true(
    "cap_prompt_explains_budget_boundary",
    "internal continuation budget was exhausted"
    in R._runner_continuation_prompt("continue_after_internal_cap", 1, 2, "repair"),
)

command_args = argparse.Namespace(opencode_bin="opencode", model="minimax/MiniMax-M2.7")
initial_cmd = R._opencode_run_command(command_args, "java-refactor-agent")
continued_cmd = R._opencode_run_command(command_args, "java-refactor-agent", "ses_loop")
check_true("initial_uses_command", "--command" in initial_cmd and "--session" not in initial_cmd)
check_true("continuation_uses_session", "--session" in continued_cmd and "ses_loop" in continued_cmd)
check("continuation_no_command", "--command" in continued_cmd, False)

print("== fake CLI same-session integration ==")
with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp) / "fake-opencode"
    fake.write_text(
        """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
continued = "--session" in sys.argv
decision = "stop" if continued else "continue"
status = "PASS" if continued else "SMELL_GUARD_FAILED"
payload = {"status": status, "loop": {"decision": decision}}
event = {
    "type": "tool_use",
    "sessionID": "ses_fake",
    "part": {
        "tool": "smell_verify",
        "state": {
            "status": "completed",
            "output": json.dumps(payload),
            "metadata": {"loop": payload["loop"], "auto_continuation": {"status": status}},
        },
    },
}
print(json.dumps(event))
""",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    fake_args = argparse.Namespace(opencode_bin=str(fake), model="minimax/MiniMax-M2.7")
    first = subprocess.run(
        R._opencode_run_command(fake_args, "java-refactor-agent"),
        input="initial command",
        text=True,
        capture_output=True,
        check=False,
    )
    first_sid = R._parse_session_id_from_json_events(first.stdout)
    first_trace = R._verification_trace(first.stdout)
    first_action = R._runner_closure_action(
        first_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2
    )
    check("fake_initial_rc", first.returncode, 0)
    check("fake_initial_sid", first_sid, "ses_fake")
    check("fake_initial_action", first_action, "continue")
    second = subprocess.run(
        R._opencode_run_command(fake_args, "java-refactor-agent", first_sid),
        input=R._runner_continuation_prompt("continue", 1, 2, "repair"),
        text=True,
        capture_output=True,
        check=False,
    )
    second_trace = R._verification_trace(second.stdout)
    second_action = R._runner_closure_action(
        second_trace, reminder_used=False, continuations_dispatched=1, max_continuations=2
    )
    check("fake_continuation_rc", second.returncode, 0)
    check("fake_continuation_status", second_trace["last_status"], "PASS")
    check("fake_continuation_action", second_action, "stop")

print("== _task_prompt ==")
# Build a minimal fake sample/args to call _task_prompt
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
check_true("prompt_has_base", "Repair this one java smell" in prompt_plain)
roundtrip = parse_command_policy(R._command_arguments(prompt_plain, args, "local"))
check("command_roundtrip_instruction", roundtrip.loop.instruction, args.loop_instruction)
check_true("command_roundtrip_task", "Repair this one java smell" in roundtrip.task)

refused = Sample(
    sample_id="2",
    language="java",
    smell="refused_bequest",
    project_name="p",
    project_root=Path("/tmp/p"),
    location="Child.java:method=reject|line=10",
    evidence=(
        "parents=Parent; structural_expectation=capability_split; "
        "refactor_path=split_read_from_write; refactor_group_id=packet_capabilities"
    ),
    raw={},
)
refused_prompt = R._task_prompt(refused, args, "project_full", "java-refactor-agent")
check_true(
    "refused_prompt_maps_callers",
    "every production call site of the target contract" in refused_prompt,
)
check_true(
    "refused_prompt_rejects_relocation",
    "Do not relocate an empty, throwing, null-returning" in refused_prompt,
)
check_true(
    "refused_prompt_names_group",
    "Refactor group packet_capabilities identifies the shared design boundary" in refused_prompt,
)

print()
if failures:
    print(f"FAILED: {len(failures)} checks")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
