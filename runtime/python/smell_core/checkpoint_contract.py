"""Generic checkpoint acceptance contract.

Adapters expose continuous, threshold-independent metrics.  This module owns
the policy shared by every migrated smell: an unchanged production tree never
passes, and at least one measurable objective must strictly decrease from the
immutable baseline.  The ordinary smell guard and build/test guard remain the
final acceptance gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


CHECKPOINT_CONTRACT_VERSION = 1


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
    available = bool(baseline.get("ok")) and bool(shared) and any(before[name] > 0 for name in shared)
    # When the adapter can no longer locate the target entity, its objectives
    # are recorded as zero — that is a measurement failure, not a reduction.
    # Only smells whose goal is the target's absence (dead_code deletion,
    # mysterious_name rename) may treat target_missing as progress; every
    # other smell must be measured on a located target or fail closed. Legit
    # removals (e.g. an LPL signature genuinely replaced) still pass through
    # the strict guard, which verifies the original signature is gone.
    absence_goal = smell in ("dead_code", "mysterious_name")
    target_unlocated = current.get("target_missing") is True and not absence_goal
    semantic_contract = _semantic_contract_delta(baseline, current, smell=smell)
    semantic_contract_preserved = not bool(semantic_contract.get("regressions"))
    progress = bool(
        has_production_diff
        and available
        and not target_unlocated
        and semantic_contract_preserved
        and any(item["absolute_reduction"] > 0 for item in deltas.values())
    )
    if not has_production_diff:
        reason = "EDIT_REQUIRED"
    elif not available:
        reason = "BASELINE_METRIC_UNAVAILABLE"
    elif target_unlocated:
        reason = "TARGET_NOT_LOCATED"
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
    finding_remains = bool(
        isinstance(current_metrics, Mapping)
        and current_metrics.get("finding_present") is True
    )
    if delta.get("metric_progress") is True and not finding_remains:
        return None
    checkpoint_reason = str(checkpoint.get("reason") or "").strip()
    if checkpoint.get("required") is False and checkpoint_reason:
        reason = checkpoint_reason.upper()
    elif finding_remains and delta.get("metric_progress") is True:
        reason = "FINDING_REMAINS"
    else:
        reason = str(delta.get("reason") or "NO_STRUCTURAL_PROGRESS")
    if reason == "TARGET_NOT_LOCATED":
        # target_missing means the adapter could not measure the target; it is
        # not a smell verdict. Arbitrating "genuinely removed" vs "made
        # unreachable" belongs to the strict guard (which rescans for the
        # original signature), so the contract stays silent here.
        return None
    messages = {
        "EDIT_REQUIRED": "the unchanged production baseline is not an accepted repair",
        "BASELINE_CHECKPOINT_MISSING": (
            "the immutable baseline checkpoint is missing; verification cannot "
            "fall back to the threshold detector"
        ),
        "BASELINE_METRIC_UNAVAILABLE": "the immutable baseline has no comparable continuous metric",
        "DETECTOR_PROFILE_MISMATCH": (
            "the detector implementation or profile changed; recapture the immutable baseline"
        ),
        "FINDING_REMAINS": (
            "the same detector finding improved but is still present; record this result as IMPROVED, not PASS"
        ),
        "NO_STRUCTURAL_PROGRESS": "production source changed, but no checkpoint objective decreased",
        "TARGET_NOT_LOCATED": "the target entity could not be located after the edits; re-anchor it or restore the target signature instead of making it unreachable",
        "SEMANTIC_CONTRACT_REGRESSION": "the refactoring violated a smell-specific structural contract",
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
        action = _adapter_next_action(checkpoint)
        if action:
            objective_line += "; NEXT " + action
        best_partial = _best_partial_feedback(checkpoint)
        if best_partial:
            objective_line += "; " + best_partial
        highlights.append(objective_line)
    return highlights


def _adapter_next_action(checkpoint: Mapping[str, Any]) -> str:
    adapter = str(checkpoint.get("adapter") or "")
    current = checkpoint.get("current_metrics")
    if not isinstance(current, Mapping):
        return ""
    if adapter == "feature_envy":
        delta = checkpoint.get("delta")
        semantic = delta.get("semantic_contract") if isinstance(delta, Mapping) else None
        regressions = semantic.get("regressions") if isinstance(semantic, Mapping) else None
        if isinstance(regressions, list) and any(
            str(item).startswith("same_owner_receiver_finding_relocated:")
            for item in regressions
        ):
            return (
                "move the relocated receiver-heavy helper out of the original source class; "
                "use a receiver-owned operation when that boundary can execute it, otherwise "
                "use an independent workflow that preserves the existing receiver API and mock "
                "interactions; do not retain a same-class fallback"
            )
        receiver = str(current.get("expected_receiver_type") or "").strip()
        count = _integer(current.get("expected_receiver_access"))
        if receiver and count is not None and count > 0:
            return (
                f"receiver={receiver} still has {count} accesses; move one cohesive receiver-heavy "
                f"slice to {receiver}; edits toward other collaborators do not count"
            )
    if adapter == "data_clumps":
        passing_max = _integer(current.get("passing_max"))
        remaining = _integer(current.get("remaining_reductions"))
        occurrences = current.get("occurrences")
        worklist: list[str] = []
        if isinstance(occurrences, list):
            for item in occurrences[:2]:
                if not isinstance(item, Mapping):
                    continue
                file_name = str(item.get("file") or "").rsplit("/", 1)[-1]
                method = str(item.get("method") or "").split("(", 1)[0]
                if file_name or method:
                    worklist.append(f"{file_name}#{method}".rstrip("#"))
        if passing_max is not None and remaining is not None and remaining > 0:
            suffix = f"; inspect {', '.join(worklist)}" if worklist else ""
            return (
                f"reduce {remaining} more occurrence(s) to <={passing_max}{suffix}; "
                "prefer ordinary helpers over annotated/codegen-sensitive entrypoints"
            )
    return ""


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
) -> dict[str, Any]:
    """Compare route-independent semantic contracts captured by adapters.

    Refused Bequest may legitimately change the superclass, declaration owner,
    and capability topology.  The comparison therefore ignores ownership and
    hard-checks API declared by the target itself and records inherited API
    removal for review. Inherited removal cannot be a universal hard failure:
    shedding an unwanted inherited capability is the purpose of this smell.
    No project, class, method, or sample name is encoded in this policy.
    """
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
            and baseline_profile.get("reject_same_owner_receiver_relocation") is True
            and current_profile.get("reject_same_owner_receiver_relocation") is True
        )
        if not relocation_contract_enabled:
            return {"applicable": False, "regressions": []}
        before_ok = baseline.get("owner_receiver_analysis_ok") is True
        after_ok = current.get("owner_receiver_analysis_ok") is True
        before = _feature_envy_owner_receiver_entries(baseline.get("owner_receiver_findings"))
        after = _feature_envy_owner_receiver_entries(current.get("owner_receiver_findings"))
        if not before_ok or not after_ok:
            return {
                "applicable": True,
                "regressions": ["same_owner_receiver_analysis_unavailable"],
            }
        additions = [after[key] for key in sorted(set(after).difference(before))]
        return {
            "applicable": True,
            "new_same_owner_receiver_findings": additions,
            "regressions": [
                "same_owner_receiver_finding_relocated:"
                f"{item.get('file', '')}#{item.get('method', '')}"
                for item in additions
            ],
        }
    if smell == "data_clumps":
        if current.get("finding_present") is True:
            return {"applicable": False, "regressions": []}
        continuity_ok = current.get("legacy_type_continuity_ok") is True
        count = _integer(current.get("legacy_type_occurrence_count"))
        passing_max = _integer(current.get("passing_max"))
        occurrences = current.get("legacy_type_occurrences")
        inline_copy_ok = current.get("inline_copy_analysis_ok") is True
        inline_copy_expansions = current.get("inline_copy_expansions")
        if not continuity_ok or count is None or passing_max is None:
            return {
                "applicable": True,
                "regressions": ["legacy_type_signature_continuity_unavailable"],
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
            [f"legacy_type_signature_group_remains:{item}" for item in surviving]
            if count > passing_max
            else []
        )
        if count > passing_max and not regressions:
            regressions = [f"legacy_type_signature_group_remains:{count}"]
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
                regressions.append(
                    "inlined_body_window_expanded:"
                    f"{source_file}#{source_method}:{before_count}->{after_count}"
                )
        return {
            "applicable": True,
            "legacy_type_occurrence_count": count,
            "passing_max": passing_max,
            "surviving_signatures": surviving,
            "inline_copy_contract_available": bool(
                current.get("inline_copy_contract_available")
            ),
            "expanded_body_windows": expanded_windows,
            "regressions": regressions,
        }
    if smell != "refused_bequest":
        return {"applicable": False, "regressions": []}
    before = baseline.get("contract_snapshot")
    after = current.get("contract_snapshot")
    if not isinstance(before, Mapping) or not before.get("ok"):
        return {
            "applicable": False,
            "regressions": [],
            "reason": "baseline_contract_unavailable",
        }
    if not isinstance(after, Mapping) or not after.get("ok"):
        return {
            "applicable": True,
            "regressions": ["target_contract_unavailable_after_edit"],
            "before_class": before.get("class"),
            "after_class": after.get("class") if isinstance(after, Mapping) else "",
        }

    before_methods = _api_entries(before.get("visible_non_target_methods"))
    after_methods = _api_entries(after.get("visible_non_target_methods"))
    before_constructors = _api_entries(before.get("declared_visible_constructors"))
    after_constructors = _api_entries(after.get("declared_visible_constructors"))
    regressions: list[str] = []
    missing_methods = sorted(set(before_methods).difference(after_methods))
    missing_constructors = sorted(set(before_constructors).difference(after_constructors))
    review_signals: list[str] = []
    for key in missing_methods:
        if before_methods[key].get("declared_on_target") is True:
            regressions.append(f"missing_declared_method:{key}")
        else:
            review_signals.append(f"missing_inherited_method:{key}")
    for key in missing_constructors:
        regressions.append(f"missing_constructor:{key}")
    for key in sorted(set(before_methods).intersection(after_methods)):
        if (
            _visibility_rank(after_methods[key].get("visibility"))
            < _visibility_rank(before_methods[key].get("visibility"))
        ):
            signal = (
                f"{key}:{before_methods[key].get('visibility')}"
                f"->{after_methods[key].get('visibility')}"
            )
            if before_methods[key].get("declared_on_target") is True:
                regressions.append(f"narrowed_declared_method:{signal}")
            else:
                review_signals.append(f"narrowed_inherited_method:{signal}")
    for key in sorted(set(before_constructors).intersection(after_constructors)):
        if (
            _visibility_rank(after_constructors[key].get("visibility"))
            < _visibility_rank(before_constructors[key].get("visibility"))
        ):
            regressions.append(
                "narrowed_constructor:"
                f"{key}:{before_constructors[key].get('visibility')}"
                f"->{after_constructors[key].get('visibility')}"
            )
    return {
        "applicable": True,
        "before_class": before.get("class"),
        "after_class": after.get("class"),
        "superclass_changed": before.get("direct_superclass") != after.get("direct_superclass"),
        "missing_methods": missing_methods,
        "missing_constructors": missing_constructors,
        "regressions": regressions,
        "review_signals": review_signals,
        "policy": before.get("comparison_policy") or {},
    }


def _api_entries(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    entries: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("api_key") or "").strip()
        if key:
            entries[key] = item
    return entries


def _feature_envy_owner_receiver_entries(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        item = {
            "file": str(raw.get("file") or ""),
            "class": str(raw.get("class") or ""),
            "method": str(raw.get("method") or ""),
            "envied_field": str(raw.get("envied_field") or ""),
            "envied_type": str(raw.get("envied_type") or ""),
        }
        key = "#".join(
            item[name]
            for name in ("file", "class", "method", "envied_field", "envied_type")
        )
        if key.strip("#"):
            entries[key] = item
    return entries


def _visibility_rank(value: Any) -> int:
    return {"protected": 1, "public": 2}.get(str(value or ""), 0)
