from __future__ import annotations

import json
from typing import Iterable

from .config import ResolvedRunConfig, interpolate_command_text
from .java.idea_refactor import DEFAULT_IDEA_REFACTOR_CLI, resolve_idea_refactor_cli
from .prompts.idea_router import build_idea_prompt_route, render_idea_prompt_route


def build_task(
    *,
    config: ResolvedRunConfig,
    attempt_number: int,
    total_attempts: int,
    failures: Iterable[dict],
) -> str:
    failure_list = list(failures)
    location_lines = []
    for index, target in enumerate(config.locations, start=1):
        locator = []
        if target.method:
            locator.append(f"method={target.method}")
        if target.line:
            locator.append(f"line={target.line}")
        suffix = " | ".join(locator) if locator else "line not specified"
        location_lines.append(f"- Target {index}: {target.display_path} ({suffix})")

    constraint_lines = "\n".join(f"- {item}" for item in config.profile.constraints) or "- Keep the changes minimal."
    verification_lines = "\n".join(f"- {item}" for item in config.profile.verification) or "- Verify the targeted smell is resolved."
    smell_evidence = _build_smell_evidence_hint(config)
    acceptance_hint = _build_acceptance_hint(config)
    verification_guardrails = _build_verification_guardrails(config)
    build_instruction = _describe_command("Build", config.build, enabled=config.defaults.run_build, project_root=config.project_root)
    test_instruction = _describe_command("Test", config.test, enabled=config.defaults.run_tests, project_root=config.project_root)
    idea_refactor_hint = _build_idea_refactor_tool_hint(config)
    retry_block = ""
    if failure_list:
        rendered_failures = "\n".join(f"- {item['type']}: {item['message']}" for item in failure_list)
        retry_hint = config.profile.retry_hint_template or "Address the failures below before submitting again."
        retry_block = (
            f"\nRetry context: attempt {attempt_number} of {total_attempts}.\n"
            "Previous verification failures:\n"
            f"{rendered_failures}\n"
            f"{retry_hint}\n"
        )
    return (
        f"Project root: {config.project_root}\n"
        f"Language: {config.language}\n"
        f"Smell: {config.smell}\n"
        f"Attempt: {attempt_number} of {total_attempts}\n\n"
        "Goal:\n"
        f"{config.profile.instruction}\n\n"
        "Target locations:\n"
        f"{chr(10).join(location_lines)}\n\n"
        f"{smell_evidence}"
        "Constraints:\n"
        f"{constraint_lines}\n\n"
        "Verification goals:\n"
        f"{verification_lines}\n\n"
        f"{acceptance_hint}"
        f"{verification_guardrails}"
        f"{build_instruction}\n"
        f"{test_instruction}\n"
        f"{idea_refactor_hint}"
        "Use bash and standard shell tools only. Inspect files with commands like rg, sed, awk, cat, and nl.\n"
        f"{_build_editing_instruction(config)}\n"
        "When you are truly done, execute `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` as a standalone command.\n"
        f"{retry_block}"
    ).strip() + "\n"


def _build_smell_evidence_hint(config: ResolvedRunConfig) -> str:
    evidence_lines = []
    for guard in config.profile.guards:
        guard_type = str(guard.get("type", "")).strip()
        evidence = str(guard.get("audit_evidence", "")).strip()
        if guard_type == "mysterious_name" and evidence:
            evidence_lines.append(f"- Target evidence: `{evidence}`")
        if guard_type == "data_clumps" and evidence:
            evidence_lines.extend(_build_data_clumps_evidence_lines(guard, evidence))
    if not evidence_lines:
        return ""
    suffix = ""
    if config.smell == "mysterious_name":
        suffix = "- This identifier is selection context only; baseline capture must also confirm it as a strict detector finding.\n"
    if config.smell == "data_clumps":
        suffix = (
            "- Treat the reported group only as a selector for the product detector's cross-method finding family.\n"
        )
    return "Smell evidence:\n" f"{chr(10).join(evidence_lines)}\n" f"{suffix}\n"


def _build_data_clumps_evidence_lines(guard: dict, evidence: str) -> list[str]:
    lines = [f"- Target evidence: `{evidence}`"]
    reported = str(guard.get("reported_occurrence_count") or "").strip()
    listed = str(guard.get("listed_occurrence_count") or "").strip()
    if reported or listed:
        lines.append(f"- Data clump occurrences: reported={reported or 'unknown'}, listed={listed or 'unknown'}.")
    occurrences = _parse_group_occurrences(str(guard.get("group_occurrences") or ""))
    if occurrences:
        lines.append("- Listed same-group occurrences to inspect:")
        for index, occurrence in enumerate(occurrences[:10], start=1):
            location = str(occurrence.get("location") or "").strip()
            method = str(occurrence.get("method") or "").strip()
            if location:
                lines.append(f"  {index}. `{location}`")
            elif method:
                lines.append(f"  {index}. `{method}`")
        if len(occurrences) > 10:
            lines.append(f"  ... {len(occurrences) - 10} more listed occurrences omitted from the prompt.")
    return lines


def _parse_group_occurrences(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _describe_command(label: str, command_config, *, enabled: bool, project_root) -> str:
    if not enabled:
        return f"{label}: disabled for this run.\n"
    if command_config.script:
        rendered_script = interpolate_command_text(command_config.script.rstrip(), project_root)
        return (
            f"{label}: run exactly this configured {label.lower()} script before submitting.\n"
            "```bash\n"
            f"{rendered_script}\n"
            "```\n"
        )
    if command_config.command:
        rendered_command = interpolate_command_text(command_config.command, project_root)
        return (
            f"{label}: run exactly `{rendered_command}` before submitting when relevant.\n"
        )
    return f"{label}: no configured {label.lower()} command.\n"


def _build_acceptance_hint(config: ResolvedRunConfig) -> str:
    if config.smell == "long_parameter_list":
        return _build_long_parameter_list_acceptance_hint(config)
    if config.smell == "feature_envy":
        return _build_feature_envy_acceptance_hint(config)
    if config.smell == "god_class":
        return _build_god_class_acceptance_hint(config)
    if config.smell == "dead_code":
        return _build_dead_code_acceptance_hint(config)
    return ""


def _build_long_parameter_list_acceptance_hint(config: ResolvedRunConfig) -> str:
    for guard in config.profile.guards:
        guard_type = str(guard.get("type", "")).strip()
        if guard_type != "long_parameter_list":
            continue
        max_params = int(guard.get("max_params", 5))
        return (
            "Hard acceptance standard:\n"
            f"- The final target function or method must have <= {max_params} parameters.\n"
            "- The target method signature itself must meet that limit; extracting the body or adding a helper while leaving the target signature unchanged does NOT satisfy this task.\n"
            "- Before editing, choose ONE strategy for this attempt: ordinary signature compaction, framework-sensitive end-to-end signature migration, or blocked-by-protocol/codegen. Do not drift between these strategies within the same attempt.\n"
            "- If the target method is framework-, annotation-, protocol-, or codegen-sensitive (for example `@Remote`), classify that first before changing the signature.\n"
            "- For framework-sensitive entrypoints, do NOT spend attempts on signature-preserving helper extraction. Either make a real end-to-end signature change that the framework can support, or report that the target is blocked by protocol/codegen constraints.\n"
            "- For framework-sensitive entrypoints, helper extraction may improve readability but it does NOT count toward long_parameter_list acceptance if the target signature remains long.\n"
            "- If one attempt leaves the target parameter count unchanged, do not repeat the same pattern on retry. Pivot to a different signature strategy or stop and report the blocker.\n\n"
        )
    return ""


def _build_feature_envy_acceptance_hint(config: ResolvedRunConfig) -> str:
    if not any(str(guard.get("type", "")).strip() == "feature_envy" for guard in config.profile.guards):
        return ""
    return (
        "Hard acceptance standard:\n"
        "- A valid feature_envy fix must transfer substantial behavior or ownership toward the envied receiver.\n"
        "- Acceptable paths are: move the target method with `move:method`, or extract a substantial receiver-heavy block and move/add that behavior on the receiver with only a narrow call left behind.\n"
        "- Tiny wrappers such as boolean/getter helpers, helper methods left in the original class, or long_method-style decomposition do NOT satisfy feature_envy.\n"
        "- The final target method must no longer be reported by the feature_envy guard. If the guard still reports the same target method, the attempt is not complete.\n"
        "- If native move/extract cannot represent a valid ownership transfer, report the blocker instead of submitting a cosmetic helper extraction.\n\n"
    )


def _build_god_class_acceptance_hint(config: ResolvedRunConfig) -> str:
    if not any(str(guard.get("type", "")).strip() == "god_class" for guard in config.profile.guards):
        return ""
    return (
        "Hard acceptance standard:\n"
        "- A valid god_class fix must move ownership of a cohesive responsibility cluster out of the reported class.\n"
        "- For field-heavy classes, prefer native `extract:class` and select the field/state cluster together with the methods that maintain it; moving only methods while leaving fields behind is usually not enough.\n"
        "- After `extract:class`, remove private wrappers with `idea_edit`; a forwarding method for every moved member usually preserves NOM and is not complete.\n"
        "- Use `move:method` only when the behavior already belongs on a real existing owner. Do not force a field cluster into an arbitrary receiver.\n"
        "- Use targeted `idea_edit` old/new patches only when native `extract:class` is unavailable or cannot migrate the needed members.\n"
        "- Do not use a whole-class patch on a large reported god class. The reported class must be shrunk with targeted `idea_edit` patches and native ownership moves.\n"
        "- Helper extraction inside the same class, retained wrapper clusters, comments, or cosmetic reordering do NOT satisfy god_class.\n"
        "- A single narrow retained entrypoint can be acceptable for public or framework callers, but a wrapper family for moved methods is not complete.\n"
        "- The reported class must no longer be reported by the god_class guard, and deleting the target class or file is not accepted.\n\n"
    )


def _build_dead_code_acceptance_hint(config: ResolvedRunConfig) -> str:
    if not any(str(guard.get("type", "")).strip() == "dead_code" for guard in config.profile.guards):
        return ""
    return (
        "Hard acceptance standard:\n"
        "- A valid dead_code fix removes the reported unused private member through the native safe-delete route.\n"
        "- Do not delete public APIs, annotated framework entrypoints, lifecycle hooks, serialization hooks, or reflection/config-driven members.\n"
        "- If Safe Delete reports real references or required override/framework usage, stop and report that blocker instead of forcing deletion.\n"
        "- Do not broaden the task into unrelated cleanup; only remove the reported target unless the guard asks for a grouped family.\n"
        "- The reported member must no longer be found by the dead_code guard, and the configured build/test guard must pass.\n\n"
    )


def _build_verification_guardrails(config: ResolvedRunConfig) -> str:
    if not (config.defaults.run_build or config.defaults.run_tests):
        return ""
    return (
        "Verification is strict:\n"
        "- Run the configured build and test commands exactly as provided.\n"
        "- Do not add `timeout`, `head`, `tail`, `grep`, `tee`, or similar wrappers around the configured verification commands.\n"
        "- Do not replace project-level verification with single-file compilation, manual inspection, line-count checks, or inferred behavioral claims.\n"
        "- If configured verification does not complete, do not claim verification passed.\n"
        "- If refactoring succeeds but verification is blocked by environment, timeout, or repository state, report that blocker explicitly as the final state.\n\n"
    )




def _build_idea_refactor_tool_hint(config: ResolvedRunConfig) -> str:
    if config.language != "java":
        return ""
    cli_path = resolve_idea_refactor_cli(config, config.idea_refactor_cli)
    pr = config.idea_project_root
    lines: list[str] = []

    # ── Section 1: Service status ──────────────────────────────────
    if config.idea_refactor_ready:
        lines.append("IDEA refactoring tool (service is running):")
        lines.append(f"- The runner has already prepared the IDEA Refactor Service with `{cli_path} ensure-service --project-root {pr}`.")
    else:
        lines.append("IDEA refactoring tool:")
        lines.append(f"- Java projects may use `{cli_path}` for IntelliJ PSI-backed refactorings.")
        lines.append(f"- To check availability, run `{cli_path} ensure-service --project-root {pr}`.")
        lines.append("- Do not try to open IDEA from inside the model; opening/setup is handled by external runner logic.")
    lines.append("- The target location identifies where to inspect first; do not automatically feed it to locate.")
    lines.append("- Choose the actual IDEA selection from the code you read; do not assume the target line itself is the exact extractable slice.")
    lines.append("")
    lines.append("Decision priority for this task:")
    lines.append("- Use local examples as guidance, not as templates to reproduce mechanically.")
    lines.append("- Prefer the smallest IDEA-backed change that directly reduces the reported smell.")
    lines.append("")

    # ── Section 2: Smell-routed prompt injection ─────────────────
    lines.append(render_idea_prompt_route(build_idea_prompt_route(cli_path, pr, config)).rstrip())
    lines.append("")

    # ── Section 3: Delegated CLI reference ─────────────────────────
    lines.append("IDEA CLI protocol reference:")
    lines.append("- In OpenCode, load the `idea-refactor-cli` skill before executing `idea_native`, `idea_edit`, or `mixed` Java source steps.")
    lines.append("- That skill owns the detailed preview/proposal/apply/rollback lifecycle, selection repair, decisions, stale proposal handling, and fallback rules.")
    lines.append("")
    lines.append("IDEA edit fallback for Java:")
    lines.append("- Prefer native IDEA refactorings first.")
    lines.append("- Use `idea_edit` for narrow Java source patches that are not native semantic refactorings.")
    lines.append("- Ordinary `idea_edit` Java source patches need a file path, exact unique oldString, newString, and replaceAll=false.")
    lines.append("- Explicit new-file or whole-file `idea_edit` steps may use oldString=\"\" only when the selected refactor path says so.")
    lines.append("")
    lines.append("STALE_DRAFT note:")
    lines.append("- The IDEA service tracks file revisions. If a file changes between `locate` and `apply`, the current draft can become stale.")
    lines.append("- Do not edit the target file manually between `locate`, `prepare`, and `apply`.")
    lines.append("- If you hit `STALE_DRAFT`, re-run `locate`, then `prepare`, then `apply` from fresh file contents.")
    lines.append("- If `STALE_DRAFT` persists after 2 retries, restart from a smaller or clearer selection instead of repeating the same stale draft.")
    lines.append("")

    return "\n".join(lines) + "\n"


def _build_editing_instruction(config: ResolvedRunConfig) -> str:
    if config.language == "java":
        return (
            "For Java source changes, prefer IDEA-backed refactoring over direct text edits; in OpenCode, load the `idea-refactor-cli` skill for the concrete CLI protocol. "
            "Do not use `sed`, `perl`, Python rewrite scripts, here-doc overwrites, or `cat > file` to modify `.java` files as the refactoring path. "
            "Direct shell tools are for inspection only unless you are changing non-Java auxiliary files or reporting an IDEA tool blocker."
        )
    return "Edit files directly in the workspace. Preserve behavior and avoid unrelated changes."
