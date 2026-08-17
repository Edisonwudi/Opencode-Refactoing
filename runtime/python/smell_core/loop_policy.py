from __future__ import annotations

import argparse
import math
import re
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .config import VERIFICATION_COMMAND_SOURCES
from .verification_receipt import validate_formal_verification_receipt


LOOP_MODES = {"off", "verify-failure"}
FAILURE_GROUPS = {"smell", "compile", "test"}
COMMAND_LOOP_STATE_VERSION = 7
INITIAL_VERIFY_INSTRUCTION = "Call smell_verify now using the frozen command identity."
CHECKPOINT_SMELLS = frozenset({
    "long_method",
    "nested_complexity",
    "long_parameter_list",
    "feature_envy",
    "data_clumps",
    "code_clone_type1",
    "god_class",
    "refused_bequest",
    "switch_statements",
    "mysterious_name",
    "dead_code",
})
REPAIRABLE_CATEGORY_GROUPS = {
    "SMELL_GUARD_FAILED": "smell",
    "FINAL_DIFF_GENERATED_ARTIFACTS": "compile",
    "BUILD_COMPILE_ERROR": "compile",
    "TEST_BEHAVIOR_REGRESSION": "test",
    "TEST_REFLECTION_ENTRY_STALE": "test",
    "SAMPLE_TEST_FAILED": "test",
}


@dataclass(frozen=True)
class LoopPolicy:
    mode: str = "verify-failure"
    max_smell_verify_cycles: int = 10
    no_progress_limit: int = 3
    allowed_failure_groups: tuple[str, ...] = ("smell", "compile", "test")
    instruction: str = (
        "Read the latest failure_pack, make one narrow corrective edit, and call "
        "smell_verify again. Respect the test-change policy frozen in c000."
    )
    sample_deadline_seconds: int = 1800

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_failure_groups"] = list(self.allowed_failure_groups)
        return payload

    def allows(self, failure_category: str) -> bool:
        if self.mode == "off" or self.max_smell_verify_cycles <= 0:
            return False
        group = REPAIRABLE_CATEGORY_GROUPS.get(str(failure_category or "").strip())
        return bool(group and group in self.allowed_failure_groups)


@dataclass(frozen=True)
class ResolvedCommandPolicy:
    task: str
    verification_mode: str
    refactoring_backend: str
    allow_test_changes: bool
    loop: LoopPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "verification_mode": self.verification_mode,
            "refactoring_backend": self.refactoring_backend,
            "allow_test_changes": self.allow_test_changes,
            "loop": self.loop.to_dict(),
        }


@dataclass(frozen=True)
class CommandTaskIdentity:
    project_root: str
    smell: str
    location: str
    verification_mode: str
    project_override_root: str = ""
    language: str = ""
    target_context_json: str = ""
    sample_test_location: str = ""
    sample_test_command: str = ""
    build_command: str = ""
    project_test_command: str = ""
    verification_cwd: str = ""
    verification_command_source: str = ""
    sample_test_source: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class _PolicyParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"INVALID_LOOP_POLICY: {message}")


def _split_policy_and_task(arguments: str) -> tuple[str, str]:
    raw = str(arguments or "").strip()
    marker = " -- "
    if marker not in raw:
        raise ValueError("INVALID_LOOP_POLICY: expected ' -- ' between policy options and task text")
    policy_text, task = raw.split(marker, 1)
    task = task.strip()
    if not task:
        raise ValueError("INVALID_LOOP_POLICY: task text after ' -- ' must not be empty")
    return policy_text.strip(), task


_COMMAND_TASK_FIELDS = {
    "project root": "project_root",
    "language": "language",
    "smell type": "smell",
    "target location": "location",
    "target context": "target_context_json",
    "verification mode": "verification_mode",
    "sample test location": "sample_test_location",
    "sample test command": "sample_test_command",
    "build command": "build_command",
    "project test command": "project_test_command",
    "verification cwd": "verification_cwd",
}
_COMMAND_TASK_FIELD_RE = re.compile(
    r"(?:^\s*|;\s*)(?P<label>"
    + "|".join(re.escape(label) for label in _COMMAND_TASK_FIELDS)
    + r")\s*:\s*",
    re.IGNORECASE,
)


def _command_task_fields(task: str) -> dict[str, str]:
    """Parse canonical command fields without interpreting free-form task text.

    A field starts at the beginning of a physical line or after a semicolon.
    This supports both the runner's multiline prompt and the README's compact
    one-line example while leaving ordinary semicolons inside values intact.
    """

    fields: dict[str, str] = {}
    for raw_line in str(task or "").splitlines():
        matches = list(_COMMAND_TASK_FIELD_RE.finditer(raw_line))
        for index, match in enumerate(matches):
            key = _COMMAND_TASK_FIELDS[match.group("label").lower()]
            value_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_line)
            value = raw_line[match.end() : value_end].strip()
            if not value:
                raise ValueError(
                    f"INVALID_COMMAND_TASK_IDENTITY: {match.group('label')} must not be empty"
                )
            if key in fields:
                raise ValueError(
                    f"INVALID_COMMAND_TASK_IDENTITY: duplicate {match.group('label')} field"
                )
            fields[key] = value
    return fields


def parse_command_task_identity(
    task: str,
    *,
    verification_mode: str,
    defaults: Mapping[str, str | None] | None = None,
) -> CommandTaskIdentity:
    """Resolve and validate the controller-owned identity for a command task."""

    fields = _command_task_fields(task)
    defaults = defaults or {}
    batch_project_root = str(defaults.get("project_root") or "").strip()
    batch_smell = str(defaults.get("smell") or "").strip()
    batch_location = str(defaults.get("location") or "").strip()
    has_batch_identity = bool(batch_project_root and batch_smell and batch_location)

    if has_batch_identity:
        # A batch controller may carry the identity outside command text. Keep
        # that state authoritative so model-visible task text cannot retarget
        # baseline capture or verification.
        fields.update(
            {
                "project_root": batch_project_root,
                "smell": batch_smell,
                "location": batch_location,
                "language": str(defaults.get("language") or "").strip(),
                "target_context_json": str(defaults.get("target_context_json") or "").strip(),
            }
        )

    task_mode = str(fields.get("verification_mode") or "").strip()
    if task_mode and task_mode != verification_mode:
        raise ValueError(
            "COMMAND_TASK_VERIFICATION_MODE_MISMATCH: task Verification mode "
            f"'{task_mode}' does not match command policy '{verification_mode}'"
        )

    missing = [
        label
        for label, key in (
            ("Project root", "project_root"),
            ("Smell type", "smell"),
            ("Target location", "location"),
        )
        if not str(fields.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(
            "INVALID_COMMAND_TASK_IDENTITY: missing required field(s): "
            + ", ".join(missing)
        )

    def optional_value(key: str) -> str:
        if has_batch_identity and key in defaults:
            return str(defaults.get(key) or "").strip()
        field_value = str(fields.get(key) or "").strip()
        default_value = str(defaults.get(key) or "").strip()
        return field_value or default_value

    build_command = optional_value("build_command")
    project_test_command = optional_value("project_test_command")
    verification_cwd = optional_value("verification_cwd")
    sample_test_command = optional_value("sample_test_command")
    verification_command_source = optional_value("verification_command_source")
    if not has_batch_identity and any(
        str(fields.get(key) or "").strip()
        for key in ("build_command", "project_test_command", "verification_cwd")
    ):
        verification_command_source = "command"
    sample_test_source = optional_value("sample_test_source")
    if (
        not has_batch_identity
        and str(fields.get("sample_test_command") or "").strip()
    ):
        sample_test_source = "command"
    if bool(build_command) != bool(project_test_command) or (
        verification_cwd and not build_command
    ):
        raise ValueError(
            "EXPLICIT_VERIFICATION_COMMAND_PAIR_REQUIRED: build_command and "
            "project_test_command must be provided together before verification_cwd"
        )
    if verification_command_source and not build_command:
        raise ValueError(
            "VERIFICATION_COMMAND_SOURCE_WITHOUT_COMMANDS: "
            "verification_command_source requires the complete build/project-test pair"
        )
    if sample_test_source and not sample_test_command:
        raise ValueError(
            "SAMPLE_TEST_SOURCE_WITHOUT_COMMAND: sample_test_source requires "
            "sample_test_command"
        )
    for field_name, source in (
        ("verification_command_source", verification_command_source),
        ("sample_test_source", sample_test_source),
    ):
        if source and source not in VERIFICATION_COMMAND_SOURCES:
            raise ValueError(
                f"INVALID_COMMAND_TASK_IDENTITY: unsupported {field_name} '{source}'"
            )

    return CommandTaskIdentity(
        project_root=str(fields["project_root"]).strip(),
        project_override_root=optional_value("project_override_root"),
        language=str(fields.get("language") or "").strip(),
        smell=str(fields["smell"]).strip(),
        location=str(fields["location"]).strip(),
        target_context_json=str(fields.get("target_context_json") or "").strip(),
        verification_mode=verification_mode,
        sample_test_location=optional_value("sample_test_location"),
        sample_test_command=sample_test_command,
        build_command=build_command,
        project_test_command=project_test_command,
        verification_cwd=verification_cwd,
        verification_command_source=verification_command_source,
        sample_test_source=sample_test_source,
    )


def initial_command_loop_state(
    command_payload: Mapping[str, Any],
    *,
    started_at_ms: int | None = None,
) -> dict[str, Any]:
    """Create the controller-transferable state before the first model turn."""

    policy = {
        "task": "Continue the current smell refactoring task.",
        "verification_mode": command_payload["verification_mode"],
        "refactoring_backend": command_payload["refactoring_backend"],
        "allow_test_changes": command_payload["allow_test_changes"],
        "checkpoint_required": command_payload["checkpoint_required"],
        "identity": dict(command_payload["identity"]),
        "loop": dict(command_payload["loop"]),
    }
    return {
        "schema_version": COMMAND_LOOP_STATE_VERSION,
        "policy": policy,
        "target_identity_context": "",
        "started_at": (
            int(started_at_ms)
            if started_at_ms is not None
            else int(time.time() * 1000)
        ),
        "control": {
            "generation": 0,
            "decision": "verify_required",
            "instruction": INITIAL_VERIFY_INSTRUCTION,
            "termination_reason": "",
        },
        "smell_verify_cycle_count": 0,
        "no_progress_count": 0,
        "last_failure_fingerprint": "",
        "best_metric_deficit": None,
        "best_structural_failure_count": None,
        "last_blocker_codes": [],
        "seen_structural_states": [],
        "formal_candidate_state": {
            "candidate_identity": None,
            "outcome": "",
            "diagnostic_signature": "",
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
        "terminal_receipt": None,
    }


def _state_record(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _state_integer(value: Any, *, minimum: int = 0) -> bool:
    return bool(
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
    )


def _state_finite_number(value: Any, *, minimum: float = 0.0) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= minimum
    )


def _valid_transfer_policy(value: Any) -> Mapping[str, Any] | None:
    policy = _state_record(value)
    loop = _state_record(policy.get("loop")) if policy else None
    identity = _state_record(policy.get("identity")) if policy else None
    if policy is None or loop is None or identity is None:
        return None
    verification_mode = policy.get("verification_mode")
    backend = policy.get("refactoring_backend", "direct")
    if (
        not isinstance(policy.get("task"), str)
        or not str(policy.get("task")).strip()
        or verification_mode
        not in {"local", "auto", "sample_optimized", "project_full"}
        or backend not in {"direct", "idea"}
        or not isinstance(policy.get("allow_test_changes"), bool)
        or not isinstance(policy.get("checkpoint_required"), bool)
        or (
            policy.get("allow_test_changes") is True
            and verification_mode != "project_full"
        )
    ):
        return None
    identity_fields = tuple(CommandTaskIdentity.__dataclass_fields__)
    if any(not isinstance(identity.get(field), str) for field in identity_fields):
        return None
    if any(not str(identity.get(field)).strip() for field in ("project_root", "smell", "location")):
        return None
    if identity.get("verification_mode") != verification_mode:
        return None
    build_command = str(identity.get("build_command") or "").strip()
    project_test_command = str(identity.get("project_test_command") or "").strip()
    verification_cwd = str(identity.get("verification_cwd") or "").strip()
    verification_source = str(identity.get("verification_command_source") or "").strip()
    sample_test_command = str(identity.get("sample_test_command") or "").strip()
    sample_test_source = str(identity.get("sample_test_source") or "").strip()
    if (
        bool(build_command) != bool(project_test_command)
        or (verification_cwd and not build_command)
        or (verification_source and not build_command)
        or (sample_test_source and not sample_test_command)
        or verification_source not in VERIFICATION_COMMAND_SOURCES | {""}
        or sample_test_source not in VERIFICATION_COMMAND_SOURCES | {""}
        or (backend == "idea" and str(identity.get("language")).lower() != "java")
    ):
        return None
    groups = loop.get("allowed_failure_groups")
    if (
        loop.get("mode") not in LOOP_MODES
        or not _state_integer(loop.get("max_smell_verify_cycles"))
        or int(loop.get("max_smell_verify_cycles")) > 10
        or not _state_integer(loop.get("no_progress_limit"), minimum=1)
        or int(loop.get("no_progress_limit")) > 5
        or not isinstance(groups, list)
        or any(not isinstance(item, str) or item not in FAILURE_GROUPS for item in groups)
        or len(set(groups)) != len(groups)
        or (
            loop.get("mode") != "off"
            and int(loop.get("max_smell_verify_cycles")) > 0
            and not groups
        )
        or not isinstance(loop.get("instruction"), str)
        or not str(loop.get("instruction")).strip()
        or not _state_finite_number(loop.get("sample_deadline_seconds"), minimum=60)
        or float(loop.get("sample_deadline_seconds")) > 7200
    ):
        return None
    return policy


def _valid_idea_protocol_state(value: Any) -> Mapping[str, Any] | None:
    state = _state_record(value)
    if state is None:
        return None
    active = state.get("active_proposal")
    blocker = state.get("proposal_blocker")
    active_record = None if active is None else _state_record(active)
    blocker_record = None if blocker is None else _state_record(blocker)
    if active is not None and (
        active_record is None
        or not isinstance(active_record.get("proposal_id"), str)
        or not str(active_record.get("proposal_id")).strip()
        or not isinstance(active_record.get("operation"), str)
        or active_record.get("status")
        not in {"ready", "needs_input", "needs_decision", "retryable_failed"}
    ):
        return None
    if blocker is not None and (
        blocker_record is None
        or blocker_record.get("status") != "unsupported_target"
        or not isinstance(blocker_record.get("proposal_id"), str)
        or not isinstance(blocker_record.get("operation"), str)
        or not str(blocker_record.get("operation")).strip()
        or not isinstance(blocker_record.get("diagnostic_codes"), list)
        or len(blocker_record.get("diagnostic_codes", [])) > 8
        or any(
            not isinstance(item, str) or not item
            for item in blocker_record.get("diagnostic_codes", [])
        )
    ):
        return None
    mutation_generation = state.get("mutation_generation")
    verified_generation = state.get("verified_generation")
    revertible_generation = state.get("revertible_apply_generation")
    route = state.get("mutation_route")
    proposal_id = state.get("mutation_proposal_id")
    if (
        active_record is not None and blocker_record is not None
        or not _state_integer(mutation_generation)
        or not _state_integer(verified_generation)
        or int(verified_generation) > int(mutation_generation)
        or route not in {"", "native_apply", "authorized_edit", "apply_outcome_unknown"}
        or not isinstance(proposal_id, str)
        or (
            int(mutation_generation) == 0
            and (
                int(verified_generation) != 0
                or route
                or proposal_id
                or revertible_generation is not None
            )
        )
        or (int(mutation_generation) > 0 and not route)
        or (
            revertible_generation is not None
            and (
                not _state_integer(revertible_generation, minimum=1)
                or int(revertible_generation) != int(mutation_generation)
                or route != "native_apply"
                or int(verified_generation) >= int(mutation_generation)
            )
        )
    ):
        return None
    return state


def validate_transferable_command_loop_state(
    value: Any,
) -> Mapping[str, Any] | None:
    """Validate the complete v7 cross-process state without smell decisions."""

    state = _state_record(value)
    policy = _valid_transfer_policy(state.get("policy")) if state else None
    if state is None or state.get("schema_version") != COMMAND_LOOP_STATE_VERSION or policy is None:
        return None
    loop_policy = _state_record(policy.get("loop"))
    control = _state_record(state.get("control"))
    formal = _state_record(state.get("formal_candidate_state"))
    if loop_policy is None or control is None or formal is None:
        return None
    generation = control.get("generation")
    decision = control.get("decision")
    instruction = control.get("instruction")
    termination_reason = control.get("termination_reason")
    if (
        not _state_finite_number(state.get("started_at"))
        or not isinstance(state.get("target_identity_context"), str)
        or len(str(state.get("target_identity_context"))) > 32768
        or not _state_integer(generation)
        or decision not in {"verify_required", "continue", "stop"}
        or not isinstance(instruction, str)
        or not isinstance(termination_reason, str)
        or (
            decision == "verify_required"
            and (
                generation != 0
                or instruction != INITIAL_VERIFY_INSTRUCTION
                or termination_reason
            )
        )
        or (decision == "continue" and (not instruction or termination_reason))
        or (decision == "stop" and (instruction or not termination_reason))
        or not _state_integer(state.get("smell_verify_cycle_count"))
        or int(state.get("smell_verify_cycle_count"))
        > int(loop_policy.get("max_smell_verify_cycles"))
        or not _state_integer(state.get("no_progress_count"))
        or not isinstance(state.get("last_failure_fingerprint"), str)
    ):
        return None
    for name, integer_only in (
        ("best_metric_deficit", False),
        ("best_structural_failure_count", True),
    ):
        item = state.get(name)
        if item is not None and not (
            _state_integer(item) if integer_only else _state_finite_number(item)
        ):
            return None
    blockers = state.get("last_blocker_codes")
    seen = state.get("seen_structural_states")
    if (
        not isinstance(blockers, list)
        or not isinstance(seen, list)
        or len(blockers) > 32
        or len(seen) > 32
        or any(not isinstance(item, str) or not item for item in blockers)
        or any(not isinstance(item, str) or not item for item in seen)
        or len(set(seen)) != len(seen)
    ):
        return None
    candidate_value = formal.get("candidate_identity")
    candidate = None if candidate_value is None else _state_record(candidate_value)
    outcome = formal.get("outcome")
    signature = formal.get("diagnostic_signature")
    confirmation = formal.get("confirmation_required")
    if (
        (candidate_value is not None and candidate is None)
        or outcome not in {"", "pass", "test_failed", "failed"}
        or not isinstance(signature, str)
        or len(signature) > 128
        or not isinstance(confirmation, bool)
    ):
        return None
    if candidate is None:
        if outcome != "" or signature != "" or confirmation is not False:
            return None
    else:
        java_identity = str(
            (_state_record(policy.get("identity")) or {}).get("language") or ""
        ).lower() == "java"
        for name, nonempty in (
            ("baseline_revision", True),
            ("baseline_tree", False),
            ("production_diff", True),
            ("test_tree", java_identity),
            ("verification_config_tree", java_identity),
        ):
            item = candidate.get(name)
            if (
                not isinstance(item, str)
                or len(item) > 128
                or (nonempty and not item)
            ):
                return None
        if outcome == "" or not signature:
            return None
    idea_state = state.get("idea_protocol_state")
    validated_idea_state = _valid_idea_protocol_state(idea_state)
    if validated_idea_state is None:
        return None

    terminal_value = state.get("terminal_receipt")
    terminal = None if terminal_value is None else _state_record(terminal_value)
    if terminal_value is not None and terminal is None:
        return None
    if terminal is None:
        return state if decision != "stop" else None
    terminal_loop = _state_record(terminal.get("loop"))
    if (
        terminal.get("stage") not in {"cheap_guard", "formal_verify", "protocol"}
        or not isinstance(terminal.get("status"), str)
        or not isinstance(terminal.get("success"), bool)
        or not isinstance(terminal.get("accepted"), bool)
        or not isinstance(terminal.get("resolution"), str)
        or not isinstance(terminal.get("terminationReason"), str)
        or not isinstance(terminal.get("failureCategory"), str)
        or not isinstance(terminal.get("failureGroup"), str)
        or "formalVerificationReceipt" not in terminal
        or "ideaProtocolReceipt" not in terminal
        or terminal_loop is None
        or terminal_loop.get("decision") != "stop"
        or terminal_loop.get("generation") != generation
        or terminal_loop.get("termination_reason") != termination_reason
        or terminal.get("terminationReason") != termination_reason
        or decision != "stop"
        or (terminal.get("accepted") is True and terminal.get("success") is not True)
    ):
        return None
    formal_receipt_value = terminal.get("formalVerificationReceipt")
    if terminal.get("stage") == "formal_verify":
        formal_receipt = (
            None
            if formal_receipt_value is None
            else validate_formal_verification_receipt(formal_receipt_value)
        )
        if formal_receipt_value is not None and formal_receipt is None:
            return None
        if terminal.get("accepted") is True and formal_receipt is None:
            return None
        if formal_receipt is not None and any(
            formal_receipt.get(key) != terminal.get(key)
            for key in ("status", "success", "accepted", "resolution")
        ):
            return None
    elif formal_receipt_value is not None:
        return None
    idea_receipt_value = terminal.get("ideaProtocolReceipt")
    idea_receipt = (
        None if idea_receipt_value is None else _state_record(idea_receipt_value)
    )
    idea_formal = (
        terminal.get("stage") == "formal_verify"
        and policy.get("refactoring_backend") == "idea"
    )
    if idea_formal:
        if idea_receipt is None:
            return None
        mutation_generation = int(validated_idea_state.get("mutation_generation"))
        verified_generation = int(validated_idea_state.get("verified_generation"))
        blocker = _state_record(validated_idea_state.get("proposal_blocker"))
        expected_blocker_status = str(blocker.get("status") or "") if blocker else ""
        expected_blocker_codes = list(blocker.get("diagnostic_codes") or []) if blocker else []
        complete = mutation_generation > 0 and mutation_generation == verified_generation
        if (
            idea_receipt.get("schema_version") != "smell.idea-protocol-receipt/v1"
            or idea_receipt.get("mutation_generation") != mutation_generation
            or idea_receipt.get("verified_generation") != verified_generation
            or idea_receipt.get("mutation_route")
            != validated_idea_state.get("mutation_route")
            or idea_receipt.get("proposal_id")
            != validated_idea_state.get("mutation_proposal_id")
            or idea_receipt.get("blocker_status") != expected_blocker_status
            or idea_receipt.get("blocker_codes") != expected_blocker_codes
            or idea_receipt.get("complete") is not complete
            or (terminal.get("accepted") is True and complete is not True)
        ):
            return None
    elif idea_receipt_value is not None:
        return None
    return state


def resolve_command_payload(
    arguments: str,
    *,
    defaults: Mapping[str, str | None] | None = None,
    started_at_ms: int | None = None,
) -> dict[str, Any]:
    """Resolve command policy, identity and initial transferable state once."""

    policy = parse_command_policy(arguments)
    identity = parse_command_task_identity(
        policy.task,
        verification_mode=policy.verification_mode,
        defaults=defaults,
    )
    payload = policy.to_dict()
    payload["identity"] = identity.to_dict()
    if policy.refactoring_backend == "idea" and identity.language.lower() != "java":
        raise ValueError(
            "IDEA_BACKEND_REQUIRES_JAVA: --refactoring-backend=idea requires "
            "an explicit Java command identity"
        )
    payload["checkpoint_required"] = identity.smell in CHECKPOINT_SMELLS
    payload["command_loop_state"] = initial_command_loop_state(
        payload,
        started_at_ms=started_at_ms,
    )
    return payload


def parse_command_policy(arguments: str) -> ResolvedCommandPolicy:
    policy_text, task = _split_policy_and_task(arguments)
    # OpenCode joins command message argv into one string, so shell quote
    # boundaries are not preserved. Keep the free-form instruction as the last
    # policy option and extract it before token parsing; this allows spaces
    # without inventing another quoting grammar.
    instruction = LoopPolicy().instruction
    # Yargs may reconstruct a single argv item containing spaces with quotes
    # around the entire option (for example
    # '--loop-instruction=repair the latest failure'). Accept that native
    # OpenCode representation as well as the unquoted command-text form.
    instruction_match = re.search(
        r"(?:^|\s)(?P<quote>['\"])?--loop-instruction=(?P<value>.*)$",
        policy_text,
    )
    if instruction_match:
        instruction = instruction_match.group("value").strip()
        policy_text = policy_text[: instruction_match.start()].strip()
        opening_quote = instruction_match.group("quote")
        if opening_quote and instruction.endswith(opening_quote):
            instruction = instruction[:-1].rstrip()
        if not instruction:
            raise ValueError("INVALID_LOOP_POLICY: --loop-instruction must not be empty")
        if len(instruction) >= 2 and instruction[0] == instruction[-1] and instruction[0] in {"'", '"'}:
            instruction = instruction[1:-1]
    try:
        tokens = shlex.split(policy_text)
    except ValueError as exc:
        raise ValueError(f"INVALID_LOOP_POLICY: {exc}") from exc

    parser = _PolicyParser(add_help=False, allow_abbrev=False)
    parser.add_argument(
        "--verification-mode",
        # The shared command parser still serves the non-Java agent. The Java
        # plugin surface exposes only the two strict modes below.
        choices=("local", "auto", "sample_optimized", "project_full"),
        default="project_full",
    )
    parser.add_argument(
        "--refactoring-backend",
        choices=("direct", "idea"),
        default="direct",
    )
    parser.add_argument(
        "--allow-test-changes",
        action="store_true",
        help="controller-owned opt-in; frozen into c000 before the repair starts",
    )
    parser.add_argument("--loop-mode", choices=sorted(LOOP_MODES), default="verify-failure")
    parser.add_argument("--max-smell-verify-cycles", type=int, default=10)
    parser.add_argument("--loop-no-progress-limit", type=int, default=3)
    parser.add_argument("--loop-on", default="smell,compile,test")
    parser.add_argument("--sample-deadline", type=int, default=1800)
    parsed = parser.parse_args(tokens)

    if not 0 <= parsed.max_smell_verify_cycles <= 10:
        raise ValueError("INVALID_LOOP_POLICY: --max-smell-verify-cycles must be between 0 and 10")
    if not 1 <= parsed.loop_no_progress_limit <= 5:
        raise ValueError("INVALID_LOOP_POLICY: --loop-no-progress-limit must be between 1 and 5")
    if not 60 <= parsed.sample_deadline <= 7200:
        raise ValueError("INVALID_LOOP_POLICY: --sample-deadline must be between 60 and 7200 seconds")
    groups = tuple(dict.fromkeys(item.strip() for item in parsed.loop_on.split(",") if item.strip()))
    unknown = sorted(set(groups).difference(FAILURE_GROUPS))
    if unknown:
        raise ValueError(f"INVALID_LOOP_POLICY: unsupported --loop-on groups: {', '.join(unknown)}")
    if not groups and parsed.loop_mode != "off" and parsed.max_smell_verify_cycles > 0:
        raise ValueError("INVALID_LOOP_POLICY: --loop-on must contain at least one failure group")
    if parsed.allow_test_changes and parsed.verification_mode != "project_full":
        raise ValueError(
            "TEST_CHANGE_REQUIRES_PROJECT_FULL: --allow-test-changes requires "
            "--verification-mode=project_full"
        )
    mode = "off" if parsed.max_smell_verify_cycles == 0 else parsed.loop_mode

    return ResolvedCommandPolicy(
        task=task,
        verification_mode=parsed.verification_mode,
        refactoring_backend=parsed.refactoring_backend,
        allow_test_changes=bool(parsed.allow_test_changes),
        loop=LoopPolicy(
            mode=mode,
            max_smell_verify_cycles=parsed.max_smell_verify_cycles,
            no_progress_limit=parsed.loop_no_progress_limit,
            allowed_failure_groups=groups,
            instruction=instruction,
            sample_deadline_seconds=parsed.sample_deadline,
        ),
    )
