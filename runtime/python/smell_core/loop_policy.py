from __future__ import annotations

import argparse
import re
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping


LOOP_MODES = {"off", "verify-failure"}
FAILURE_GROUPS = {"smell", "compile", "test"}
COMMAND_LOOP_STATE_VERSION = 3
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
    "BUILD_COMPILE_ERROR": "compile",
    "TEST_BEHAVIOR_REGRESSION": "test",
    "TEST_REFLECTION_ENTRY_STALE": "test",
    "SAMPLE_TEST_FAILED": "test",
}


@dataclass(frozen=True)
class LoopPolicy:
    mode: str = "verify-failure"
    max_continuations: int = 2
    no_progress_limit: int = 1
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
        if self.mode == "off" or self.max_continuations <= 0:
            return False
        group = REPAIRABLE_CATEGORY_GROUPS.get(str(failure_category or "").strip())
        return bool(group and group in self.allowed_failure_groups)


@dataclass(frozen=True)
class ResolvedCommandPolicy:
    task: str
    verification_mode: str
    allow_test_changes: bool
    loop: LoopPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "verification_mode": self.verification_mode,
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
        default_value = str(defaults.get(key) or "").strip()
        return default_value or str(fields.get(key) or "").strip()

    return CommandTaskIdentity(
        project_root=str(fields["project_root"]).strip(),
        project_override_root=optional_value("project_override_root"),
        language=str(fields.get("language") or "").strip(),
        smell=str(fields["smell"]).strip(),
        location=str(fields["location"]).strip(),
        target_context_json=str(fields.get("target_context_json") or "").strip(),
        verification_mode=verification_mode,
        sample_test_location=optional_value("sample_test_location"),
        sample_test_command=optional_value("sample_test_command"),
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
        "allow_test_changes": command_payload["allow_test_changes"],
        "checkpoint_required": command_payload["checkpoint_required"],
        "identity": dict(command_payload["identity"]),
        "loop": dict(command_payload["loop"]),
    }
    return {
        "schema_version": COMMAND_LOOP_STATE_VERSION,
        "policy": policy,
        "started_at": (
            int(started_at_ms)
            if started_at_ms is not None
            else int(time.time() * 1000)
        ),
        "continuation_count": 0,
        "cap_recovery_used": False,
        "no_progress_count": 0,
        "last_failure_fingerprint": "",
    }


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
        "--allow-test-changes",
        action="store_true",
        help="controller-owned opt-in; frozen into c000 before the repair starts",
    )
    parser.add_argument("--loop-mode", choices=sorted(LOOP_MODES), default="verify-failure")
    parser.add_argument("--loop-max", type=int, default=3)
    parser.add_argument("--loop-no-progress-limit", type=int, default=2)
    parser.add_argument("--loop-on", default="smell,compile,test")
    parser.add_argument("--sample-deadline", type=int, default=1800)
    parsed = parser.parse_args(tokens)

    if not 0 <= parsed.loop_max <= 5:
        raise ValueError("INVALID_LOOP_POLICY: --loop-max must be between 0 and 5")
    if not 1 <= parsed.loop_no_progress_limit <= 5:
        raise ValueError("INVALID_LOOP_POLICY: --loop-no-progress-limit must be between 1 and 5")
    if not 60 <= parsed.sample_deadline <= 7200:
        raise ValueError("INVALID_LOOP_POLICY: --sample-deadline must be between 60 and 7200 seconds")
    groups = tuple(dict.fromkeys(item.strip() for item in parsed.loop_on.split(",") if item.strip()))
    unknown = sorted(set(groups).difference(FAILURE_GROUPS))
    if unknown:
        raise ValueError(f"INVALID_LOOP_POLICY: unsupported --loop-on groups: {', '.join(unknown)}")
    if not groups and parsed.loop_mode != "off" and parsed.loop_max > 0:
        raise ValueError("INVALID_LOOP_POLICY: --loop-on must contain at least one failure group")
    if parsed.allow_test_changes and parsed.verification_mode != "project_full":
        raise ValueError(
            "TEST_CHANGE_REQUIRES_PROJECT_FULL: --allow-test-changes requires "
            "--verification-mode=project_full"
        )
    mode = "off" if parsed.loop_max == 0 else parsed.loop_mode

    return ResolvedCommandPolicy(
        task=task,
        verification_mode=parsed.verification_mode,
        allow_test_changes=bool(parsed.allow_test_changes),
        loop=LoopPolicy(
            mode=mode,
            max_continuations=parsed.loop_max,
            no_progress_limit=parsed.loop_no_progress_limit,
            allowed_failure_groups=groups,
            instruction=instruction,
            sample_deadline_seconds=parsed.sample_deadline,
        ),
    )
