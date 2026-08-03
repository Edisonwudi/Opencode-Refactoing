#!/usr/bin/env python3
"""Inline self-test for command-owned loop policy and runner helpers.

Does NOT hit models. Validates the pure decision helpers
(_compute_status, command policy parsing, session-id parsing, and task shaping).

Run: python3 scripts/self_check_runner_continue.py
"""
from __future__ import annotations

import contextlib
import argparse
import io
import inspect
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
from smell_core.loop_policy import (  # noqa: E402
    parse_command_policy,
    parse_command_task_identity,
)

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
    return {
        "success": status == "PASS",
        "accepted": status == "PASS",
        "status": status,
        "resolution": "resolved" if status == "PASS" else ("improved" if status == "IMPROVED" else "failed"),
        "failure_pack": pack,
        **{k: v for k, v in extra.items() if k != "pack_extra"},
    }


print("== _failure_category_from_verify_payload ==")
check("empty payload", R._failure_category_from_verify_payload({}), "")
check("no failure_pack", R._failure_category_from_verify_payload({"status": "X"}), "")
check("with category", R._failure_category_from_verify_payload(make_payload("X", "SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("non-dict pack", R._failure_category_from_verify_payload({"failure_pack": "nope"}), "")

print("== _compute_status ==")
check("pass", R._compute_status(0, 0, make_payload("PASS")), "PASS")
check("pass_nonzero_bridge_rc_fails_closed", R._compute_status(0, 1, make_payload("PASS")), "VERIFY_FAILED")
check("pass_missing_resolution_fails_closed", R._compute_status(0, 0, {"status": "PASS", "success": True, "accepted": True}), "VERIFY_FAILED")
check("pass_missing_accepted_fails_closed", R._compute_status(0, 0, {"status": "PASS", "success": True, "resolution": "resolved"}), "VERIFY_FAILED")
check("pass_false_success_fails_closed", R._compute_status(0, 0, {"status": "PASS", "success": False, "accepted": True, "resolution": "resolved"}), "VERIFY_FAILED")
check("verify_fail_rc", R._compute_status(0, 1, make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("verify_fail_nostatus", R._compute_status(0, 1, {"status": ""}), "VERIFY_FAILED")
check("both_fail", R._compute_status(1, 1, make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("opencode_fail_verify_pass", R._compute_status(1, 0, make_payload("PASS")), "PASS")
check("opencode_timeout_verify_pass", R._compute_status(124, 0, make_payload("PASS")), "PASS")
check("opencode_timeout_verify_fail", R._compute_status(124, 1, make_payload("SMELL_GUARD_FAILED")), "SMELL_GUARD_FAILED")
check("improved_not_accepted", R._compute_status(0, 1, make_payload("IMPROVED")), "IMPROVED")
check("timeout_preserves_improved", R._compute_status(124, 1, make_payload("IMPROVED")), "IMPROVED")
check("improved_status_not_accepted", R._is_accepted_status("IMPROVED"), False)
check(
    "provider_quota_is_metadata_when_final_verify_passes",
    R._compute_status(R.OPENCODE_FATAL_PROVIDER_RETURN_CODE, 0, make_payload("PASS")),
    "PASS",
)

print("== fatal provider error ==")
check(
    "minimax_token_plan",
    R._fatal_provider_error("AI_APICallError: 已达到 Token Plan 用量上限：请升级套餐。 (2056)"),
    "MINIMAX_TOKEN_PLAN_EXHAUSTED",
)
check(
    "generic_insufficient_quota",
    R._fatal_provider_error('{"error":{"code":"insufficient_quota"}}'),
    "PROVIDER_INSUFFICIENT_QUOTA",
)
check("transient_rate_limit_not_fatal", R._fatal_provider_error("HTTP 429 rate limit"), "")
check("ordinary_model_error_not_fatal", R._fatal_provider_error("tool call failed"), "")

print("== dataset evidence identity ==")
god_row = {"smell_type": "god_class", "class": "Configuration", "evidence": "nom=143;wmc=162"}
check("god_class_evidence_is_audit_only", R._dataset_evidence(god_row), "nom=143;wmc=162")
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
check(
    "explicit_target_context_loaded",
    R._dataset_target_context({
        "smell_type": "mysterious_name",
        "target_context_json": json.dumps({
            "symbol_kind": "local",
            "symbol_name": "tmp",
            "container_method": "work()",
            "target_class": "Fixture",
        }),
        "evidence": "local=forged",
    }),
    {
        "symbol_kind": "local",
        "symbol_name": "tmp",
        "container_method": "work()",
        "target_class": "Fixture",
    },
)
check(
    "evidence_never_constructs_target_context",
    R._dataset_target_context({
        "smell_type": "data_clumps",
        "evidence": "group=int:a|int:b|int:c",
    }),
    {},
)
try:
    R._dataset_target_context({"target_context_json": '{"score":99}'})
except ValueError:
    pass
else:
    raise AssertionError("forbidden target_context_json verdict field was accepted")
with tempfile.TemporaryDirectory() as tmp:
    dataset = Path(tmp) / "samples.csv"
    header = (
        "sample_id,language,smell_type,project_name,project_path,location,"
        "group_occurrences,target_context_json\n"
    )
    dataset.write_text(
        header
        + '1,java,long_method,p,/tmp/p,src/Foo.java:method=target|line=42,'
        + '"{\""method\"":\""forged\""}","{}"\n',
        encoding="utf-8",
    )
    loaded = R._load_samples(dataset)
    check(
        "explicit_location_is_not_rewritten_from_group_occurrences",
        loaded[0].location,
        "src/Foo.java:method=target|line=42",
    )
    dataset.write_text(
        header + '1,java,long_method,p,/tmp/p,src/Foo.java:42,,"{}"\n',
        encoding="utf-8",
    )
    try:
        R._load_samples(dataset)
    except ValueError as exc:
        check_true("missing_method_selector_fails_fast", "explicit method selector" in str(exc))
    else:
        failures.append("missing_method_selector_fails_fast: invalid row was accepted")

print("== single time budget ==")
check("opencode_timeout_derived", R._opencode_timeout_seconds(1800), 1860)
check("pass_is_accepted", R._is_accepted_status("PASS"), True)
check("removed_timeout_pass_is_not_accepted", R._is_accepted_status("PASS_AFTER_OPENCODE_TIMEOUT"), False)
check("opencode_failure_not_accepted", R._is_accepted_status("OPENCODE_FAILED"), False)
parser = R.build_parser()
parsed = parser.parse_args(["--dataset", "/tmp/input.csv", "--sample-deadline", "2400"])
check("sample_deadline_public_entry", parsed.sample_deadline, 2400)
check("project_full_is_default", parsed.verification_mode, "project_full")
check("test_changes_default_forbidden", parsed.allow_test_changes, False)
check(
    "test_changes_explicit_opt_in",
    parser.parse_args(["--dataset", "/tmp/input.csv", "--allow-test-changes"]).allow_test_changes,
    True,
)
for removed_flag in ("--timeout", "--verify-timeout", "--opencode-log-idle-timeout"):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--dataset", "/tmp/input.csv", removed_flag, "1"])
    except SystemExit:
        pass
    else:
        raise AssertionError(f"removed flag still accepted: {removed_flag}")
for removed_idea_args in (
    ["--idea"],
    ["--no-idea"],
    ["--idea-refactor-cli", "/tmp/idea-refactor"],
    ["--agent", "java-refactor-agent-idea"],
):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--dataset", "/tmp/input.csv", *removed_idea_args])
    except SystemExit:
        pass
    else:
        raise AssertionError(f"removed IDEA runner entry still accepted: {removed_idea_args}")
for removed_mode in ("local", "auto"):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--dataset", "/tmp/input.csv", "--verification-mode", removed_mode])
    except SystemExit:
        pass
    else:
        raise AssertionError(f"removed verification mode still accepted: {removed_mode}")

print("== command policy parser ==")
resolved = parse_command_policy('--verification-mode=sample_optimized --loop-max=2 --loop-on=smell,test --loop-instruction="Use the pack" -- Project root: /tmp/p')
check("policy_mode", resolved.verification_mode, "sample_optimized")
check("policy_max", resolved.loop.max_continuations, 2)
check("policy_groups", resolved.loop.allowed_failure_groups, ("smell", "test"))
check("policy_instruction", resolved.loop.instruction, "Use the pack")
quoted_argv = parse_command_policy("--loop-max=2 '--loop-instruction=修复最新 failure pack 并保持行为不变' -- task")
check("policy_instruction_quoted_argv", quoted_argv.loop.instruction, "修复最新 failure pack 并保持行为不变")
check_true("policy_allows_guard", resolved.loop.allows("SMELL_GUARD_FAILED"))
check("policy_removes_dataset_route_category", resolved.loop.allows("STRUCTURAL_ROUTE_MISMATCH"), False)
check("policy_blocks_compile", resolved.loop.allows("BUILD_COMPILE_ERROR"), False)
check("policy_task", resolved.task, "Project root: /tmp/p")
readme_identity = parse_command_task_identity(
    (
        "Project root: /abs/java-project; Smell type: long_method; "
        "Target location: src/main/java/Foo.java:42"
    ),
    verification_mode="sample_optimized",
)
check("readme_semicolon_project", readme_identity.project_root, "/abs/java-project")
check("readme_semicolon_smell", readme_identity.smell, "long_method")
check(
    "readme_semicolon_location",
    readme_identity.location,
    "src/main/java/Foo.java:42",
)
readme_bridge = subprocess.run(
    [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "resolve-command",
        "--arguments",
        (
            "--verification-mode=sample_optimized --loop-max=2 -- "
            "Project root: /abs/java-project; Smell type: long_method; "
            "Target location: src/main/java/Foo.java:42"
        ),
    ],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
check("readme_bridge_resolve_rc", readme_bridge.returncode, 0)
if readme_bridge.returncode == 0:
    readme_bridge_payload = json.loads(readme_bridge.stdout)
    check(
        "readme_bridge_structured_identity",
        readme_bridge_payload["identity"]["location"],
        "src/main/java/Foo.java:42",
    )
    check("readme_bridge_checkpoint_required", readme_bridge_payload["checkpoint_required"], True)
    initial_state = readme_bridge_payload["command_loop_state"]
    check("readme_bridge_state_schema", initial_state["schema_version"], 3)
    check(
        "readme_bridge_state_identity",
        initial_state["policy"]["identity"]["project_root"],
        "/abs/java-project",
    )
    check("readme_bridge_state_continuations", initial_state["continuation_count"], 0)
multiline_identity = parse_command_task_identity(
    "\n".join(
        [
            "Project root: /tmp/p",
            "Language: java",
            "Smell type: feature_envy",
            "Target location: src/Foo.java:10",
            "Verification mode: project_full",
            "Test changes: forbidden.",
            "",
            "Repair the target and preserve behavior.",
        ]
    ),
    verification_mode="project_full",
)
check("multiline_identity_language", multiline_identity.language, "java")
check("multiline_identity_mode", multiline_identity.verification_mode, "project_full")
for name, task, expected_prefix in [
    (
        "identity_missing_required",
        "Project root: /tmp/p; Smell type: long_method",
        "INVALID_COMMAND_TASK_IDENTITY:",
    ),
    (
        "identity_duplicate_field",
        (
            "Project root: /tmp/p; Project root: /tmp/q; "
            "Smell type: long_method; Target location: Foo.java:1"
        ),
        "INVALID_COMMAND_TASK_IDENTITY:",
    ),
    (
        "identity_mode_mismatch",
        (
            "Project root: /tmp/p; Smell type: long_method; "
            "Target location: Foo.java:1; Verification mode: sample_optimized"
        ),
        "COMMAND_TASK_VERIFICATION_MODE_MISMATCH:",
    ),
]:
    try:
        parse_command_task_identity(
            task,
            verification_mode="project_full",
        )
        failures.append(f"{name}: expected ValueError")
    except ValueError as exc:
        check_true(name, str(exc).startswith(expected_prefix))
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
try:
    parse_command_policy(
        "--verification-mode=sample_optimized --allow-test-changes -- task"
    )
    failures.append("test_changes_require_project_full: expected ValueError")
except ValueError as exc:
    check_true(
        "test_changes_require_project_full",
        str(exc).startswith("TEST_CHANGE_REQUIRES_PROJECT_FULL:"),
    )

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
def verify_event(payload: dict, status: str = "completed", metadata: dict | None = None) -> str:
    return json.dumps({
        "type": "tool_use",
        "sessionID": "ses_loop",
        "part": {
            "tool": "smell_verify",
            "state": {
                "status": status,
                "output": json.dumps(payload),
                **({"metadata": metadata} if metadata is not None else {}),
            },
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
    "bare_continue_has_no_extra_transport",
    R._runner_closure_action(continue_trace, reminder_used=False, continuations_dispatched=2, max_continuations=2),
    "stop",
)
cap_continue_trace = R._verification_trace(verify_event({
    "status": "SMELL_GUARD_FAILED",
    "loop": {"decision": "continue", "cap_recovery_used": True},
}))
check("trace_cap_recovery", cap_continue_trace["last_cap_recovery_used"], True)
check(
    "plugin_cap_recovery_transport",
    R._runner_closure_action(cap_continue_trace, reminder_used=False, continuations_dispatched=2, max_continuations=2),
    "continue",
)
check(
    "transport_hard_cap",
    R._runner_closure_action(cap_continue_trace, reminder_used=False, continuations_dispatched=3, max_continuations=2),
    "stop",
)
persisted_state = {
    "schema_version": 1,
    "continuation_count": 2,
    "cap_recovery_used": False,
}
state_trace = R._verification_trace(verify_event(
    {"status": "SMELL_GUARD_FAILED", "loop": {"decision": "continue"}},
    metadata={
        "loop": {"decision": "continue", "cap_recovery_used": False},
        "command_loop_state": persisted_state,
    },
))
check("trace_command_loop_state", state_trace["command_loop_state"], persisted_state)
pass_trace = R._verification_trace(verify_event({
    "success": True,
    "status": "PASS",
    "loop": {"decision": "stop"},
}))
check(
    "pass_stops",
    R._runner_closure_action(pass_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
    "stop",
)
check("trace_keeps_last_payload", pass_trace["last_payload"]["status"], "PASS")
check("trace_no_tools_after_final_verify", pass_trace["tools_after_last_verify"], 0)
check("agent_verify_is_not_reused", hasattr(R, "_reusable_verify_payload"), False)
post_verify_tool = json.dumps({
    "type": "tool_use",
    "part": {
        "tool": "edit",
        "state": {"status": "completed", "output": "changed"},
    },
})
edited_after_verify = R._verification_trace(
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}})
    + "\n"
    + post_verify_tool
)
check("trace_counts_tools_after_verify", edited_after_verify["tools_after_last_verify"], 1)
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
check(
    "runner_does_not_reinterpret_plugin_stop",
    R._runner_closure_action(cap_trace, reminder_used=False, continuations_dispatched=0, max_continuations=2),
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
check(
    "runner_does_not_reinterpret_compile_stop",
    R._runner_closure_action(
        compile_cap_trace,
        reminder_used=False,
        continuations_dispatched=0,
        max_continuations=2,
    ),
    "stop",
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
check("buildenv_mutation_status_removed", hasattr(R, "_record_buildenv_mutation"), False)

with tempfile.TemporaryDirectory() as tmp:
    sample_dir = Path(tmp) / "sample"
    sample_dir.mkdir()
    source_diff = Path(tmp) / "source.patch"
    source_diff.write_text("diff --git a/A.java b/A.java\n", encoding="utf-8")
    persisted = {
        "success": True,
        "status": "PASS",
        "artifacts": {"diff": str(source_diff)},
    }
    R._persist_verify_payload(sample_dir, persisted)
    check_true("final_verify_json_persisted", (sample_dir / "verify.json").is_file())
    check("final_verify_status", json.loads((sample_dir / "verify.json").read_text())["status"], "PASS")
    check("final_verify_diff_copied", (sample_dir / "diff.patch").read_text(), source_diff.read_text())

with tempfile.TemporaryDirectory() as tmp:
    captured: dict[str, object] = {}
    original_run = R._run

    def fake_run(cmd, cwd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(cmd, 0, '{"success":true,"status":"PASS"}', "")

    R._run = fake_run
    try:
        sample = R.Sample(
            sample_id="1",
            language="java",
            smell="data_clumps",
            project_name="p",
            project_root=Path(tmp),
            location="src/Foo.java:method=target",
            evidence="oracle_score=99;group=int:a|int:b|int:c",
            raw={},
            target_context={"group": "int:a|int:b|int:c"},
            test_location="src/test/FooTest.java",
            test_command="mvn -Dtest=FooTest test",
        )
        R._run_capture_baseline(
            sample,
            Path(tmp),
            argparse.Namespace(projects="", sample_deadline=1),
            "sample_optimized",
        )
        baseline_cmd = captured["cmd"]
        baseline_env = captured["env"]
        check_true("runner_explicitly_captures_baseline", "capture-baseline" in baseline_cmd)
        check_true(
            "baseline_requests_compact_decision",
            "--output-detail" in baseline_cmd
            and baseline_cmd[baseline_cmd.index("--output-detail") + 1] == "decision",
        )
        check("baseline_excludes_smell_evidence", "--smell-evidence" in baseline_cmd, False)
        check_true("baseline_keeps_target_context", "--target-context-json" in baseline_cmd)
        check_true("baseline_keeps_test_location", "--sample-test-location" in baseline_cmd)
        check_true("baseline_keeps_test_command", "--sample-test-command" in baseline_cmd)
        check("baseline_disallows_test_changes_cli", "--allow-test-changes" in baseline_cmd, False)
        check("baseline_disallows_test_changes_env", baseline_env.get("SMELL_ALLOW_TEST_CHANGES"), "0")
        R._run_verify(
            sample,
            Path(tmp),
            argparse.Namespace(projects="", sample_deadline=1),
            "sample_optimized",
            baseline_seal="controller-seal",
        )
    finally:
        R._run = original_run
    final_cmd = captured["cmd"]
    final_env = captured["env"]
    check("final_verify_excludes_smell_evidence", "--smell-evidence" in final_cmd, False)
    check_true(
        "final_verify_requests_compact_decision",
        "--output-detail" in final_cmd
        and final_cmd[final_cmd.index("--output-detail") + 1] == "decision",
    )
    check_true("final_verify_keeps_target_context", "--target-context-json" in final_cmd)
    check_true("final_verify_uses_controller_seal", "--baseline-seal" in final_cmd and "controller-seal" in final_cmd)
    check("dataset_runner_freezes_tests", final_env.get("SMELL_ALLOW_TEST_CHANGES"), "0")
    check("final_verify_requires_build_test", final_env.get("SMELL_REQUIRE_BUILD_TEST"), "1")

    R._run = fake_run
    try:
        R._run_capture_baseline(
            sample,
            Path(tmp),
            argparse.Namespace(projects="", sample_deadline=1, allow_test_changes=True),
            "sample_optimized",
        )
        allowed_baseline_cmd = captured["cmd"]
        allowed_baseline_env = captured["env"]
        check_true("baseline_allows_test_changes_cli", "--allow-test-changes" in allowed_baseline_cmd)
        check("baseline_allows_test_changes_env", allowed_baseline_env.get("SMELL_ALLOW_TEST_CHANGES"), "1")
        R._run_verify(
            sample,
            Path(tmp),
            argparse.Namespace(projects="", sample_deadline=1, allow_test_changes=True),
            "sample_optimized",
            baseline_seal="controller-seal",
        )
        allowed_final_cmd = captured["cmd"]
        allowed_final_env = captured["env"]
        # verify reads the frozen policy from c000; its parser intentionally
        # has no policy-mutating CLI flag. The controller repeats the value in
        # the child environment for audit consistency.
        check("final_verify_has_no_policy_mutation_cli", "--allow-test-changes" in allowed_final_cmd, False)
        check("final_verify_allows_test_changes_env", allowed_final_env.get("SMELL_ALLOW_TEST_CHANGES"), "1")
    finally:
        R._run = original_run

check(
    "baseline_not_found_status_is_exact",
    R._baseline_failure_status(1, {"success": False, "error": "BASELINE_FINDING_NOT_FOUND"}),
    "BASELINE_FINDING_NOT_FOUND",
)
check(
    "baseline_ambiguity_status_is_exact",
    R._baseline_failure_status(1, {"success": False, "error": "TARGET_AMBIGUOUS: 2"}),
    "TARGET_AMBIGUOUS",
)
check(
    "baseline_success_has_no_failure_status",
    R._baseline_failure_status(0, {"success": True, "status": "BASELINE_CAPTURED"}),
    "",
)

run_sample_source = inspect.getsource(R._run_sample)
check("run_sample_has_one_final_verify_call", run_sample_source.count("_run_verify("), 1)
check("run_sample_has_no_verify_reuse", "reusable_verify" in run_sample_source, False)
check_true(
    "baseline_capture_precedes_model",
    run_sample_source.index("_run_capture_baseline(") < run_sample_source.index("_run_opencode("),
)
check_true(
    "initial_command_state_precedes_model",
    run_sample_source.index("_initial_command_loop_state(")
    < run_sample_source.index("_run_opencode("),
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
    project_root=Path("/tmp/p"), location="Foo.java:1", evidence="oracle_score=99", raw={},
)
args = argparse.Namespace(
    loop_mode="verify-failure",
    loop_max=2,
    loop_no_progress_limit=1,
    loop_on="smell,compile,test",
    loop_instruction="Repair from the latest failure pack",
    sample_deadline=1800,
    allow_test_changes=False,
)
prompt_plain = R._task_prompt(sample, args, "project_full")
check_true("prompt_has_base", "Repair this one java smell" in prompt_plain)
check("prompt_excludes_raw_dataset_evidence", "oracle_score=99" in prompt_plain, False)
roundtrip = parse_command_policy(R._command_arguments(prompt_plain, args, "project_full"))
check("command_roundtrip_instruction", roundtrip.loop.instruction, args.loop_instruction)
check_true("command_roundtrip_task", "Repair this one java smell" in roundtrip.task)
check_true("prompt_freezes_test_policy", "Test changes: forbidden" in prompt_plain)
initial_controller_state = R._initial_command_loop_state(
    sample,
    args,
    "project_full",
    started_at_ms=123456,
)
check("initial_controller_state_schema", initial_controller_state["schema_version"], 3)
check("initial_controller_state_started_at", initial_controller_state["started_at"], 123456)
check(
    "initial_controller_state_identity",
    initial_controller_state["policy"]["identity"]["location"],
    sample.location,
)
check(
    "initial_controller_state_task_redacted",
    initial_controller_state["policy"]["task"],
    "Continue the current smell refactoring task.",
)

print("== zero-verify state handoff integration ==")
with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    project = temp / "project"
    artifacts = temp / "artifacts"
    project.mkdir()
    artifacts.mkdir()
    fake = temp / "fake-opencode-state"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, sys
if os.environ.get("SMELL_BATCH_RUN") != "1":
    raise SystemExit(20)
continued = "--session" in sys.argv
if continued:
    raw = os.environ.get("SMELL_COMMAND_LOOP_STATE_JSON", "")
    if not raw:
        raise SystemExit(21)
    state = json.loads(raw)
    identity = state.get("policy", {}).get("identity", {})
    if state.get("schema_version") != 3 or identity.get("location") != "Foo.java:1":
        raise SystemExit(22)
print(json.dumps({"type": "message", "sessionID": "ses_zero_verify"}))
""",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    handoff_sample = Sample(
        sample_id="state",
        language="java",
        smell="long_method",
        project_name="p",
        project_root=project,
        location="Foo.java:1",
        evidence="",
        raw={},
    )
    handoff_args = argparse.Namespace(
        opencode_bin=str(fake),
        model="minimax/MiniMax-M2.7",
        opencode_api_key="",
        opencode_api_key_env="",
        opencode_auth_json="disabled",
        opencode_base_url="",
        projects="",
        sample_deadline=60,
        allow_test_changes=False,
        loop_mode="verify-failure",
        loop_max=2,
        loop_no_progress_limit=1,
        loop_on="smell,compile,test",
        loop_instruction="repair narrowly",
    )
    handoff_state = R._initial_command_loop_state(
        handoff_sample,
        handoff_args,
        "project_full",
        started_at_ms=123456,
    )
    first_rc, first_session = R._run_opencode(
        handoff_sample,
        artifacts,
        handoff_args,
        "java-refactor-agent",
        "project_full",
        command_loop_state=handoff_state,
        hard_timeout_seconds=5,
    )
    check("zero_verify_first_process_rc", first_rc, 0)
    check("zero_verify_first_process_session", first_session, "ses_zero_verify")
    second_rc, second_session = R._run_opencode(
        handoff_sample,
        artifacts,
        handoff_args,
        "java-refactor-agent",
        "project_full",
        session_id=first_session,
        continuation_prompt=R._runner_continuation_prompt(
            "verify_required", 0, 2, "repair narrowly"
        ),
        command_loop_state=handoff_state,
        attempt_suffix=".continue-1",
        hard_timeout_seconds=5,
    )
    check("zero_verify_second_process_receives_state", second_rc, 0)
    check("zero_verify_second_process_session", second_session, "ses_zero_verify")

allowed_args = argparse.Namespace(**{**vars(args), "allow_test_changes": True})
allowed_prompt = R._task_prompt(sample, allowed_args, "project_full")
allowed_roundtrip = parse_command_policy(R._command_arguments(allowed_prompt, allowed_args, "project_full"))
check_true("prompt_allows_sha_audited_tests", "explicitly allowed" in allowed_prompt and "SHA-audited" in allowed_prompt)
check("command_roundtrip_allows_test_changes", allowed_roundtrip.allow_test_changes, True)

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
refused_prompt = R._task_prompt(refused, args, "project_full")
check("runner_prompt_has_no_smell_protocol", "Refused Bequest structural protocol:" in refused_prompt, False)
refused_skill = (
    ROOT
    / ".opencode"
    / "skills"
    / "java-smell-edit-patterns"
    / "references"
    / "edit-patterns"
    / "refused_bequest.md"
).read_text(encoding="utf-8")
check_true("refused_skill_maps_callers", "production callers" in refused_skill)
check_true("refused_skill_rejects_relocation", "Never relocate an empty, throwing, null-returning" in refused_skill)
check_true(
    "refused_skill_source_derived_route",
    "Choose the narrowest correct route: implement real behavior, delegate" in refused_skill,
)
check_true(
    "refused_skill_requires_impact_ledger",
    "Use ordinary source read/search tools to build a small capability matrix" in refused_skill
    and "separate planning-tool phase" in refused_skill,
)
check_true(
    "refused_skill_batches_compile_closure",
    "complete diagnostic set into one" in refused_skill
    and "repair all related sites in one pass" in refused_skill,
)
check_true(
    "refused_skill_avoids_broad_alias_and_downcasts",
    "making it extend every new narrow capability" in refused_skill
    and "scatter downcasts" in refused_skill,
)

feature_envy_skill = (
    ROOT
    / ".opencode"
    / "skills"
    / "java-smell-edit-patterns"
    / "references"
    / "edit-patterns"
    / "feature_envy.md"
).read_text(encoding="utf-8")
check_true(
    "feature_envy_skill_closes_receiver_operation",
    "at most one semantically named receiver operation" in feature_envy_skill
    and "field and its aliases before `smell_verify`" in feature_envy_skill,
)
check_true(
    "feature_envy_skill_preserves_ordered_effects",
    "outgoing write, callback, or notification" in feature_envy_skill
    and "must not collapse multiple effects into one result" in feature_envy_skill,
)
check_true(
    "feature_envy_skill_rejects_accessor_gaming",
    "one trivial getter, setter, or method-reference wrapper" in feature_envy_skill
    and "bulk snapshot" in feature_envy_skill,
)
check_true(
    "feature_envy_skill_uses_architecture_boundary_and_frozen_test_policy",
    "extract-collaboration-workflow-preserve-receiver-api" in feature_envy_skill
    and "ordered application workflow" in feature_envy_skill
    and "external or stable protocol/port" in feature_envy_skill
    and "same-source-class fallback" in feature_envy_skill
    and "controller-frozen `allow_test_changes`" in feature_envy_skill
    and "frozen `project_full`" in feature_envy_skill
    and all(
        term not in feature_envy_skill.lower()
        for term in ("mock", "spy", "test double", "test-visible", "wanted but not invoked")
    ),
)
data_clumps_skill = (
    ROOT
    / ".opencode"
    / "skills"
    / "java-smell-edit-patterns"
    / "references"
    / "edit-patterns"
    / "data_clumps.md"
).read_text(encoding="utf-8")
check_true(
    "data_clumps_skill_uses_declaration_budget",
    "projected occurrences = N - migrated old declarations" in data_clumps_skill
    and "call expressions are compile-repair sites" in data_clumps_skill,
)
check_true(
    "data_clumps_skill_requires_real_holder",
    "holder owns matching" in data_clumps_skill
    and "empty or generic holder" in data_clumps_skill,
)
check_true(
    "data_clumps_skill_has_no_adapter_exemption",
    "including deprecated one-statement delegates" in data_clumps_skill
    and "Guard exemption" in data_clumps_skill
    and "remove the old" in data_clumps_skill,
)
check_true(
    "data_clumps_skill_rejects_parameter_rename_bypass",
    "freezes the baseline declaration owners and the original group's" in data_clumps_skill
    and "parameter slots and types" in data_clumps_skill
    and "legacy_type_signature_group_remains" in data_clumps_skill,
)
check_true(
    "data_clumps_skill_rejects_inline_copy_bypass",
    "inlined_body_window_expanded" in data_clumps_skill
    and "restore one shared implementation" in data_clumps_skill,
)
check_true(
    "data_clumps_skill_separates_semantic_components",
    "semantic connected component" in data_clumps_skill
    and "Matching parameter" in data_clumps_skill
    and "names/types alone is not an edge" in data_clumps_skill
    and "cross-domain bag" in data_clumps_skill,
)

edit_patterns = (
    ROOT
    / ".opencode"
    / "skills"
    / "java-smell-edit-patterns"
    / "references"
    / "edit-patterns"
)
lpl_skill = (edit_patterns / "long_parameter_list.md").read_text(encoding="utf-8")
check_true(
    "lpl_skill_covers_complete_signature_shapes",
    "Complete-signature migration protocol" in lpl_skill
    and "constructor-parameters-to-value-object" in lpl_skill
    and "instance-operation-parameters-to-request-object" in lpl_skill
    and "override-family-parameters-to-request-object" in lpl_skill,
)
check_true(
    "lpl_skill_removes_old_signature_without_fallback",
    "delete the old long signature as one source-level transaction" in lpl_skill
    and "do not restore the old" in lpl_skill
    and "signature as a delegate" in lpl_skill
    and "preserve an old abstract root as a fallback" in lpl_skill,
)

god_class_skill = (edit_patterns / "god_class.md").read_text(encoding="utf-8")
check_true(
    "god_class_skill_completes_profile_in_cohesive_stages",
    "Profile-closure protocol" in god_class_skill
    and "combined removal is projected to make the" in god_class_skill
    and "complete target Guard profile false" in god_class_skill
    and "If verification returns `IMPROVED`" in god_class_skill
    and "next cohesive cluster" in god_class_skill,
)

long_method_skill = (edit_patterns / "long_method.md").read_text(encoding="utf-8")
check_true(
    "long_method_skill_has_ncss_fast_path",
    "AST-NCSS fast-path closure" in long_method_skill
    and "smallest cohesive set" in long_method_skill
    and "`smell_verify` once" in long_method_skill,
)
switch_skill = (edit_patterns / "switch_statements.md").read_text(encoding="utf-8")
check_true(
    "switch_skill_has_zero_switch_fast_path",
    "Zero-switch fast-path closure" in switch_skill
    and "`switch_count == 0`" in switch_skill
    and "call `smell_verify` once" in switch_skill,
)

residual_contracts = {
    "feature_envy": ("returned", "residual finding identities as the exact next worklist"),
    "data_clumps": ("returned declaration", "families to cross the target Guard boundary"),
    "mysterious_name": ("complete residual", "set as the next exact worklist"),
    "nested_complexity": ("returned residual", "deficit to choose the next hotspot"),
    "code_clone_type1": ("returned endpoints as the exact next worklist",),
    "refused_bequest": ("returned rejection set as the residual closure",),
    "dead_code": ("residual", "declaration is returned, delete that exact entity"),
}
for smell_name, required_texts in residual_contracts.items():
    skill_text = (edit_patterns / f"{smell_name}.md").read_text(encoding="utf-8")
    check_true(
        f"{smell_name}_skill_has_exact_residual_closure",
        all(required_text in skill_text for required_text in required_texts),
    )

index_text = (edit_patterns / "index.md").read_text(encoding="utf-8")
check_true(
    "edit_pattern_index_matches_expanded_routes",
    "[`code_clone_type1.md`](code_clone_type1.md) | 7" in index_text
    and "[`feature_envy.md`](feature_envy.md) | 5" in index_text
    and "[`long_parameter_list.md`](long_parameter_list.md) | 4" in index_text,
)
noidea_skill_text = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / ".opencode" / "skills" / "java-smell-edit-patterns").rglob("*.md"))
)
check_true(
    "noidea_skill_has_no_idea_operation_fallback",
    "idea_edit" not in noidea_skill_text
    and "idea-refactor-cli" not in noidea_skill_text
    and "IDEA-enhanced agent" not in noidea_skill_text
    and "`direct:edit`" in noidea_skill_text,
)

check(
    "legacy_idea_agent_removed",
    (ROOT / ".opencode" / "agents" / "java-refactor-agent-idea.md").exists(),
    False,
)
check(
    "legacy_idea_command_removed",
    (ROOT / ".opencode" / "commands" / "java-refactor-run-idea.md").exists(),
    False,
)
print()
if failures:
    print(f"FAILED: {len(failures)} checks")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
