#!/usr/bin/env python3
"""Validate per-smell skill routing without executing a model."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".opencode" / "skills"
IDEA = SKILLS / "idea-refactor-cli" / "references" / "refactor-paths"

JAVA_SMELLS = (
    "code_clone_type1",
    "data_clumps",
    "dead_code",
    "feature_envy",
    "god_class",
    "long_method",
    "long_parameter_list",
    "mysterious_name",
    "nested_complexity",
    "refused_bequest",
    "switch_statements",
)
GENERIC_SMELLS = tuple(smell for smell in JAVA_SMELLS if smell != "refused_bequest")


failures: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


java_dataset_smells = {
    path.stem for path in (ROOT / "dataset" / "java" / "delivery_schema").glob("*.csv")
}
require(java_dataset_smells == set(JAVA_SMELLS), "per-smell skills do not cover the Java delivery schema")
for language in ("python", "c", "cpp"):
    dataset_root = ROOT / "dataset" / "nonjava" / language
    dataset_smells = {
        path.name.removesuffix("_30.csv")
        for path in dataset_root.glob("*_30.csv")
    }
    if (dataset_root / "dead_code_curated.csv").is_file():
        dataset_smells.add("dead_code")
    require(dataset_smells == set(GENERIC_SMELLS), f"per-smell skills do not cover {language} datasets")

route_total = 0
for smell in JAVA_SMELLS:
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    skill_root = SKILLS / skill_name
    skill_file = skill_root / "SKILL.md"
    java_reference = skill_root / "references" / "java.md"
    operation_reference = skill_root / "references" / "operation-translations.md"
    idea_reference = IDEA / f"{smell}.yaml"

    require(skill_file.is_file(), f"missing primary skill: {skill_name}")
    require(java_reference.is_file(), f"missing Java reference: {skill_name}")
    require(operation_reference.is_file(), f"missing operation reference: {smell}")
    require(idea_reference.is_file(), f"missing IDEA route: {smell}")
    if not all(path.is_file() for path in (skill_file, java_reference, operation_reference, idea_reference)):
        continue

    main_text = skill_file.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", main_text, re.DOTALL)
    require(frontmatter is not None, f"invalid frontmatter: {skill_name}")
    if frontmatter:
        require(f"name: {skill_name}" in frontmatter.group(1), f"skill name mismatch: {skill_name}")
    require("TODO" not in main_text, f"unfinished primary skill: {skill_name}")
    require("Read exactly one language route" in main_text or smell == "refused_bequest", f"missing one-route contract: {skill_name}")
    require("references/java.md" in main_text, f"Java route not linked: {skill_name}")
    require(
        "references/operation-translations.md" in main_text,
        f"operation mechanics not linked: {skill_name}",
    )
    if smell != "refused_bequest":
        require("does not replace the Java semantic route" in main_text, f"IDEA route replaces Java semantics: {skill_name}")

    expected_references = {"java.md", "operation-translations.md"}
    if smell in GENERIC_SMELLS:
        expected_references.update({"python.md", "c.md", "cpp.md"})
        for language in ("Python", "C", "C++"):
            require(language in main_text, f"{skill_name} does not route {language}")
    else:
        require("Java-only" in main_text, "refused_bequest is not marked Java-only")
        require("Reject Python, C, or C++" in main_text, "refused_bequest lacks unsupported-language guard")
    actual_references = {path.name for path in (skill_root / "references").iterdir() if path.is_file()}
    require(actual_references == expected_references, f"unexpected references for {skill_name}: {sorted(actual_references)}")
    require(not list((skill_root / "references").rglob("SKILL.md")), f"nested skill found in {skill_name}")

    route_total += len(re.findall(r"^### `[^`]+`$", java_reference.read_text(encoding="utf-8"), re.MULTILINE))

require(route_total == 47, f"expected 47 preserved Java direct routes, found {route_total}")

generic_agent = (ROOT / ".opencode" / "agents" / "smell-refactor-agent.md").read_text(encoding="utf-8")
java_agent = (ROOT / ".opencode" / "agents" / "java-refactor-agent.md").read_text(encoding="utf-8")
generic_agent_flat = " ".join(generic_agent.split())
for smell in GENERIC_SMELLS:
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    require(f'"{skill_name}": allow' in generic_agent, f"generic agent cannot load {skill_name}")
for smell in JAVA_SMELLS:
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    require(f'"{skill_name}": allow' in java_agent, f"Java agent cannot load {skill_name}")
require('"smell-repair-refused-bequest": allow' not in generic_agent, "generic agent exposes Java-only refused_bequest")
require("load exactly the\n   matching `smell-repair-<smell>` skill" in generic_agent, "generic agent lacks exact-smell loading")
require(
    "Only migrate test references when the controller explicitly allows test changes" in generic_agent_flat,
    "generic agent ignores the controller-owned test-change policy",
)
require(
    "Guard output is a bounded target anchor, not a complete dependency closure" in generic_agent_flat,
    "generic agent treats Guard output as a complete dependency closure",
)
require(
    "only/exact Guard worklist" in generic_agent_flat
    and "Guard items only prioritize that ledger" in generic_agent_flat,
    "generic agent does not resolve shared-skill Guard worklist wording",
)
require(
    "When an initial metric or threshold is absent" in generic_agent_flat,
    "generic agent requires unavailable initial Guard metrics",
)
require(
    "current`, passing boundary, and `required_reduction` scalars" in generic_agent_flat
    and "not a caller or dependency closure" in generic_agent_flat,
    "generic agent does not treat the baseline metric budget as bounded scalar planning input",
)
require(
    "If the source projection is still outside the numeric budget, the call at that stage is only its isolated focused preflight" in generic_agent_flat
    and "Once the source projection reaches a passing route" in generic_agent_flat,
    "generic agent does not defer project_full verification until the projected edit reaches a passing route",
)
require(
    "In a controller-managed, checkpoint-required `project_full` Python/C/C++ command" in generic_agent_flat
    and
    "Outside a controller-managed `project_full` Python/C/C++ command" in generic_agent_flat,
    "generic agent applies the protected shell and focused-verification policy outside its controller scope",
)
require("load exactly the\n   matching `smell-repair-<smell>` semantic skill" in java_agent, "Java agent lacks exact-smell loading")
require("and read its Java reference" in java_agent, "Java route is not reference-scoped")
require("also load `idea-refactor-cli`" in java_agent, "Java IDEA mechanics route was lost")

for smell in GENERIC_SMELLS:
    c_reference = (
        SKILLS
        / f"smell-repair-{smell.replace('_', '-')}"
        / "references"
        / "c.md"
    ).read_text(encoding="utf-8")
    reverse_search = c_reference.find("Before calling `smell_verify`")
    first_verify = c_reference.find("`smell_verify`")
    require(
        reverse_search >= 0 and reverse_search == first_verify - len("Before calling "),
        f"C route does not put source reverse-search before its first smell_verify: {smell}",
    )

dead_code_root = SKILLS / "smell-repair-dead-code" / "references"
for language in ("python", "c", "cpp"):
    dead_code_reference = (dead_code_root / f"{language}.md").read_text(encoding="utf-8")
    require(
        "Only an independently reachable production use outside the proposed deletion unit blocks deletion."
        in dead_code_reference,
        f"dead-code {language} route treats internal dead closure as live",
    )

for smell in GENERIC_SMELLS:
    cpp_reference = (
        SKILLS
        / f"smell-repair-{smell.replace('_', '-')}"
        / "references"
        / "cpp.md"
    ).read_text(encoding="utf-8")
    require(
        "call `smell_verify`; it owns the configured full project suite" in cpp_reference,
        f"C++ route duplicates controller-owned full verification: {smell}",
    )

for smell in GENERIC_SMELLS:
    skill_root = SKILLS / f"smell-repair-{smell.replace('_', '-')}" / "references"
    for language in ("python", "c", "cpp"):
        reference = (skill_root / f"{language}.md").read_text(encoding="utf-8")
        require(
            "owns the isolated focused" not in reference,
            f"{language} route globally overrides non-project_full focused-check semantics: {smell}",
        )

plugin_text = (ROOT / ".opencode" / "plugins" / "smell.ts").read_text(encoding="utf-8")
require("smell-repair-<task-smell-with-hyphens> semantic skill and idea-refactor-cli backend skill" in plugin_text, "controller still advertises the old IDEA-only route")

if failures:
    for failure in failures:
        print(f"FAIL: {failure}")
    raise SystemExit(1)

print(
    "PASS: 11 per-smell skills; 10 four-language routes; refused_bequest Java-only; "
    "47 Java direct routes and 11 IDEA routes preserved"
)
