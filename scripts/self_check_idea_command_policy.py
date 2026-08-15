#!/usr/bin/env python3
"""Shared command-policy checks for the IDEA backend.

This check intentionally exercises the command parser and transferable v6
state directly.  It does not invoke the dataset runner.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.loop_policy import resolve_command_payload  # noqa: E402


def command(backend: str, language: str = "java") -> str:
    return (
        "--verification-mode=project_full "
        f"--refactoring-backend={backend} "
        "--loop-max=2 --sample-deadline=600 -- "
        f"Project root: /tmp/project; Language: {language}; "
        "Smell type: long_method; Target location: src/Foo.java:method=run|line=1"
    )


idea = resolve_command_payload(command("idea"), started_at_ms=1234)
assert idea["refactoring_backend"] == "idea", idea
assert idea["command_loop_state"]["policy"]["refactoring_backend"] == "idea", idea
assert idea["command_loop_state"]["idea_protocol_state"] == {
    "active_proposal": None,
    "proposal_blocker": None,
    "mutation_generation": 0,
    "verified_generation": 0,
    "mutation_route": "",
    "mutation_proposal_id": "",
    "revertible_apply_generation": None,
}, idea

direct = resolve_command_payload(command("direct"), started_at_ms=1234)
assert direct["refactoring_backend"] == "direct", direct
assert direct["command_loop_state"]["policy"]["refactoring_backend"] == "direct", direct

default_direct = resolve_command_payload(
    command("direct").replace("--refactoring-backend=direct ", ""),
    started_at_ms=1234,
)
assert default_direct["refactoring_backend"] == "direct", default_direct

try:
    resolve_command_payload(command("idea", language="python"), started_at_ms=1234)
except ValueError as exc:
    assert "IDEA_BACKEND_REQUIRES_JAVA" in str(exc), exc
else:
    raise AssertionError("IDEA backend accepted a non-Java command identity")

print("idea command policy self-check passed")
