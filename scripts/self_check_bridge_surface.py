#!/usr/bin/env python3
"""Keep the Python bridge limited to the three product entry points."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python" / "bridge"))

import smell_bridge  # noqa: E402


def main() -> int:
    parser = smell_bridge.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)
    expected = {"resolve-command", "capture-baseline", "verify"}
    assert commands == expected, (commands, expected)

    build_timeout = {
        "status": "BUILD_FAILED",
        "build_test_guard": {
            "details": {"build": {"success": False, "status": "timeout"}}
        },
    }
    sample_test_timeout = {
        "status": "SAMPLE_TEST_FAILED",
        "build_test_guard": {
            "details": {"test": {"success": False, "status": "timeout"}}
        },
    }
    for payload in (build_timeout, sample_test_timeout):
        category, _ = smell_bridge._classify_failure_pack(payload, "")
        assert category == "TIMEOUT_OR_MODAL_SUSPECTED", category
        failure_pack = smell_bridge._build_failure_pack(payload, {})
        assert failure_pack["failure_group"] == "", failure_pack
        assert failure_pack["retryable"] is False, failure_pack
        assert failure_pack["repair_contract"]["repair_agent_may_edit"] is False

    print(
        "bridge-surface self-check: PASS commands=3 legacy_context_commands=0 "
        "timeout_classification=nonrepairable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
