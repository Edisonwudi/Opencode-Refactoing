from __future__ import annotations

import argparse
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any


LOOP_MODES = {"off", "verify-failure"}
FAILURE_GROUPS = {"smell", "compile", "test"}
REPAIRABLE_CATEGORY_GROUPS = {
    "SMELL_GUARD_FAILED": "smell",
    "STRUCTURAL_ROUTE_MISMATCH": "smell",
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
    instruction: str = "Read the latest failure_pack, make one narrow corrective edit, and call smell_verify again. Do not modify or weaken tests."
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
    loop: LoopPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "verification_mode": self.verification_mode,
            "loop": self.loop.to_dict(),
        }


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
        choices=("local", "auto", "sample_optimized", "project_full"),
        default="local",
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
    mode = "off" if parsed.loop_max == 0 else parsed.loop_mode

    return ResolvedCommandPolicy(
        task=task,
        verification_mode=parsed.verification_mode,
        loop=LoopPolicy(
            mode=mode,
            max_continuations=parsed.loop_max,
            no_progress_limit=parsed.loop_no_progress_limit,
            allowed_failure_groups=groups,
            instruction=instruction,
            sample_deadline_seconds=parsed.sample_deadline,
        ),
    )
