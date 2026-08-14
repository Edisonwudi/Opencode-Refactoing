"""Source-derived resolution plans for checkpoint-backed smell repairs.

The target Guard remains the sole authority for whether the supplied target
still has the requested smell. This module turns the Guard/checkpoint snapshot into an executable
worklist for the repair loop.  It never reads dataset evidence, oracle labels,
or tests, and it never provides a weaker fallback verdict.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


RESOLUTION_PLAN_VERSION = 4


_PRIMARY_OBJECTIVES: dict[str, tuple[str, ...]] = {
    "long_method": ("ast_ncss",),
    "nested_complexity": ("cognitive_complexity",),
    "long_parameter_list": ("parameter_count",),
    # The product predicate is envy_access_diff > profile.finding_min_exclusive.
    # Raw receiver access is useful as a repair worklist, but it is not a
    # second acceptance objective: a method with foreign=8/self=7 is resolved.
    "feature_envy": ("envy_access_diff",),
    "data_clumps": ("occurrence_count",),
    "code_clone_type1": ("clone_token_count",),
    "god_class": ("nom", "nof", "wmc", "loc", "atfd", "class_loc"),
    "refused_bequest": (
        "refusal_finding_present",
        "rejection_signals",
        "refusal_score",
    ),
    "switch_statements": ("switch_count",),
    "mysterious_name": ("target_suspicious_name_present",),
    "dead_code": ("unused_private_finding_present",),
}


_ROUTE_FAMILIES: dict[str, str] = {
    "long_method": "extract-cohesive-blocks-to-ncss-target",
    "nested_complexity": "reduce-ranked-complexity-hotspots",
    "long_parameter_list": "migrate-signature-closure-without-legacy-delegate",
    "feature_envy": "close-one-receiver-collaboration",
    "data_clumps": "migrate-semantic-occurrence-component",
    "code_clone_type1": "redirect-frozen-pair-to-one-shared-implementation",
    "god_class": "extract-ranked-cohesive-responsibility-clusters",
    "refused_bequest": "migrate-complete-rejection-capability-closure",
    "switch_statements": "remove-all-switches-from-frozen-method",
    "mysterious_name": "rename-frozen-symbol-reference-closure",
    "dead_code": "remove-unused-private-declaration-closure",
}


_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "long_method": ("move or rename unrelated code", "extract a trivial block that leaves AST-NCSS above the product boundary"),
    "nested_complexity": ("cosmetic condition rewrites", "move the same nesting into a new helper without reducing the frozen finding"),
    "long_parameter_list": ("retain the original long signature as a delegate", "construct a holder inside the unchanged long signature"),
    "feature_envy": ("move the finding to another method in the same source owner", "add one getter wrapper per foreign access"),
    "data_clumps": ("retain the old parameter group in a wrapper", "replace typed parameters with Object, map, array, or varargs"),
    "code_clone_type1": ("perturb tokens without deduplicating behavior", "copy or relocate both clone bodies"),
    "god_class": ("rename or replace the reported class", "leave the extracted responsibility duplicated behind wrappers"),
    "refused_bequest": ("move a rejecting stub to an ancestor or sibling", "replace rejection with another placeholder value"),
    "switch_statements": ("hide the switch in a helper", "replace it with an equally large conditional chain"),
    "mysterious_name": ("rename an unrelated symbol", "leave stale production references to the frozen symbol"),
    "dead_code": ("comment out the declaration", "move the unused declaration to another owner"),
}


def build_resolution_plan(
    smell: str,
    *,
    finding_contract: Mapping[str, Any] | None,
    baseline_metrics: Mapping[str, Any] | None,
    current_metrics: Mapping[str, Any] | None = None,
    delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one deterministic repair plan from target-Guard snapshots."""
    frozen = dict(finding_contract or {})
    baseline = dict(baseline_metrics or {})
    current = dict(current_metrics or baseline)
    objective_delta = dict((delta or {}).get("objectives") or {})
    objectives = _objective_deficits(
        smell,
        baseline=baseline,
        current=current,
        objective_delta=objective_delta,
    )
    metric_budget = _metric_budget(
        smell,
        current=current,
        objectives=objectives,
    )
    worklist, worklist_total = _worklist(smell, frozen=frozen, current=current)
    next_action = _next_action(smell, current=current, objectives=objectives, worklist=worklist)
    regressions = _semantic_regressions(delta)
    finding_present = current.get(
        "target_smell_present", current.get("finding_present")
    ) is True
    detector_blocker = _current_detector_blocker(current)
    target_unlocated = (
        current.get("target_missing") is True
        and current.get("target_absence_allowed") is not True
    )
    return {
        "version": RESOLUTION_PLAN_VERSION,
        "smell": smell,
        "finding_id": str(frozen.get("target_id") or frozen.get("finding_id") or ""),
        "entity_identity": dict(
            frozen.get("entity_identity")
            if isinstance(frozen.get("entity_identity"), Mapping)
            else current.get("entity_identity")
            if isinstance(current.get("entity_identity"), Mapping)
            else current.get("finding_identity")
            if isinstance(current.get("finding_identity"), Mapping)
            else {}
        ),
        "finding_present": finding_present,
        "resolved": bool(
            not detector_blocker
            and not finding_present
            and not target_unlocated
            and not regressions
        ),
        "route_family": _ROUTE_FAMILIES.get(smell, "close-frozen-finding"),
        "objective_deficits": objectives,
        "metric_budget": metric_budget,
        "worklist": worklist,
        # The prompt receives only a bounded priority batch, while this count
        # is computed from the complete target-Guard snapshot.  Large LPL/RB/DC
        # closures therefore never masquerade as a 16/24/31-item problem.
        "remaining_work_count": (
            (worklist_total or 1)
            if detector_blocker or finding_present or target_unlocated
            else 0
        ),
        "worklist_complete": (
            not detector_blocker
            and (
                not finding_present
                or worklist_total <= sum(
                    1 for item in worklist if item.get("kind") != "frozen_finding"
                )
            )
        ),
        "worklist_artifact": "checkpoint_manifest.current_metrics",
        "semantic_regressions": regressions,
        "target_unlocated": target_unlocated,
        "detector_blocker": detector_blocker,
        "next_action": next_action,
        "forbidden": list(_FORBIDDEN.get(smell, ())),
        "authority": "target_guard_and_frozen_checkpoint",
    }


def resolution_plan_next_action(value: Mapping[str, Any] | None) -> str:
    if not isinstance(value, Mapping):
        return ""
    return " ".join(str(value.get("next_action") or "").split())


def _objective_deficits(
    smell: str,
    *,
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    objective_delta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    before = _numeric_objectives(baseline.get("objectives"))
    after = _numeric_objectives(current.get("objectives"))
    profile = current.get("guard_profile") or current.get("detector_profile")
    if not isinstance(profile, Mapping):
        profile = baseline.get("guard_profile") or baseline.get("detector_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    preferred = _PRIMARY_OBJECTIVES.get(smell, tuple(after))
    names = [name for name in preferred if name in after]
    if not names:
        names = sorted(after)
    result: list[dict[str, Any]] = []
    for name in names:
        target_max = _passing_max(smell, name, current=current, profile=profile)
        value = after[name]
        remaining = max(0.0, value - target_max) if target_max is not None else None
        recorded = objective_delta.get(name)
        reduction = (
            float(recorded.get("absolute_reduction") or 0.0)
            if isinstance(recorded, Mapping)
            else before.get(name, value) - value
        )
        item: dict[str, Any] = {
            "name": name,
            "baseline": _compact_number(before.get(name, value)),
            "current": _compact_number(value),
            "reduction": _compact_number(reduction),
        }
        if target_max is None:
            item.update({
                "passing_condition": "product profile no longer reports the frozen finding",
                "remaining": None,
            })
        else:
            item.update({
                "passing_max": _compact_number(target_max),
                "remaining": _compact_number(remaining or 0.0),
            })
        result.append(item)
    return result


def _passing_max(
    smell: str,
    name: str,
    *,
    current: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> float | None:
    if smell == "data_clumps" and name == "occurrence_count":
        minimum = _number(profile.get("min_occurrences"))
        return max(0.0, minimum - 1.0) if minimum is not None else None
    if smell == "feature_envy" and name == "envy_access_diff":
        return _number(profile.get("finding_min_exclusive"))
    if smell == "code_clone_type1" and name == "clone_token_count":
        minimum = _number(profile.get("finding_min_tokens"))
        return max(0.0, minimum - 1.0) if minimum is not None else None
    if smell in {"refused_bequest", "switch_statements", "mysterious_name", "dead_code"}:
        return 0.0
    minimum = _number(profile.get("finding_min"))
    if minimum is not None:
        return max(0.0, minimum - 1.0)
    # God Class is a multi-signal predicate. Each scalar target is deliberately
    # left profile-owned rather than reimplemented here.
    return None


def _metric_budget(
    smell: str,
    *,
    current: Mapping[str, Any],
    objectives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return prompt-safe scalar planning budgets, never dependency closure.

    These values help size the first coherent edit. They are not an alternate
    verdict: target identity, semantic contracts and project verification
    remain authoritative.
    """
    if smell == "feature_envy":
        return _feature_envy_metric_budget(current)
    if smell == "god_class":
        return _god_class_metric_budget(current)

    profile = current.get("guard_profile") or current.get("detector_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    display_override = {
        "long_method": str(profile.get("metric") or "meaningful_line_count"),
        "nested_complexity": str(profile.get("metric") or "max_nesting_depth"),
    }.get(smell)
    result: list[dict[str, Any]] = []
    for item in objectives:
        current_value = _number(item.get("current"))
        passing_max = _number(item.get("passing_max"))
        required = _number(item.get("remaining"))
        if current_value is None or passing_max is None or required is None:
            continue
        metric = display_override or str(item.get("name") or "")
        if not metric:
            continue
        result.append({
            "metric": metric,
            "current": _compact_number(current_value),
            "passing_max": _compact_number(passing_max),
            "required_reduction": _compact_number(required),
            "unit": metric,
        })
    return result[:8]


def _feature_envy_metric_budget(
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    access = _number(current.get("guard_receiver_access"))
    access_max = _number(current.get("guard_receiver_access_passing_max"))
    access_required = _number(
        current.get("guard_receiver_access_required_reduction")
    )
    ratio = _number(current.get("guard_receiver_ratio"))
    ratio_boundary = _number(current.get("guard_receiver_ratio_finding_min"))
    ratio_required = _number(
        current.get("guard_receiver_ratio_required_access_reduction")
    )
    result: list[dict[str, Any]] = []
    if None not in (access, access_max, access_required):
        result.append({
            "metric": "receiver_access",
            "current": _compact_number(access),
            "passing_max": _compact_number(access_max),
            "required_reduction": _compact_number(access_required),
            "unit": "receiver_access",
        })
    if None not in (access, ratio, ratio_boundary, ratio_required):
        ratio_route_passing_max = max(0.0, access - ratio_required)
        result.append({
            # Keep every row arithmetically executable: current, boundary and
            # reduction use the same receiver-access unit.  The metric name
            # retains the exact ratio route whose boundary produced this
            # access budget.
            "metric": (
                "receiver_access_for_ratio_lt_"
                f"{_compact_number(ratio_boundary)}"
            ),
            "current": _compact_number(access),
            "passing_max": _compact_number(ratio_route_passing_max),
            "required_reduction": _compact_number(ratio_required),
            "unit": "receiver_access",
        })
    return result


def _god_class_metric_budget(
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    profile = current.get("god_class_profile")
    if not isinstance(profile, Mapping):
        return []
    result: list[dict[str, Any]] = []
    mandatory = profile.get("mandatory")
    if not isinstance(mandatory, list):
        return []
    for value in mandatory:
        if not isinstance(value, Mapping) or value.get("matched") is not True:
            continue
        name = str(value.get("name") or "")
        if name not in {"nom", "wmc"}:
            continue
        current_value = _number(value.get("value"))
        boundary = _number(value.get("boundary"))
        if current_value is None or boundary is None:
            continue
        result.append({
            # The finding predicate requires every mandatory condition.  Each
            # row below is therefore a complete scalar OR route by itself.
            # Individual signal thresholds are deliberately omitted because
            # crossing one may still leave the required signal count matched.
            "metric": f"god_class_mandatory_{name}",
            "current": _compact_number(current_value),
            "passing_max": _compact_number(max(0.0, boundary - 1.0)),
            "required_reduction": _compact_number(
                max(0.0, current_value - boundary + 1.0)
            ),
            "unit": name,
        })
    return result


def _worklist(
    smell: str,
    *,
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    actionable_total: int | None = None
    identity = frozen.get("entity_identity")
    if not isinstance(identity, Mapping):
        identity = current.get("finding_identity")
    if isinstance(identity, Mapping) and identity:
        items.append({"kind": "frozen_finding", **_compact_mapping(identity)})

    if smell == "feature_envy":
        receiver = str(current.get("expected_receiver_type") or current.get("envied_type") or "")
        field = str(current.get("envied_field") or "")
        accesses = current.get("receiver_access_worklist")
        if isinstance(accesses, list):
            items.extend(_mapping_items("receiver_access", accesses))
        elif receiver or field:
            items.append({"kind": "receiver_cluster", "receiver_type": receiver, "field": field})
    elif smell == "long_parameter_list":
        closure = current.get("migration_closure")
        if isinstance(closure, Mapping):
            for key, kind in (
                ("declarations", "declaration"),
                ("constructor_chain", "constructor_chain"),
                ("production_call_sites", "production_call_site"),
                ("method_references", "method_reference"),
                ("unresolved_sites", "unresolved_site"),
            ):
                values = closure.get(key)
                if isinstance(values, list):
                    items.extend(_mapping_items(kind, values))
    elif smell == "data_clumps":
        witness = current.get("witness")
        occurrences = (
            witness.get("occurrences")
            if isinstance(witness, Mapping)
            else None
        )
        if isinstance(occurrences, list):
            items.extend(
                _mapping_items("remaining_occurrence", occurrences)
            )
        occurrence_count = _number(
            (current.get("objectives") or {}).get("occurrence_count")
            if isinstance(current.get("objectives"), Mapping)
            else None
        )
        if occurrence_count is not None:
            actionable_total = max(0, math.ceil(occurrence_count))
    elif smell == "code_clone_type1":
        structure = current.get("clone_structure")
        endpoints = structure.get("endpoints") if isinstance(structure, Mapping) else None
        if isinstance(endpoints, list):
            items.extend(_mapping_items("clone_endpoint", endpoints))
    elif smell == "god_class":
        profile = current.get("god_class_profile")
        clusters = current.get("responsibility_clusters")
        if not isinstance(clusters, list) and isinstance(profile, Mapping):
            clusters = profile.get("responsibility_clusters")
        if isinstance(clusters, list):
            items.extend(
                _god_class_cluster_item(value)
                for value in clusters
                if isinstance(value, Mapping)
            )
        if isinstance(profile, Mapping):
            for section, kind in (("mandatory", "mandatory_signal"), ("signals", "profile_signal")):
                values = profile.get(section)
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, Mapping) or value.get("matched") is not True:
                        continue
                    items.append({
                        "kind": kind,
                        "name": str(value.get("name") or ""),
                        "operator": str(value.get("operator") or ""),
                        "boundary": value.get("boundary", value.get("boundaries")),
                        "value": value.get("value", value.get("values")),
                    })
    deduped = _dedupe(items)
    frozen_items = [item for item in deduped if item.get("kind") == "frozen_finding"]
    actionable = [item for item in deduped if item.get("kind") != "frozen_finding"]
    # Full closure data remains in current_metrics.  Duplicate only the first
    # deterministic batch in the plan to keep bridge/plugin payloads bounded.
    return (
        [*frozen_items[:1], *actionable[:32]],
        max(len(actionable), actionable_total or 0),
    )


def _next_action(
    smell: str,
    *,
    current: Mapping[str, Any],
    objectives: list[dict[str, Any]],
    worklist: list[dict[str, Any]],
) -> str:
    detector_blocker = _current_detector_blocker(current)
    if detector_blocker == "CURRENT_DETECTOR_UNAVAILABLE":
        return "restore target Guard availability and obtain one valid current snapshot before making further source edits"
    if detector_blocker == "TARGET_AMBIGUOUS":
        return "restore an unambiguous identity for the frozen target; do not treat multiple Guard matches as resolution"
    if detector_blocker:
        return "repair the invalid target Guard result before evaluating structural progress"
    remaining = _largest_remaining(objectives)
    if (
        current.get("target_missing") is True
        and current.get("target_absence_allowed") is not True
    ):
        return "restore or re-anchor the frozen source entity; target disappearance is not a resolved finding"
    if current.get("target_smell_present", current.get("finding_present")) is not True:
        return "preserve the resolved source structure and repair only the reported semantic or build/test regression"
    if smell == "long_method":
        return f"extract cohesive blocks totaling at least {remaining} AST-NCSS from the frozen method, then remove obsolete wrappers"
    if smell == "nested_complexity":
        return f"remove at least {remaining} complexity points from the highest-contributing nested branches using guard clauses or cohesive extraction"
    if smell == "long_parameter_list":
        unresolved = _work_count(worklist, {"unresolved_site"})
        count = _work_count(worklist, {
            "declaration",
            "constructor_chain",
            "production_call_site",
            "method_reference",
            "unresolved_site",
        })
        suffix = f" across the {count} listed closure entities" if count else " across every declaration, caller, override, constructor call, and method reference"
        review = f"; resolve the {unresolved} ambiguous site(s) before editing" if unresolved else ""
        action = "migrate the frozen long signature to one typed parameter object" + suffix + review + "; delete the old long signature instead of retaining a delegate"
        preferred = {"unresolved_site"} if unresolved else {
            "declaration", "constructor_chain", "production_call_site", "method_reference"
        }
        return _with_priority_item(action, worklist, preferred)
    if smell == "feature_envy":
        receiver = str(current.get("expected_receiver_type") or current.get("envied_type") or "frozen receiver")
        diff = _objective_current(objectives, "envy_access_diff")
        boundary = next(
            (
                item.get("passing_max")
                for item in objectives
                if item.get("name") == "envy_access_diff"
            ),
            "the product boundary",
        )
        action = (
            f"reduce the frozen envy_access_diff from {_compact_number(diff) if diff is not None else 'its current value'} "
            f"to <= {boundary} by closing the {receiver} collaboration as one receiver-owned operation or one independent workflow; "
            "do not manufacture self-accesses and do not force raw receiver access to zero"
        )
        return _with_priority_item(action, worklist, {"receiver_access", "receiver_cluster"})
    if smell == "data_clumps":
        remaining = _objective_remaining(objectives, "occurrence_count")
        budget = (
            f"at least {max(1, math.ceil(remaining))} occurrence(s)"
            if remaining is not None and remaining > 0
            else "the next complete occurrence family"
        )
        return _with_priority_item(
            f"migrate {budget} from the frozen scoped occurrence witness in one semantic component to one cohesive typed holder; update every affected production caller and method reference, then remove every old-group wrapper",
            worklist,
            {"remaining_occurrence"},
        )
    if smell == "code_clone_type1":
        return _with_priority_item(
            "redirect both frozen clone endpoints to one shared implementation and remove both duplicate bodies; do not perturb or relocate tokens",
            worklist,
            {"clone_endpoint"},
        )
    if smell == "god_class":
        cluster = next(
            (
                item
                for item in worklist
                if item.get("kind") == "responsibility_cluster"
            ),
            None,
        )
        triggered = [
            str(item.get("name") or "")
            for item in worklist
            if str(item.get("kind") or "") == "profile_signal"
            and str(item.get("name") or "")
        ]
        suffix = f"; currently triggered profile signals: {', '.join(triggered)}" if triggered else ""
        if isinstance(cluster, Mapping):
            fields = ", ".join(str(value) for value in cluster.get("field_names", [])[:6]) or "no owner fields"
            methods = ", ".join(str(value) for value in cluster.get("method_signatures", [])[:6])
            reductions = (
                f"NOM -{cluster.get('nom_reduction', 0)}, "
                f"NOF -{cluster.get('nof_reduction', 0)}, "
                f"WMC -{cluster.get('wmc_reduction', 0)}, "
                f"LOC -{cluster.get('loc_reduction', 0)}"
            )
            return (
                f"extract source-derived rank-{cluster.get('rank', 1)} responsibility "
                f"{cluster.get('cluster_id', '')} as one cohesive component "
                f"(fields: {fields}; methods: {methods}; projected {reductions}); "
                "remove the superseded members from the original class until the complete product profile becomes false"
                + suffix
            )
        return (
            "no source-derived field/method responsibility cluster is available; "
            "reduce the highest-complexity behavior directly and remove superseded members "
            "until the complete product profile becomes false"
            + suffix
        )
    if smell == "refused_bequest":
        return _with_priority_item(
            "complete the listed inheritance capability migration and remove the rejecting override without creating a rejecting implementation elsewhere in the affected hierarchy",
            worklist,
            {"contract_declaration", "implementer", "production_call_site"},
        )
    if smell == "switch_statements":
        count = int(
            _number(current.get("switch_count"))
            or _objective_current(objectives, "switch_count")
            or 0
        )
        return f"remove all {count} remaining switch statement(s) from the frozen method through one coherent dispatch strategy"
    if smell == "mysterious_name":
        name = str(current.get("target_name") or "the frozen symbol")
        return f"rename {name} meaningfully across its complete production symbol-reference closure and migrate test API references only when the controller authorizes test changes"
    if smell == "dead_code":
        name = str(current.get("target_name") or "the frozen declaration")
        return f"remove {name} and any private declarations reachable only from it, then run fresh project tests"
    return "complete the remaining frozen-finding worklist and call smell_verify again"


def _semantic_regressions(delta: Mapping[str, Any] | None) -> list[str]:
    semantic = (delta or {}).get("semantic_contract")
    regressions = semantic.get("regressions") if isinstance(semantic, Mapping) else None
    if not isinstance(regressions, list):
        return []
    return [str(item) for item in regressions if str(item).strip()]


def _mapping_items(kind: str, values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        result.append({"kind": kind, **_compact_mapping(value)})
    return result


def _god_class_cluster_item(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = value.get("fields")
    methods = value.get("methods")
    return {
        "kind": "responsibility_cluster",
        "rank": int(value.get("rank") or 0),
        "cluster_id": str(value.get("cluster_id") or ""),
        "cluster_kind": str(value.get("kind") or ""),
        "method_count": int(value.get("method_count") or 0),
        "field_count": int(value.get("field_count") or 0),
        "nom_reduction": int(value.get("nom_reduction") or 0),
        "nof_reduction": int(value.get("nof_reduction") or 0),
        "wmc_reduction": int(value.get("wmc_reduction") or 0),
        "loc_reduction": int(value.get("loc_reduction") or 0),
        "cohesion": value.get("cohesion"),
        "field_names": [
            str(item.get("name") or "")
            for item in fields or []
            if isinstance(item, Mapping) and str(item.get("name") or "")
        ],
        "method_signatures": [
            str(item.get("signature") or "")
            for item in methods or []
            if isinstance(item, Mapping) and str(item.get("signature") or "")
        ],
        "omitted_field_count": int(value.get("omitted_field_count") or 0),
        "omitted_method_count": int(value.get("omitted_method_count") or 0),
    }


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "file", "class", "class_name", "owner", "method", "signature", "line",
        "begin_line",
        "parameter_types", "group", "field", "envied_field", "envied_type",
        "receiver", "static_receiver_type", "expression", "role",
        "receiver_type", "member", "access_kind", "reason",
        "declared_method", "effective_method", "implementation_kind",
        "relationship", "body_kind", "resolved_owner", "resolved_signature",
        "receiver_resolution", "target_owner", "target_signature",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        raw = value.get(key)
        if isinstance(raw, (str, int, float, bool)) and raw not in {"", None}:
            result[key] = raw
        elif isinstance(raw, list) and raw:
            result[key] = [str(item) for item in raw[:12]]
        elif isinstance(raw, Mapping) and raw:
            result[key] = _compact_mapping(raw)
    return result


def _dedupe(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = repr(sorted((name, repr(raw)) for name, raw in value.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _work_count(worklist: Iterable[Mapping[str, Any]], kinds: set[str]) -> int:
    return sum(1 for item in worklist if str(item.get("kind") or "") in kinds)


def _with_priority_item(
    action: str,
    worklist: Iterable[Mapping[str, Any]],
    preferred_kinds: set[str],
) -> str:
    item = next(
        (
            value
            for value in worklist
            if str(value.get("kind") or "") in preferred_kinds
        ),
        None,
    )
    if not isinstance(item, Mapping):
        return action
    parts: list[str] = []
    file_name = str(item.get("file") or "")
    line = item.get("begin_line")
    if not isinstance(line, int):
        line = item.get("line")
    if file_name:
        parts.append(f"{file_name}:{line}" if isinstance(line, int) else file_name)
    signature = str(
        item.get("signature")
        or item.get("resolved_signature")
        or item.get("target_signature")
        or item.get("method")
        or ""
    )
    if signature:
        parts.append(signature)
    expression = str(item.get("expression") or item.get("member") or "")
    if expression:
        parts.append(expression)
    return action + (f"; start with {' | '.join(parts)}" if parts else "")


def _objective_current(
    objectives: Iterable[Mapping[str, Any]],
    name: str,
) -> float | None:
    for item in objectives:
        if str(item.get("name") or "") != name:
            continue
        return _number(item.get("current"))
    return None


def _objective_remaining(
    objectives: Iterable[Mapping[str, Any]],
    name: str,
) -> float | None:
    for item in objectives:
        if str(item.get("name") or "") != name:
            continue
        return _number(item.get("remaining"))
    return None


def _largest_remaining(objectives: Iterable[Mapping[str, Any]]) -> str:
    values = [
        float(item.get("remaining"))
        for item in objectives
        if isinstance(item.get("remaining"), (int, float))
        and not isinstance(item.get("remaining"), bool)
    ]
    value = max(values) if values else 1.0
    return str(int(value)) if value.is_integer() else f"{value:.6g}"


def _numeric_objectives(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): float(raw)
        for name, raw in value.items()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool)
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _current_detector_blocker(current: Mapping[str, Any]) -> str:
    if current.get("ok") is not True:
        return "CURRENT_DETECTOR_UNAVAILABLE"
    raw_count = current.get("target_match_count", current.get("candidate_count"))
    if raw_count is None:
        return ""
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


def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 6)
