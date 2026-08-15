"""Generic checkpoint acceptance contract.

Adapters expose continuous, threshold-independent metrics.  This module owns
the single smell verdict shared by every migrated Java smell: an unchanged
production tree never passes, at least one objective must strictly decrease,
and the frozen target-Guard finding must disappear. Build/test is the only
separate final gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .analysis import syntax_issue_witness_additions
from .java.catalog_identity import (
    clone_catalog_additions_in_impact_cone,
    feature_envy_catalog_additions_in_impact_cone,
)
from .resolution_plan import resolution_plan_next_action


CHECKPOINT_CONTRACT_VERSION = 6


@dataclass(frozen=True)
class ContractEvaluation:
    metric_available: bool
    metric_progress: bool
    reason: str
    objective_delta: dict[str, dict[str, float]]
    semantic_contract_preserved: bool
    semantic_contract_delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CHECKPOINT_CONTRACT_VERSION,
            "metric_available": self.metric_available,
            "metric_progress": self.metric_progress,
            "reason": self.reason,
            "objectives": self.objective_delta,
            "semantic_contract_preserved": self.semantic_contract_preserved,
            "semantic_contract": self.semantic_contract_delta,
        }


def evaluate_checkpoint_contract(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    has_production_diff: bool,
    smell: str = "",
    changed_production_source_files: Iterable[str] | None = None,
) -> ContractEvaluation:
    """Compare adapter snapshots using the shared strict-decrease contract."""
    before = _numeric_objectives(baseline.get("objectives"))
    after = _numeric_objectives(current.get("objectives"))
    shared = sorted(set(before).intersection(after))
    deltas = {
        name: {
            "before": before[name],
            "after": after[name],
            "absolute_reduction": before[name] - after[name],
            "relative_reduction": (
                round((before[name] - after[name]) / before[name], 6)
                if before[name] != 0
                else 0.0
            ),
        }
        for name in shared
    }
    baseline_available = bool(baseline.get("ok")) and bool(before) and any(
        value > 0 for value in before.values()
    )
    current_metric_available = bool(shared)
    available = baseline_available and current_metric_available
    # A missing target is never inferred to be success. An adapter may mark an
    # absence transition valid only after its smell-specific closure has
    # resolved the frozen finding (for example a rename, safe deletion, or
    # Feature Envy move). There is no second guard fallback.
    target_unlocated = (
        current.get("target_missing") is True
        and current.get("target_absence_allowed") is not True
    )
    semantic_contract = _semantic_contract_delta(
        baseline,
        current,
        smell=smell,
        changed_production_source_files=(
            tuple(str(path) for path in changed_production_source_files)
            if changed_production_source_files is not None
            else None
        ),
    )
    semantic_contract_preserved = not bool(semantic_contract.get("regressions"))
    current_detector_failure = _current_detector_failure(current)
    progress = bool(
        has_production_diff
        and available
        and not current_detector_failure
        and not target_unlocated
        and semantic_contract_preserved
        and any(item["absolute_reduction"] > 0 for item in deltas.values())
    )
    if not has_production_diff:
        reason = "EDIT_REQUIRED"
    elif not baseline_available:
        reason = "BASELINE_METRIC_UNAVAILABLE"
    elif current_detector_failure:
        reason = current_detector_failure
    elif target_unlocated:
        reason = "TARGET_NOT_LOCATED"
    elif not current_metric_available:
        reason = "CURRENT_METRIC_UNAVAILABLE"
    elif not semantic_contract_preserved:
        reason = "SEMANTIC_CONTRACT_REGRESSION"
    elif not progress:
        reason = "NO_STRUCTURAL_PROGRESS"
    else:
        reason = "METRIC_PROGRESS"
    return ContractEvaluation(
        available,
        progress,
        reason,
        deltas,
        semantic_contract_preserved,
        semantic_contract,
    )


def checkpoint_gate_result(smell: str, checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a guard failure when the shared contract blocks acceptance."""
    delta = dict(checkpoint.get("delta") or {})
    current_metrics = checkpoint.get("current_metrics")
    current_detector_failure = ""
    if checkpoint.get("required") is True:
        if not isinstance(current_metrics, Mapping):
            current_detector_failure = "CURRENT_DETECTOR_UNAVAILABLE"
        else:
            current_detector_failure = _current_detector_failure(
                current_metrics,
                require_candidate_count=True,
            )
    finding_remains = bool(
        isinstance(current_metrics, Mapping)
        and current_metrics.get(
            "target_smell_present", current_metrics.get("finding_present")
        )
        is True
    )
    data_clumps_project_full_required = bool(
        smell == "data_clumps"
        and isinstance(current_metrics, Mapping)
        and current_metrics.get("project_full_required") is True
        and str(checkpoint.get("verification_mode") or "").strip()
        != "project_full"
    )
    if (
        not current_detector_failure
        and delta.get("metric_progress") is True
        and not finding_remains
        and not data_clumps_project_full_required
    ):
        return None
    checkpoint_reason = str(checkpoint.get("reason") or "").strip()
    if checkpoint.get("required") is False and checkpoint_reason:
        reason = checkpoint_reason.upper()
    elif current_detector_failure:
        reason = current_detector_failure
    elif finding_remains and delta.get("metric_progress") is True:
        reason = "FINDING_REMAINS"
    elif data_clumps_project_full_required and delta.get("metric_progress") is True:
        reason = "DATA_CLUMPS_PROJECT_FULL_REQUIRED"
    else:
        reason = str(delta.get("reason") or "NO_STRUCTURAL_PROGRESS")
    messages = {
        "EDIT_REQUIRED": "the unchanged production baseline is not an accepted repair",
        "BASELINE_CHECKPOINT_MISSING": (
            "the immutable baseline checkpoint is missing; verification cannot "
            "fall back to an unfrozen threshold"
        ),
        "BASELINE_METRIC_UNAVAILABLE": "the immutable baseline has no comparable continuous metric",
        "CURRENT_DETECTOR_UNAVAILABLE": (
            "the target Guard could not produce a valid current snapshot"
        ),
        "CURRENT_METRIC_UNAVAILABLE": (
            "the current target Guard snapshot has no metric comparable with "
            "the immutable baseline"
        ),
        "CURRENT_DETECTOR_RESULT_INVALID": (
            "the target Guard returned an invalid current match count"
        ),
        "TARGET_AMBIGUOUS": (
            "the current source matches multiple candidates for the frozen finding"
        ),
        "DETECTOR_PROFILE_MISMATCH": (
            "the Guard implementation or profile changed; recapture the immutable baseline"
        ),
        "CHECKPOINT_RECAPTURE_REQUIRED": (
            "the checkpoint predates schema v4; recapture c000 before editing"
        ),
        "BASELINE_CONTROLLER_SEAL_MISSING": (
            "the controller-owned c000 integrity seal is missing"
        ),
        "BASELINE_CONTROLLER_SEAL_MISMATCH": (
            "c000 no longer matches the integrity seal retained by the controller"
        ),
        "VERIFICATION_CONTRACT_MISMATCH": (
            "the live build/test execution identity differs from the contract frozen in c000"
        ),
        "CHECKPOINT_SELECTION_CONTEXT_MISMATCH": (
            "the live target selector differs from the selection_context frozen in c000"
        ),
        "FINDING_REMAINS": (
            "the same frozen target improved but its smell is still present; record this result as IMPROVED, not PASS"
        ),
        "NO_STRUCTURAL_PROGRESS": "production source changed, but no checkpoint objective decreased",
        "TARGET_NOT_LOCATED": "the target entity could not be located after the edits; re-anchor it or restore the target signature instead of making it unreachable",
        "SEMANTIC_CONTRACT_REGRESSION": "the refactoring violated a smell-specific structural contract",
        "DATA_CLUMPS_PROJECT_FULL_REQUIRED": (
            "the controlled declaration migration requires verification_mode=project_full"
        ),
    }
    return {
        "type": smell,
        "success": False,
        "message": f"{smell} checkpoint contract: {messages.get(reason, messages['NO_STRUCTURAL_PROGRESS'])}.",
        "details": {
            "detector": "checkpoint_contract",
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "adapter": checkpoint.get("adapter"),
            "baseline_metrics": checkpoint.get("baseline_metrics"),
            "current_metrics": checkpoint.get("current_metrics"),
            "metric_delta": delta,
            "has_production_diff": bool(checkpoint.get("production_diff")),
            "reason": reason,
        },
    }


def _current_detector_failure(
    current: Mapping[str, Any],
    *,
    require_candidate_count: bool = False,
) -> str:
    """Validate the authoritative current target-Guard result without guessing.

    Product snapshots always include an integer candidate count.  Metric-only
    unit fixtures may omit it when calling the evaluator directly, but the
    guard boundary requires the complete snapshot and fails closed.
    """
    if current.get("ok") is not True:
        return "CURRENT_DETECTOR_UNAVAILABLE"
    raw_count = current.get("target_match_count", current.get("candidate_count"))
    if raw_count is None:
        return "CURRENT_DETECTOR_RESULT_INVALID" if require_candidate_count else ""
    if (
        isinstance(raw_count, bool)
        or not isinstance(raw_count, (int, float))
        or not float(raw_count).is_integer()
        or float(raw_count) < 0
    ):
        return "CURRENT_DETECTOR_RESULT_INVALID"
    if int(raw_count) > 1:
        return "TARGET_AMBIGUOUS"
    return ""


def checkpoint_feedback_highlights(checkpoint: Mapping[str, Any] | None) -> list[str]:
    """Render compact metric feedback for failure_pack and continuation prompts."""
    if not isinstance(checkpoint, Mapping) or not checkpoint.get("required"):
        return []
    delta = checkpoint.get("delta")
    if not isinstance(delta, Mapping):
        return []
    reason = str(delta.get("reason") or "NO_STRUCTURAL_PROGRESS")
    production_diff = str(bool(delta.get("has_production_diff"))).lower()
    progress = str(bool(delta.get("metric_progress"))).lower()
    highlights = [
        f"CHECKPOINT reason={reason} production_diff={production_diff} metric_progress={progress}"
    ]
    semantic_contract = delta.get("semantic_contract")
    if isinstance(semantic_contract, Mapping):
        regressions = semantic_contract.get("regressions")
        if isinstance(regressions, list) and regressions:
            highlights.append(
                "CHECKPOINT_CONTRACT "
                + "; ".join(str(item) for item in regressions[:4])
            )
        review_signals = semantic_contract.get("review_signals")
        if isinstance(review_signals, list) and review_signals:
            highlights.append(
                "CHECKPOINT_CONTRACT_REVIEW "
                + "; ".join(str(item) for item in review_signals[:4])
            )
    objectives = delta.get("objectives")
    if not isinstance(objectives, Mapping):
        return highlights
    rendered: list[str] = []
    for name in sorted(objectives):
        values = objectives.get(name)
        if not isinstance(values, Mapping):
            continue
        before = values.get("before")
        after = values.get("after")
        reduction = values.get("absolute_reduction")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (before, after, reduction)):
            continue
        rendered.append(
            f"{name}:{_compact_number(before)}->{_compact_number(after)}"
            f"(reduction={_compact_number(reduction)})"
        )
    if rendered:
        objective_line = "CHECKPOINT_OBJECTIVES " + "; ".join(rendered)
        action = resolution_plan_next_action(checkpoint.get("resolution_plan"))
        if action:
            objective_line += "; NEXT " + action
        residual = _guard_residual_feedback(checkpoint.get("resolution_plan"))
        if residual:
            objective_line += "; " + residual
        best_partial = _best_partial_feedback(checkpoint)
        if best_partial:
            objective_line += "; " + best_partial
        highlights.append(objective_line)
    return highlights


def _guard_residual_feedback(value: Any) -> str:
    """Render a bounded post-edit Guard residual, never a dependency guess.

    Resolution plans contain only entities already returned by the selected
    target Guard.  Showing a small preview helps the repair loop inspect the
    remaining source without claiming that the Guard discovered a complete
    cross-project call or declaration closure.
    """
    if not isinstance(value, Mapping):
        return ""
    remaining = _integer(value.get("remaining_work_count"))
    worklist = value.get("worklist")
    if remaining is None or remaining <= 0 or not isinstance(worklist, list):
        return ""
    rendered: list[str] = []
    for item in worklist:
        if not isinstance(item, Mapping) or item.get("kind") == "frozen_finding":
            continue
        kind = str(item.get("kind") or "residual")
        file_name = str(item.get("file") or item.get("source_file") or "")
        line = _integer(item.get("line")) or _integer(item.get("begin_line"))
        location = file_name + (f":{line}" if file_name and line else "")
        identity = str(
            item.get("method")
            or item.get("signature")
            or item.get("member")
            or item.get("cluster_id")
            or item.get("name")
            or ""
        )
        details = "#".join(part for part in (location, identity) if part)
        rendered.append(kind + (f"@{details}" if details else ""))
        if len(rendered) == 3:
            break
    if not rendered:
        return ""
    complete = str(bool(value.get("worklist_complete"))).lower()
    return (
        f"CHECKPOINT_RESIDUAL remaining={remaining} "
        f"preview_complete={complete} items=" + ",".join(rendered)
    )


def _best_partial_feedback(checkpoint: Mapping[str, Any]) -> str:
    best = checkpoint.get("best_partial")
    if not isinstance(best, Mapping):
        return ""
    checkpoint_id = str(best.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        return ""
    if best.get("smell_guard_success") is True and best.get("build_test_success") is False:
        return (
            f"BEST_PARTIAL {checkpoint_id} already clears the smell; preserve its production structure "
            "and repair only the build/test regression"
        )
    if checkpoint.get("regressed_from_best_partial") is True:
        patch = str(best.get("production_patch") or "").strip()
        reference = f" in {patch}" if patch else ""
        return (
            f"BEST_PARTIAL {checkpoint_id} is structurally better; inspect its production-only patch"
            f"{reference} and recover only lost production edits before continuing"
        )
    if checkpoint.get("current_is_best_partial") is True:
        return f"BEST_PARTIAL {checkpoint_id} saved; do not undo these metric gains"
    return ""


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _compact_number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.6g}"


def _numeric_objectives(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for name, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        result[str(name)] = float(raw)
    return result


def _semantic_contract_delta(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    smell: str,
    changed_production_source_files: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Compare route-independent semantic contracts captured by adapters."""
    guard_violations = current.get("guard_violations")
    if isinstance(guard_violations, list) and guard_violations:
        regressions = [
            str(item.get("code") or item.get("message") or item)
            if isinstance(item, Mapping)
            else str(item)
            for item in guard_violations
            if str(item)
        ]
        return {
            "applicable": True,
            "scope": "target_plus_changed_production_files",
            "regressions": regressions,
        }
    if (
        "target_syntax_issue_witnesses" in baseline
        and "target_syntax_issue_witnesses" in current
    ):
        syntax_additions = syntax_issue_witness_additions(
            baseline.get("target_syntax_issue_witnesses"),
            current.get("target_syntax_issue_witnesses"),
        )
        if syntax_additions:
            return {
                "applicable": True,
                "scope": "frozen_explicit_target_parser_recovery",
                "new_syntax_issue_witnesses": syntax_additions,
                "regressions": ["TARGET_SYNTAX_RECOVERY_REGRESSION"],
            }
    if smell == "feature_envy":
        if current.get("finding_present") is True:
            return {"applicable": False, "regressions": []}
        baseline_profile = baseline.get("detector_profile")
        current_profile = current.get("detector_profile")
        relocation_contract_enabled = bool(
            isinstance(baseline_profile, Mapping)
            and isinstance(current_profile, Mapping)
            and baseline_profile.get("language") == "java"
            and current_profile.get("language") == "java"
            and baseline_profile.get("reject_finding_relocation_in_impact_cone") is True
            and current_profile.get("reject_finding_relocation_in_impact_cone") is True
        )
        if not relocation_contract_enabled:
            return {"applicable": False, "regressions": []}
        before_catalog = baseline.get("project_finding_catalog")
        after_catalog = current.get("project_finding_catalog")
        if not isinstance(before_catalog, list) or not isinstance(after_catalog, list):
            return {
                "applicable": True,
                "regressions": ["feature_envy_project_catalog_unavailable"],
            }
        additions = (
            feature_envy_catalog_additions_in_impact_cone(
                before_catalog,
                after_catalog,
                changed_files=changed_production_source_files,
            )
            if changed_production_source_files is not None
            else _finding_catalog_additions(
                before_catalog,
                after_catalog,
                fields=("file", "class_name", "method", "rule_id"),
            )
        )
        return {
            "applicable": True,
            "new_feature_envy_findings": additions,
            "regressions": [
                "feature_envy_finding_relocated:"
                f"{item.get('file', '')}#{item.get('class_name', '')}#"
                f"{item.get('method', '')}"
                for item in additions
            ],
        }
    if smell == "data_clumps":
        if current.get("finding_present") is True:
            return {"applicable": False, "regressions": []}
        continuity_ok = current.get("continuity_ok") is True
        count = _integer(current.get("continuity_occurrence_count"))
        passing_max = _integer(current.get("passing_max"))
        occurrences = current.get("continuity_occurrences")
        inline_copy_ok = current.get("inline_copy_analysis_ok") is True
        inline_copy_expansions = current.get("inline_copy_expansions")
        if not continuity_ok or count is None or passing_max is None:
            return {
                "applicable": True,
                "regressions": ["parameter_group_continuity_unavailable"],
            }
        surviving: list[str] = []
        if isinstance(occurrences, list):
            for item in occurrences[:8]:
                if not isinstance(item, Mapping):
                    continue
                file_name = str(item.get("file") or "")
                method = str(item.get("method") or "")
                surviving.append(f"{file_name}#{method}".rstrip("#"))
        regressions = (
            [f"parameter_group_remains:{item}" for item in surviving]
            if count > passing_max
            else []
        )
        if count > passing_max and not regressions:
            regressions = [f"parameter_group_remains:{count}"]
        if not inline_copy_ok:
            regressions.append("inline_copy_analysis_unavailable")
        expanded_windows: list[dict[str, Any]] = []
        if isinstance(inline_copy_expansions, list):
            for item in inline_copy_expansions[:8]:
                if not isinstance(item, Mapping):
                    continue
                expanded_windows.append(dict(item))
                source_file = str(item.get("source_file") or "")
                source_method = str(item.get("source_method") or "")
                before_count = _integer(item.get("baseline_occurrences")) or 0
                after_count = _integer(item.get("current_occurrences")) or 0
                expansion_reason = str(item.get("reason") or "")
                regressions.append(
                    (
                        "inlined_body_window_relocated:"
                        if expansion_reason == "source_window_relocated"
                        else "inlined_body_window_expanded:"
                    )
                    + f"{source_file}#{source_method}:{before_count}->{after_count}"
                )
        return {
            "applicable": True,
            "continuity_occurrence_count": count,
            "passing_max": passing_max,
            "surviving_signatures": surviving,
            "inline_copy_contract_available": bool(
                current.get("inline_copy_contract_available")
            ),
            "expanded_body_windows": expanded_windows,
            "regressions": regressions,
        }
    if smell == "code_clone_type1":
        if current.get("finding_present") is True:
            return {"applicable": False, "regressions": []}
        before = baseline.get("clone_structure")
        after = current.get("clone_structure")
        if not isinstance(before, Mapping) or before.get("ok") is not True:
            return {
                "applicable": False,
                "regressions": [],
                "reason": "baseline_clone_structure_unavailable",
            }
        if not isinstance(after, Mapping) or after.get("ok") is not True:
            return {
                "applicable": True,
                "regressions": ["clone_structure_analysis_unavailable"],
            }
        route_proof = _clone_strict_deduplication(before, after)
        carrier = str(route_proof.get("carrier") or "")
        shared_implementation = str(after.get("shared_implementation") or "")
        inherited_deduplication = bool(route_proof.get("inherited_deduplication"))
        introduced_common = (
            [] if inherited_deduplication or not carrier else [carrier]
        )
        catalog_additions = (
            clone_catalog_additions_in_impact_cone(
                before.get("clone_catalog"),
                after.get("clone_catalog"),
                changed_files=changed_production_source_files,
                affected_methods=introduced_common,
            )
            if changed_production_source_files is not None
            else _clone_catalog_additions(
                before.get("clone_catalog"),
                after.get("clone_catalog"),
            )
        )
        # A legal extraction leaves one shared carrier, which cannot form an
        # exact-clone group. Any new catalog member therefore remains a
        # relocation/parallelization regression even when the global count
        # happened to decrease from three copies to two.
        catalog_regressions = list(catalog_additions)
        retained_endpoints = _clone_endpoints_retaining_baseline_body(
            before.get("endpoints"),
            after.get("endpoints"),
        )
        after_endpoints = _clone_endpoint_list(after.get("endpoints"))
        retained_endpoints = [
            index for index in retained_endpoints
            if index >= len(after_endpoints)
            or str(
                after_endpoints[index].get("declared_method")
                or after_endpoints[index].get("effective_method")
                or ""
            ) not in introduced_common
        ]
        regressions: list[str] = list(route_proof.get("regressions") or [])
        if catalog_regressions:
            regressions.extend(
                "clone_relocated_or_parallelized:"
                f"{item.get('fingerprint', '')[:12]}:{','.join(item.get('new_methods', [])[:3])}"
                for item in catalog_regressions[:8]
            )
        regressions.extend(
            f"delegating_fallback_retains_clone_body:endpoint_{index + 1}"
            for index in retained_endpoints
        )
        return {
            "applicable": True,
            "introduced_shared_callees": introduced_common,
            "implementation_carriers": [carrier] if carrier else [],
            "carrier_overlap_tokens": int(route_proof.get("overlap_tokens") or 0),
            "carrier_overlap_ratio": float(route_proof.get("overlap_ratio") or 0.0),
            "deduplication_routes": list(route_proof.get("routes") or []),
            "redirected_endpoint_callers": list(
                route_proof.get("redirected_endpoint_callers") or []
            ),
            "shared_implementation": shared_implementation,
            "inherited_deduplication": inherited_deduplication,
            "clone_catalog_additions": catalog_additions,
            "endpoints_retaining_baseline_body": retained_endpoints,
            "regressions": regressions,
        }
    return {"applicable": False, "regressions": []}


def _clone_strict_deduplication(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    before_graph = _clone_call_graph(before.get("call_graph"))
    before_endpoints = _clone_endpoint_list(before.get("endpoints"))
    after_endpoints = _clone_endpoint_list(after.get("endpoints"))
    profiles = {
        str(item.get("method") or ""): item
        for item in _clone_implementation_catalog(after.get("implementation_catalog"))
    }
    if len(before_endpoints) != 2 or len(after_endpoints) != 2 or not profiles:
        return {
            "carrier": "",
            "routes": [],
            "redirected_endpoint_callers": [],
            "regressions": ["clone_call_graph_unavailable"],
        }
    side_carriers: list[str] = []
    routes: list[dict[str, Any]] = []
    redirected: list[dict[str, Any]] = []
    regressions: list[str] = []
    for index in range(2):
        baseline_key = str(before_endpoints[index].get("declared_method") or "")
        current_key = str(
            after_endpoints[index].get("declared_method")
            or after_endpoints[index].get("effective_method")
            or ""
        )
        if not baseline_key:
            regressions.append(f"baseline_clone_endpoint_missing:endpoint_{index + 1}")
            continue
        starts: list[str]
        if current_key:
            starts = [current_key]
        else:
            callers = sorted(
                caller
                for caller, callees in before_graph.items()
                if caller != baseline_key and baseline_key in callees
            )
            missing_callers = [caller for caller in callers if caller not in profiles]
            if not callers:
                regressions.append(
                    f"removed_clone_endpoint_has_no_frozen_callers:endpoint_{index + 1}"
                )
                continue
            if missing_callers:
                regressions.append(
                    f"removed_clone_endpoint_callers_missing:endpoint_{index + 1}:"
                    + ",".join(missing_callers[:3])
                )
                continue
            starts = callers
            redirected.append({
                "endpoint": index + 1,
                "baseline_callers": callers,
            })
        traces = [
            _clone_forwarder_route(start, profiles)
            for start in starts
        ]
        trace_errors = [str(item.get("error") or "") for item in traces if item.get("error")]
        if trace_errors:
            regressions.append(
                f"clone_forwarder_route_unproven:endpoint_{index + 1}:"
                + ",".join(sorted(set(trace_errors))[:3])
            )
            continue
        carriers = {str(item.get("carrier") or "") for item in traces}
        carriers.discard("")
        if len(carriers) != 1:
            regressions.append(
                f"clone_endpoint_routes_diverge:endpoint_{index + 1}"
            )
            continue
        carrier = next(iter(carriers))
        side_carriers.append(carrier)
        for trace in traces:
            routes.append({
                "endpoint": index + 1,
                "start": str(trace.get("start") or ""),
                "path": list(trace.get("path") or []),
                "carrier": carrier,
            })
    carrier = ""
    if len(side_carriers) == 2 and side_carriers[0] == side_carriers[1]:
        carrier = side_carriers[0]
    elif not regressions:
        regressions.append("clone_endpoint_routes_do_not_converge")
    overlap_tokens = 0
    overlap_ratio = 0.0
    if carrier:
        baseline_tokens = _clone_endpoint_body_tokens(before_endpoints)
        carrier_tokens = _trim_clone_body_tokens(
            list(profiles.get(carrier, {}).get("body_tokens") or [])
        )
        overlap_tokens = _longest_common_token_window(
            baseline_tokens,
            carrier_tokens,
        )
        overlap_ratio = (
            overlap_tokens / len(baseline_tokens)
            if baseline_tokens
            else 0.0
        )
        # Keep overlap as diagnostic context only. Convergence of both frozen
        # endpoints on one statically proven carrier is the structural contract;
        # legitimate rewrites are not required to retain an arbitrary fraction
        # of the baseline token sequence.
    if not carrier:
        regressions.append("clone_deduplication_proof_missing")
    shared_implementation = str(after.get("shared_implementation") or "")
    return {
        "carrier": carrier,
        "routes": routes,
        "redirected_endpoint_callers": redirected,
        "overlap_tokens": overlap_tokens,
        "overlap_ratio": overlap_ratio,
        "inherited_deduplication": bool(
            carrier and shared_implementation == carrier
        ),
        "regressions": list(dict.fromkeys(regressions)),
    }


def _clone_forwarder_route(
    start: str,
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    current = start
    path: list[str] = []
    seen: set[str] = set()
    while current:
        if current in seen:
            return {"start": start, "path": path, "error": "cycle"}
        seen.add(current)
        path.append(current)
        profile = profiles.get(current)
        if not isinstance(profile, Mapping):
            return {"start": start, "path": path, "error": "method_missing"}
        if profile.get("thin_forwarder") is not True:
            return {"start": start, "path": path, "carrier": current}
        if int(profile.get("unresolved_call_count") or 0) != 0:
            return {"start": start, "path": path, "error": "unresolved_dispatch"}
        callees = [str(item) for item in (profile.get("callees") or []) if str(item)]
        if len(callees) != 1:
            return {"start": start, "path": path, "error": "not_single_forwarder"}
        current = callees[0]
    return {"start": start, "path": path, "error": "route_missing"}


def _clone_endpoint_body_tokens(
    endpoints: Sequence[Mapping[str, Any]],
) -> list[str]:
    for endpoint in endpoints:
        tokens = endpoint.get("body_tokens")
        if isinstance(tokens, list) and tokens:
            return _trim_clone_body_tokens([str(token) for token in tokens])
    return []


def _trim_clone_body_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) >= 2 and tokens[0] == "{" and tokens[-1] == "}":
        return tokens[1:-1]
    return tokens


def _longest_common_token_window(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_token in left:
        current = [0] * (len(right) + 1)
        for index, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current[index] = previous[index - 1] + 1
                longest = max(longest, current[index])
        previous = current
    return longest


def _clone_endpoint_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)][:2]


def _clone_implementation_catalog(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, Mapping)
        and str(item.get("method") or "")
    ]


def _clone_call_graph(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, list):
        return {}
    graph: dict[str, set[str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        caller = str(item.get("caller") or "")
        callees = item.get("callees")
        if caller and isinstance(callees, list):
            graph[caller] = {str(callee) for callee in callees if str(callee)}
    return graph


def _clone_catalog_entries(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, list):
        return {}
    entries: dict[str, set[str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        fingerprint = str(item.get("fingerprint") or "")
        methods = item.get("methods")
        if fingerprint and isinstance(methods, list):
            entries[fingerprint] = {str(method) for method in methods if str(method)}
    return entries


def _clone_catalog_additions(before_value: Any, after_value: Any) -> list[dict[str, Any]]:
    before = _clone_catalog_entries(before_value)
    after = _clone_catalog_entries(after_value)
    additions: list[dict[str, Any]] = []
    for fingerprint in sorted(after):
        new_methods = sorted(after[fingerprint].difference(before.get(fingerprint, set())))
        if fingerprint not in before or new_methods:
            additions.append({
                "fingerprint": fingerprint,
                "new_methods": new_methods or sorted(after[fingerprint]),
                "before_count": len(before.get(fingerprint, set())),
                "after_count": len(after[fingerprint]),
            })
    return additions


def _clone_endpoints_retaining_baseline_body(
    before_value: Any,
    after_value: Any,
) -> list[int]:
    if not isinstance(before_value, list) or not isinstance(after_value, list):
        return []
    baseline_tokens: list[str] = []
    for item in before_value:
        if not isinstance(item, Mapping) or not isinstance(item.get("body_tokens"), list):
            continue
        baseline_tokens = [str(token) for token in item["body_tokens"]]
        if len(baseline_tokens) >= 2 and baseline_tokens[0] == "{" and baseline_tokens[-1] == "}":
            baseline_tokens = baseline_tokens[1:-1]
        if baseline_tokens:
            break
    if not baseline_tokens:
        return []
    retained: list[int] = []
    for index, item in enumerate(after_value[:2]):
        if not isinstance(item, Mapping) or not isinstance(item.get("body_tokens"), list):
            continue
        current_tokens = [str(token) for token in item["body_tokens"]]
        if _contains_token_window(current_tokens, baseline_tokens):
            retained.append(index)
    return retained


def _contains_token_window(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        haystack[index : index + width] == needle
        for index in range(len(haystack) - width + 1)
    )


def _finding_catalog_additions(
    before_value: Any,
    after_value: Any,
    *,
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    def entries(value: Any) -> dict[tuple[str, ...], dict[str, Any]]:
        if not isinstance(value, list):
            return {}
        result: dict[tuple[str, ...], dict[str, Any]] = {}
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            item = {field: str(raw.get(field) or "") for field in fields}
            key = tuple(item[field] for field in fields)
            if any(key):
                result[key] = item
        return result

    before = entries(before_value)
    after = entries(after_value)
    return [after[key] for key in sorted(set(after).difference(before))]
