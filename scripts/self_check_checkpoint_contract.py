#!/usr/bin/env python3
"""Contract-level regression checks shared by all migrated smell adapters."""
from __future__ import annotations

import json
import sys
import tempfile
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
from smell_core.checkpoints import (  # noqa: E402
    _partial_checkpoint_rank,
    checkpoint_task_root,
    finalize_checkpoint,
)
from smell_core.resolution_plan import build_resolution_plan  # noqa: E402
from smell_bridge import (  # noqa: E402
    VERIFY_DECISION_SCHEMA,
    _build_failure_pack,
    _verify_decision_payload,
    _verify_status,
)


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


_LARGE_PAYLOAD_SENTINEL = "HEAVY_CLONE_EVIDENCE_MUST_STAY_IN_FULL_ARTIFACT"
_MAX_DECISION_BYTES = 64 * 1024
_MAX_FAILURE_HIGHLIGHTS = 3
_MAX_FAILURE_HIGHLIGHT_CHARS = 512


def _large_clone_failure_payload() -> dict[str, object]:
    """Build detector evidence large enough to expose accidental full-payload reuse."""
    clone_catalog = [
        {
            "fingerprint": f"clone-{index:04d}",
            "methods": [
                f"src/main/java/example/Owner{index:04d}.java#left()",
                f"src/main/java/example/Owner{index:04d}.java#right()",
            ],
            "token_count": 96,
            "normalized_tokens": (
                [_LARGE_PAYLOAD_SENTINEL, *("token" for _ in range(127))]
                if index == 0
                else ["token" for _ in range(128)]
            ),
        }
        for index in range(1_024)
    ]
    worklist = [
        {
            "kind": "clone_endpoint",
            "file": f"src/main/java/example/Owner{index:04d}.java",
            "method": "left()" if index % 2 == 0 else "right()",
        }
        for index in range(1_024)
    ]
    test_manifest = {
        f"src/test/java/example/Owner{index:04d}Test.java": f"sha256-{index:064d}"
        for index in range(512)
    }
    return {
        "success": False,
        "accepted": False,
        "progress": False,
        "status": "SMELL_GUARD_FAILED",
        "resolution": "unresolved",
        "smell_guard": {
            "success": False,
            "failure_count": 1,
            "results": [{
                "type": "code_clone_type1",
                "success": False,
                "message": "The frozen exact-clone finding remains present.",
                "details": {
                    "detector": "checkpoint_contract",
                    "reason": "NO_STRUCTURAL_PROGRESS",
                    "current_metrics": {"clone_catalog": clone_catalog},
                },
            }],
        },
        "checkpoint": {
            "required": True,
            "checkpoint_id": "c001",
            "adapter": "code_clone_type1",
            "baseline_metrics": {"clone_catalog": clone_catalog},
            "current_metrics": {"clone_catalog": clone_catalog},
            "delta": {
                "reason": "NO_STRUCTURAL_PROGRESS",
                "has_production_diff": True,
                "metric_progress": False,
                "objectives": {
                    "clone_token_count": {
                        "before": 96,
                        "after": 96,
                        "absolute_reduction": 0,
                    },
                },
            },
            "resolution_plan": {
                "route_family": "consolidate-frozen-clone-pair",
                "next_action": "Consolidate the frozen clone endpoints through one shared implementation.",
                "worklist": worklist,
            },
            "test_change_contract": {
                "mode": "frozen",
                "files": test_manifest,
            },
        },
    }


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key) for key in value),
            *(nested for item in value.values() for nested in _nested_keys(item)),
        }
    if isinstance(value, (list, tuple)):
        return {nested for item in value for nested in _nested_keys(item)}
    return set()


def _check_large_clone_failure_pack_is_bounded() -> None:
    with tempfile.TemporaryDirectory(prefix="large-clone-decision-") as temp_dir:
        verify_full = Path(temp_dir) / "verify.full.json"
        build_log = Path(temp_dir) / "build.log"
        build_log.write_text("build passed\n", encoding="utf-8")
        artifact_paths = {
            "verify_full": str(verify_full),
            "build_log": str(build_log),
        }
        payload = _large_clone_failure_payload()
        payload_bytes = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
        assert payload_bytes > 1_000_000, payload_bytes

        failure_pack = _build_failure_pack(
            payload,
            artifact_paths,
            smell="code_clone_type1",
        )
        rendered = json.dumps(failure_pack, separators=(",", ":"), ensure_ascii=True)
        highlights = failure_pack.get("highlights") or []

        assert len(rendered.encode("utf-8")) < _MAX_DECISION_BYTES, (
            f"large clone failure_pack exceeded {_MAX_DECISION_BYTES} bytes"
        )
        assert isinstance(highlights, list), type(highlights)
        assert len(highlights) <= _MAX_FAILURE_HIGHLIGHTS, len(highlights)
        assert all(
            isinstance(item, str) and len(item) <= _MAX_FAILURE_HIGHLIGHT_CHARS
            for item in highlights
        ), [len(item) if isinstance(item, str) else type(item).__name__ for item in highlights]
        assert _LARGE_PAYLOAD_SENTINEL not in rendered
        assert failure_pack.get("artifact_paths") == artifact_paths, failure_pack.get("artifact_paths")

        # The complete detector evidence remains readable in the durable
        # artifact; only the stdout decision is projected.
        payload["failure_pack"] = failure_pack
        verify_full.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        decision = _verify_decision_payload(payload, artifact_paths)
        decision_json = json.dumps(decision, separators=(",", ":"), ensure_ascii=True)
        decision_keys = _nested_keys(decision)
        forbidden_heavy_keys = {
            "clone_catalog",
            "implementation_catalog",
            "normalized_tokens",
            "occurrence_catalog",
            "project_finding_catalog",
            "receiver_access_worklist",
            "semantic_audit",
            "test_change_contract",
            "verification_config_files",
            "worklist",
        }

        assert decision["schema_version"] == VERIFY_DECISION_SCHEMA, decision["schema_version"]
        assert len(decision_json.encode("utf-8")) < _MAX_DECISION_BYTES
        assert _LARGE_PAYLOAD_SENTINEL not in decision_json
        assert forbidden_heavy_keys.isdisjoint(decision_keys), (
            forbidden_heavy_keys & decision_keys
        )
        assert decision["artifacts"] == artifact_paths, decision["artifacts"]
        assert decision["artifact_index"]["verify_full"]["path"] == str(verify_full)
        assert decision["artifact_index"]["verify_full"]["bytes"] == verify_full.stat().st_size
        assert decision["failure_fingerprint"], decision
        assert (
            decision["success"],
            decision["accepted"],
            decision["progress"],
            decision["status"],
            decision["resolution"],
        ) == (False, False, False, "SMELL_GUARD_FAILED", "unresolved")

        persisted = json.loads(verify_full.read_text(encoding="utf-8"))
        assert _LARGE_PAYLOAD_SENTINEL in verify_full.read_text(encoding="utf-8")
        assert len(persisted["checkpoint"]["current_metrics"]["clone_catalog"]) == 1_024
        assert len(persisted["checkpoint"]["resolution_plan"]["worklist"]) == 1_024

        # The projector is shared by every smell.  A non-Clone PASS and
        # IMPROVED outcome must keep the same atomic status semantics without
        # exposing an arbitrary detector worklist.
        for status, success, accepted, progress, resolution in (
            ("PASS", True, True, True, "resolved"),
            ("IMPROVED", False, False, True, "improved"),
        ):
            non_clone = {
                "success": success,
                "accepted": accepted,
                "progress": progress,
                "status": status,
                "resolution": resolution,
                "smell_guard": {
                    "success": success,
                    "failure_count": 0 if success else 1,
                    "results": [{
                        "type": "long_method",
                        "success": success,
                        "message": "long-method decision fixture",
                    }],
                },
                "checkpoint": {
                    "required": True,
                    "checkpoint_id": "c002",
                    "adapter": "long_method",
                    "current_metrics": {
                        "finding_present": not success,
                        "objectives": {"ast_ncss": 3 if success else 61},
                        "receiver_access_worklist": [_LARGE_PAYLOAD_SENTINEL],
                    },
                    "delta": {
                        "reason": "RESOLVED" if success else "FINDING_REMAINS",
                        "has_production_diff": True,
                        "metric_progress": True,
                        "objectives": {
                            "ast_ncss": {
                                "before": 66,
                                "after": 3 if success else 61,
                                "absolute_reduction": 63 if success else 5,
                            },
                        },
                    },
                    "resolution_plan": {
                        "next_action": "finish the remaining method extraction",
                        "worklist": [_LARGE_PAYLOAD_SENTINEL],
                    },
                },
            }
            projected = _verify_decision_payload(non_clone, artifact_paths)
            projected_json = json.dumps(projected, separators=(",", ":"), ensure_ascii=True)
            assert len(projected_json.encode("utf-8")) < _MAX_DECISION_BYTES
            assert _LARGE_PAYLOAD_SENTINEL not in projected_json
            assert forbidden_heavy_keys.isdisjoint(_nested_keys(projected))
            assert (
                projected["success"],
                projected["accepted"],
                projected["progress"],
                projected["status"],
                projected["resolution"],
            ) == (success, accepted, progress, status, resolution)


def main() -> int:
    assert CHECKPOINT_SMELLS == EXPECTED, CHECKPOINT_SMELLS

    objective_fixtures = {
        "long_method": {"ast_ncss": 74},
        "nested_complexity": {"cognitive_complexity": 25},
        "long_parameter_list": {"parameter_count": 8},
        "feature_envy": {"envy_access_diff": 7, "expected_receiver_access": 9},
        "data_clumps": {"occurrence_count": 5},
        "code_clone_type1": {"clone_token_count": 42},
        "god_class": {"nom": 30, "nof": 25, "wmc": 80, "loc": 700, "atfd": 8},
        "refused_bequest": {"refusal_finding_present": 1, "rejection_signals": 1},
        "switch_statements": {"switch_count": 2},
        "mysterious_name": {"target_suspicious_name_present": 1},
        "dead_code": {"unused_private_finding_present": 1},
    }
    for smell, objectives in objective_fixtures.items():
        metrics = {
            "ok": True,
            "finding_present": True,
            "objectives": objectives,
            "detector_profile": {"finding_min": 1},
        }
        if smell == "data_clumps":
            metrics.update({"passing_max": 2, "remaining_reductions": 3})
        plan = build_resolution_plan(
            smell,
            finding_contract={"finding_id": f"finding-{smell}", "entity_identity": {"method": "target"}},
            baseline_metrics=metrics,
        )
        assert plan["route_family"] != "close-frozen-finding", plan
        assert plan["next_action"], plan
        assert plan["authority"] == "target_guard_and_frozen_checkpoint", plan

    resolved_envy = build_resolution_plan(
        "feature_envy",
        finding_contract={"finding_id": "envy", "entity_identity": {"method": "target"}},
        baseline_metrics={
            "ok": True,
            "finding_present": True,
            "objectives": {"envy_access_diff": 4, "expected_receiver_access": 8},
            "detector_profile": {"finding_min_exclusive": 1},
        },
        current_metrics={
            "ok": True,
            "finding_present": False,
            "objectives": {"envy_access_diff": 1, "expected_receiver_access": 8},
            "detector_profile": {"finding_min_exclusive": 1},
        },
    )
    assert resolved_envy["resolved"] is True, resolved_envy
    assert resolved_envy["remaining_work_count"] == 0, resolved_envy
    assert [item["name"] for item in resolved_envy["objective_deficits"]] == [
        "envy_access_diff"
    ], resolved_envy

    large_lpl = build_resolution_plan(
        "long_parameter_list",
        finding_contract={"finding_id": "lpl", "entity_identity": {"method": "target"}},
        baseline_metrics={
            "ok": True,
            "finding_present": True,
            "objectives": {"parameter_count": 8},
            "detector_profile": {"finding_min": 6},
            "migration_closure": {
                "declarations": [{"file": "src/Api.java", "line": 5, "signature": "target(A,B,C,D,E,F,G,H)"}],
                "production_call_sites": [
                    {"file": f"src/Caller{index}.java", "line": index + 10, "signature": "target(A,B,C,D,E,F,G,H)"}
                    for index in range(100)
                ],
            },
        },
    )
    assert large_lpl["remaining_work_count"] == 101, large_lpl
    assert large_lpl["worklist_complete"] is False, large_lpl
    assert len(large_lpl["worklist"]) == 33, large_lpl
    assert "src/Api.java:5" in large_lpl["next_action"], large_lpl

    god_plan = build_resolution_plan(
        "god_class",
        finding_contract={"finding_id": "god", "entity_identity": {"class": "LedgerService"}},
        baseline_metrics={
            "ok": True,
            "finding_present": True,
            "objectives": {"nom": 20, "nof": 12, "wmc": 70, "loc": 500, "atfd": 5},
            "responsibility_clusters": [{
                "rank": 1,
                "cluster_id": "god-responsibility-orders",
                "kind": "state_and_behavior",
                "method_count": 3,
                "field_count": 2,
                "nom_reduction": 3,
                "nof_reduction": 2,
                "wmc_reduction": 18,
                "loc_reduction": 120,
                "cohesion": 0.75,
                "fields": [{"name": "orders"}, {"name": "ledger"}],
                "methods": [{"signature": "post(Order)"}, {"signature": "settle()"}],
            }],
            "god_class_profile": {
                "mandatory": [],
                "signals": [],
            },
        },
    )
    assert "god-responsibility-orders" in god_plan["next_action"], god_plan
    assert "post(Order)" in god_plan["next_action"], god_plan
    assert god_plan["worklist"][1]["field_names"] == ["orders", "ledger"], god_plan

    evidence_missing_build_test = {
        "success": False,
        "verification_mode": "sample_optimized",
        "details": {
            "build": {"success": True, "returncode": 0},
            "test": {
                "success": False,
                "status": "test_not_executed",
                "returncode": 0,
            },
        },
    }
    assert _verify_status(
        False,
        {"success": True},
        evidence_missing_build_test,
    ) == "SAMPLE_TEST_EVIDENCE_MISSING"
    assert _verify_status(
        False,
        {"success": True},
        {
            **evidence_missing_build_test,
            "details": {
                **evidence_missing_build_test["details"],
                "test": {
                    "success": False,
                    "status": "failed",
                    "returncode": 1,
                },
            },
        },
    ) == "SAMPLE_TEST_FAILED"
    assert _verify_status(
        False,
        {"success": False},
        {"success": True, "verification_mode": "sample_optimized"},
        improvement_pass=True,
    ) == "IMPROVED"
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

    detector_unavailable_current = {
        "ok": False,
        "candidate_count": 0,
        "finding_present": False,
        "objectives": {"primary": 0, "secondary": 0},
    }
    detector_unavailable = evaluate_checkpoint_contract(
        baseline,
        detector_unavailable_current,
        has_production_diff=True,
    )
    assert (
        detector_unavailable.reason == "CURRENT_DETECTOR_UNAVAILABLE"
        and not detector_unavailable.metric_progress
    ), detector_unavailable
    unavailable_plan = build_resolution_plan(
        "long_method",
        finding_contract={"finding_id": "unavailable", "entity_identity": {"method": "target"}},
        baseline_metrics=baseline,
        current_metrics=detector_unavailable_current,
        delta=detector_unavailable.to_dict(),
    )
    assert unavailable_plan["resolved"] is False, unavailable_plan
    assert unavailable_plan["detector_blocker"] == "CURRENT_DETECTOR_UNAVAILABLE", unavailable_plan
    assert "restore target Guard availability" in unavailable_plan["next_action"], unavailable_plan
    unavailable_gate = checkpoint_gate_result(
        "long_method",
        {
            "required": True,
            "checkpoint_id": "c-unavailable",
            "adapter": "long_method",
            "production_diff": True,
            "baseline_metrics": baseline,
            "current_metrics": detector_unavailable_current,
            "delta": detector_unavailable.to_dict(),
        },
    )
    assert unavailable_gate is not None, unavailable_gate
    assert (
        unavailable_gate["details"]["reason"]
        == "CURRENT_DETECTOR_UNAVAILABLE"
    ), unavailable_gate

    ambiguous_current = {
        "ok": True,
        "candidate_count": 2,
        "finding_present": False,
        "objectives": {"primary": 0, "secondary": 0},
    }
    ambiguous = evaluate_checkpoint_contract(
        baseline,
        ambiguous_current,
        has_production_diff=True,
    )
    assert ambiguous.reason == "TARGET_AMBIGUOUS" and not ambiguous.metric_progress, ambiguous
    ambiguous_gate = checkpoint_gate_result(
        "long_method",
        {
            "required": True,
            "checkpoint_id": "c-ambiguous",
            "adapter": "long_method",
            "production_diff": True,
            "baseline_metrics": baseline,
            "current_metrics": ambiguous_current,
            "delta": ambiguous.to_dict(),
        },
    )
    assert ambiguous_gate is not None, ambiguous_gate
    assert ambiguous_gate["details"]["reason"] == "TARGET_AMBIGUOUS", ambiguous_gate
    ambiguous_plan = build_resolution_plan(
        "long_method",
        finding_contract={"finding_id": "ambiguous", "entity_identity": {"method": "target"}},
        baseline_metrics=baseline,
        current_metrics=ambiguous_current,
        delta=ambiguous.to_dict(),
    )
    assert ambiguous_plan["resolved"] is False, ambiguous_plan
    assert ambiguous_plan["detector_blocker"] == "TARGET_AMBIGUOUS", ambiguous_plan

    # target_missing is a measurement failure, not a reduction: for every
    # smell except the absence-goal pair it must fail closed.
    missing = {"ok": True, "target_missing": True, "objectives": {"primary": 0, "secondary": 0}}
    for smell in sorted(EXPECTED):
        verdict = evaluate_checkpoint_contract(baseline, missing, has_production_diff=True, smell=smell)
        assert verdict.reason == "TARGET_NOT_LOCATED" and not verdict.metric_progress, (smell, verdict.reason)
    for smell in ("dead_code", "mysterious_name"):
        verdict = evaluate_checkpoint_contract(
            baseline,
            {**missing, "target_absence_allowed": True},
            has_production_diff=True,
            smell=smell,
        )
        assert verdict.reason == "METRIC_PROGRESS" and verdict.metric_progress, (smell, verdict.reason)
    gate = checkpoint_gate_result(
        "long_parameter_list",
        {"checkpoint_id": "c002", "adapter": "long_parameter_list", "delta": evaluate_checkpoint_contract(
            baseline, missing, has_production_diff=True, smell="long_parameter_list").to_dict()},
    )
    # There is no second guard to guess whether a missing target was a legal
    # migration. The finding contract fails closed unless a smell-specific
    # adapter proves an allowed absence transition.
    assert gate is not None, gate
    assert gate["details"]["reason"] == "TARGET_NOT_LOCATED", gate

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

    feature_current = {
        "ok": True,
        "finding_present": True,
        "objectives": {"envy_access_diff": 7, "expected_receiver_access": 9},
        "detector_profile": {"finding_min_exclusive": 1},
        "expected_receiver_type": "CanalParameter",
        "expected_receiver_access": 9,
        "receiver_access_worklist": [{
            "file": "src/CanalService.java",
            "line": 42,
            "member": "flowRate",
        }],
    }
    feature_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "feature_envy",
        "current_metrics": feature_current,
        "resolution_plan": build_resolution_plan(
            "feature_envy",
            finding_contract={"finding_id": "feature", "entity_identity": {"method": "target"}},
            baseline_metrics=feature_current,
        ),
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"envy_access_diff": 7, "expected_receiver_access": 9}},
            {"ok": True, "objectives": {"envy_access_diff": 7, "expected_receiver_access": 9}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert "envy_access_diff from 7 to <= 1" in feature_feedback[-1], feature_feedback
    assert "do not force raw receiver access to zero" in feature_feedback[-1], feature_feedback
    assert "src/CanalService.java:42" in feature_feedback[-1], feature_feedback

    clump_current = {
        "ok": True,
        "finding_present": True,
        "objectives": {"occurrence_count": 5},
        "passing_max": 2,
        "remaining_reductions": 3,
        "occurrences": [{"file": "src/Tile.java", "method": "setTile(Tile, Block, Team)"}],
    }
    clump_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "data_clumps",
        "current_metrics": clump_current,
        "resolution_plan": build_resolution_plan(
            "data_clumps",
            finding_contract={"finding_id": "clump", "entity_identity": {"group": "int:x|string:y|boolean:z"}},
            baseline_metrics=clump_current,
        ),
        "delta": evaluate_checkpoint_contract(
            {"ok": True, "objectives": {"occurrence_count": 5}},
            {"ok": True, "objectives": {"occurrence_count": 5}},
            has_production_diff=True,
        ).to_dict() | {"has_production_diff": True},
    })
    assert (
            "migrate at least 3 occurrence(s) from the frozen scoped occurrence witness"
        in clump_feedback[-1]
    ), clump_feedback

    saved_feedback = checkpoint_feedback_highlights({
        "required": True,
        "adapter": "data_clumps",
        "current_is_best_partial": True,
        "best_partial": {
            "checkpoint_id": "c003",
            "smell_guard_success": False,
            "build_test_success": True,
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
            "build_test_success": True,
            "delta": {
                "metric_progress": True,
                "target_missing": False,
                "objectives": {
                    "primary": {"relative_reduction": 0.5},
                    "secondary": {"relative_reduction": -0.1},
                },
            },
        },
        {"resolution": "improved", "smell_guard": {"success": False}},
    )
    assert rank == (1, 0.4, 1), rank
    resolved_rank = _partial_checkpoint_rank(
        {
            "production_diff": True,
            "build_test_success": True,
            "delta": {
                "metric_progress": True,
                "target_missing": False,
                "objectives": {
                    "primary": {"relative_reduction": 0.1},
                },
            },
        },
        {"resolution": "resolved", "smell_guard": {"success": True}},
    )
    assert resolved_rank == (2, 0.1, 1), resolved_rank
    invalid_rank = _partial_checkpoint_rank(
        {
            "production_diff": True,
            "build_test_success": False,
            "delta": {
                "metric_progress": True,
                "target_missing": False,
                "objectives": {
                    "primary": {"relative_reduction": 0.9},
                },
            },
        },
        {"resolution": "resolved", "smell_guard": {"success": True}},
    )
    assert invalid_rank is None, invalid_rank
    missing_resolution_rank = _partial_checkpoint_rank(
        {
            "production_diff": True,
            "build_test_success": True,
            "delta": {
                "metric_progress": True,
                "target_missing": False,
                "objectives": {
                    "primary": {"relative_reduction": 0.9},
                },
            },
        },
        {"smell_guard": {"success": True}},
    )
    assert missing_resolution_rank is None, missing_resolution_rank

    # A legacy caller cannot turn success/smell_guard into an implicit resolved
    # checkpoint. finalize must retain the audit record but make it neither best
    # nor restorable when the explicit resolution state is absent.
    with tempfile.TemporaryDirectory(prefix="checkpoint-resolution-") as temp_dir:
        project = Path(temp_dir)
        location = "Fixture.java:method=target|line=1"
        task_root = checkpoint_task_root(project, "long_method", location)
        verify_dir = task_root / "c001-verify"
        verify_dir.mkdir(parents=True)
        (verify_dir / "manifest.json").write_text(
            json.dumps({
                "checkpoint_id": "c001",
                "production_diff": True,
                "current_metrics": {"objectives": {"primary": 1}},
                "delta": {
                    "metric_progress": True,
                    "target_missing": False,
                    "objectives": {
                        "primary": {"relative_reduction": 0.9},
                    },
                },
            }),
            encoding="utf-8",
        )
        (task_root / "task-state.json").write_text(
            json.dumps({"latest": "c001", "next_sequence": 2}),
            encoding="utf-8",
        )
        finalized = finalize_checkpoint(
            project,
            "long_method",
            location,
            "c001",
            {
                "success": True,
                "progress": True,
                "status": "PASS",
                "smell_guard": {"success": True},
                "build_test_guard": {"success": True},
            },
        )
        assert finalized is not None, finalized
        assert finalized["resolution"] == "", finalized
        assert finalized["accepted"] is False, finalized
        assert finalized["best_checkpoint"] is False, finalized
        assert finalized["best_partial_eligible"] is False, finalized
        assert finalized["restorable"] is False, finalized

        # A Java checkpoint cannot become accepted from a superficially
        # resolved payload that omitted its mandatory behavior gate.
        (verify_dir / "manifest.json").write_text(
            json.dumps({
                "checkpoint_id": "c001",
                "production_diff": True,
                "finding_contract": {
                    "detector_profile": {"language": "java"},
                },
                "current_metrics": {"objectives": {"primary": 0}},
                "delta": {
                    "metric_progress": True,
                    "target_missing": False,
                    "objectives": {"primary": {"relative_reduction": 1.0}},
                },
            }),
            encoding="utf-8",
        )
        no_behavior_gate = finalize_checkpoint(
            project,
            "long_method",
            location,
            "c001",
            {
                "success": True,
                "accepted": True,
                "progress": True,
                "resolution": "resolved",
                "status": "PASS",
                "smell_guard": {"success": True},
                "build_test_guard": None,
            },
        )
        assert no_behavior_gate is not None, no_behavior_gate
        assert no_behavior_gate["accepted"] is False, no_behavior_gate
        assert no_behavior_gate["restorable"] is False, no_behavior_gate

    _check_large_clone_failure_pack_is_bounded()

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
    assert structural_pack["failure_category"] == "SMELL_GUARD_FAILED", structural_pack
    assert structural_pack["failure_group"] == "smell", structural_pack
    assert structural_pack["retryable"] is True, structural_pack
    assert structural_pack["highlights"][0].startswith(
        "GUARD_TARGET refused_bequest guard:"
    ), structural_pack
    assert "continue the refactoring" in structural_pack["recommendations"][0], structural_pack

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

    weakened_api_migration_pack = _build_failure_pack({
        "status": "TEST_SOURCE_MIGRATION_REJECTED",
        "test_changes": {
            "mode": "api_migration",
            "allow_test_changes": True,
            "status": "TEST_SOURCE_MIGRATION_REJECTED",
            "test_strength_violations": [
                {"reason": "assertion_count_decreased", "path": "src/test/java/FooTest.java"}
            ],
        },
    }, {})
    assert weakened_api_migration_pack["failure_category"] == "TEST_BEHAVIOR_REGRESSION", (
        weakened_api_migration_pack
    )
    assert weakened_api_migration_pack["failure_group"] == "test", weakened_api_migration_pack
    assert weakened_api_migration_pack["retryable"] is True, weakened_api_migration_pack
    assert weakened_api_migration_pack["repair_contract"]["tests_may_change"] is True, (
        weakened_api_migration_pack
    )

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
    assert checkpoint_only_structural_pack["failure_category"] == "SMELL_GUARD_FAILED", (
        checkpoint_only_structural_pack
    )
    assert checkpoint_only_structural_pack["failure_group"] == "smell", (
        checkpoint_only_structural_pack
    )

    print(
        f"checkpoint-contract-self-check PASS smells={len(EXPECTED)} "
        "unchanged_pass=0 metric_decrease=IMPROVED feedback=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
