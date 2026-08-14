#!/usr/bin/env python3
"""Adversarial checks for the non-Java cheap Guard progress gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNTIME = ROOT / "runtime" / "python"
if str(PYTHON_RUNTIME) not in sys.path:
    sys.path.insert(0, str(PYTHON_RUNTIME))

from bridge import smell_bridge


def _run_case(
    *,
    language: str,
    smell: str,
    budget: dict[str, object],
    source_guard_passed: bool,
) -> dict[str, object]:
    resolved = SimpleNamespace(
        language=language,
        smell=smell,
        locations=[SimpleNamespace()],
    )
    checkpoint = {
        "required": True,
        "current_metrics": {"finding_present": not source_guard_passed},
        "resolution_plan": {
            "metric_budget": [
                {
                    **budget,
                    "files": ["must-not-leak.c"],
                    "callers": ["must_not_leak()"],
                }
            ],
            "next_action": (
                "migrate the frozen long signature across the listed closure; "
                "start with src/private_closure.c:41 | "
                "call_api(int,int,int,int,int,int,int)"
            ),
            "worklist": [{"file": "must-not-leak.c"}],
        },
    }
    guard_results = [{
        "type": smell,
        "success": True,
        "message": "frozen target source Guard passed",
    }] if source_guard_passed else [
        {
            "type": smell,
            "success": False,
            "message": "frozen target finding remains",
        }
    ]
    original_resolve = smell_bridge._resolve
    original_checkpoint = smell_bridge._checkpoint_context
    original_guards = smell_bridge.run_smell_guards
    original_build = smell_bridge.run_build_test_guard
    project_full_calls: list[bool] = []
    checkpoint_persist_modes: list[bool] = []
    try:
        smell_bridge._resolve = lambda _args: resolved

        def _checkpoint(*_args, **kwargs):
            checkpoint_persist_modes.append(bool(kwargs.get("persist", True)))
            return SimpleNamespace(), checkpoint

        smell_bridge._checkpoint_context = _checkpoint
        smell_bridge.run_smell_guards = lambda *_args, **_kwargs: guard_results

        def _unexpected_project_full(*_args, **_kwargs):
            project_full_calls.append(True)
            raise AssertionError("cheap Guard progress must not run project_full")

        smell_bridge.run_build_test_guard = _unexpected_project_full
        payload = smell_bridge.cmd_guard_progress(SimpleNamespace(
            smell_evidence="",
            baseline_seal="",
        ))
    finally:
        smell_bridge._resolve = original_resolve
        smell_bridge._checkpoint_context = original_checkpoint
        smell_bridge.run_smell_guards = original_guards
        smell_bridge.run_build_test_guard = original_build

    assert project_full_calls == [], project_full_calls
    assert checkpoint_persist_modes == [False], checkpoint_persist_modes
    assert payload["project_full_executed"] is False, payload
    assert payload["ready_for_project_full"] is source_guard_passed, payload
    assert payload["status"] == (
        "GUARD_PROGRESS_PASSED"
        if source_guard_passed
        else "GUARD_PROGRESS_REQUIRED"
    ), payload
    assert payload["metric_budget"] == ([budget] if budget else []), payload
    rendered = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "worklist",
        "files",
        "callers",
        "must-not-leak",
        "private_closure",
        "call_api",
        "listed closure",
    ):
        assert forbidden not in rendered, (forbidden, payload)
    return payload


def main() -> None:
    replay_cases = (
        (
            "55",
            "python",
            "long_method",
            {
                "metric": "meaningful_line_count",
                "current": 91,
                "passing_max": 80,
                "required_reduction": 11,
                "unit": "meaningful_line_count",
            },
        ),
        (
            "57",
            "python",
            "long_method",
            {
                "metric": "meaningful_line_count",
                "current": 84,
                "passing_max": 80,
                "required_reduction": 4,
                "unit": "meaningful_line_count",
            },
        ),
        (
            "185",
            "c",
            "nested_complexity",
            {
                "metric": "max_nesting_depth",
                "current": 6,
                "passing_max": 4,
                "required_reduction": 2,
                "unit": "max_nesting_depth",
            },
        ),
    )
    for replay, language, smell, budget in replay_cases:
        early = _run_case(
            language=language,
            smell=smell,
            budget=budget,
            source_guard_passed=False,
        )
        for key in ("metric", "current", "passing_max", "required_reduction"):
            assert key in early["next_action"], (replay, key, early)
        crossed = _run_case(
            language=language,
            smell=smell,
            budget={**budget, "required_reduction": 0},
            source_guard_passed=True,
        )
        assert crossed["source_guard_passed"] is True, (replay, crossed)

    no_budget = _run_case(
        language="python",
        smell="long_method",
        budget={},
        source_guard_passed=False,
    )
    assert no_budget["metric_budget"] == [], no_budget
    assert no_budget["next_action"] == "restore frozen source Guard", no_budget

    original_resolve = smell_bridge._resolve
    original_checkpoint = smell_bridge._checkpoint_context
    checkpoint_calls: list[str] = []
    try:
        for language, smell, location in (
            ("java", "long_method", "Sample.java:1"),
            ("python", "unsupported_smell", "sample.py:1"),
        ):
            smell_bridge._resolve = lambda _args, language=language, smell=smell: (
                SimpleNamespace(
                    language=language,
                    smell=smell,
                    locations=[SimpleNamespace(raw=location)],
                )
            )
            smell_bridge._checkpoint_context = lambda *_args, **_kwargs: (
                checkpoint_calls.append("called")
            )
            payload = smell_bridge.cmd_guard_progress(SimpleNamespace(
                smell_evidence="",
                baseline_seal="",
            ))
            assert payload["applicable"] is False, payload
            assert payload["status"] == "GUARD_PROGRESS_NOT_APPLICABLE", payload
            assert payload["project_full_executed"] is False, payload
    finally:
        smell_bridge._resolve = original_resolve
        smell_bridge._checkpoint_context = original_checkpoint
    assert checkpoint_calls == [], checkpoint_calls

    print(
        "guard progress preflight self-check passed: replay 55/57/185 early "
        "calls avoid project_full; scalar budget only; Java/noncheckpoint bypass"
    )


if __name__ == "__main__":
    main()
