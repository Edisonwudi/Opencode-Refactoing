from __future__ import annotations

import json
from typing import Any


_SMELL_BLOCKERS = {
    "long_method": ["invalid extractable selection", "extraction would only move complexity sideways"],
    "long_parameter_list": ["framework entrypoint", "public API surface", "reflection or generated callers", "protocol signature"],
    "nested_complexity": ["no coherent extractable block", "flattening changes control-flow semantics"],
    "switch_statements": ["branch behavior lacks stable ownership", "strategy/table route would broaden scope"],
    "code_clone_type1": ["no shared semantic owner", "shared parent should stay generic"],
    "data_clumps": ["parameters do not form a stable domain group", "cross-method family cannot be migrated safely"],
    "mysterious_name": ["identifier is externally referenced by string/protocol", "rename target is ambiguous"],
    "refused_bequest": ["parent contract requires override", "hierarchy ownership is not actually wrong"],
    "dead_code": [
        "safe delete reports real references",
        "target is public or framework-visible",
        "target may be used by reflection, serialization, or lifecycle hooks",
    ],
    "god_class": [
        "no cohesive responsibility cluster",
        "public API or framework contract prevents extraction",
        "candidate split leaves target class above guard thresholds",
        "native extract:class cannot migrate the needed field and behavior cluster",
        "method cluster has no existing owner for move:method",
        "split would require preserving wrapper methods on the reported class",
        "field cluster cannot be separated without duplicating mutable state",
    ],
}

def build_refactor_paths(smell: str, route_payload: dict[str, Any]) -> list[dict[str, Any]]:
    examples = route_payload.get("examples") or []
    paths: list[dict[str, Any]] = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        path = _example_refactor_path(smell, example)
        if path:
            paths.append(path)
    if paths:
        return paths

    preferred = route_payload.get("preferred_operations") or []
    return [
        {
            "id": f"{smell}-smallest-valid-refactor",
            "intent": "Apply the smallest behavior-preserving refactor that directly resolves the reported smell.",
            "preferred_operations": [str(item) for item in preferred if str(item).strip()],
            "when_to_use": "No local verified refactor path is defined for this smell, so use the smell guide and source inspection.",
            "blockers": _SMELL_BLOCKERS.get(smell, ["no behavior-preserving route found"]),
            "action_chain": [],
            "required_end_state": "The reported smell guard no longer reports the target while behavior remains unchanged.",
        }
    ]


def _example_refactor_path(smell: str, example: dict[str, Any]) -> dict[str, Any] | None:
    path_id = str(example.get("id") or "").strip()
    if not path_id:
        return None
    actions = _example_action_chain(example)
    operations = [str(action["operation"]) for action in actions]
    when_to_use = str(example.get("when") or "").strip()
    before_summary = _summarize_block(example.get("before"))
    after_summary = _summarize_block(example.get("after"))
    path = {
        "id": path_id,
        "intent": _intent_from_example(path_id, when_to_use),
        "preferred_operations": operations,
        "when_to_use": when_to_use or "Use when this verified refactor path matches the current smell structure.",
        "blockers": _SMELL_BLOCKERS.get(smell, ["operation is unavailable for the current target"]),
        "action_chain": actions,
        "required_end_state": _required_end_state(smell, after_summary),
    }
    if before_summary:
        path["before_summary"] = before_summary
    if after_summary:
        path["after_summary"] = after_summary
    source = example.get("source")
    if isinstance(source, dict):
        path["source"] = {str(key): value for key, value in source.items()}
    notes = _normalize_notes(example.get("notes"))
    if notes:
        path["notes"] = notes
    return path


def _example_action_chain(example: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for index, raw_action in enumerate(example.get("actions", []) or [], start=1):
        if not isinstance(raw_action, dict):
            continue
        operation = str(raw_action.get("op") or "").strip()
        if not operation:
            continue
        action: dict[str, Any] = {
            "index": index,
            "operation": operation,
            "apply_args": _parse_apply_args(raw_action.get("apply_args")),
            "cli_steps": [str(step) for step in raw_action.get("cli_steps", []) or [] if str(step).strip()],
        }
        argument_contract = str(raw_action.get("argument_contract") or "").strip()
        if argument_contract:
            action["argument_contract"] = argument_contract
        ui_steps = [str(step) for step in raw_action.get("idea_ui_steps", []) or [] if str(step).strip()]
        if ui_steps:
            action["idea_ui_steps"] = ui_steps
        notes = _normalize_notes(raw_action.get("notes"))
        if notes:
            action["notes"] = notes
        actions.append(action)
    return actions


def _example_operations(example: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    operations: list[str] = []
    for action in example.get("actions", []) or []:
        if not isinstance(action, dict):
            continue
        op = str(action.get("op") or "").strip()
        if not op or op in seen:
            continue
        seen.add(op)
        operations.append(op)
    return operations


def _parse_apply_args(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _normalize_notes(raw_notes: Any) -> list[str]:
    if raw_notes is None:
        return []
    if isinstance(raw_notes, list):
        return [str(note).strip() for note in raw_notes if str(note).strip()]
    note = str(raw_notes).strip()
    return [note] if note else []


def _summarize_block(raw: Any, *, max_lines: int = 12) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head_count = max_lines // 2
    tail_count = max_lines - head_count
    return "\n".join(lines[:head_count] + ["..."] + lines[-tail_count:])


def _intent_from_example(path_id: str, when_to_use: str) -> str:
    if when_to_use:
        return when_to_use[0].upper() + when_to_use[1:]
    return _intent_from_path_id(path_id)


def _required_end_state(smell: str, after_summary: str) -> str:
    base = "Complete every action_chain step in order; do not stop after a prefix of the chain."
    if smell == "feature_envy":
        return (
            base
            + " The foreign-heavy behavior must end on the envied receiver or an equivalent receiver-side helper, "
            + "and the source target must no longer be reported by the feature_envy guard. "
            + "For extract-then-move paths, a helper left in the original source class is an incomplete prefix, "
            + "not a valid final state."
        )
    if smell == "switch_statements":
        return (
            base
            + " The switch dispatch shape must be replaced or reduced according to the chosen path, "
            + "not merely hidden behind helper extraction. For switch_statements, extract:method is only an intermediate step: "
            + "the final plan must use idea_edit old/new patches or an equivalent final rewrite to remove the original switch "
            + "or significantly reduce its case count."
        )
    if smell == "code_clone_type1":
        return base + " Both clone targets must be eliminated or delegated to the same shared implementation."
    if smell == "data_clumps":
        return (
            base
            + " The repeated parameter group must be migrated with a single anchor parameter object and residual cleanup. "
            + "Use native introduce:parameter-object once for the safest anchor signature family that can create the holder "
            + "and migrate its call sites. Use group_occurrences and verify remaining_occurrences as the residual cleanup "
            + "worklist; migrate ordinary remaining helpers and local callers to the existing holder with idea_edit old/new "
            + "patches. Do not repeat native introduce:parameter-object for the same reported group and holder name. "
            + "Do not preserve or add backward-compatible overloads that keep the old repeated parameter "
            + "group; remove or migrate old-parameter wrappers when SMELL_GUARD_FAILED reports them. Do not create the holder "
            + "with direct_edit before native introduce:parameter-object unless IDEA has produced a concrete blocker for holder creation."
        )
    if smell == "refused_bequest":
        return (
            base
            + " Removing or inlining a throwing override is valid only when the plan marks the contract risk "
            + "and proves the inherited parent behavior is acceptable for existing callers. "
            + "Use the evidence refactor_path as the primary repair route and treat the supplied test as an immutable "
            + "project-behavior regression oracle, not as production code to edit. Logging, comments, swallowed exceptions, "
            + "and placeholder constants are not valid contract implementations. For relaxed clear-path rows, broader hierarchy "
            + "or interface changes should be planned as explicit ordered steps with risk tags."
        )
    if smell == "god_class":
        return (
            base
            + " Move one cohesive responsibility cluster out of the reported class, and the reported class must no "
            + "longer be reported by the god_class guard. For field-heavy classes, prefer native `extract:class` so "
            + "the field cluster and its maintaining behavior move together. Private wrappers left after extraction "
            + "must be removed with `idea_edit`; a retained wrapper family is an incomplete prefix, not a final "
            + "state. Use `move:method` only when a real existing owner already owns the behavior. Use `idea_edit` "
            + "old/new patches only as the auxiliary route when native `extract:class` cannot migrate the "
            + "needed members. Do not satisfy this by replacing a large reported class wholesale; the reported class "
            + "should be shrunk with targeted `idea_edit` patches and native `move:method` steps."
        )
    if after_summary:
        return base + " The final source shape should match the after_summary at the same abstraction level."
    return base + " The reported smell guard no longer reports the target while behavior remains unchanged."


def _intent_from_path_id(path_id: str) -> str:
    words = path_id.replace("_", "-").split("-")
    return " ".join(word for word in words if word).capitalize() + "."


def build_direct_edit_policy() -> dict[str, Any]:
    return {
        "allowed_when": [
            "repairing reflection getDeclaredMethod/getMethod/invoke signatures after production signature migration",
            "updating string configs, XML, YAML, properties, fixtures, or test data",
            "making narrow test-entry repairs that IDEA cannot represent",
            "reporting or working around an IDEA edit/refactor blocker without broad Java text rewrites",
        ],
        "not_allowed_when": [
            "performing broad Java source rewrites that native IDEA refactorings can express",
            "planning a Java source step as direct_edit when idea_edit can apply an IDEA-backed oldString/newString patch",
            "planning a Java source step as direct_edit when its refactor_paths preferred_operations name IDEA operations and no concrete IDEA blocker has been observed",
            "rewriting Java files with sed/perl/python/cat shell commands",
            "weakening tests to hide behavior regressions",
        ],
    }


def build_plan_context_payload(*, resolved: Any, context_payload: dict[str, Any], route_payload: dict[str, Any]) -> dict[str, Any]:
    locations = context_payload.get("locations") or []
    return {
        "success": True,
        "project_root": str(resolved.project_root),
        "roots": context_payload.get("roots") or {},
        "language": resolved.language,
        "smell": resolved.smell,
        "targets": [
            {
                "display_path": item.get("display_path"),
                "project_path": item.get("project_path"),
                "idea_project_path": item.get("idea_project_path"),
                "file_path": item.get("file_path"),
                "line": item.get("line"),
                "method": item.get("method"),
                "class_name": item.get("class_name"),
                "signature_text": item.get("signature_text"),
                "parameter_count": item.get("parameter_count"),
            }
            for item in locations
            if isinstance(item, dict)
        ],
        "profile": context_payload.get("profile") or {},
        "idea": {
            "ready": bool(context_payload.get("idea_refactor_ready")),
            "cli": context_payload.get("idea_refactor_cli"),
            "root": (context_payload.get("idea") or {}).get("root"),
            "recommended_skill": "idea-refactor-cli",
        },
        "refactor_paths": build_refactor_paths(resolved.smell, route_payload),
        "smell_guide": route_payload.get("guide") or "",
        "direct_edit_policy": build_direct_edit_policy(),
    }


def build_repair_context_payload(*, context_payload: dict[str, Any], route_payload: dict[str, Any]) -> dict[str, Any]:
    idea_context = context_payload.get("idea") or {}
    idea_recommended_skill = str(idea_context.get("recommended_skill") or "")
    idea_ready = bool(context_payload.get("idea_refactor_ready")) if idea_recommended_skill else False
    return {
        "success": True,
        "mode": "repair",
        "context": context_payload,
        "idea": {
            "ready": idea_ready,
            "cli": context_payload.get("idea_refactor_cli") if idea_recommended_skill else None,
            "root": idea_context.get("root") if idea_recommended_skill else "",
            "recommended_skill": idea_recommended_skill,
        },
        "refactor_paths": build_refactor_paths(str(context_payload.get("smell") or ""), route_payload),
        "smell_guide": route_payload.get("guide") or "",
        "execution_contract": {
            "read_source_with_opencode_tools": True,
            "use_smell_verify_for_build_test": True,
            "use_idea_cli_for_planned_idea_routes": bool(idea_recommended_skill),
            "fallback_requires_concrete_idea_blocker": bool(idea_recommended_skill),
        },
    }
