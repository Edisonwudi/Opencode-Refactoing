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
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as R  # noqa: E402
from smell_core.loop_policy import (  # noqa: E402
    parse_command_policy,
    parse_command_task_identity,
)
from smell_core.guards import _effective_command_timeout  # noqa: E402

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
check("both_fail", R._compute_status(1, 1, make_payload("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED")), "OPENCODE_FAILED")
check("opencode_fail_verify_pass", R._compute_status(1, 0, make_payload("PASS")), "OPENCODE_FAILED")
check(
    "opencode_timeout_verify_pass_fails_closed",
    R._compute_status(124, 0, make_payload("PASS")),
    "OPENCODE_TIMEOUT",
)
check("opencode_timeout_verify_fail", R._compute_status(124, 1, make_payload("SMELL_GUARD_FAILED")), "OPENCODE_TIMEOUT")
check("improved_not_accepted", R._compute_status(0, 1, make_payload("IMPROVED")), "IMPROVED")
check("timeout_overrides_improved", R._compute_status(124, 1, make_payload("IMPROVED")), "OPENCODE_TIMEOUT")
check("improved_status_not_accepted", R._is_accepted_status("IMPROVED"), False)
check(
    "provider_quota_cannot_be_overridden_by_final_pass",
    R._compute_status(R.OPENCODE_FATAL_PROVIDER_RETURN_CODE, 0, make_payload("PASS")),
    "PROVIDER_QUOTA_FAILED",
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

    data_clumps = Path(tmp) / "data-clumps.csv"
    data_clumps.write_text(
        "sample_id,language,smell_type,project_name,project_path,location,target_context_json\n"
        + '1,python,data_clumps,p,/tmp/p,src/a.py:method=a|line=1,"{}"\n',
        encoding="utf-8",
    )
    try:
        R._load_samples(data_clumps)
    except ValueError as exc:
        check_true("data_clumps_group_required", "target_context_json.group" in str(exc))
    else:
        failures.append("data_clumps_group_required: invalid row was accepted")
    explicit_context = json.dumps({"group": "int:start|int:end|int:retry"}).replace('"', '""')
    data_clumps.write_text(
        "sample_id,language,smell_type,project_name,project_path,location,target_context_json\n"
        + "1,python,data_clumps,p,/tmp/p,"
        + '"src/a.py:method=a|line=1;src/b.py:method=b|line=1",'
        + f'"{explicit_context}"\n',
        encoding="utf-8",
    )
    try:
        R._load_samples(data_clumps)
    except ValueError as exc:
        check_true("data_clumps_three_locations_required", "at least three" in str(exc))
    else:
        failures.append("data_clumps_three_locations_required: invalid row was accepted")

    mysterious = Path(tmp) / "mysterious-name.csv"
    mysterious.write_text(
        "sample_id,language,smell_type,project_name,project_path,location,target_context_json\n"
        + '1,python,mysterious_name,p,/tmp/p,src/a.py:method=a|line=1,"{}"\n',
        encoding="utf-8",
    )
    try:
        R._load_samples(mysterious)
    except ValueError as exc:
        check_true(
            "mysterious_name_selector_required",
            "target_context_json.symbol_kind" in str(exc),
        )
    else:
        failures.append("mysterious_name_selector_required: invalid row was accepted")

print("== single time budget ==")
check("opencode_timeout_is_exact_policy_budget", R._opencode_timeout_seconds(1800), 1800)
check_true(
    "opencode_timeout_terminates_nested_process_groups",
    "_terminate_process_tree(proc)" in inspect.getsource(R._run_opencode),
)
check_true(
    "runner_final_timeout_terminates_nested_process_groups",
    "_terminate_process_tree(proc)" in inspect.getsource(R._run),
)
check_true(
    "runner_timeout_drain_is_bounded",
    "proc.communicate()" not in inspect.getsource(R._run)
    and "communicate(timeout=" in inspect.getsource(R._run),
)


class _NeverReapedProcess:
    pid = 987654321
    returncode = None

    def __init__(self) -> None:
        self.wait_timeouts: list[float | None] = []
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired("never-reaped", timeout)

    def terminate(self):
        pass

    def kill(self):
        self.killed = True


never_reaped = _NeverReapedProcess()
original_process_tree_groups = R._process_tree_groups
R._process_tree_groups = lambda _pid: []
try:
    shutdown_started = time.monotonic()
    shutdown_evidence = R._terminate_process_tree(
        never_reaped,
        term_timeout=0.01,
        kill_timeout=0.01,
    )
finally:
    R._process_tree_groups = original_process_tree_groups
check_true("termination_waits_are_bounded", all(value is not None for value in never_reaped.wait_timeouts))
check_true("termination_returns_promptly_when_unreapable", time.monotonic() - shutdown_started < 1)
check("termination_evidence_is_bounded", shutdown_evidence["bounded"], True)
check("termination_evidence_reports_unreaped", shutdown_evidence["process_reaped"], False)
check_true("termination_evidence_has_phase_durations", "term_wait_ms" in shutdown_evidence and "kill_wait_ms" in shutdown_evidence)
check("pass_is_accepted", R._is_accepted_status("PASS"), True)
check("removed_timeout_pass_is_not_accepted", R._is_accepted_status("PASS_AFTER_OPENCODE_TIMEOUT"), False)
check("opencode_failure_not_accepted", R._is_accepted_status("OPENCODE_FAILED"), False)
check(
    "nested_build_honors_expired_sample_deadline",
    _effective_command_timeout(
        600,
        {R.SAMPLE_DEADLINE_EPOCH_MS_ENV: str(int((time.time() - 1) * 1000))},
    ),
    0.0,
)
parser = R.build_parser()
parsed = parser.parse_args(["--dataset", "/tmp/input.csv", "--sample-deadline", "2400"])
check("sample_deadline_public_entry", parsed.sample_deadline, 2400)
check(
    "model_event_inactivity_timeout_default",
    getattr(parsed, "model_event_inactivity_timeout", None),
    300,
)
check("verification_mode_cli_default_is_unset", parsed.verification_mode, None)
check(
    "project_full_is_effective_default",
    R._effective_verification_mode(
        loaded[0],
        argparse.Namespace(verification_mode=None, allow_test_changes=False),
    ),
    "project_full",
)
check("test_changes_default_forbidden", parsed.allow_test_changes, False)
check("direct_backend_is_default", parsed.refactoring_backend, "direct")
check(
    "idea_backend_is_explicit",
    parser.parse_args(["--dataset", "/tmp/input.csv", "--refactoring-backend", "idea"]).refactoring_backend,
    "idea",
)

with tempfile.TemporaryDirectory() as tmp:
    timeout_root = Path(tmp)
    wrapper = timeout_root / "nested-timeout.py"
    child_pid_path = timeout_root / "child.pid"
    child_ready_path = timeout_root / "child.ready"
    child_term_path = timeout_root / "child.terminated"
    child_source = """
import os
import signal
import time
from pathlib import Path

ready = Path(os.environ["SELF_CHECK_CHILD_READY"])
terminated = Path(os.environ["SELF_CHECK_CHILD_TERMINATED"])

def stop(_signum, _frame):
    terminated.write_text("terminated", encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
ready.write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
"""
    wrapper.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", os.environ["SELF_CHECK_CHILD_SOURCE"]],
    env=os.environ.copy(),
    start_new_session=True,
)
Path(os.environ["SELF_CHECK_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
while not Path(os.environ["SELF_CHECK_CHILD_READY"]).is_file():
    time.sleep(0.01)
child.wait()
""",
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)
    timeout_env = {
        **os.environ,
        "SELF_CHECK_CHILD_SOURCE": child_source,
        "SELF_CHECK_CHILD_PID": str(child_pid_path),
        "SELF_CHECK_CHILD_READY": str(child_ready_path),
        "SELF_CHECK_CHILD_TERMINATED": str(child_term_path),
    }
    try:
        try:
            R._run([str(wrapper)], timeout_root, env=timeout_env, timeout=1)
        except subprocess.TimeoutExpired:
            pass
        else:
            failures.append("nested_process_group_timeout: command did not time out")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not child_term_path.is_file():
            time.sleep(0.02)
        check_true("nested_process_group_received_structured_termination", child_term_path.is_file())
    finally:
        if child_pid_path.is_file():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
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
resolved = parse_command_policy('--verification-mode=sample_optimized --max-smell-verify-cycles=10 --loop-on=smell,test --loop-instruction="Use the pack" -- Project root: /tmp/p')
check("policy_mode", resolved.verification_mode, "sample_optimized")
check("policy_max_smell_verify_cycles", resolved.loop.max_smell_verify_cycles, 10)
check("policy_groups", resolved.loop.allowed_failure_groups, ("smell", "test"))
check("policy_instruction", resolved.loop.instruction, "Use the pack")
quoted_argv = parse_command_policy("--max-smell-verify-cycles=10 '--loop-instruction=修复最新 failure pack 并保持行为不变' -- task")
check("policy_instruction_quoted_argv", quoted_argv.loop.instruction, "修复最新 failure pack 并保持行为不变")
try:
    parse_command_policy("--loop-max=10 -- task")
except ValueError as exc:
    check_true("legacy_loop_max_rejected", "unrecognized arguments" in str(exc))
else:
    raise AssertionError("legacy --loop-max unexpectedly remained accepted")
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
            "--verification-mode=sample_optimized --max-smell-verify-cycles=2 -- "
            "Project root: /abs/java-project; Smell type: long_method; "
            "Target location: src/main/java/Foo.java:42; "
            "Build command: ./mvnw -q package; "
            "Project test command: ./mvnw -q test; Verification cwd: module-a"
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
    check(
        "readme_bridge_build_command",
        readme_bridge_payload["identity"]["build_command"],
        "./mvnw -q package",
    )
    check(
        "readme_bridge_project_test_command",
        readme_bridge_payload["identity"]["project_test_command"],
        "./mvnw -q test",
    )
    check(
        "readme_bridge_verification_source",
        readme_bridge_payload["identity"]["verification_command_source"],
        "command",
    )
    initial_state = readme_bridge_payload["command_loop_state"]
    check("readme_bridge_state_schema", initial_state["schema_version"], 7)
    check(
        "readme_bridge_state_identity",
        initial_state["policy"]["identity"]["project_root"],
        "/abs/java-project",
    )
    check("readme_bridge_state_continuations", initial_state["smell_verify_cycle_count"], 0)
    check("readme_bridge_state_target_context", initial_state["target_identity_context"], "")
    check("readme_bridge_state_best_metric", initial_state["best_metric_deficit"], None)
    check("readme_bridge_state_best_structural", initial_state["best_structural_failure_count"], None)
    check("readme_bridge_state_terminal", initial_state["terminal_receipt"], None)
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
structured_verification_identity = parse_command_task_identity(
    "\n".join(
        [
            "Project root: /tmp/p",
            "Language: java",
            "Smell type: feature_envy",
            "Target location: src/Foo.java:10",
            "Build command: ./mvnw -q -DskipTests package && echo built",
            "Project test command: ./mvnw -q test; echo tested",
            "Verification cwd: module-a",
            "Sample test command: ./mvnw -q -Dtest=FocusedTest test",
        ]
    ),
    verification_mode="project_full",
)
check(
    "structured_verification_build_command",
    structured_verification_identity.build_command,
    "./mvnw -q -DskipTests package && echo built",
)
check(
    "structured_verification_project_test_command",
    structured_verification_identity.project_test_command,
    "./mvnw -q test; echo tested",
)
check("structured_verification_cwd", structured_verification_identity.verification_cwd, "module-a")
check(
    "structured_verification_source",
    structured_verification_identity.verification_command_source,
    "command",
)
check(
    "structured_sample_test_source",
    structured_verification_identity.sample_test_source,
    "command",
)
interactive_defaults_identity = parse_command_task_identity(
    "\n".join(
        [
            "Project root: /tmp/p",
            "Smell type: feature_envy",
            "Target location: src/Foo.java:10",
            "Build command: ./mvnw package",
            "Project test command: ./mvnw test",
            "Verification cwd: module-a",
            "Sample test command: ./mvnw focusedTest",
        ]
    ),
    verification_mode="project_full",
    defaults={
        "build_command": "false",
        "project_test_command": "false",
        "verification_cwd": "forged",
        "verification_command_source": "dataset",
        "sample_test_command": "false",
        "sample_test_source": "cli",
    },
)
check(
    "interactive_task_build_overrides_incomplete_env",
    interactive_defaults_identity.build_command,
    "./mvnw package",
)
check(
    "interactive_task_test_overrides_incomplete_env",
    interactive_defaults_identity.project_test_command,
    "./mvnw test",
)
check(
    "interactive_task_cwd_overrides_incomplete_env",
    interactive_defaults_identity.verification_cwd,
    "module-a",
)
check(
    "interactive_task_verification_source_is_command",
    interactive_defaults_identity.verification_command_source,
    "command",
)
check(
    "interactive_task_sample_overrides_incomplete_env",
    interactive_defaults_identity.sample_test_command,
    "./mvnw focusedTest",
)
check(
    "interactive_task_sample_source_is_command",
    interactive_defaults_identity.sample_test_source,
    "command",
)
controller_owned_identity = parse_command_task_identity(
    (
        "Project root: /tmp/model; Smell type: long_method; Target location: Model.java:1; "
        "Build command: false; Project test command: false; Verification cwd: forged"
    ),
    verification_mode="project_full",
    defaults={
        "project_root": "/tmp/controller",
        "smell": "long_method",
        "location": "Controller.java:1",
        "build_command": "./gradlew assemble",
        "project_test_command": "./gradlew test",
        "verification_cwd": "module-b",
        "verification_command_source": "dataset",
        "sample_test_command": "./gradlew focusedTest",
        "sample_test_source": "dataset",
    },
)
check("controller_owned_build_command", controller_owned_identity.build_command, "./gradlew assemble")
check("controller_owned_project_test_command", controller_owned_identity.project_test_command, "./gradlew test")
check("controller_owned_verification_cwd", controller_owned_identity.verification_cwd, "module-b")
check("controller_owned_verification_source", controller_owned_identity.verification_command_source, "dataset")
empty_controller_identity = parse_command_task_identity(
    (
        "Project root: /tmp/model; Smell type: long_method; Target location: Model.java:1; "
        "Build command: false; Project test command: false; Verification cwd: forged; "
        "Sample test command: false"
    ),
    verification_mode="project_full",
    defaults={
        "project_root": "/tmp/controller",
        "smell": "long_method",
        "location": "Controller.java:1",
        "build_command": "",
        "project_test_command": "",
        "verification_cwd": "",
        "verification_command_source": "",
        "sample_test_command": "",
        "sample_test_source": "",
    },
)
check("controller_empty_build_blocks_task_fallback", empty_controller_identity.build_command, "")
check("controller_empty_test_blocks_task_fallback", empty_controller_identity.project_test_command, "")
check("controller_empty_cwd_blocks_task_fallback", empty_controller_identity.verification_cwd, "")
check("controller_empty_sample_test_blocks_task_fallback", empty_controller_identity.sample_test_command, "")
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
for name, defaults, expected_prefix in [
    (
        "identity_build_requires_project_test",
        {"build_command": "./mvnw package"},
        "EXPLICIT_VERIFICATION_COMMAND_PAIR_REQUIRED:",
    ),
    (
        "identity_verification_cwd_requires_pair",
        {"verification_cwd": "module-a"},
        "EXPLICIT_VERIFICATION_COMMAND_PAIR_REQUIRED:",
    ),
    (
        "identity_verification_source_requires_pair",
        {"verification_command_source": "dataset"},
        "VERIFICATION_COMMAND_SOURCE_WITHOUT_COMMANDS:",
    ),
    (
        "identity_sample_source_requires_command",
        {"sample_test_source": "dataset"},
        "SAMPLE_TEST_SOURCE_WITHOUT_COMMAND:",
    ),
]:
    try:
        parse_command_task_identity(
            "Project root: /tmp/p; Smell type: long_method; Target location: Foo.java:1",
            verification_mode="project_full",
            defaults=defaults,
        )
        failures.append(f"{name}: expected ValueError")
    except ValueError as exc:
        check_true(name, str(exc).startswith(expected_prefix))
disabled = parse_command_policy('--max-smell-verify-cycles=0 -- Project root: /tmp/p')
check("zero_disables", disabled.loop.mode, "off")
for name, raw in [
    ("missing_delimiter", "--max-smell-verify-cycles=2 task"),
    ("bad_max", "--max-smell-verify-cycles=11 -- task"),
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


def command_state(
    generation: int,
    decision: str,
    instruction: str,
    termination_reason: str = "",
    *,
    terminal_receipt: dict | None = None,
) -> dict:
    candidate_identity = {
        "baseline_revision": "base-revision",
        "baseline_tree": "base-tree",
        "production_diff": "production-diff",
        "test_tree": "test-tree",
        "verification_config_tree": "verification-config-tree",
    }
    if terminal_receipt is not None and "formalVerificationReceipt" not in terminal_receipt:
        terminal_receipt = {
            **terminal_receipt,
            "formalVerificationReceipt": (
                {
                    "schema_version": "smell.formal-verification-receipt/v1",
                    "terminal_stage": "formal_verify",
                    "status": "PASS",
                    "success": True,
                    "accepted": True,
                    "resolution": "resolved",
                    "candidate_identity": candidate_identity,
                    "outcome": "pass",
                    "diagnostic_signature": "PASS",
                    "guard": {"success": True, "failure_count": 0, "artifact_ref": "/tmp/guard.json"},
                    "build_test": {
                        "success": True,
                        "reason": "",
                        "project_full_executed": True,
                        "build_status": "ok",
                        "test_status": "ok",
                        "sample_test_status": "",
                    },
                    "fresh_isolation": {
                        "contract_version": "project-full-fresh-worktree/v1",
                        "mode": "detached_git_worktree",
                        "success": True,
                        "stage": "completed",
                        "cleanup_success": True,
                    },
                    "artifact_refs": {
                        "guard_evidence": "/tmp/guard.json",
                        "build_result": "/tmp/build.json",
                        "test_result": "/tmp/test.json",
                        "diff": "/tmp/diff.patch",
                    },
                }
                if terminal_receipt.get("stage") == "formal_verify"
                and terminal_receipt.get("status") == "PASS"
                and terminal_receipt.get("accepted") is True
                else None
            ),
        }
    if terminal_receipt is not None and "ideaProtocolReceipt" not in terminal_receipt:
        terminal_receipt = {**terminal_receipt, "ideaProtocolReceipt": None}
    formal_receipt = (
        terminal_receipt.get("formalVerificationReceipt")
        if isinstance(terminal_receipt, dict)
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
                "language": "python",
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
        "control": {
            "generation": generation,
            "decision": decision,
            "instruction": instruction,
            "termination_reason": termination_reason,
        },
        "smell_verify_cycle_count": 0,
        "no_progress_count": 0,
        "last_failure_fingerprint": "",
        "best_metric_deficit": None,
        "best_structural_failure_count": None,
        "last_blocker_codes": [],
        "seen_structural_states": [],
        "formal_candidate_state": {
            "candidate_identity": (
                formal_receipt.get("candidate_identity")
                if isinstance(formal_receipt, dict)
                else None
            ),
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
        "terminal_receipt": terminal_receipt,
    }


def formal_pass_payload(loop: dict, state: dict) -> dict:
    receipt = state["terminal_receipt"]["formalVerificationReceipt"]
    artifacts = dict(receipt["artifact_refs"])
    return {
        "schema_version": "smell.verify.decision/v1",
        "success": True,
        "accepted": True,
        "progress": True,
        "status": "PASS",
        "resolution": "resolved",
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
        "termination_reason": "PASS",
        "loop": loop,
    }


initial_control_state = command_state(
    0,
    "verify_required",
    "Call smell_verify now using the frozen command identity.",
)
truncated_v7_state = {
    "schema_version": 7,
    "control": dict(initial_control_state["control"]),
}
check(
    "truncated_v7_state_is_not_transportable",
    R._runner_transport_plan(
        R._verification_trace('{"type":"text","part":{"text":"done"}}'),
        previous_state=truncated_v7_state,
        transported_generations=set(),
    )["action"],
    "stop",
)
idea_terminal_state = command_state(
    1,
    "stop",
    "",
    "PASS",
    terminal_receipt={
        "stage": "formal_verify",
        "status": "PASS",
        "success": True,
        "accepted": True,
        "resolution": "resolved",
        "terminationReason": "PASS",
        "failureCategory": "",
        "failureGroup": "",
        "loop": {
            "generation": 1,
            "decision": "stop",
            "instruction": "",
            "termination_reason": "PASS",
        },
    },
)
idea_terminal_state["policy"]["refactoring_backend"] = "idea"
idea_terminal_state["policy"]["identity"]["language"] = "java"
idea_terminal_state["idea_protocol_state"].update(
    {
        "mutation_generation": 1,
        "verified_generation": 1,
        "mutation_route": "native_apply",
        "mutation_proposal_id": "proposal-1",
    }
)
idea_terminal_state["terminal_receipt"]["ideaProtocolReceipt"] = {
    "schema_version": "smell.idea-protocol-receipt/v1",
    "mutation_generation": 1,
    "verified_generation": 1,
    "mutation_route": "native_apply",
    "proposal_id": "proposal-1",
    "blocker_status": "",
    "blocker_codes": [],
    "complete": True,
}
check(
    "idea_terminal_receipt_is_transferable",
    R._typed_command_control(idea_terminal_state)["decision"],
    "stop",
)
missing_idea_receipt = json.loads(json.dumps(idea_terminal_state))
missing_idea_receipt["terminal_receipt"]["ideaProtocolReceipt"] = None
check(
    "idea_terminal_receipt_is_required",
    R._typed_command_control(missing_idea_receipt),
    None,
)
mismatched_idea_receipt = json.loads(json.dumps(idea_terminal_state))
mismatched_idea_receipt["terminal_receipt"]["ideaProtocolReceipt"][
    "verified_generation"
] = 0
check(
    "idea_terminal_receipt_matches_state",
    R._typed_command_control(mismatched_idea_receipt),
    None,
)
zero_verify_plan = R._runner_transport_plan(
    R._verification_trace('{"type":"text","part":{"text":"done"}}'),
    previous_state=initial_control_state,
    transported_generations=set(),
)
check("zero_verify_typed_control_transports_once", zero_verify_plan["action"], "verify_required")
check("zero_verify_generation", zero_verify_plan["generation"], 0)
check(
    "zero_verify_same_generation_not_repeated",
    R._runner_transport_plan(
        R._verification_trace('{"type":"text","part":{"text":"done"}}'),
        previous_state=initial_control_state,
        transported_generations={0},
    )["action"],
    "stop",
)

continue_loop = {
    "generation": 1,
    "decision": "continue",
    "instruction": "Repair the typed blocker and call smell_verify again.",
    "termination_reason": "",
}
continue_state = command_state(1, "continue", continue_loop["instruction"])
typed_continue_trace = R._verification_trace(
    verify_event(
        {"status": "SMELL_GUARD_FAILED", "loop": continue_loop},
        metadata={"loop": continue_loop, "command_loop_state": continue_state},
    )
)
typed_continue_plan = R._runner_transport_plan(
    typed_continue_trace,
    previous_state=initial_control_state,
    transported_generations={0},
)
check("typed_continue_transports", typed_continue_plan["action"], "continue")
check("typed_continue_generation_increments", typed_continue_plan["generation"], 1)
check(
    "typed_continue_uses_plugin_instruction",
    R._runner_continuation_prompt(typed_continue_plan),
    continue_loop["instruction"],
)

second_continue_loop = {
    **continue_loop,
    "generation": 2,
    "instruction": "Apply the next typed repair and call smell_verify again.",
}
second_continue_state = command_state(2, "continue", second_continue_loop["instruction"])
multi_verify_trace = R._verification_trace(
    verify_event(
        {"status": "SMELL_GUARD_FAILED", "loop": continue_loop},
        metadata={"loop": continue_loop, "command_loop_state": continue_state},
    )
    + "\n"
    + verify_event(
        {"status": "SMELL_GUARD_FAILED", "loop": second_continue_loop},
        metadata={"loop": second_continue_loop, "command_loop_state": second_continue_state},
    )
)
multi_verify_plan = R._runner_transport_plan(
    multi_verify_trace,
    previous_state=initial_control_state,
    transported_generations={0},
)
check("same_turn_control_chain_is_validated", multi_verify_plan["action"], "continue")
check("same_turn_control_chain_reaches_latest_generation", multi_verify_plan["generation"], 2)

duplicate_generation_trace = R._verification_trace(
    verify_event(
        {"status": "SMELL_GUARD_FAILED", "loop": continue_loop},
        metadata={"loop": continue_loop, "command_loop_state": continue_state},
    )
    + "\n"
    + verify_event(
        {"status": "SMELL_GUARD_FAILED", "loop": continue_loop},
        metadata={"loop": continue_loop, "command_loop_state": continue_state},
    )
)
check(
    "same_turn_duplicate_generation_fails_closed",
    R._runner_transport_plan(
        duplicate_generation_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    )["action"],
    "stop",
)

missing_state_trace = R._verification_trace(
    verify_event({"status": "SMELL_GUARD_FAILED", "loop": continue_loop})
)
check(
    "continue_missing_state_fails_closed",
    R._runner_transport_plan(
        missing_state_trace,
        previous_state=initial_control_state,
        transported_generations={0},
    )["action"],
    "stop",
)
contradictory_state = command_state(1, "stop", "", "NO_PROGRESS")
check(
    "continue_state_payload_contradiction_fails_closed",
    R._runner_transport_plan(
        R._verification_trace(
            verify_event(
                {"status": "SMELL_GUARD_FAILED", "loop": continue_loop},
                metadata={"loop": continue_loop, "command_loop_state": contradictory_state},
            )
        ),
        previous_state=initial_control_state,
        transported_generations={0},
    )["action"],
    "stop",
)
old_generation_loop = {**continue_loop, "generation": 0}
old_generation_state = command_state(0, "continue", continue_loop["instruction"])
check(
    "continue_old_generation_fails_closed",
    R._runner_transport_plan(
        R._verification_trace(
            verify_event(
                {"status": "SMELL_GUARD_FAILED", "loop": old_generation_loop},
                metadata={"loop": old_generation_loop, "command_loop_state": old_generation_state},
            )
        ),
        previous_state=initial_control_state,
        transported_generations={0},
    )["action"],
    "stop",
)

formal_pass_loop = {
    "generation": 1,
    "decision": "stop",
    "instruction": "",
    "termination_reason": "PASS",
}
formal_pass_receipt = {
    "stage": "formal_verify",
    "status": "PASS",
    "success": True,
    "accepted": True,
    "resolution": "resolved",
    "terminationReason": "PASS",
    "failureCategory": "",
    "failureGroup": "",
    "loop": formal_pass_loop,
}
formal_pass_state = command_state(
    1,
    "stop",
    "",
    "PASS",
    terminal_receipt=formal_pass_receipt,
)
formal_pass_trace = R._verification_trace(
    verify_event(
        formal_pass_payload(formal_pass_loop, formal_pass_state),
        metadata={"loop": formal_pass_loop, "command_loop_state": formal_pass_state},
    )
)
formal_pass_trace["runner_control_plan"] = R._runner_transport_plan(
    formal_pass_trace,
    previous_state=initial_control_state,
    transported_generations=set(),
)
formal_authorization = R._runner_terminal_authorization(formal_pass_trace, 0)
check("formal_pass_terminal_authorizes_confirmation", formal_authorization["authorized"], True)

for authorization_name, state_mutation, returncode in (
    (
        "cheap_pass_does_not_authorize",
        {**formal_pass_state, "terminal_receipt": {**formal_pass_receipt, "stage": "cheap_guard"}},
        0,
    ),
    (
        "protocol_pass_does_not_authorize",
        {**formal_pass_state, "terminal_receipt": {**formal_pass_receipt, "stage": "protocol"}},
        0,
    ),
    (
        "formal_improved_does_not_authorize",
        {
            **formal_pass_state,
            "terminal_receipt": {
                **formal_pass_receipt,
                "status": "IMPROVED",
                "accepted": False,
                "terminationReason": "IMPROVED",
                "loop": {**formal_pass_loop, "termination_reason": "IMPROVED"},
            },
            "control": {**formal_pass_state["control"], "termination_reason": "IMPROVED"},
        },
        0,
    ),
    ("runtime_abort_does_not_authorize", formal_pass_state, 124),
):
    mutation_trace = R._verification_trace(
        verify_event(
            {
                "success": True,
                "accepted": True,
                "status": "PASS",
                "resolution": "resolved",
                "loop": state_mutation["control"],
            },
            metadata={"loop": state_mutation["control"], "command_loop_state": state_mutation},
        )
    )
    mutation_trace["runner_control_plan"] = R._runner_transport_plan(
        mutation_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    )
    check(
        authorization_name,
        R._runner_terminal_authorization(mutation_trace, returncode)["authorized"],
        False,
    )

continue_not_closed_authorization = R._runner_terminal_authorization(typed_continue_trace, 0)
check("continue_not_closed_does_not_authorize", continue_not_closed_authorization["authorized"], False)
check(
    "zero_verify_does_not_authorize",
    R._runner_terminal_authorization(
        R._verification_trace('{"type":"text","part":{"text":"done"}}'),
        0,
    )["authorized"],
    False,
)

minimal_fresh_pass = {
    "schema_version": "smell.verify.decision/v1",
    "success": True,
    "accepted": True,
    "status": "PASS",
    "resolution": "resolved",
    "termination_reason": "PASS",
}
invalid_fresh_payload, invalid_fresh_audit = R._apply_runner_confirmation(
    0,
    minimal_fresh_pass,
    formal_authorization,
)
check("minimal_fresh_pass_is_rejected", invalid_fresh_payload["accepted"], False)
check(
    "minimal_fresh_pass_protocol_reason",
    invalid_fresh_audit["reason"],
    "FRESH_VERIFY_PROTOCOL_INVALID",
)
fresh_pass = formal_pass_payload(formal_pass_loop, formal_pass_state)
confirmed_payload, confirmed_audit = R._apply_runner_confirmation(
    0,
    fresh_pass,
    formal_authorization,
)
check("authorized_fresh_pass_stays_pass", confirmed_payload["status"], "PASS")
check("authorized_fresh_pass_is_accepted", confirmed_payload["accepted"], True)
check("authorized_fresh_pass_audit", confirmed_audit["reason"], "FORMAL_PASS_CONFIRMED")

blocked_payload, blocked_audit = R._apply_runner_confirmation(
    0,
    fresh_pass,
    continue_not_closed_authorization,
)
check("fresh_pass_cannot_promote_unclosed_continue", blocked_payload["accepted"], False)
check("fresh_pass_unclosed_status", blocked_payload["status"], "RUNNER_CONFIRMATION_NOT_AUTHORIZED")
check("fresh_pass_raw_observation_is_audited", blocked_audit["raw_observation"]["status"], "PASS")

empty_trace = R._verification_trace('{"type":"text","part":{"text":"done"}}')
check("trace_empty_calls", empty_trace["smell_verify_calls"], 0)
check(
    "missing_loop_decision_fails_closed",
    R._runner_closure_action(
        empty_trace,
        previous_state=None,
        transported_generations=set(),
    ),
    "stop",
)
guard_progress_payload = {
    "schema_version": "smell.guard-progress/v1",
    "success": False,
    "status": "GUARD_PROGRESS_REQUIRED",
    "applicable": True,
    "checkpoint_required": True,
    "source_guard_passed": False,
    "ready_for_project_full": False,
    "project_full_executed": False,
    "metric_budget": {
        "current": 55,
        "passing_max": 50,
        "required_reduction": 5,
    },
    "next_action": "Reduce the target method below the Guard threshold.",
}
guard_progress_trace = R._verification_trace(verify_event(
    guard_progress_payload,
    metadata={
        "command_loop_state": {
            "schema_version": 1,
            "smell_verify_cycle_count": 0,
            "no_progress_count": 0,
            "last_failure_fingerprint": "",
        },
    },
))
check(
    "guard_progress_without_loop_decision_fails_closed",
    R._runner_closure_action(
        guard_progress_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
guard_progress_continues = dict(guard_progress_payload)
guard_progress_continues["loop"] = {"decision": "continue"}
check(
    "guard_progress_with_untyped_continue_fails_closed",
    R._runner_closure_action(
        R._verification_trace(verify_event(guard_progress_continues)),
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
guard_progress_stopped = dict(guard_progress_payload)
guard_progress_stopped["loop"] = {
    "decision": "stop",
    "termination_reason": "GUARD_PROGRESS_NO_PROGRESS",
}
guard_progress_stopped_trace = R._verification_trace(verify_event(
    guard_progress_stopped,
    metadata={"loop": guard_progress_stopped["loop"]},
))
check(
    "guard_progress_no_progress_reason_is_audited",
    guard_progress_stopped_trace["last_loop_termination_reason"],
    "GUARD_PROGRESS_NO_PROGRESS",
)
check(
    "guard_progress_no_progress_stops_same_session",
    R._runner_closure_action(
        guard_progress_stopped_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
continue_trace = R._verification_trace(verify_event({"status": "SMELL_GUARD_FAILED", "loop": {"decision": "continue"}}))
check("trace_verify_calls", continue_trace["smell_verify_calls"], 1)
check("trace_decision", continue_trace["last_loop_decision"], "continue")
check(
    "untyped_continue_is_not_transportable",
    R._runner_closure_action(
        continue_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
persisted_state = {
    "schema_version": 1,
    "smell_verify_cycle_count": 2,
}
state_trace = R._verification_trace(verify_event(
    {"status": "SMELL_GUARD_FAILED", "loop": {"decision": "continue"}},
    metadata={
        "loop": {"decision": "continue"},
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
    R._runner_closure_action(
        pass_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
check("trace_keeps_last_payload", pass_trace["last_payload"]["status"], "PASS")
check("trace_no_tools_after_final_verify", pass_trace["tools_after_last_verify"], 0)
read_only_after_verify = R._verification_trace(
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}})
    + "\n"
    + json.dumps({
        "type": "tool_use",
        "part": {
            "tool": "todowrite",
            "state": {"status": "completed", "output": "done"},
        },
    })
)
check("trace_counts_read_only_after_verify", read_only_after_verify["tools_after_last_verify"], 1)
check(
    "trace_names_read_only_after_verify",
    read_only_after_verify["completed_tools_after_last_verify"],
    ["todowrite"],
)
check("runner_has_guarded_agent_receipt_reuse_path", hasattr(R, "_agent_project_full_receipt"), True)
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
check(
    "trace_names_tools_after_verify",
    edited_after_verify["completed_tools_after_last_verify"],
    ["edit"],
)
check(
    "trace_counts_tool_attempts_after_verify",
    edited_after_verify["tool_attempts_after_last_verify"],
    1,
)
failed_post_verify_tool = json.dumps({
    "type": "tool_use",
    "part": {
        "tool": "edit",
        "state": {"status": "error", "output": "partially changed"},
    },
})
failed_edit_after_verify = R._verification_trace(
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}})
    + "\n"
    + failed_post_verify_tool
)
check(
    "trace_does_not_count_failed_tool_as_completed",
    failed_edit_after_verify["tools_after_last_verify"],
    0,
)
check(
    "trace_counts_failed_tool_attempt_after_verify",
    failed_edit_after_verify["tool_attempts_after_last_verify"],
    1,
)
failed_verify_after_pass = R._verification_trace(
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}})
    + "\n"
    + json.dumps({
        "type": "tool_use",
        "part": {
            "tool": "smell_verify",
            "state": {"status": "error", "output": "bridge unavailable"},
        },
    })
)
check(
    "new_failed_verify_invalidates_older_pass_receipt",
    failed_verify_after_pass["last_output_parsed"],
    False,
)
check(
    "new_failed_verify_clears_older_stop_decision",
    failed_verify_after_pass["last_loop_decision"],
    "",
)
idea_protocol_events = "\n".join([
    json.dumps({"type": "tool_use", "part": {"tool": "idea_refactor_preview", "state": {"status": "completed", "output": "{}"}}}),
    json.dumps({"type": "tool_use", "part": {"tool": "idea_refactor_apply", "state": {"status": "completed", "output": "{}"}}}),
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}}),
])
idea_trace = R._verification_trace(idea_protocol_events)
idea_contract = R._idea_protocol_contract([{**idea_trace, "last_payload": None}])
check("idea_trace_preview_calls", idea_trace["idea_refactor_preview_calls"], 1)
check("idea_trace_apply_calls", idea_trace["idea_refactor_apply_calls"], 1)
check("idea_protocol_contract_passes", idea_contract["success"], True)
check("idea_protocol_contract_sequence", idea_contract["tool_sequence"], ["idea_refactor_preview", "idea_refactor_apply", "smell_verify"])
idea_bypass_trace = R._verification_trace("\n".join([
    json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "/usr/local/bin/idea-refactor locate --project-root /tmp/p"}, "output": "{}"}}}),
    verify_event({"success": True, "status": "PASS", "loop": {"decision": "stop"}}),
]))
idea_bypass_contract = R._idea_protocol_contract([{**idea_bypass_trace, "last_payload": None}])
check("idea_trace_detects_direct_cli", idea_bypass_trace["direct_idea_cli_calls"], 1)
check("idea_bypass_contract_fails", idea_bypass_contract["success"], False)
check_true("idea_bypass_contract_reason", "DIRECT_IDEA_CLI_USED" in idea_bypass_contract["violations"])
run_sample_source = inspect.getsource(R._run_sample)
check_true(
    "idea_trace_contract_is_audit_only",
    '"authority": "audit_only"' in run_sample_source,
)
check_true(
    "idea_trace_contract_cannot_override_typed_status",
    'final_status = "IDEA_PROTOCOL_FAILED"' not in run_sample_source
    and 'idea_protocol["success"]' not in run_sample_source,
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
    "loop": {"decision": "stop", "termination_reason": "MAX_SMELL_VERIFY_CYCLES_REACHED"},
}
cap_trace = R._verification_trace(verify_event(cap_payload))
check(
    "runner_does_not_reinterpret_plugin_stop",
    R._runner_closure_action(
        cap_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    ),
    "stop",
)
compile_cap_payload = {
    "status": "BUILD_FAILED",
    "failure_pack": {"retryable": True},
    "checkpoint": {"delta": {"metric_progress": False}},
    "snapshot": {"diff_stat": {"stdout": " Foo.java | 2 +-\n 1 file changed"}},
    "loop": {"decision": "stop", "termination_reason": "MAX_SMELL_VERIFY_CYCLES_REACHED"},
}
compile_cap_trace = R._verification_trace(verify_event(compile_cap_payload))
check(
    "runner_does_not_reinterpret_compile_stop",
    R._runner_closure_action(
        compile_cap_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
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
        previous_state=initial_control_state,
        transported_generations=set(),
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
        R._runner_closure_action(
            trace,
            previous_state=initial_control_state,
            transported_generations=set(),
        ),
        "stop",
    )
continue_resume_prompt = R._runner_continuation_prompt(typed_continue_plan)
check("continue_prompt_is_exact_control_instruction", continue_resume_prompt, continue_loop["instruction"])
check_true(
    "continue_prompt_does_not_duplicate_mutable_details",
    "SECRET" not in continue_resume_prompt
    and "BUILD_FAILED" not in continue_resume_prompt
    and "test API migrations" not in continue_resume_prompt
    and "guard-progress" not in continue_resume_prompt
    and "verify-required" not in continue_resume_prompt
    and "narrow corrective edit" not in continue_resume_prompt,
)

control_reason, verification_reason, compatible_reason = R._termination_reasons(
    {"termination_reason": "FINAL_BUILD_FAILED"},
    {"last_loop_termination_reason": "GUARD_PROGRESS_NO_PROGRESS"},
    "",
)
check("control_reason_is_preserved_separately", control_reason, "GUARD_PROGRESS_NO_PROGRESS")
check("verification_reason_is_preserved_separately", verification_reason, "FINAL_BUILD_FAILED")
check("verification_reason_wins_compatible_field", compatible_reason, "FINAL_BUILD_FAILED")
control_only, verification_empty, compatible_control = R._termination_reasons(
    {},
    {"last_loop_termination_reason": "NO_PROGRESS"},
    "",
)
check("control_only_reason", control_only, "NO_PROGRESS")
check("empty_verification_reason", verification_empty, "")
check("compatible_reason_falls_back_to_control", compatible_control, "NO_PROGRESS")
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
            build_command="mvn -DskipTests package",
            project_test_command="mvn test",
            verification_cwd=".",
            verification_command_source="dataset",
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
        check_true("baseline_keeps_build_command", "--build-command" in baseline_cmd)
        check_true(
            "baseline_keeps_project_test_command",
            "--project-test-command" in baseline_cmd,
        )
        check_true("baseline_keeps_verification_cwd", "--verification-cwd" in baseline_cmd)
        check_true(
            "baseline_keeps_verification_source",
            "--verification-command-source" in baseline_cmd,
        )
        check_true("baseline_keeps_sample_test_source", "--sample-test-source" in baseline_cmd)
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

    baseline_spawn = {"started": False}

    def forbidden_baseline_run(*_args, **_kwargs):
        baseline_spawn["started"] = True
        raise AssertionError("baseline capture must not start after the sample deadline")

    expired_baseline_dir = Path(tmp) / "expired-baseline"
    expired_baseline_dir.mkdir()
    R._run = forbidden_baseline_run
    try:
        baseline_timeout_rc, baseline_timeout_payload = R._run_capture_baseline(
            sample,
            expired_baseline_dir,
            argparse.Namespace(projects="", sample_deadline=1, allow_test_changes=True),
            "project_full",
            deadline_monotonic=time.monotonic() - 1,
        )
    finally:
        R._run = original_run
    check("expired_baseline_returncode", baseline_timeout_rc, 124)
    check("expired_baseline_status", baseline_timeout_payload["status"], "OPENCODE_TIMEOUT")
    check(
        "expired_baseline_schema",
        baseline_timeout_payload["schema_version"],
        "smell.verify.decision/v1",
    )
    check("expired_baseline_child_not_spawned", baseline_spawn["started"], False)
    baseline_timeout_artifact = json.loads(
        (expired_baseline_dir / "baseline-capture.json").read_text(encoding="utf-8")
    )
    check("expired_baseline_artifact_returncode", baseline_timeout_artifact["returncode"], 124)
    check(
        "expired_baseline_artifact_status",
        baseline_timeout_artifact["payload"]["status"],
        "OPENCODE_TIMEOUT",
    )

    final_spawn = {"started": False}

    def forbidden_final_run(*_args, **_kwargs):
        final_spawn["started"] = True
        raise AssertionError("final verification must not start after the sample deadline")

    R._run = forbidden_final_run
    try:
        timeout_rc, timeout_payload = R._run_verify(
            sample,
            Path(tmp),
            argparse.Namespace(projects="", sample_deadline=1, allow_test_changes=True),
            "project_full",
            baseline_seal="controller-seal",
            deadline_monotonic=time.monotonic() - 1,
        )
    finally:
        R._run = original_run
    check("expired_final_verify_returncode", timeout_rc, 124)
    check("expired_final_verify_status", timeout_payload["status"], "OPENCODE_TIMEOUT")
    check(
        "expired_final_verify_schema",
        timeout_payload["schema_version"],
        "smell.verify.decision/v1",
    )
    check("expired_final_verify_category", timeout_payload["failure_pack"]["failure_category"], "OPENCODE_TIMEOUT")
    check("expired_final_verify_project_full_not_started", timeout_payload["project_full_executed"], False)
    check("expired_final_verify_child_not_spawned", final_spawn["started"], False)
    final_cmd = captured["cmd"]
    final_env = captured["env"]
    check("final_verify_excludes_smell_evidence", "--smell-evidence" in final_cmd, False)
    check_true("final_verify_keeps_build_command", "--build-command" in final_cmd)
    check_true(
        "final_verify_keeps_project_test_command",
        "--project-test-command" in final_cmd,
    )
    check_true("final_verify_keeps_verification_cwd", "--verification-cwd" in final_cmd)
    check_true(
        "final_verify_keeps_verification_source",
        "--verification-command-source" in final_cmd,
    )
    check_true("final_verify_keeps_sample_test_source", "--sample-test-source" in final_cmd)
    check_true(
        "final_verify_requests_compact_decision",
        "--output-detail" in final_cmd
        and final_cmd[final_cmd.index("--output-detail") + 1] == "decision",
    )
    check_true("final_verify_keeps_target_context", "--target-context-json" in final_cmd)
    check_true("final_verify_uses_controller_seal", "--baseline-seal" in final_cmd and "controller-seal" in final_cmd)
    check("final_verify_remains_standard_decision", "--guard-progress-only" in final_cmd, False)
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

print("== baseline deadline terminal artifacts ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    run_dir = root / "run"
    run_dir.mkdir()
    timeout_sample = R.Sample(
        sample_id="baseline-timeout",
        language="java",
        smell="data_clumps",
        project_name="p",
        project_root=root,
        location="src/Foo.java:method=target",
        evidence="",
        raw={},
        verification_mode="project_full",
    )
    timeout_args = argparse.Namespace(
        project_revisions="unused.json",
        worktree=False,
        agent="",
        verification_mode="project_full",
        allow_test_changes=True,
        refactoring_backend="direct",
        sample_deadline=0,
        projects="",
    )
    originals = {
        "load_revisions": R.load_revisions,
        "resolve_revision": R.resolve_revision,
        "assert_commit_present": R.assert_commit_present,
        "audit_test_commit": R.audit_test_commit,
        "verify_test_oracle": R.verify_test_oracle,
    }
    R.load_revisions = lambda _path: {}
    R.resolve_revision = lambda _project, _revisions, _path: argparse.Namespace(
        project_commit="frozen-commit"
    )
    R.assert_commit_present = lambda _root, _commit: None
    R.audit_test_commit = lambda *_args, **_kwargs: {}
    R.verify_test_oracle = lambda *_args, **_kwargs: {}
    try:
        timeout_row = R._run_sample(timeout_sample, run_dir, timeout_args)
    finally:
        for name, value in originals.items():
            setattr(R, name, value)
    timeout_result = json.loads(
        (Path(timeout_row["sample_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    timeout_verify = json.loads(
        (Path(timeout_row["sample_dir"]) / "verify.json").read_text(encoding="utf-8")
    )
    results_path = run_dir / "results.csv"
    R._append_result(results_path, timeout_row)
    with results_path.open(encoding="utf-8", newline="") as handle:
        timeout_csv = next(R.csv.DictReader(handle))
    check("baseline_timeout_row_status", timeout_row["status"], "OPENCODE_TIMEOUT")
    check("baseline_timeout_result_status", timeout_result["status"], "OPENCODE_TIMEOUT")
    check("baseline_timeout_verify_status", timeout_verify["status"], "OPENCODE_TIMEOUT")
    check("baseline_timeout_csv_status", timeout_csv["status"], "OPENCODE_TIMEOUT")
    check("baseline_timeout_result_accepted", timeout_result["accepted"], False)
    check("baseline_timeout_verify_schema", timeout_verify["schema_version"], "smell.verify.decision/v1")

print("== inactivity timeout terminal artifacts ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    run_dir = root / "run"
    run_dir.mkdir()
    inactivity_sample = R.Sample(
        sample_id="inactivity-timeout",
        language="python",
        smell="long_method",
        project_name="p",
        project_root=root,
        location="src/a.py:method=target|line=1",
        evidence="",
        raw={},
        verification_mode="project_full",
    )
    inactivity_args = argparse.Namespace(
        project_revisions="unused.json",
        worktree=False,
        agent="",
        verification_mode="project_full",
        allow_test_changes=False,
        refactoring_backend="direct",
        sample_deadline=60,
        model_event_inactivity_timeout=1,
        projects="",
        loop_mode="verify-failure",
        max_smell_verify_cycles=2,
        loop_no_progress_limit=1,
        loop_on="smell,compile,test",
        loop_instruction="repair narrowly",
    )
    originals = {
        "load_revisions": R.load_revisions,
        "resolve_revision": R.resolve_revision,
        "assert_commit_present": R.assert_commit_present,
        "audit_test_commit": R.audit_test_commit,
        "verify_test_oracle": R.verify_test_oracle,
        "_bootstrap_opencode": R._bootstrap_opencode,
        "_run_opencode": R._run_opencode,
        "_runner_final_verify": R._runner_final_verify,
    }
    R.load_revisions = lambda _path: {}
    R.resolve_revision = lambda _project, _revisions, _path: argparse.Namespace(
        project_commit="frozen-commit"
    )
    R.assert_commit_present = lambda _root, _commit: None
    R.audit_test_commit = lambda *_args, **_kwargs: {}
    R.verify_test_oracle = lambda *_args, **_kwargs: {}
    R._bootstrap_opencode = lambda *_args, **_kwargs: None
    R._run_opencode = lambda *_args, **_kwargs: (
        124,
        "ses_inactivity",
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )
    R._runner_final_verify = lambda *_args, **_kwargs: (
        0,
        {
            "schema_version": "smell.verify.decision/v1",
            **make_payload("PASS", project_full_executed=True),
        },
        {"schema_version": R.RUNNER_FINAL_RECEIPT_SCHEMA, "reused": False},
    )
    try:
        inactivity_row = R._run_sample(inactivity_sample, run_dir, inactivity_args)
    finally:
        for name, value in originals.items():
            setattr(R, name, value)
    inactivity_result = json.loads(
        (Path(inactivity_row["sample_dir"]) / "result.json").read_text(
            encoding="utf-8"
        )
    )
    inactivity_verify = json.loads(
        (Path(inactivity_row["sample_dir"]) / "verify.json").read_text(
            encoding="utf-8"
        )
    )
    inactivity_results_path = run_dir / "results.csv"
    R._append_result(inactivity_results_path, inactivity_row)
    with inactivity_results_path.open(encoding="utf-8", newline="") as handle:
        inactivity_csv = next(R.csv.DictReader(handle))
    check("inactivity_timeout_row_status", inactivity_row["status"], "OPENCODE_TIMEOUT")
    check("inactivity_timeout_result_status", inactivity_result["status"], "OPENCODE_TIMEOUT")
    check("inactivity_timeout_verify_status", inactivity_verify["status"], "OPENCODE_TIMEOUT")
    check("inactivity_timeout_csv_status", inactivity_csv["status"], "OPENCODE_TIMEOUT")
    check(
        "inactivity_timeout_reason_persisted",
        inactivity_verify["termination_reason"],
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )
    check(
        "inactivity_timeout_scope_persisted",
        inactivity_verify["timeout"]["scope"],
        "model-event-inactivity",
    )
    check(
        "inactivity_control_reason_is_separate",
        inactivity_result["control_termination_reason"],
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )
    check(
        "inactivity_verification_reason_is_separate",
        inactivity_result["verification_termination_reason"],
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )
    check(
        "inactivity_csv_preserves_control_reason",
        inactivity_csv["control_termination_reason"],
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )

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
check(
    "baseline_deadline_status_is_exact",
    R._baseline_failure_status(124, {"success": False, "status": "OPENCODE_TIMEOUT"}),
    "OPENCODE_TIMEOUT",
)

run_sample_source = inspect.getsource(R._run_sample)
check(
    "run_sample_has_one_final_verify_selection_call",
    run_sample_source.count("_runner_final_verify("),
    1,
)
check_true(
    "baseline_capture_precedes_model",
    run_sample_source.index("_run_capture_baseline(") < run_sample_source.index("_run_opencode("),
)
check_true(
    "initial_command_state_precedes_model",
    run_sample_source.index("_initial_command_loop_state(")
    < run_sample_source.index("_run_opencode("),
)
check(
    "idea_readiness_is_not_runner_authority",
    "_prepare_idea_service(" in run_sample_source,
    False,
)
check(
    "idea_readiness_precheck_is_plugin_owned",
    hasattr(R, "_prepare_idea_service"),
    False,
)
check_true(
    "idea_close_follows_final_verify",
    run_sample_source.index("_runner_final_verify(")
    < run_sample_source.index("_close_idea_project("),
)
check_true(
    "typed_control_is_the_only_runner_transport",
    "_runner_transport_plan(" in run_sample_source
    and "transported_control_generations.add(generation)" in run_sample_source
    and 'action == "guard_progress"' not in run_sample_source
    and "max_smell_verify_cycles" not in inspect.getsource(R._runner_transport_plan),
)
check_true(
    "one_absolute_deadline_precedes_baseline",
    run_sample_source.index("sample_deadline_monotonic =")
    < run_sample_source.index("_run_capture_baseline("),
)
check(
    "one_absolute_deadline_covers_baseline_and_final",
    run_sample_source.count("deadline_monotonic=sample_deadline_monotonic"),
    2,
)
check_true(
    "timeout_pass_is_normalized_before_persistence",
    "_normalize_sample_timeout(" in run_sample_source,
)
normalized_timeout_rc, normalized_timeout_payload = R._normalize_sample_timeout(
    124,
    0,
    make_payload("PASS"),
    1800,
)
check("normalized_timeout_returncode", normalized_timeout_rc, 124)
check("normalized_timeout_status", normalized_timeout_payload["status"], "OPENCODE_TIMEOUT")
check(
    "normalized_timeout_observes_rejected_pass",
    normalized_timeout_payload["timeout"]["final_verify_observation"]["status"],
    "PASS",
)
for failure_name, failure_rc, expected_status in (
    ("provider_quota", R.OPENCODE_FATAL_PROVIDER_RETURN_CODE, "PROVIDER_QUOTA_FAILED"),
    ("ordinary_nonzero", 7, "OPENCODE_FAILED"),
):
    normalized_failure_rc, normalized_failure_payload = R._normalize_opencode_failure(
        failure_rc,
        0,
        make_payload("PASS", project_full_executed=True),
    )
    check(f"normalized_{failure_name}_verify_returncode", normalized_failure_rc, 0)
    check(f"normalized_{failure_name}_status", normalized_failure_payload["status"], expected_status)
    check(
        f"normalized_{failure_name}_schema",
        normalized_failure_payload["schema_version"],
        "smell.verify.decision/v1",
    )
    check(
        f"normalized_{failure_name}_observes_rejected_pass",
        normalized_failure_payload["execution_failure"]["final_verify_observation"]["status"],
        "PASS",
    )
runner_help = R.build_parser().format_help()
check_true(
    "sample_deadline_help_has_no_per_phase_or_shutdown_grace",
    "per-phase" not in runner_help and "60-second" not in runner_help,
)
check_true(
    "command_metadata_has_no_shutdown_grace_budget",
    "opencode_shutdown_grace_seconds" not in inspect.getsource(R._run_opencode),
)
sanitized_service = R._sanitize_idea_service_payload({
    "status": "ok",
    "token": "top-level-secret",
    "server": {"token": "nested-secret", "tokenSummary": "safe-summary", "status": "running"},
})
check("idea_preflight_removes_top_level_token", "token" in sanitized_service, False)
check("idea_preflight_removes_server_token", "token" in sanitized_service["server"], False)
check("idea_preflight_keeps_safe_summary", sanitized_service["server"]["tokenSummary"], "safe-summary")

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
generation = 2 if continued else 1
instruction = "" if continued else "Repair the typed blocker and call smell_verify again."
termination_reason = "PASS" if continued else ""
loop = {
    "generation": generation,
    "decision": decision,
    "instruction": instruction,
    "termination_reason": termination_reason,
}
terminal = None
if continued:
    formal_receipt = {
        "schema_version": "smell.formal-verification-receipt/v1",
        "terminal_stage": "formal_verify",
        "status": "PASS",
        "success": True,
        "accepted": True,
        "resolution": "resolved",
        "candidate_identity": {
            "baseline_revision": "base",
            "baseline_tree": "",
            "production_diff": "diff",
            "test_tree": "",
            "verification_config_tree": "",
        },
        "outcome": "pass",
        "diagnostic_signature": "PASS",
        "guard": {"success": True, "failure_count": 0, "artifact_ref": "/tmp/guard"},
        "build_test": {
            "success": True,
            "reason": "",
            "project_full_executed": True,
            "build_status": "ok",
            "test_status": "ok",
            "sample_test_status": "",
        },
        "fresh_isolation": {
            "contract_version": "project-full-fresh-worktree/v1",
            "mode": "detached_git_worktree",
            "success": True,
            "stage": "completed",
            "cleanup_success": True,
        },
        "artifact_refs": {
            "guard_evidence": "/tmp/guard",
            "build_result": "/tmp/build",
            "test_result": "/tmp/test",
            "diff": "/tmp/diff",
        },
    }
    terminal = {
        "stage": "formal_verify",
        "status": "PASS",
        "success": True,
        "accepted": True,
        "resolution": "resolved",
        "terminationReason": "PASS",
        "failureCategory": "",
        "failureGroup": "",
        "formalVerificationReceipt": formal_receipt,
        "ideaProtocolReceipt": None,
        "loop": loop,
    }
state = {
    "schema_version": 7,
    "policy": {
        "task": "Continue the current smell refactoring task.",
        "verification_mode": "project_full",
        "refactoring_backend": "direct",
        "allow_test_changes": False,
        "checkpoint_required": True,
        "identity": {
            "project_root": "/tmp/project", "smell": "long_method",
            "location": "sample.py:1", "verification_mode": "project_full",
            "project_override_root": "", "language": "python",
            "target_context_json": "", "sample_test_location": "",
            "sample_test_command": "", "build_command": "",
            "project_test_command": "", "verification_cwd": "",
            "verification_command_source": "", "sample_test_source": "",
        },
        "loop": {
            "mode": "verify-failure", "max_smell_verify_cycles": 5,
            "no_progress_limit": 1,
            "allowed_failure_groups": ["smell", "compile", "test"],
            "instruction": "repair narrowly", "sample_deadline_seconds": 1800,
        },
    },
    "target_identity_context": "",
    "started_at": 1,
    "control": loop,
    "smell_verify_cycle_count": 0,
    "no_progress_count": 0,
    "last_failure_fingerprint": "",
    "best_metric_deficit": None,
    "best_structural_failure_count": None,
    "last_blocker_codes": [],
    "seen_structural_states": [],
    "formal_candidate_state": {
        "candidate_identity": formal_receipt["candidate_identity"] if continued else None,
        "outcome": "pass" if continued else "",
        "diagnostic_signature": "PASS" if continued else "",
        "confirmation_required": False,
    },
    "idea_protocol_state": {
        "active_proposal": None, "proposal_blocker": None,
        "mutation_generation": 0, "verified_generation": 0,
        "mutation_route": "", "mutation_proposal_id": "",
        "revertible_apply_generation": None,
    },
    "terminal_receipt": terminal,
}
payload = {"success": continued, "accepted": continued, "status": status, "loop": loop}
event = {
    "type": "tool_use",
    "sessionID": "ses_fake",
    "part": {
        "tool": "smell_verify",
        "state": {
            "status": "completed",
            "output": json.dumps(payload),
            "metadata": {
                "loop": loop,
                "command_loop_state": state,
                "auto_continuation": {"status": status},
            },
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
    first_plan = R._runner_transport_plan(
        first_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    )
    first_action = first_plan["action"]
    check("fake_initial_rc", first.returncode, 0)
    check("fake_initial_sid", first_sid, "ses_fake")
    check("fake_initial_action", first_action, "continue")
    second = subprocess.run(
        R._opencode_run_command(fake_args, "java-refactor-agent", first_sid),
        input=R._runner_continuation_prompt(first_plan),
        text=True,
        capture_output=True,
        check=False,
    )
    second_trace = R._verification_trace(second.stdout)
    second_action = R._runner_closure_action(
        second_trace,
        previous_state=first_plan["state"],
        transported_generations={1},
    )
    check("fake_continuation_rc", second.returncode, 0)
    check("fake_continuation_status", second_trace["last_status"], "PASS")
    check("fake_continuation_action", second_action, "stop")

print("== fake CLI guard-progress missing-decision closure ==")
with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp) / "fake-opencode-guard-progress"
    fake.write_text(
        """#!/usr/bin/env python3
import json, sys
payload = {
        "schema_version": "smell.guard-progress/v1",
        "success": False,
        "status": "GUARD_PROGRESS_REQUIRED",
        "applicable": True,
        "checkpoint_required": True,
        "source_guard_passed": False,
        "ready_for_project_full": False,
        "project_full_executed": False,
        "metric_budget": {"current": 55, "passing_max": 50, "required_reduction": 5},
        "next_action": "Reduce the target method below the Guard threshold.",
}
event = {
    "type": "tool_use",
    "sessionID": "ses_guard_progress",
    "part": {
        "tool": "smell_verify",
        "state": {
            "status": "completed",
            "output": json.dumps(payload),
            "metadata": {
                "command_loop_state": {
                    "schema_version": 1,
                    "smell_verify_cycle_count": 0,
                    "no_progress_count": 0,
                    "last_failure_fingerprint": "",
                },
            },
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
        R._opencode_run_command(fake_args, "smell-refactor-agent"),
        input="initial command",
        text=True,
        capture_output=True,
        check=False,
    )
    first_sid = R._parse_session_id_from_json_events(first.stdout)
    first_trace = R._verification_trace(first.stdout)
    first_action = R._runner_closure_action(
        first_trace,
        previous_state=initial_control_state,
        transported_generations=set(),
    )
    check("guard_progress_fake_initial_sid", first_sid, "ses_guard_progress")
    check("guard_progress_fake_missing_decision_stops", first_action, "stop")

print("== _task_prompt ==")
# Build a minimal fake sample/args to call _task_prompt
from run_smell_dataset import Sample
sample = Sample(
    sample_id="1", language="java", smell="long_method", project_name="p",
    project_root=Path("/tmp/p"), location="Foo.java:1", evidence="oracle_score=99", raw={},
)
args = argparse.Namespace(
    loop_mode="verify-failure",
    max_smell_verify_cycles=2,
    loop_no_progress_limit=1,
    loop_on="smell,compile,test",
    loop_instruction="Repair from the latest failure pack",
    sample_deadline=1800,
    allow_test_changes=False,
    refactoring_backend="direct",
)
prompt_plain = R._task_prompt(sample)
check_true("prompt_has_base", "Repair this one java smell" in prompt_plain)
check("prompt_excludes_raw_dataset_evidence", "oracle_score=99" in prompt_plain, False)
roundtrip = parse_command_policy(R._command_arguments(prompt_plain, args, "project_full"))
check("command_roundtrip_instruction", roundtrip.loop.instruction, args.loop_instruction)
check_true("command_roundtrip_task", "Repair this one java smell" in roundtrip.task)
check("command_roundtrip_direct_backend", roundtrip.refactoring_backend, "direct")
idea_args = argparse.Namespace(**{**vars(args), "refactoring_backend": "idea"})
idea_roundtrip = parse_command_policy(
    R._command_arguments(prompt_plain, idea_args, "project_full")
)
check("command_roundtrip_idea_backend", idea_roundtrip.refactoring_backend, "idea")
check_true(
    "prompt_excludes_controller_policy",
    all(
        marker not in prompt_plain
        for marker in ("Refactoring backend:", "Verification mode:", "Test changes:")
    ),
)
grouped_sample = Sample(
    sample_id="grouped",
    language="c",
    smell="data_clumps",
    project_name="p",
    project_root=Path("/tmp/p"),
    location="src/a.c:method=alpha|line=10;src/b.c:method=beta|line=20",
    evidence="",
    raw={},
)
grouped_prompt = R._task_prompt(grouped_sample)
check_true("grouped_prompt_uses_target_locations", "listed target locations" in grouped_prompt)
check("grouped_prompt_avoids_java_method_wording", "target methods" in grouped_prompt, False)
initial_controller_state = R._initial_command_loop_state(
    sample,
    args,
    "project_full",
    started_at_ms=123456,
)
check("initial_controller_state_schema", initial_controller_state["schema_version"], 7)
check("initial_controller_state_started_at", initial_controller_state["started_at"], 123456)
check("initial_controller_state_target_context", initial_controller_state["target_identity_context"], "")
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
check("initial_controller_state_best_metric", initial_controller_state["best_metric_deficit"], None)
check("initial_controller_state_best_structural", initial_controller_state["best_structural_failure_count"], None)
check("initial_controller_state_terminal", initial_controller_state["terminal_receipt"], None)

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
if any(
    name in os.environ
    for name in (
        "SMELL_REFACTORING_BACKEND",
        "SMELL_ENABLE_IDEA_TOOLS",
        "SMELL_IDEA_PREPARED",
        "SMELL_IDEA_PROJECT_ROOT",
    )
):
    raise SystemExit(23)
if os.environ.get("SMELL_BUILD_COMMAND") != "./gradlew classes":
    raise SystemExit(24)
if os.environ.get("SMELL_PROJECT_TEST_COMMAND") != "./gradlew test":
    raise SystemExit(25)
if os.environ.get("SMELL_VERIFICATION_CWD") != ".":
    raise SystemExit(26)
if os.environ.get("SMELL_VERIFICATION_COMMAND_SOURCE") != "cli":
    raise SystemExit(27)
if os.environ.get("SMELL_SAMPLE_TEST_SOURCE") != "dataset":
    raise SystemExit(28)
continued = "--session" in sys.argv
if continued:
    raw = os.environ.get("SMELL_COMMAND_LOOP_STATE_JSON", "")
    if not raw:
        raise SystemExit(21)
    state = json.loads(raw)
    identity = state.get("policy", {}).get("identity", {})
    if state.get("schema_version") != 7 or identity.get("location") != "Foo.java:1":
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
        test_command="./gradlew focusedTest",
        build_command="./gradlew classes",
        project_test_command="./gradlew test",
        verification_cwd=".",
        verification_command_source="cli",
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
        refactoring_backend="idea",
        loop_mode="verify-failure",
        max_smell_verify_cycles=2,
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
    first_rc, first_session, first_termination_reason = R._run_opencode(
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
    check("zero_verify_first_process_not_terminated", first_termination_reason, "")
    first_manifest = json.loads(
        (artifacts / "message-manifest.json").read_text(encoding="utf-8")
    )
    first_command = json.loads(
        (artifacts / "command.json").read_text(encoding="utf-8")
    )
    check(
        "command_audits_shared_sample_deadline_scope",
        first_command["time_budget"]["scope"],
        "baseline-model-continuations-and-runner-final",
    )
    check(
        "command_audits_remaining_final_budget",
        first_command["time_budget"]["final_verify_budget"],
        "remaining-sample-budget",
    )
    check("command_audits_event_watchdog", first_command["time_budget"]["idle_watchdog_enabled"], True)
    check(
        "command_audits_verification_source",
        first_command["verification_commands"]["source"],
        "cli",
    )
    check(
        "command_audits_sample_test_source",
        first_command["verification_commands"]["sample_test_source"],
        "dataset",
    )
    check("initial_message_provenance", first_manifest["provenance"], "user_command")
    check(
        "initial_user_parts_not_mutated",
        first_manifest["user_parts_mutated_by_plugin"],
        False,
    )
    check_true(
        "initial_raw_user_input_audited",
        (artifacts / "raw-user-input.txt").is_file()
        and bool(first_manifest["raw_user_input"]["sha256"]),
    )
    zero_verify_transport = R._runner_transport_plan(
        R._verification_trace(
            (artifacts / "run.events.jsonl").read_text(encoding="utf-8")
        ),
        previous_state=handoff_state,
        transported_generations=set(),
    )
    check("zero_verify_integration_transport", zero_verify_transport["action"], "verify_required")
    second_rc, second_session, second_termination_reason = R._run_opencode(
        handoff_sample,
        artifacts,
        handoff_args,
        "java-refactor-agent",
        "project_full",
        session_id=first_session,
        continuation_prompt=R._runner_continuation_prompt(zero_verify_transport),
        command_loop_state=handoff_state,
        attempt_suffix=".continue-1",
        hard_timeout_seconds=5,
    )
    check("zero_verify_second_process_receives_state", second_rc, 0)
    check("zero_verify_second_process_session", second_session, "ses_zero_verify")
    check("zero_verify_second_process_not_terminated", second_termination_reason, "")
    second_manifest = json.loads(
        (artifacts / "message-manifest.json.continue-1").read_text(encoding="utf-8")
    )
    check("resume_message_provenance", second_manifest["provenance"], "controller_resume")
    check_true(
        "resume_context_excludes_mutable_fields",
        "loop_instruction"
        in json.loads(
            (artifacts / "controller-context.json.continue-1").read_text(
                encoding="utf-8"
            )
        )["excluded_mutable_fields"],
    )

print("== model event inactivity watchdog ==")
with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    project = temp / "project"
    project.mkdir()
    base_args = argparse.Namespace(
        **{
            **vars(handoff_args),
            "refactoring_backend": "direct",
            "model_event_inactivity_timeout": 1,
        }
    )

    silent = temp / "fake-opencode-silent"
    silent.write_text(
        """#!/usr/bin/env python3
import json
import time

print(json.dumps({"type": "message", "sessionID": "ses_silent"}), flush=True)
time.sleep(3)
""",
        encoding="utf-8",
    )
    os.chmod(silent, 0o755)
    silent_artifacts = temp / "silent-artifacts"
    silent_artifacts.mkdir()
    silent_rc, _, silent_reason = R._run_opencode(
        Sample(
            sample_id="silent",
            language="python",
            smell="long_method",
            project_name="p",
            project_root=project,
            location="a.py:method=f|line=1",
            evidence="",
            raw={},
        ),
        silent_artifacts,
        argparse.Namespace(**{**vars(base_args), "opencode_bin": str(silent)}),
        "smell-refactor-agent",
        "project_full",
        hard_timeout_seconds=5,
    )
    check("silent_model_is_terminated_early", silent_rc, 124)
    check("silent_model_termination_reason", silent_reason, "MODEL_EVENT_INACTIVITY_TIMEOUT")
    silent_termination = json.loads(
        (silent_artifacts / "opencode-termination.json").read_text(encoding="utf-8")
    )
    check(
        "silent_model_termination_artifact",
        silent_termination["termination_reason"],
        "MODEL_EVENT_INACTIVITY_TIMEOUT",
    )
    check("silent_shutdown_is_bounded", silent_termination["shutdown"]["bounded"], True)
    check_true(
        "silent_shutdown_has_phase_timing",
        "term_wait_ms" in silent_termination["shutdown"]
        and "kill_wait_ms" in silent_termination["shutdown"]
        and "stdout_drain_ms" in silent_termination["shutdown"],
    )

    periodic = temp / "fake-opencode-periodic"
    periodic.write_text(
        """#!/usr/bin/env python3
import json
import time

for index in range(6):
    print(json.dumps({"type": "message", "sessionID": "ses_periodic", "index": index}), flush=True)
    time.sleep(0.35)
""",
        encoding="utf-8",
    )
    os.chmod(periodic, 0o755)
    periodic_artifacts = temp / "periodic-artifacts"
    periodic_artifacts.mkdir()
    periodic_rc, _, periodic_reason = R._run_opencode(
        Sample(
            sample_id="periodic",
            language="python",
            smell="long_method",
            project_name="p",
            project_root=project,
            location="a.py:method=f|line=1",
            evidence="",
            raw={},
        ),
        periodic_artifacts,
        argparse.Namespace(**{**vars(base_args), "opencode_bin": str(periodic)}),
        "smell-refactor-agent",
        "project_full",
        hard_timeout_seconds=5,
    )
    check("periodic_model_events_keep_turn_alive", periodic_rc, 0)
    check("periodic_model_is_not_terminated", periodic_reason, "")

    active_tool = temp / "fake-opencode-active-tool"
    active_tool.write_text(
        """#!/usr/bin/env python3
import json
import subprocess
import sys

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
child.wait()
print(json.dumps({"type": "message", "sessionID": "ses_active_tool"}), flush=True)
""",
        encoding="utf-8",
    )
    os.chmod(active_tool, 0o755)
    tool_artifacts = temp / "tool-artifacts"
    tool_artifacts.mkdir()
    tool_rc, _, tool_reason = R._run_opencode(
        Sample(
            sample_id="active-tool",
            language="python",
            smell="long_method",
            project_name="p",
            project_root=project,
            location="a.py:method=f|line=1",
            evidence="",
            raw={},
        ),
        tool_artifacts,
        argparse.Namespace(**{**vars(base_args), "opencode_bin": str(active_tool)}),
        "smell-refactor-agent",
        "project_full",
        hard_timeout_seconds=5,
    )
    check("active_tool_process_suspends_inactivity_watchdog", tool_rc, 0)
    check("active_tool_process_is_not_terminated", tool_reason, "")

allowed_args = argparse.Namespace(**{**vars(args), "allow_test_changes": True})
allowed_roundtrip = parse_command_policy(R._command_arguments(prompt_plain, allowed_args, "project_full"))
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
refused_prompt = R._task_prompt(refused)
check("runner_prompt_has_no_smell_protocol", "Refused Bequest structural protocol:" in refused_prompt, False)


def primary_java_skill_reference(smell_name: str) -> Path:
    return (
        ROOT
        / ".opencode"
        / "skills"
        / f"smell-repair-{smell_name.replace('_', '-')}"
        / "references"
        / "java.md"
    )


refused_skill = primary_java_skill_reference("refused_bequest").read_text(encoding="utf-8")
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

feature_envy_skill = primary_java_skill_reference("feature_envy").read_text(encoding="utf-8")
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
data_clumps_skill = primary_java_skill_reference("data_clumps").read_text(encoding="utf-8")
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

lpl_skill = primary_java_skill_reference("long_parameter_list").read_text(encoding="utf-8")
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

god_class_skill = primary_java_skill_reference("god_class").read_text(encoding="utf-8")
check_true(
    "god_class_skill_completes_profile_in_cohesive_stages",
    "Profile-closure protocol" in god_class_skill
    and "combined removal is projected to make the" in god_class_skill
    and "complete target Guard profile false" in god_class_skill
    and "If verification returns `IMPROVED`" in god_class_skill
    and "next cohesive cluster" in god_class_skill,
)

long_method_skill = primary_java_skill_reference("long_method").read_text(encoding="utf-8")
check_true(
    "long_method_skill_has_ncss_fast_path",
    "AST-NCSS fast-path closure" in long_method_skill
    and "smallest cohesive set" in long_method_skill
    and "`smell_verify` once" in long_method_skill,
)
switch_skill = primary_java_skill_reference("switch_statements").read_text(encoding="utf-8")
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
    skill_text = primary_java_skill_reference(smell_name).read_text(encoding="utf-8")
    check_true(
        f"{smell_name}_skill_has_exact_residual_closure",
        all(required_text in skill_text for required_text in required_texts),
    )

java_smells = (
    "code_clone_type1",
    "data_clumps",
    "dead_code",
    "feature_envy",
    "god_class",
    "long_method",
    "long_parameter_list",
    "mysterious_name",
    "nested_complexity",
    "refused_bequest",
    "switch_statements",
)

noidea_skill_text = "\n".join(
    primary_java_skill_reference(smell_name).read_text(encoding="utf-8")
    for smell_name in java_smells
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
