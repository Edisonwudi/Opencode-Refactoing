#!/usr/bin/env python3
"""Adversarial checks for the source-only cheap Guard progress gate."""
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
    baseline_seal: str = "",
    location: str = "sample:1",
    guard_reason: str = "FINDING_REMAINS",
    guard_message: str = "frozen target finding remains",
    semantic_regressions: list[str] | None = None,
    plan_next_action: str | None = None,
    plan_overrides: dict[str, object] | None = None,
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
            "next_action": plan_next_action or (
                "migrate the frozen long signature across the listed closure; "
                "start with src/private_closure.c:41 | "
                "call_api(int,int,int,int,int,int,int)"
            ),
            "worklist": [{"file": "must-not-leak.c"}],
            "semantic_regressions": list(semantic_regressions or []),
            **dict(plan_overrides or {}),
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
            "message": guard_message,
            "details": {"reason": guard_reason},
        }
    ]
    original_resolve = smell_bridge._resolve
    original_checkpoint = smell_bridge._checkpoint_context
    original_guards = smell_bridge.run_smell_guards
    original_build = smell_bridge.run_build_test_guard
    original_snapshot = smell_bridge._snapshot_project
    project_full_calls: list[bool] = []
    snapshot_calls: list[bool] = []
    checkpoint_persist_modes: list[bool] = []
    checkpoint_seals: list[str] = []
    try:
        smell_bridge._resolve = lambda _args: resolved

        def _checkpoint(_resolved, _evidence, seal="", **kwargs):
            checkpoint_persist_modes.append(bool(kwargs.get("persist", True)))
            checkpoint_seals.append(str(seal))
            return SimpleNamespace(), checkpoint

        smell_bridge._checkpoint_context = _checkpoint
        smell_bridge.run_smell_guards = lambda *_args, **_kwargs: guard_results

        def _unexpected_project_full(*_args, **_kwargs):
            project_full_calls.append(True)
            raise AssertionError("cheap Guard progress must not run project_full")

        smell_bridge.run_build_test_guard = _unexpected_project_full
        smell_bridge._snapshot_project = lambda *_args, **_kwargs: (
            snapshot_calls.append(True)
        )
        payload = smell_bridge.cmd_guard_progress(SimpleNamespace(
            smell_evidence="",
            baseline_seal=baseline_seal,
            location=location,
        ))
    finally:
        smell_bridge._resolve = original_resolve
        smell_bridge._checkpoint_context = original_checkpoint
        smell_bridge.run_smell_guards = original_guards
        smell_bridge.run_build_test_guard = original_build
        smell_bridge._snapshot_project = original_snapshot

    assert project_full_calls == [], project_full_calls
    assert snapshot_calls == [], snapshot_calls
    assert checkpoint_persist_modes == [False], checkpoint_persist_modes
    assert checkpoint_seals == [baseline_seal], checkpoint_seals
    assert payload["project_full_executed"] is False, payload
    assert payload["ready_for_project_full"] is source_guard_passed, payload
    assert payload["status"] == (
        "GUARD_PROGRESS_PASSED"
        if source_guard_passed
        else "GUARD_PROGRESS_REQUIRED"
    ), payload
    assert payload["metric_budget"] == ([budget] if budget else []), payload
    feedback = payload["source_guard_feedback"]
    assert feedback["schema_version"] == "smell.source-guard-feedback/v1", feedback
    assert feedback["passed"] is source_guard_passed, feedback
    assert payload["next_action"] == feedback["next_action"], payload
    observation = feedback["progress_observation"]
    assert set(observation) == {
        "metric_deficit",
        "structural_failure_count",
        "blocker_codes",
    }, observation
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
        assert early["source_guard_feedback"]["blocker"]["kind"] == (
            "metric_budget"
        ), early
        assert early["source_guard_feedback"]["progress_observation"] == {
            "metric_deficit": budget["required_reduction"],
            "structural_failure_count": 0,
            "blocker_codes": ["SCALAR_GUARD_THRESHOLD_NOT_MET"],
        }, early
        crossed = _run_case(
            language=language,
            smell=smell,
            budget={**budget, "required_reduction": 0},
            source_guard_passed=True,
        )
        assert crossed["source_guard_passed"] is True, (replay, crossed)
        assert crossed["source_guard_feedback"]["progress_observation"] == {
            "metric_deficit": 0,
            "structural_failure_count": 0,
            "blocker_codes": [],
        }, crossed

    java_early = _run_case(
        language="java",
        smell="long_method",
        budget={
            "metric": "ast_ncss",
            "current": 91,
            "passing_max": 80,
            "required_reduction": 11,
            "unit": "ast_ncss",
        },
        source_guard_passed=False,
        baseline_seal="controller-seal",
    )
    assert java_early["applicable"] is True, java_early
    assert java_early["ready_for_project_full"] is False, java_early

    java_crossed = _run_case(
        language="java",
        smell="long_method",
        budget={
            "metric": "ast_ncss",
            "current": 80,
            "passing_max": 80,
            "required_reduction": 0,
            "unit": "ast_ncss",
        },
        source_guard_passed=True,
        baseline_seal="controller-seal",
    )
    assert java_crossed["ready_for_project_full"] is True, java_crossed

    java_location_only = _run_case(
        language="",
        smell="long_method",
        budget={
            "metric": "ast_ncss",
            "current": 91,
            "passing_max": 80,
            "required_reduction": 11,
            "unit": "ast_ncss",
        },
        source_guard_passed=False,
        baseline_seal="controller-seal",
        location="src/Sample.java:1",
    )
    assert java_location_only["applicable"] is True, java_location_only

    no_budget = _run_case(
        language="python",
        smell="long_method",
        budget={},
        source_guard_passed=False,
    )
    assert no_budget["metric_budget"] == [], no_budget
    assert no_budget["next_action"] == "restore frozen source Guard", no_budget

    structural = _run_case(
        language="python",
        smell="code_clone_type1",
        budget={
            "metric": "clone_token_count",
            "current": 0,
            "passing_max": 16,
            "required_reduction": 0,
            "unit": "clone_token_count",
        },
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        guard_message=(
            "code_clone_type1 checkpoint contract: the refactoring violated "
            "a smell-specific structural contract."
        ),
        semantic_regressions=["CLONE_TARGET_DECLARATION_IDENTITY_FAILED"],
        plan_next_action="generic resolved-finding regression guidance",
    )
    structural_feedback = structural["source_guard_feedback"]
    assert structural_feedback["blocker"] == {
        "kind": "semantic_contract",
        "code": "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
        "guard_type": "code_clone_type1",
        "message": (
            "code_clone_type1 checkpoint contract: the refactoring violated "
            "a smell-specific structural contract."
        ),
    }, structural_feedback
    assert structural_feedback["progress_observation"] == {
        "metric_deficit": 0,
        "structural_failure_count": 1,
        "blocker_codes": ["CLONE_TARGET_DECLARATION_IDENTITY_FAILED"],
    }, structural_feedback
    assert "CLONE_TARGET_DECLARATION_IDENTITY_FAILED" in structural["next_action"], (
        structural
    )
    assert "thin wrapper" in structural["next_action"], structural
    assert "scalar Guard route" not in structural["next_action"], structural

    multiple_structural = _run_case(
        language="cpp",
        smell="code_clone_type1",
        budget={
            "metric": "clone_token_count",
            "current": 0,
            "passing_max": 16,
            "required_reduction": 0,
            "unit": "clone_token_count",
        },
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        semantic_regressions=[
            "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
            "CPP_PURE_VIRTUAL_ABI_CHANGED",
            "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
        ],
        plan_next_action="restore the frozen structural contracts",
    )
    multiple_observation = multiple_structural[
        "source_guard_feedback"
    ]["progress_observation"]
    assert multiple_observation == {
        "metric_deficit": 0,
        "structural_failure_count": 2,
        "blocker_codes": [
            "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
            "CPP_PURE_VIRTUAL_ABI_CHANGED",
        ],
    }, multiple_structural
    assert smell_bridge._compact_source_guard_feedback(
        multiple_structural["source_guard_feedback"]
    )["progress_observation"] == multiple_observation

    second_blocker_only = _run_case(
        language="cpp",
        smell="code_clone_type1",
        budget={
            "metric": "clone_token_count",
            "current": 0,
            "passing_max": 16,
            "required_reduction": 0,
            "unit": "clone_token_count",
        },
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        semantic_regressions=["CPP_PURE_VIRTUAL_ABI_CHANGED"],
        plan_next_action="restore the frozen structural contract",
    )
    second_observation = second_blocker_only[
        "source_guard_feedback"
    ]["progress_observation"]
    assert second_observation["structural_failure_count"] == 1, (
        second_blocker_only
    )
    assert second_observation["blocker_codes"] == [
        "CPP_PURE_VIRTUAL_ABI_CHANGED"
    ], second_blocker_only
    assert (
        multiple_observation["structural_failure_count"]
        > second_observation["structural_failure_count"]
    ), (multiple_observation, second_observation)

    typed_contract_actions = (
        (
            "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
            ("original owner", "thin wrapper", "shared implementation"),
            {},
        ),
        (
            "TARGET_NOT_LOCATED",
            ("frozen target declaration", "original identity", "unambiguous"),
            {"target_unlocated": True},
        ),
        (
            "CURRENT_DETECTOR_UNAVAILABLE",
            ("source parseability", "Guard availability", "valid current snapshot"),
            {"detector_blocker": "CURRENT_DETECTOR_UNAVAILABLE"},
        ),
        (
            "MN_REFERENCE_CLOSURE_MISMATCH",
            ("production reference closure", "stale old-name", "continuity"),
            {},
        ),
        (
            "MN_STALE_REFERENCE_REMAINS",
            ("production reference closure", "stale old-name", "continuity"),
            {},
        ),
        (
            "parameter_group_continuity_unavailable",
            ("parameter-group occurrence", "successor lineage", "unlocatable"),
            {},
        ),
        (
            "CPP_PURE_VIRTUAL_ABI_CHANGED",
            ("pure-virtual declaration", "vtable slot", "ABI-compatible"),
            {},
        ),
    )
    for blocker_code, expected_phrases, plan_overrides in typed_contract_actions:
        typed = _run_case(
            language="cpp",
            smell="data_clumps",
            budget={
                "metric": "occurrence_count",
                "current": 0,
                "passing_max": 0,
                "required_reduction": 0,
                "unit": "occurrence_count",
            },
            source_guard_passed=False,
            guard_reason=(
                blocker_code
                if plan_overrides
                else "SEMANTIC_CONTRACT_REGRESSION"
            ),
            semantic_regressions=([] if plan_overrides else [blocker_code]),
            plan_next_action="generic plan text that must not hide the blocker",
            plan_overrides=plan_overrides,
        )
        action = typed["next_action"]
        assert blocker_code in action, (blocker_code, action)
        for phrase in expected_phrases:
            assert phrase in action, (blocker_code, phrase, action)

    unknown_contract = _run_case(
        language="python",
        smell="long_method",
        budget={},
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        semantic_regressions=["UNRECOGNIZED_FROZEN_CONTRACT"],
        plan_next_action="preserve the frozen contract and repair the failure",
    )
    assert "UNRECOGNIZED_FROZEN_CONTRACT" in unknown_contract["next_action"], (
        unknown_contract
    )
    assert (
        "preserve the frozen contract and repair the failure"
        in unknown_contract["next_action"]
    ), unknown_contract

    many_codes = [f"STRUCTURAL_BLOCKER_{index:02d}" for index in range(20)]
    bounded_codes = _run_case(
        language="python",
        smell="long_method",
        budget={},
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        semantic_regressions=many_codes + many_codes[:3],
        plan_next_action="repair the reported frozen structural blockers",
    )
    bounded_observation = bounded_codes[
        "source_guard_feedback"
    ]["progress_observation"]
    assert bounded_observation["structural_failure_count"] == 20, bounded_codes
    assert bounded_observation["blocker_codes"] == many_codes[:8], bounded_codes
    assert len(json.dumps(bounded_codes).encode("utf-8")) < (
        smell_bridge.DECISION_MAX_BYTES
    )

    full_pack = smell_bridge._build_failure_pack(
        {
            "status": "SMELL_GUARD_FAILED",
            "smell_guard": {
                "success": False,
                "failure_count": 1,
                "results": [{
                    "type": "code_clone_type1",
                    "success": False,
                    "message": (
                        "code_clone_type1 checkpoint contract: the refactoring "
                        "violated a smell-specific structural contract."
                    ),
                    "details": {"reason": "SEMANTIC_CONTRACT_REGRESSION"},
                }],
            },
            "checkpoint": {
                "required": True,
                "resolution_plan": {
                    "metric_budget": [{
                        "metric": "clone_token_count",
                        "current": 0,
                        "passing_max": 16,
                        "required_reduction": 0,
                        "unit": "clone_token_count",
                    }],
                    "semantic_regressions": [
                        "CLONE_TARGET_DECLARATION_IDENTITY_FAILED"
                    ],
                    "next_action": (
                        "restore both frozen target declarations as thin wrappers "
                        "over one shared helper"
                    ),
                },
            },
        },
        {},
        smell="code_clone_type1",
    )
    assert full_pack["source_guard_feedback"] == structural_feedback, full_pack
    compact_full_pack = smell_bridge._compact_failure_pack(full_pack)
    assert compact_full_pack["source_guard_feedback"]["progress_observation"] == (
        structural_feedback["progress_observation"]
    ), compact_full_pack
    assert full_pack["next_action"] == structural["next_action"], full_pack
    assert len(json.dumps(full_pack).encode("utf-8")) < smell_bridge.DECISION_MAX_BYTES

    huge = "X" * (smell_bridge.DECISION_MAX_BYTES * 2)
    bounded = _run_case(
        language="python",
        smell="code_clone_type1",
        budget={
            "metric": "clone_token_count",
            "current": 0,
            "passing_max": 16,
            "required_reduction": 0,
            "unit": "clone_token_count",
        },
        source_guard_passed=False,
        guard_reason="SEMANTIC_CONTRACT_REGRESSION",
        guard_message=huge,
        semantic_regressions=[huge],
        plan_next_action=huge,
    )
    assert len(json.dumps(bounded).encode("utf-8")) < smell_bridge.DECISION_MAX_BYTES

    original_resolve = smell_bridge._resolve
    original_checkpoint = smell_bridge._checkpoint_context
    checkpoint_calls: list[str] = []
    try:
        for language, smell, location in (
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
        "calls avoid project_full; scalar and structural feedback are typed and "
        "bounded; Java uses controller-sealed source gate; noncheckpoint bypass"
    )


if __name__ == "__main__":
    main()
