#!/usr/bin/env python3
"""Contract-level regression checks shared by all migrated smell adapters."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))
sys.path.insert(0, str(ROOT / "runtime" / "python" / "bridge"))

from smell_core.checkpoint_adapters import CHECKPOINT_SMELLS  # noqa: E402
from smell_core.checkpoint_contract import (  # noqa: E402
    checkpoint_feedback_highlights,
    checkpoint_gate_result,
    evaluate_checkpoint_contract,
)
from smell_core.checkpoints import _partial_checkpoint_rank  # noqa: E402
from smell_bridge import _build_failure_pack  # noqa: E402


EXPECTED = {
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
}


def main() -> int:
    assert CHECKPOINT_SMELLS == EXPECTED, CHECKPOINT_SMELLS
    baseline = {"ok": True, "objectives": {"primary": 10, "secondary": 3}}

    unchanged = evaluate_checkpoint_contract(baseline, baseline, has_production_diff=False)
    assert unchanged.reason == "EDIT_REQUIRED" and not unchanged.metric_progress

    cosmetic = evaluate_checkpoint_contract(baseline, baseline, has_production_diff=True)
    assert cosmetic.reason == "NO_STRUCTURAL_PROGRESS" and not cosmetic.metric_progress

    zero = evaluate_checkpoint_contract(
        {"ok": True, "objectives": {"primary": 0}},
        {"ok": True, "objectives": {"primary": 0}},
        has_production_diff=True,
    )
    assert zero.reason == "BASELINE_METRIC_UNAVAILABLE" and not zero.metric_available

    improved = evaluate_checkpoint_contract(
        baseline,
        {"ok": True, "objectives": {"primary": 9, "secondary": 4}},
        has_production_diff=True,
    )
    assert improved.reason == "METRIC_PROGRESS" and improved.metric_progress

    # target_missing is a measurement failure, not a reduction: for every
    # smell except the absence-goal pair it must fail closed.
    missing = {"ok": True, "target_missing": True, "objectives": {"primary": 0, "secondary": 0}}
    for smell in sorted(EXPECTED - {"dead_code", "mysterious_name"}):
        verdict = evaluate_checkpoint_contract(baseline, missing, has_production_diff=True, smell=smell)
        assert verdict.reason == "TARGET_NOT_LOCATED" and not verdict.metric_progress, (smell, verdict.reason)
    for smell in ("dead_code", "mysterious_name"):
        verdict = evaluate_checkpoint_contract(baseline, missing, has_production_diff=True, smell=smell)
        assert verdict.reason == "METRIC_PROGRESS" and verdict.metric_progress, (smell, verdict.reason)
    gate = checkpoint_gate_result(
        "long_parameter_list",
        {"checkpoint_id": "c002", "adapter": "long_parameter_list", "delta": evaluate_checkpoint_contract(
            baseline, missing, has_production_diff=True, smell="long_parameter_list").to_dict()},
    )
    # TARGET_NOT_LOCATED is a measurement state, not a smell verdict: the
    # contract stays silent and the strict guard arbitrates.
    assert gate is None, gate

    for smell in sorted(EXPECTED):
        checkpoint = {
            "checkpoint_id": "c001",
            "adapter": smell,
            "production_diff": False,
            "baseline_metrics": baseline,
            "current_metrics": baseline,
            "delta": unchanged.to_dict(),
        }
        failure = checkpoint_gate_result(smell, checkpoint)
        assert failure is not None and failure["details"]["reason"] == "EDIT_REQUIRED", failure

    feedback = checkpoint_feedback_highlights({
        "required": True,
        "delta": evaluate_checkpoint_contract(
            baseline,
            {"ok": True, "objectives": {"primary": 9, "secondary": 3}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert feedback == [
        "CHECKPOINT reason=METRIC_PROGRESS production_diff=true metric_progress=true",
        "CHECKPOINT_OBJECTIVES primary:10->9(reduction=1); secondary:3->3(reduction=0)",
    ], feedback

    assert checkpoint_feedback_highlights(None) == []

    feature_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "feature_envy",
        "current_metrics": {
            "expected_receiver_type": "CanalParameter",
            "expected_receiver_access": 9,
        },
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"expected_receiver_access": 9}},
            {"ok": True, "objectives": {"expected_receiver_access": 9}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert "receiver=CanalParameter still has 9 accesses" in feature_feedback[-1], feature_feedback

    clump_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "data_clumps",
        "current_metrics": {
            "passing_max": 2,
            "remaining_reductions": 3,
            "occurrences": [{"file": "src/Tile.java", "method": "setTile(Tile, Block, Team)"}],
        },
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"occurrence_count": 5}},
            {"ok": True, "objectives": {"occurrence_count": 5}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert "reduce 3 more occurrence(s) to <=2" in clump_feedback[-1], clump_feedback
    assert "Tile.java#setTile" in clump_feedback[-1], clump_feedback

    saved_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "data_clumps",
        "current_is_best_partial": True,
        "best_partial": {
            "checkpoint_id": "c003",
            "smell_guard_success": False,
            "build_test_success": None,
            "production_patch": ".smell-artifacts/checkpoints/task/c003-verify/production.patch",
        },
        "current_metrics": {"objectives": {"occurrence_count": 3}},
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"occurrence_count": 6}},
            {"ok": True, "objectives": {"occurrence_count": 3}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert "BEST_PARTIAL c003 saved" in saved_feedback[-1], saved_feedback

    regressed_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "data_clumps",
        "regressed_from_best_partial": True,
        "best_partial": {
            "checkpoint_id": "c003",
            "smell_guard_success": True,
            "build_test_success": False,
            "production_patch": ".smell-artifacts/checkpoints/task/c003-verify/production.patch",
        },
        "current_metrics": {"objectives": {"occurrence_count": 4}},
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"occurrence_count": 6}},
            {"ok": True, "objectives": {"occurrence_count": 4}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert "already clears the smell" in regressed_feedback[-1], regressed_feedback
    assert "repair only the build/test regression" in regressed_feedback[-1], regressed_feedback

    rank = _partial_checkpoint_rank(
        {
            "production_diff": True,
            "delta": {
                "metric_progress": True,
                "target_missing": False,
                "objectives": {
                    "primary": {"relative_reduction": 0.5},
                    "secondary": {"relative_reduction": -0.1},
                },
            },
        },
        {"smell_guard": {"success": False}},
    )
    assert rank == (0, 0.4, 1), rank

    failure_pack = _build_failure_pack({
        "status": "SMELL_GUARD_FAILED",
        "smell_guard": {
            "success": False,
            "results": [{
                "type": "long_method",
                "success": False,
                "message": (
                    "long_method guard: Java AST still reports a/very/long/path/to/Example.java#get "
                    "with enough explanatory context to require compacting while preserving the "
                    "actionable suffix AST-NCSS 61 (threshold 60)."
                ),
            }],
        },
        "checkpoint": {
            "required": True,
            "delta": evaluate_checkpoint_contract(
                baseline,
                {"ok": True, "objectives": {"primary": 9, "secondary": 3}},
                has_production_diff=True,
            ).to_dict() | {"has_production_diff": True},
        },
    }, {})
    highlights = failure_pack["highlights"]
    assert highlights[0].startswith("GUARD_TARGET long_method guard:"), highlights
    assert highlights[0].endswith("AST-NCSS 61 (threshold 60)."), highlights
    assert len(highlights[0]) <= 190, highlights
    assert highlights[1:3] == feedback, failure_pack

    structural_pack = _build_failure_pack({
        "status": "SMELL_GUARD_FAILED",
        "smell_guard": {
            "success": False,
            "results": [{
                "type": "refused_bequest",
                "success": False,
                "message": (
                    "refused_bequest guard: the reported parent capability is still "
                    "inherited and still exposes the target method."
                ),
                "details": {
                    "structural_expectation": "capability_split",
                    "capability_profile": {
                        "ok": True,
                        "target_class": "example.ReadOnlyPacket",
                        "method": "toBytes",
                        "reported_parent": "Packet",
                        "inherits_reported_parent": True,
                        "child_declares_target": True,
                        "parent_declares_target": True,
                        "capability_split_satisfied": False,
                    },
                },
            }],
        },
    }, {})
    assert structural_pack["failure_category"] == "STRUCTURAL_ROUTE_MISMATCH", structural_pack
    assert structural_pack["failure_group"] == "smell", structural_pack
    assert structural_pack["retryable"] is True, structural_pack
    assert structural_pack["highlights"][0].startswith(
        "CAPABILITY_SPLIT_REQUIRED target=example.ReadOnlyPacket"
    ), structural_pack
    assert "implement or delegate" in structural_pack["recommendations"][0], structural_pack

    invalid_test_evidence_pack = _build_failure_pack({
        "status": "SAMPLE_TEST_FAILED",
        "build_test_guard": {
            "success": False,
            "details": {
                "test": {
                    "success": False,
                    "status": "test_not_executed",
                    "returncode": 0,
                    "failure_highlights": [
                        "Pinned sample test location does not identify a test class.",
                    ],
                },
            },
        },
    }, {})
    assert invalid_test_evidence_pack["failure_category"] == "SAMPLE_TEST_EVIDENCE_INVALID", (
        invalid_test_evidence_pack
    )
    assert invalid_test_evidence_pack["failure_group"] == "", invalid_test_evidence_pack
    assert invalid_test_evidence_pack["retryable"] is False, invalid_test_evidence_pack

    smell_before_invalid_test_pack = _build_failure_pack({
        "status": "SAMPLE_TEST_FAILED",
        "smell_guard": {
            "success": False,
            "results": [{
                "type": "code_clone_type1",
                "success": False,
                "message": "code clone helpers are still duplicated.",
            }],
        },
        "build_test_guard": {
            "success": False,
            "details": {
                "test": {
                    "success": False,
                    "status": "test_not_executed",
                    "returncode": 0,
                    "failure_highlights": [
                        "Pinned sample test location does not identify a test class.",
                    ],
                },
            },
        },
    }, {})
    assert smell_before_invalid_test_pack["failure_category"] == "SMELL_GUARD_FAILED", (
        smell_before_invalid_test_pack
    )
    assert smell_before_invalid_test_pack["retryable"] is True, smell_before_invalid_test_pack

    checkpoint_only_structural_pack = _build_failure_pack({
        "status": "SMELL_GUARD_FAILED",
        "smell_guard": {
            "success": False,
            "results": [{
                "type": "refused_bequest",
                "success": False,
                "message": (
                    "refused_bequest checkpoint contract: production source changed, "
                    "but no checkpoint objective decreased."
                ),
                "details": {
                    "detector": "checkpoint_contract",
                    "reason": "NO_STRUCTURAL_PROGRESS",
                },
            }],
        },
    }, {}, smell="refused_bequest", evidence=(
        "parents=IPacket; structural_expectation=capability_split; "
        "refactor_path=split_readable_packets_from_writable_packets"
    ))
    assert (
        checkpoint_only_structural_pack["failure_category"]
        == "STRUCTURAL_ROUTE_MISMATCH"
    ), checkpoint_only_structural_pack
    assert checkpoint_only_structural_pack["failure_group"] == "smell", (
        checkpoint_only_structural_pack
    )

    print(f"checkpoint-contract-self-check PASS smells={len(EXPECTED)} unchanged_pass=0 strict_decrease=PASS feedback=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
