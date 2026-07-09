from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from .examples import retrieve_refactor_examples
from .idea_guides import build_smell_specific_idea_guide

if TYPE_CHECKING:
    from ..config import ResolvedRunConfig


@dataclass(frozen=True)
class IdeaPromptRoute:
    smell: str
    guide: str
    examples: Tuple[Dict[str, Any], ...]
    route_ids: Tuple[str, ...]
    preferred_operations: Tuple[str, ...]


def build_idea_prompt_route(cli_path: str, project_root, config: "ResolvedRunConfig") -> IdeaPromptRoute:
    examples = tuple(retrieve_refactor_examples(config))
    return IdeaPromptRoute(
        smell=config.smell,
        guide=build_smell_specific_idea_guide(cli_path, project_root, config),
        examples=examples,
        route_ids=_collect_route_ids(examples),
        preferred_operations=_extract_operations(examples),
    )


def render_idea_prompt_route(route: IdeaPromptRoute) -> str:
    lines: List[str] = ["Prompt route:"]
    lines.append(f"- Smell route: `{route.smell}`.")
    if route.route_ids:
        route_list = ", ".join(f"`{item}`" for item in route.route_ids)
        lines.append(f"- Loaded local refactor paths for this smell: {route_list}.")
    else:
        lines.append("- No local refactor paths are defined for this smell; use the canonical smell guidance below.")
    if route.preferred_operations:
        operations = ", ".join(f"`{item}`" for item in route.preferred_operations)
        lines.append(f"- Preferred IDEA operations across local examples: {operations}.")
    lines.append("")

    if route.examples:
        lines.append("Local refactor path examples for this smell:")
        lines.append("- These examples are guidance for plausible refactoring shapes under this smell.")
        lines.append("- Do not try to reproduce their structure mechanically; choose the smallest valid refactoring that fits the current target.")
        lines.extend(_render_route_selection_rules(route))
        lines.append("")
        for index, example in enumerate(route.examples, start=1):
            lines.extend(_render_example(example, index=index))
            lines.append("")

    lines.append(route.guide)
    return "\n".join(lines).rstrip() + "\n"


def _render_route_selection_rules(route: IdeaPromptRoute) -> List[str]:
    if route.smell == "switch_statements":
        return [
            "- For switch_statements, first classify the target against the loaded examples and choose the closest matching route.",
            "- Prefer a loaded table-driven, handler/strategy, or state-polymorphism route when it fits without broad unrelated changes.",
            "- Do not default to helper extraction while a loaded switch route directly fits the current target.",
            "- If none of the loaded switch routes fit, state why before choosing the smallest behavior-preserving local refactoring.",
        ]
    if route.smell == "data_clumps":
        return [
            "- For data_clumps, use `introduce:parameter-object` once on the safest anchor signature family; it creates the holder and migrates that family's call sites.",
            "- Treat group_occurrences as the residual cleanup worklist. Do not repeat native introduce:parameter-object for the same reported group and holder name.",
            "- Treat `idea_edit` actions as follow-up migration to the existing holder for ordinary helpers and their local callers, not as a way to create a second holder.",
            "- Do not create the holder with direct_edit before native introduce:parameter-object unless IDEA reports a concrete blocker for holder creation.",
            "- Do not preserve or add backward-compatible overloads that keep the old repeated parameter group; when SMELL_GUARD_FAILED reports them, remove or migrate old-parameter wrappers.",
            "- Ordinary `idea_edit` cleanup must provide a unique oldString and the exact newString patch; do not use replaceAll for ordinary Java source cleanup.",
            "- Do not expect an old/new patch to wrap old arguments into `new Holder(...)` at arbitrary call sites; use native parameter-object refactoring or explicitly migrate those call sites first.",
        ]
    if route.smell == "god_class":
        return [
            "- For god_class, choose one cohesive responsibility cluster and a route that moves ownership out of the reported class.",
            "- Prefer `extract-class-state-and-behavior` when the current class has an obvious field/state cluster such as cache, font, text, image, or geometry state.",
            "- The extract-class route is complete only after the field cluster and maintaining methods leave the reported class; private wrappers left by IDEA must be cleaned with `idea_edit`.",
            "- Choose `move-method-cluster-to-owner` only when an existing real owner/collaborator already owns the behavior. Do not use it to force a field cluster into an arbitrary receiver.",
            "- Choose `insert-type-state-cluster-member-migration` only as an auxiliary route when native `extract:class` is unavailable or cannot migrate the needed members.",
            "- In the auxiliary route, use targeted `idea_edit` old/new patches. Do not plan a whole-class patch on a large reported god class.",
            "- Do not choose wrapper-only routes as complete god_class repairs; they are only prefixes if follow-up actions shrink the original class below the guard threshold.",
            "- Complete every action in the selected action_chain before verify, because the god_class guard only accepts the final class-level shape.",
        ]
    if route.smell == "dead_code":
        return [
            "- For dead_code, use the loaded safe-delete route only for the reported unused private member.",
            "- Prefer the native safe-delete route; do not replace the method body with a stub or comment it out.",
            "- If IDEA reports references or framework/override blockers, stop and report the blocker instead of forcing deletion.",
            "- Do not delete adjacent methods or unrelated unused-looking members in the same file.",
        ]
    return []


def _render_example(example: Dict[str, Any], *, index: int) -> List[str]:
    route_id = _clean_route_id(example)
    heading = f"Example {index}"
    if route_id:
        heading += f" ({route_id})"
    lines: List[str] = [heading + ":", ""]
    when_text = str(example.get("when") or "").strip()
    if when_text:
        lines.append(f"- Scenario: {when_text}")
    source = example.get("source")
    if isinstance(source, dict):
        project = str(source.get("project") or "").strip()
        klass = str(source.get("class") or "").strip()
        verified = str(source.get("verified") or "").strip()
        source_parts = [part for part in [project, klass, verified and f"verified {verified}"] if part]
        if source_parts:
            lines.append(f"- Source: {', '.join(source_parts)}")
    lines.append("")
    lines.append("BEFORE:")
    for ln in str(example.get("before") or "").strip().splitlines():
        lines.append(f"  {ln}")
    lines.append("")
    lines.append("ACTIONS:")
    for action in example.get("actions", []) or []:
        op = str(action.get("op") or "").strip()
        args = str(action.get("apply_args") or "").strip()
        argument_contract = str(action.get("argument_contract") or "").strip()
        idea_ui_steps = action.get("idea_ui_steps", [])
        if op:
            lines.append(f"  - {op}")
        if args:
            lines.append(f"    apply arguments: {args}")
        if argument_contract:
            lines.append(f"    argument contract: {argument_contract}")
        if idea_ui_steps:
            lines.append("    verified IDEA UI steps:")
            for step in idea_ui_steps:
                lines.append(f"      {step}")
    lines.append("")
    lines.append("AFTER:")
    for ln in str(example.get("after") or "").strip().splitlines():
        lines.append(f"  {ln}")
    notes = _render_notes(example.get("notes"))
    if notes:
        lines.append("")
        lines.append("NOTES:")
        for note in notes:
            lines.append(f"  - {note}")
    return lines


def _collect_route_ids(examples: Tuple[Dict[str, Any], ...]) -> Tuple[str, ...]:
    ordered: List[str] = []
    for example in examples:
        route_id = _clean_route_id(example)
        if route_id:
            ordered.append(route_id)
    return tuple(ordered)


def _clean_route_id(example: Dict[str, Any]) -> str:
    return str(example.get("id") or "").strip()


def _render_notes(raw_notes: Any) -> List[str]:
    if raw_notes is None:
        return []
    if isinstance(raw_notes, list):
        return [str(note).strip() for note in raw_notes if str(note).strip()]
    note = str(raw_notes).strip()
    return [note] if note else []


def _extract_operations(examples: Tuple[Dict[str, Any], ...]) -> Tuple[str, ...]:
    seen = set()
    ordered: List[str] = []
    for example in examples:
        for action in example.get("actions", []) or []:
            op = str(action.get("op") or "").strip()
            if not op or op in seen:
                continue
            seen.add(op)
            ordered.append(op)
    return tuple(ordered)
