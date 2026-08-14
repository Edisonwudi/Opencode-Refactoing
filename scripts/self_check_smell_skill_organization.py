#!/usr/bin/env python3
"""Validate per-smell skill routing without executing a model."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".opencode" / "skills"
LEGACY = SKILLS / "java-smell-edit-patterns" / "references" / "edit-patterns"
IDEA = SKILLS / "idea-refactor-cli" / "references" / "refactor-paths"

JAVA_HASHES = {
    "code_clone_type1": "ef201bd590b7c27888cfb7d374d5262a8aaddffe3334b54c6dce8857b022deae",
    "data_clumps": "a748b7bdabd80b8209fa33cf8ce1e7e309c3f0e6e8d6584f6a968da7981ec7d2",
    "dead_code": "d0c956de7c392e0768456c2112d4824f290562e0209111daf1d99c5a3208a83d",
    "feature_envy": "20cec88e6cba007832b6390b568216722c986170f686de25420eb6738adb4ff9",
    "god_class": "0a29c6bb8e3fd73a461cc0cdfdf932172788e8c6c868c9b859f429b5ef82ada3",
    "long_method": "1f5e661efc47df8ba37625dd7914fb09648e0736f855c7ddccaed633b9f7a4bc",
    "long_parameter_list": "5a7010e922f56395ca08848de035ef041cce3c79110738afdd6933e71748c9b9",
    "mysterious_name": "26f909f2972ec1ea75b31bdee5fe196041d4d9ec7fdc472fd20a0af9eb1fc20e",
    "nested_complexity": "09f1df2f7ecda60799147dcf14166c0f48e2a2bf6d58eea6a63843b6bb509634",
    "refused_bequest": "c579dfa1ffc382d26d33d230481d41592fbb5663083108f6327a7035ca193e5b",
    "switch_statements": "7e478364f08bd9d12469649d7f92ed47bfc7cb00e3fc82c151da2fdca3a6d36b",
}
IDEA_HASHES = {
    "code_clone_type1": "20dd244091377696ee0e11f6b1ea914c461af970980d03b170bcca6c81bb53f6",
    "data_clumps": "37123d14e78002a0de9670da50e6f44daf6e88898a72ac72927ce0acf2c2595e",
    "dead_code": "c0b04c2639b3db13187b33d13391738c9389d93059966936e0c1ddf76a10e092",
    "feature_envy": "00cc74a781c1b8987779585f8bbeb6c004c3166266ced4c1f7d535ffcce2d073",
    "god_class": "8807ab7192f6c8831775061d674e920d631b4ca4001fe216741bd1bd36ed1685",
    "long_method": "5184d014874bb875fb6f441c28fe9eba9c168ae26b760ed85ba9f427d87aa2a9",
    "long_parameter_list": "24cf4efccc90d4806df43d93e681df59d5716637917549b5a6f6856f61a3ae09",
    "mysterious_name": "752324a061056b8a9667071d7a8b7624e4c08887eec100c8af917efd871061b3",
    "nested_complexity": "ce5a0754d109dce1077eafc0d561773ae197ef263a021ff7991a1c9f642dfaf4",
    "refused_bequest": "bbed6fa985436585906682d22ecbec16c768e4b9b192d4e20c2aef15ab258a24",
    "switch_statements": "9650e71c4caf7071c11839b2f216af3220cfdd19de2f43ae3410b44f868cff9c",
}
OPERATION_HASH = "b6140fdd5efb18221cc23d80461c726591ab12235b2ab373557c11899be599ea"
GENERIC_SMELLS = tuple(smell for smell in JAVA_HASHES if smell != "refused_bequest")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


failures: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


operation_source = LEGACY / "operation-translations.md"
require(digest(operation_source) == OPERATION_HASH, "legacy operation translations drifted")

java_dataset_smells = {
    path.stem for path in (ROOT / "dataset" / "java" / "delivery_schema").glob("*.csv")
}
require(java_dataset_smells == set(JAVA_HASHES), "per-smell skills do not cover the Java delivery schema")
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
for smell, expected_hash in JAVA_HASHES.items():
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    skill_root = SKILLS / skill_name
    skill_file = skill_root / "SKILL.md"
    java_reference = skill_root / "references" / "java.md"
    legacy_reference = LEGACY / f"{smell}.md"
    operation_reference = skill_root / "references" / "operation-translations.md"
    idea_reference = IDEA / f"{smell}.yaml"

    require(skill_file.is_file(), f"missing primary skill: {skill_name}")
    require(java_reference.is_file(), f"missing Java reference: {skill_name}")
    require(legacy_reference.is_file(), f"missing legacy Java reference: {smell}")
    require(idea_reference.is_file(), f"missing IDEA route: {smell}")
    if not all(path.is_file() for path in (skill_file, java_reference, legacy_reference, idea_reference)):
        continue

    main_text = skill_file.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", main_text, re.DOTALL)
    require(frontmatter is not None, f"invalid frontmatter: {skill_name}")
    if frontmatter:
        require(f"name: {skill_name}" in frontmatter.group(1), f"skill name mismatch: {skill_name}")
    require("TODO" not in main_text, f"unfinished primary skill: {skill_name}")
    require("Read exactly one language route" in main_text or smell == "refused_bequest", f"missing one-route contract: {skill_name}")
    require("references/java.md" in main_text, f"Java route not linked: {skill_name}")
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

    require(digest(legacy_reference) == expected_hash, f"legacy Java route drifted: {smell}")
    require(digest(java_reference) == expected_hash, f"copied Java route is not byte-identical: {smell}")
    require(digest(operation_reference) == OPERATION_HASH, f"operation reference drifted: {smell}")
    require(digest(idea_reference) == IDEA_HASHES[smell], f"IDEA route drifted: {smell}")
    route_total += len(re.findall(r"^### `[^`]+`$", java_reference.read_text(encoding="utf-8"), re.MULTILINE))

require(route_total == 47, f"expected 47 preserved Java direct routes, found {route_total}")

generic_agent = (ROOT / ".opencode" / "agents" / "smell-refactor-agent.md").read_text(encoding="utf-8")
java_agent = (ROOT / ".opencode" / "agents" / "java-refactor-agent.md").read_text(encoding="utf-8")
generic_agent_flat = " ".join(generic_agent.split())
for smell in GENERIC_SMELLS:
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    require(f'"{skill_name}": allow' in generic_agent, f"generic agent cannot load {skill_name}")
for smell in JAVA_HASHES:
    skill_name = f"smell-repair-{smell.replace('_', '-')}"
    require(f'"{skill_name}": allow' in java_agent, f"Java agent cannot load {skill_name}")
require('"smell-repair-refused-bequest": allow' not in generic_agent, "generic agent exposes Java-only refused_bequest")
require('"java-smell-edit-patterns": allow' not in java_agent, "Java agent still routes through the legacy umbrella")
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
    "If the source projection is still outside the numeric budget, continue the same repair without calling `smell_verify`" in generic_agent_flat
    and "Once the source projection reaches a passing route" in generic_agent_flat,
    "generic agent does not defer project_full verification until the projected edit reaches a passing route",
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
