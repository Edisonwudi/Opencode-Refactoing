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

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CHECKPOINT_CONTRACT_VERSION,
            "metric_available": self.metric_available,
            "metric_progress": self.metric_progress,
            "reason": self.reason,
            "objectives": self.objective_delta,
        }


def evaluate_checkpoint_contract(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    has_production_diff: bool,
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
    progress = bool(
        has_production_diff
        and available
        and any(item["absolute_reduction"] > 0 for item in deltas.values())
    )
    if not has_production_diff:
        reason = "EDIT_REQUIRED"
    elif not available:
        reason = "BASELINE_METRIC_UNAVAILABLE"
    elif not progress:
        reason = "NO_STRUCTURAL_PROGRESS"
    else:
        reason = "METRIC_PROGRESS"
    return ContractEvaluation(available, progress, reason, deltas)


def checkpoint_gate_result(smell: str, checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a guard failure when the shared contract blocks acceptance."""
    delta = dict(checkpoint.get("delta") or {})
    if delta.get("metric_progress") is True:
        return None
    reason = str(delta.get("reason") or "NO_STRUCTURAL_PROGRESS")
    messages = {
        "EDIT_REQUIRED": "the unchanged production baseline is not an accepted repair",
        "BASELINE_METRIC_UNAVAILABLE": "the immutable baseline has no comparable continuous metric",
        "NO_STRUCTURAL_PROGRESS": "production source changed, but no checkpoint objective decreased",
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
