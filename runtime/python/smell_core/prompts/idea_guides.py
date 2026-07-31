"""IDEA refactoring guidance, organized by smell type."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import ResolvedRunConfig


class SmellGuideBuilder:
    """Builds smell-specific IDEA refactoring guidance."""

    def __init__(self, cli_path: str, project_root) -> None:
        self.cli = cli_path
        self.pr = project_root

    def for_long_method(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for long_method:",
            "",
            "- Identify 2-4 logical blocks that can become helpers.",
            "- Prefer `extract:method` on complete statement ranges; select full blocks, not partial expressions or broken statements.",
            "- Use descriptive helper names such as `validateInput`, `processBatch`, or `buildResponse`.",
            "- If the method is in a static context, set `\"makeStatic\": true` when needed.",
            "- After each successful extraction, re-read the remaining method body and continue only if the smell still remains.",
        ])

    def for_long_parameter_list(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for long_parameter_list:",
            "",
            "- Classify the target first: ordinary method, framework-sensitive signature, or protocol/codegen-sensitive entrypoint.",
            "- Choose one signature strategy for this attempt before editing; do not alternate between helper extraction and signature migration inside the same attempt.",
            "- For ordinary methods, prefer `introduce:parameter-object`; use `change-signature:method` when you mainly need to reorder, remove, or replace parameters.",
            "- For framework-sensitive entrypoints such as `@Remote`, helper extraction can improve readability but does NOT satisfy this smell if the target signature stays long.",
            "- The hard acceptance standard is the target signature itself crossing the threshold; moving body logic without reducing that signature does not count as complete.",
            "- If one attempt leaves the parameter count unchanged, pivot to a different signature strategy or report the blocker instead of retrying the same pattern.",
        ])

    def for_nested_complexity(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for nested_complexity:",
            "",
            "- Focus on the deepest `if`/`for`/`while`/`catch` nesting first; those blocks usually contribute the most to the smell.",
            "- Prefer `extract:method` on the deepest coherent statement block before editing shallower structure.",
            "- Use guard clauses or inverted conditions when they flatten the control flow without broad rewrites.",
            "- After extraction, check that the new helper is simpler than the original nested block; do not just move complexity sideways.",
        ])

    def for_switch_statements(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for switch_statements:",
            "",
            "- Classify the switch first: substantial branch bodies, repeated branch shape, or simple key-to-result dispatch.",
            "- Helper extraction alone does not satisfy the switch_statements guard; `extract:method` is only an intermediate step before a final dispatch replacement or case-count reduction.",
            "- For substantial branch bodies, extract coherent helper methods only when the selected refactor path still ends with `idea_edit` old/new patches or an equivalent rewrite that removes or significantly reduces the original switch.",
            "- For simple key-to-result dispatch, prefer the local table-driven route instead of extracting tiny one-line cases.",
            "- If several cases share the same body shape, consider duplicate elimination only after the first path is clear.",
            "- Do not stop at a thin switch with many one-line cases when the active guard still counts switch cases and density.",
        ])

    def for_code_clone_type1(self, config: "ResolvedRunConfig") -> str:
        return "\n".join([
            "IDEA refactoring hints for code_clone_type1:",
            "",
            "- Treat whole-function repetition first, then choose the smallest refactoring that matches the structural relation.",
            f"- Same file and identical whole-function clone: prefer `extract:method` with `replaceDuplicates: true`.",
            "- Different files with a meaningful shared parent or interface: prefer `pullUp:method`.",
            "- Different files with only a generic base or no real owner: prefer a shared helper or utility instead of forcing pull-up.",
            "- Re-check ownership before editing sibling classes; do not move behavior into a parent that should stay generic.",
        ])

    def for_feature_envy(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for feature_envy:",
            "",
            "- Identify the foreign receiver that owns most of the accessed data or behavior.",
            "- This is an ownership-transfer task, not a long_method decomposition task.",
            "- Prefer `move:method` when the whole target method truly belongs on that receiver and callers can be updated coherently.",
            "- If a full move is too broad, extract a substantial receiver-heavy block with `extract:method`, then move that extracted method to the receiver or add the equivalent behavior on the receiver and replace the source with a narrow call.",
            "- Before using `extract:method`, follow the `idea-refactor-cli` skill's selection repair protocol and choose a complete candidate selection.",
            "- Do NOT count tiny boolean/getter wrappers, helper extraction inside the original class, or one-expression delegation as a valid feature_envy fix.",
            "- Do NOT stop after merely reducing a few foreign accesses; the target method must no longer be reported by the feature_envy guard.",
            "- Preserve the public caller surface unless the chosen IDEA operation safely updates all call sites.",
        ])

    def for_data_clumps(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for data_clumps:",
            "",
            "- Use the reported parameter group as the center of the refactoring; keep the clump together.",
            "- Prefer one native `introduce:parameter-object` on the safest anchor signature family; it creates the holder and migrates that family's call sites.",
            "- Treat group_occurrences as the residual cleanup worklist. Do not repeat native introduce:parameter-object for the same reported group and holder name.",
            "- After native migration, migrate ordinary residual helpers and local callers to the existing holder with `idea_edit` old/new patches.",
            "- Do not preserve or add backward-compatible overloads that keep the old repeated parameter group; if SMELL_GUARD_FAILED reports them, remove or migrate old-parameter wrappers.",
            "- Do not create the holder with direct_edit before native introduce:parameter-object; let the native operation create or reuse the parameter object unless IDEA reports a concrete blocker.",
            "- If the repeated group spans several classes, place the holder near the real domain owner instead of creating an arbitrary helper type.",
            "- Avoid splitting the same reported clump across several partial objects.",
        ])

    def for_god_class(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for god_class:",
            "",
            "- Identify one cohesive responsibility cluster in the reported class before choosing operations.",
            "- Primary path: for field-heavy classes with a clear cache/font/text/image/state cluster, use native `extract:class` when IDEA exposes it and can migrate the selected fields plus maintaining methods.",
            "- After `extract:class`, inspect the original class. Private wrapper methods left behind must be removed with `idea_edit`; do not keep a forwarding method for every moved member.",
            "- Secondary path: use `move:method` only when a method cluster already belongs on an existing domain owner or collaborator.",
            "- Do not force `move:method` onto a field/state cluster that has no real owner; extract the state and behavior instead.",
            "- Auxiliary path: use `idea_edit` old/new patches only when native `extract:class` is unavailable or cannot cover the needed fields and methods.",
            "- Do not use a whole-class text replacement on a large reported god class.",
            "- Shrink the reported class with targeted `idea_edit` patches and native `move:method` steps when native extraction cannot do the migration.",
            "- Do not count helper extraction inside the same class as complete; responsibility must move out of the god class.",
            "- Preserve caller behavior and public/framework contracts while shrinking the reported class below the god_class guard threshold.",
        ])

    def for_dead_code(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for dead_code:",
            "",
            "- Treat the reported target as an unused private-member removal task, not a general cleanup pass.",
            "- Prefer the native safe-delete route for unused private methods so IDEA can reject hidden references.",
            "- If Safe Delete reports real references, framework usage, or override/lifecycle constraints, stop and report the blocker instead of forcing deletion.",
            "- Do not delete public APIs, annotated methods, serialization hooks, lifecycle hooks, or reflection/config-driven entrypoints.",
            "- Do not broaden the edit to unrelated unused members in the same file.",
            "- The final state is that the reported private member no longer exists and the dead_code guard no longer reports it.",
        ])

    def for_mysterious_name(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for mysterious_name:",
            "",
            "- Decide first whether the unclear identifier is a method, parameter, local variable, or type name.",
            "- Use the matching IDEA rename or signature refactoring for that declaration kind.",
            "- Pick domain-meaningful names; do not stop at mechanically expanding a single-letter abbreviation.",
            "- Keep the rename narrow and avoid unrelated naming cleanup in the same file.",
        ])

    def for_refused_bequest(self) -> str:
        return "\n".join([
            "IDEA refactoring hints for refused_bequest:",
            "",
            "- Identify overrides that reject, ignore, or duplicate parent behavior.",
            "- Treat the supplied sample test as an immutable project-behavior regression oracle; never edit or weaken it.",
            "- Derive the parent contract, sibling implementations, and caller impact from source before choosing a route.",
            "- Do not guess `safe-delete`; this route does not expose a `safe-delete` IDEA CLI operation.",
            "- For pure super-delegation or delete-legal useless overrides, prefer `inline:method` with `keepMethodDeclaration=false`.",
            "- When the method is required by an interface or abstract parent, implementing or delegating the narrow contract is valid if behavior is preserved.",
            "- When real behavior is owned by a narrower subtype, use `pushDown:method` instead of leaving a rejecting hook on the parent.",
            "- Use pull-up or push-down only when the ownership boundary is genuinely wrong; do not flatten the hierarchy broadly.",
            "- Remove useless overrides only when parent behavior is actually correct for the subtype.",
            "- Logging, comments, swallowed exceptions, and placeholder constants do not implement a refused contract.",
            "- Build and test after hierarchy edits because the caller impact often extends beyond the reported file.",
        ])

    def generic(self, smell: str) -> str:
        return "\n".join([
            f"IDEA refactoring hints for {smell}:",
            "",
            "- Use IDEA preview/apply with an explicit proposalId on the smallest change that directly addresses the reported smell.",
            "- Choose the native refactoring that matches the real structure you see.",
        ])

    def build(self, smell: str, config: "ResolvedRunConfig") -> str:
        dispatch = {
            "long_method": self.for_long_method,
            "long_parameter_list": self.for_long_parameter_list,
            "nested_complexity": self.for_nested_complexity,
            "switch_statements": self.for_switch_statements,
            "code_clone_type1": lambda: self.for_code_clone_type1(config),
            "feature_envy": self.for_feature_envy,
            "data_clumps": self.for_data_clumps,
            "god_class": self.for_god_class,
            "dead_code": self.for_dead_code,
            "mysterious_name": self.for_mysterious_name,
            "refused_bequest": self.for_refused_bequest,
        }
        return dispatch.get(smell, lambda: self.generic(smell))()


def build_smell_specific_idea_guide(cli_path: str, project_root, config: "ResolvedRunConfig") -> str:
    """Build the smell-specific IDEA refactoring guidance."""
    return SmellGuideBuilder(cli_path, project_root).build(config.smell, config)
