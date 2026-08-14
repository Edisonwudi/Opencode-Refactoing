"""Controlled declaration migration for non-Java Data Clumps.

The ordinary Data Clumps target contract keeps every frozen declaration on a
one-to-one patch anchor.  Introducing a parameter object can legitimately
change that declaration surface, however, and several old entry points can be
consolidated into one new implementation.  This module provides the narrow
exception: every migrated predecessor must have an explicit, patch-backed
successor that no longer accepts the frozen parameter group.

The module consumes only already-frozen target records and caller-supplied
diffs.  It does not discover source files or manufacture a dependency list.
Production caller and override closure remains a final ``project_full``
obligation; the returned lineage makes that obligation auditable.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .analysis import function_signatures_in_text
from .compatibility_contract import (
    _changed_virtual_declarations,
    _cpp_declaration_name,
    _pure_virtual_declarations,
)
from .detector_utils import normalize_group
from .target_patch_identity import (
    added_blocks_from_target_hunk_units,
    current_target_hunk_units,
    evaluate_target_patch_identity,
    previous_target_removed_blocks,
    same_hunk_identifier_replacement,
    validate_ast_declaration_identity,
)


DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT = (
    "data-clump-controlled-declaration-lineage-v2"
)
DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT = (
    "data-clump-lineage-plus-project-full-closure-v2"
)
_TOKEN = re.compile(r"[A-Za-z_]\w*|::|->|\.\.\.|[^\w\s]")


def evaluate_data_clump_declaration_migration(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    language: str,
    group: str,
) -> dict[str, Any]:
    """Return an auditable old-to-new declaration lineage.

    A target that still accepts the frozen group stays on the ordinary strict
    route.  A target whose signature drops the group enters migration mode and
    must satisfy all of the following:

    * both its old and new declaration spans are explicitly edited by the supplied
      target patch;
    * each successor has a parser-derived identity and no frozen group;
    * an owner/name change is witnessed by one unique same-hunk declaration
      replacement; and
    * every migrated predecessor has at least one successor.

    Reusing one successor declaration for several frozen indexes is retained
    as explicit many-to-one lineage. Duplicate current records for one frozen
    index are ambiguous and fail closed.
    """

    normalized_group = normalize_group(group)
    baseline = [dict(item) for item in baseline_targets]
    current = [dict(item) for item in current_targets]
    result: dict[str, Any] = {
        "contract": DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
        "language": str(language).strip().lower(),
        "ok": False,
        "applicable": False,
        "mode": "strict_identity",
        "lineage": [],
        "migrated_target_indexes": [],
        "relation_kinds": [],
        "failures": [],
        "project_full_required": False,
        "closure_contract": DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT,
        "closure_status": "not_applicable",
        "closure_obligations": [],
        "old_group_entries_removed": False,
        "parallel_old_group_entries": [],
        "error": "",
    }
    if not normalized_group:
        result["error"] = "baseline_parameter_group_unavailable"
        return result
    if not baseline:
        result["error"] = "baseline_target_anchors_unavailable"
        return result
    if not changed_patch:
        result["error"] = "changed_target_hunks_unavailable"
        return result

    removed_blocks, removed_error = previous_target_removed_blocks(changed_patch)
    current_units, current_error = current_target_hunk_units(changed_patch)
    patch_error = removed_error or current_error
    if patch_error:
        result["error"] = patch_error
        return result

    failures: list[dict[str, Any]] = []
    current_by_index: dict[int, dict[str, Any]] = {}
    for candidate in current:
        target_index = candidate.get("target_index")
        if isinstance(target_index, int) and not isinstance(target_index, bool):
            if target_index in current_by_index:
                failures.append({
                    "target_index": target_index,
                    "reason": "current_target_index_not_unique",
                })
                continue
            current_by_index[target_index] = candidate

    predecessor_edges: dict[int, dict[str, Any]] = {}
    baseline_by_index: dict[int, dict[str, Any]] = {}
    for frozen in baseline:
        target_index = frozen.get("target_index")
        if not isinstance(target_index, int) or isinstance(target_index, bool):
            failures.append({
                "target_index": target_index,
                "reason": "baseline_target_index_invalid",
            })
            continue
        baseline_by_index[target_index] = frozen
        successor = current_by_index.get(target_index)
        if not isinstance(successor, Mapping) or successor.get("resolved") is not True:
            # The ordinary target-identity gate owns unchanged missing targets.
            continue
        if (
            _signature_contains_group(successor, normalized_group)
            or not _signature_changed(frozen, successor)
        ):
            continue
        edge, edge_error = _migration_edge(
            frozen,
            successor,
            changed_patch=changed_patch,
            removed_blocks=removed_blocks,
            current_units=current_units,
        )
        if edge_error:
            failures.append({
                "target_index": target_index,
                "file": str(frozen.get("file") or ""),
                "method": str(frozen.get("method") or ""),
                "reason": edge_error,
            })
            continue
        predecessor_edges[target_index] = edge

    if not predecessor_edges:
        result.update({
            "ok": not failures,
            "failures": failures,
        })
        return result

    successor_predecessors: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    for target_index, edge in predecessor_edges.items():
        successor = dict(edge["successor"])
        successor_predecessors[_successor_key(successor)].add(target_index)

    lineage: list[dict[str, Any]] = []
    relation_kinds: set[str] = set()
    for target_index, edge in sorted(predecessor_edges.items()):
        predecessor = _declaration_payload(baseline_by_index[target_index])
        successor = dict(edge["successor"])
        if len(successor_predecessors[_successor_key(successor)]) > 1:
            relation = "many_to_one"
        else:
            relation = "one_to_one"
        relation_kinds.add(relation)
        lineage.append({
            "predecessor": predecessor,
            "successors": [successor],
            "relation": relation,
            "old_group_entry_removed": True,
            "patch_witness": [dict(edge["patch_witness"])],
        })

    parallel_old_group_entries, parallel_error = (
        _parallel_old_group_entries(
            current_units,
            language=str(language).strip().lower(),
            group=normalized_group,
            lineage=lineage,
        )
    )
    if parallel_error:
        failures.append({
            "reason": "parallel_old_group_analysis_failed",
            "error": parallel_error,
        })
    if parallel_old_group_entries:
        failures.append({
            "reason": "parallel_old_group_entry_added",
            "entries": parallel_old_group_entries,
        })

    result.update({
        "ok": not failures,
        "applicable": True,
        "mode": "api_abi_migration",
        "lineage": lineage,
        "migrated_target_indexes": sorted(predecessor_edges),
        "relation_kinds": sorted(relation_kinds),
        "failures": failures,
        "project_full_required": True,
        "closure_obligations": [
            "production_declarations",
            "production_definitions",
            "production_overrides",
            "production_callers",
            "tests_when_allowed",
        ],
        "old_group_entries_removed": not parallel_old_group_entries,
        "parallel_old_group_entries": parallel_old_group_entries,
        "closure_status": (
            "requires_project_full"
            if not failures
            else "lineage_rejected"
        ),
    })
    return result


def authorize_data_clump_compatibility_changes(
    compatibility: Mapping[str, Any],
    migration: Mapping[str, Any],
    *,
    production_patch: str | None,
    group: str,
) -> dict[str, Any]:
    """Partition compatibility violations using an accepted lineage.

    Python public-signature changes are authorized only for migrated target
    indexes.  A C++ pure-virtual change additionally requires a replacement
    pure-virtual declaration with the same method name that no longer exposes
    the frozen group.  Other compatibility violations remain fatal.
    """

    violations = [
        dict(item)
        for item in list(compatibility.get("violations") or [])
        if isinstance(item, Mapping)
    ]
    lineage_by_index = _validated_lineage_by_index(migration, group=group)
    migration_ready = bool(
        migration.get("applicable") is True
        and migration.get("ok") is True
        and migration.get("project_full_required") is True
        and migration.get("closure_status") == "requires_project_full"
        and migration.get("old_group_entries_removed") is True
        and not list(migration.get("parallel_old_group_entries") or [])
        and lineage_by_index
    )
    method_lineage: dict[str, list[tuple[int, Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    for target_index, item in lineage_by_index.items():
        predecessor = item["predecessor"]
        method_lineage[str(predecessor.get("declared_name") or "")].append(
            (target_index, item)
        )
    added_virtuals = _added_pure_virtuals(production_patch)
    authorized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if migration_ready:
        production_units, production_patch_error = current_target_hunk_units(
            production_patch
        )
        if production_patch_error:
            rejected.append({
                "code": "DATA_CLUMP_MIGRATION_PRODUCTION_PATCH_UNAVAILABLE",
                "error": production_patch_error,
                "migration_rejection": "production_patch_analysis_failed",
            })
        else:
            cross_file_entries, cross_file_error = _parallel_old_group_entries(
                production_units,
                language=str(migration.get("language") or "").strip().lower(),
                group=normalize_group(group),
                lineage=list(migration.get("lineage") or []),
                allow_cross_file=True,
            )
            if cross_file_error:
                rejected.append({
                    "code": "DATA_CLUMP_MIGRATION_PRODUCTION_PATCH_UNAVAILABLE",
                    "error": cross_file_error,
                    "migration_rejection": "production_patch_analysis_failed",
                })
            elif cross_file_entries:
                rejected.append({
                    "code": "DATA_CLUMP_PARALLEL_OLD_GROUP_ENTRY_ADDED",
                    "entries": cross_file_entries,
                    "migration_rejection": "parallel_old_group_entry_added",
                })
    for violation in violations:
        code = str(violation.get("code") or "")
        target_index = violation.get("target_index")
        if (
            migration_ready
            and code == "PUBLIC_PYTHON_SIGNATURE_CHANGED"
            and isinstance(target_index, int)
            and target_index in lineage_by_index
            and _python_violation_matches_lineage(
                violation,
                lineage_by_index[target_index],
            )
        ):
            authorized.append({
                **violation,
                "authorization": DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
                "migration_target_index": target_index,
            })
            continue
        if (
            migration_ready
            and code == "CPP_PURE_VIRTUAL_ABI_CHANGED"
            and _text_contains_group(
                str(violation.get("baseline_declaration") or ""),
                group,
            )
        ):
            file_name = str(violation.get("file") or "")
            method = str(violation.get("method") or "")
            matching_lineage = method_lineage.get(method, [])
            replacements = []
            matched_index = -1
            if len(matching_lineage) == 1:
                matched_index, lineage_item = matching_lineage[0]
                replacements = [
                    declaration
                    for declaration in added_virtuals.get(file_name, [])
                    if _cpp_pure_virtual_replacement_matches_lineage(
                        declaration,
                        lineage_item,
                        group=group,
                    )
                ]
            if matched_index >= 0 and replacements:
                authorized.append({
                    **violation,
                    "authorization": DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
                    "migration_target_index": matched_index,
                    "replacement_declarations": replacements,
                })
                continue
        rejected.append({
            **violation,
            "migration_rejection": (
                "migration_contract_incomplete"
                if not migration_ready
                else "compatibility_violation_not_exactly_mapped"
            ),
        })
    return {
        "contract": DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
        "ok": not rejected,
        "authorized": authorized,
        "violations": rejected,
    }


def _migration_edge(
    frozen: Mapping[str, Any],
    successor: Mapping[str, Any],
    *,
    changed_patch: str,
    removed_blocks: list[dict[str, Any]],
    current_units: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    frozen_identity, frozen_error = validate_ast_declaration_identity(
        frozen.get("declaration_identity")
    )
    successor_identity, successor_error = validate_ast_declaration_identity(
        successor.get("declaration_identity")
    )
    if frozen_error or frozen_identity is None:
        return {}, "baseline_declaration_identity_invalid"
    if successor_error or successor_identity is None:
        return {}, "successor_declaration_identity_invalid"
    if (
        str(frozen_identity.get("owner_qualified_name") or "")
        != str(successor_identity.get("owner_qualified_name") or "")
    ):
        return {}, "migration_successor_owner_changed"
    frozen_file = str(frozen.get("file") or "")
    successor_file = str(successor.get("file") or "")
    if not frozen_file or frozen_file != successor_file:
        return {}, "migration_successor_file_changed"
    old_line = _positive_int(frozen.get("begin_line"))
    current_line = _positive_int(successor.get("begin_line"))
    if old_line is None or current_line is None:
        return {}, "migration_declaration_anchor_invalid"
    touched = _declaration_span_touched(
        frozen,
        successor,
        removed_blocks=removed_blocks,
        current_units=current_units,
    )
    if not touched:
        return {}, "migration_declaration_not_edited"

    strict_identity = evaluate_target_patch_identity(
        [frozen],
        [successor],
        changed_patch=changed_patch,
    )
    patch_witness: dict[str, Any]
    if strict_identity.get("ok") is True:
        patch_witness = {
            "kind": "strict_patch_anchor_with_changed_signature",
            "file": frozen_file,
            "baseline_begin_line": old_line,
            "current_begin_line": current_line,
        }
    else:
        if (
            frozen_identity == successor_identity
            and not str(frozen_identity.get("owner_qualified_name") or "")
        ):
            return {}, "migration_free_function_successor_not_strictly_anchored"
        replacement = same_hunk_identifier_replacement(
            changed_patch,
            old_line=old_line,
            current_line=current_line,
            old_identifier=str(frozen_identity["declared_name"]),
            current_identifier=str(successor_identity["declared_name"]),
        )
        if replacement.get("ok") is not True:
            return {}, "migration_successor_not_same_unique_hunk"
        patch_witness = {
            "kind": "same_hunk_identity_replacement",
            "file": str(replacement.get("file") or frozen_file),
            "hunk_index": int(replacement.get("hunk_index") or 0),
            "baseline_begin_line": old_line,
            "current_begin_line": current_line,
        }

    return {
        "predecessor": _declaration_payload(frozen),
        "successor": _declaration_payload(successor),
        "patch_witness": patch_witness,
    }, ""


def _declaration_span_touched(
    frozen: Mapping[str, Any],
    successor: Mapping[str, Any],
    *,
    removed_blocks: list[dict[str, Any]],
    current_units: list[dict[str, Any]],
) -> bool:
    file_name = str(frozen.get("file") or "")
    old_begin = _positive_int(frozen.get("begin_line")) or 0
    old_end = old_begin + str(frozen.get("signature_text") or "").count("\n")
    current_begin = _positive_int(successor.get("begin_line")) or 0
    current_end = (
        current_begin
        + str(successor.get("signature_text") or "").count("\n")
    )
    frozen_identity, _ = validate_ast_declaration_identity(
        frozen.get("declaration_identity")
    )
    successor_identity, _ = validate_ast_declaration_identity(
        successor.get("declaration_identity")
    )
    old_name = str((frozen_identity or {}).get("declared_name") or "")
    current_name = str((successor_identity or {}).get("declared_name") or "")
    removed = any(
        str(block.get("file") or "") == file_name
        and int(block.get("start_line") or 0) <= old_end
        and int(block.get("end_line") or 0) >= old_begin
        and _identifier_present(str(block.get("body_text") or ""), old_name)
        for block in removed_blocks
    )
    added = any(
        str(unit.get("file") or "") == file_name
        and any(
            current_begin <= int(line) <= current_end
            and _identifier_present(
                _unit_line_text(unit, int(line)),
                current_name,
            )
            for line in list(unit.get("added_lines") or [])
            if isinstance(line, int) and not isinstance(line, bool)
        )
        for unit in current_units
    )
    # Both sides of the declaration signature must be explicit edits. A body
    # edit near an unchanged old API, or a newly added decoy beside it, cannot
    # authorize API/ABI migration.
    return removed and added


def _signature_contains_group(target: Mapping[str, Any], group: str) -> bool:
    expected = set(normalize_group(group).split("|"))
    actual = {
        normalize_group(str(value))
        for value in list(target.get("parameter_fingerprints") or [])
        if str(value).strip()
    }
    expanded = set(actual)
    expanded.update(value[1:] for value in actual if value.startswith(":"))
    return bool(expected) and expected.issubset(expanded)


def _signature_changed(
    frozen: Mapping[str, Any],
    successor: Mapping[str, Any],
) -> bool:
    return _normalized_declaration(str(frozen.get("signature_text") or "")) != (
        _normalized_declaration(str(successor.get("signature_text") or ""))
    )


def _normalized_declaration(value: str) -> str:
    return " ".join(_TOKEN.findall(value))


def _declaration_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    identity, _ = validate_ast_declaration_identity(
        value.get("declaration_identity")
    )
    return {
        "target_index": value.get("target_index"),
        "file": str(value.get("file") or ""),
        "begin_line": int(value.get("begin_line") or 0),
        "end_line": int(value.get("end_line") or 0),
        "declared_name": str((identity or {}).get("declared_name") or ""),
        "owner_qualified_name": str(
            (identity or {}).get("owner_qualified_name") or ""
        ),
        "signature_text": str(value.get("signature_text") or ""),
        "parameter_fingerprints": [
            str(item)
            for item in list(value.get("parameter_fingerprints") or [])
        ],
        "parameter_slots": [
            dict(item)
            for item in list(value.get("parameter_slots") or [])
            if isinstance(item, Mapping)
        ],
        "group": str(value.get("group") or ""),
    }


def _validated_lineage_by_index(
    migration: Mapping[str, Any],
    *,
    group: str,
) -> dict[int, Mapping[str, Any]]:
    """Validate the auditable minimum before compatibility can be waived."""

    result: dict[int, Mapping[str, Any]] = {}
    for raw_item in list(migration.get("lineage") or []):
        if not isinstance(raw_item, Mapping):
            return {}
        predecessor = raw_item.get("predecessor")
        successors = raw_item.get("successors")
        witnesses = raw_item.get("patch_witness")
        if (
            not isinstance(predecessor, Mapping)
            or not isinstance(successors, list)
            or not successors
            or not all(isinstance(item, Mapping) for item in successors)
            or not isinstance(witnesses, list)
            or len(witnesses) != len(successors)
            or not all(_valid_patch_witness(item) for item in witnesses)
            or raw_item.get("old_group_entry_removed") is not True
        ):
            return {}
        target_index = predecessor.get("target_index")
        if (
            isinstance(target_index, bool)
            or not isinstance(target_index, int)
            or target_index in result
            or not str(predecessor.get("file") or "")
            or not str(predecessor.get("declared_name") or "")
            or not _payload_contains_group(predecessor, group)
            or any(_payload_contains_group(item, group) for item in successors)
        ):
            return {}
        result[target_index] = raw_item
    expected_indexes = {
        value
        for value in list(migration.get("migrated_target_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if not result or set(result) != expected_indexes:
        return {}
    return result


def _valid_patch_witness(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return bool(
        value.get("kind") in {
            "strict_patch_anchor_with_changed_signature",
            "same_hunk_identity_replacement",
        }
        and str(value.get("file") or "")
        and _positive_int(value.get("baseline_begin_line")) is not None
        and _positive_int(value.get("current_begin_line")) is not None
    )


def _payload_contains_group(value: Mapping[str, Any], group: str) -> bool:
    if _signature_contains_group(value, normalize_group(group)):
        return True
    slots = {
        normalize_group(str(item.get("member") or ""))
        for item in list(value.get("parameter_slots") or [])
        if isinstance(item, Mapping) and str(item.get("member") or "")
    }
    expected = set(normalize_group(group).split("|"))
    return bool(expected) and expected.issubset(slots)


def _python_violation_matches_lineage(
    violation: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> bool:
    predecessor = lineage.get("predecessor")
    if not isinstance(predecessor, Mapping):
        return False
    return bool(
        str(violation.get("file") or "")
        == str(predecessor.get("file") or "")
        and str(violation.get("method") or "")
        == str(predecessor.get("declared_name") or "")
        and str(violation.get("owner") or "")
        == str(predecessor.get("owner_qualified_name") or "")
    )


def _parallel_old_group_entries(
    current_units: Iterable[Mapping[str, Any]],
    *,
    language: str,
    group: str,
    lineage: Iterable[Mapping[str, Any]],
    allow_cross_file: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Find newly-added compatibility wrappers for migrated predecessors."""

    predecessors: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    predecessor_symbols: dict[tuple[str, str], set[int]] = defaultdict(set)
    predecessor_file_names: dict[tuple[str, str], set[int]] = defaultdict(set)
    predecessor_names: dict[str, set[int]] = defaultdict(set)
    for item in lineage:
        predecessor = item.get("predecessor")
        if not isinstance(predecessor, Mapping):
            continue
        target_index = predecessor.get("target_index")
        if not isinstance(target_index, int) or isinstance(target_index, bool):
            continue
        exact_key = (
            str(predecessor.get("file") or ""),
            str(predecessor.get("declared_name") or ""),
            str(predecessor.get("owner_qualified_name") or ""),
        )
        predecessors[exact_key].add(target_index)
        predecessor_symbols[(exact_key[1], exact_key[2])].add(target_index)
        predecessor_file_names[(exact_key[0], exact_key[1])].add(target_index)
        predecessor_names[exact_key[1]].add(target_index)
    entries: list[dict[str, Any]] = []
    signature_records, signature_error = _newly_added_function_signatures(
        current_units,
        language=language,
    )
    if signature_error:
        return [], signature_error
    for file_name, signature in signature_records:
        key = (
            file_name,
            str(signature.name or ""),
            str(signature.owner_qualified_name or ""),
        )
        target_indexes = predecessors.get(key)
        if not target_indexes and allow_cross_file:
            target_indexes = predecessor_symbols.get((key[1], key[2]))
        if not target_indexes and language == "cpp" and not key[2]:
            # A hunk may begin inside an existing class and therefore omit
            # its lexical owner.  Such an added old-group member cannot be
            # proven unrelated to the frozen predecessor, so fail closed
            # by name within the target file or across the supplied
            # production patch.
            target_indexes = (
                predecessor_names.get(key[1])
                if allow_cross_file
                else predecessor_file_names.get((key[0], key[1]))
            )
        if not target_indexes:
            continue
        payload = {
            "parameter_fingerprints": list(
                signature.parameter_fingerprints
            ),
        }
        if not _signature_contains_group(payload, group):
            continue
        entries.append({
            "file": file_name,
            "declared_name": str(signature.name or ""),
            "owner_qualified_name": str(
                signature.owner_qualified_name or ""
            ),
            "begin_line": int(signature.start_line),
            "end_line": int(signature.end_line),
            "target_indexes": sorted(target_indexes),
            "reason": "compatibility_wrapper_retains_frozen_group",
        })
    entries.sort(key=lambda item: (
        str(item["file"]),
        int(item["begin_line"]),
        str(item["declared_name"]),
    ))
    return entries, ""


def _newly_added_function_signatures(
    current_units: Iterable[Mapping[str, Any]],
    *,
    language: str,
) -> tuple[list[tuple[str, Any]], str]:
    """Parse added callables while retaining C++ lexical class context."""
    units = [dict(item) for item in current_units]
    blocks: Iterable[Mapping[str, Any]] = (
        units
        if language == "cpp"
        else added_blocks_from_target_hunk_units(units)
    )
    records: list[tuple[str, Any]] = []
    for block in blocks:
        file_name = str(block.get("file") or "")
        body_text = str(block.get("body_text") or "")
        start_line = _positive_int(block.get("start_line"))
        if not file_name or start_line is None or not body_text.strip():
            continue
        try:
            signatures = function_signatures_in_text(
                body_text,
                language,
                file_path=Path(file_name),
                start_line=start_line,
            )
        except Exception as exc:
            return [], f"{type(exc).__name__}:{exc}"
        added_lines = {
            int(value)
            for value in list(block.get("added_lines") or [])
            if isinstance(value, int) and not isinstance(value, bool)
        }
        for signature in signatures:
            if language == "cpp":
                signature_start = int(signature.start_line)
                signature_end = (
                    signature_start
                    + str(signature.signature_text or "").count("\n")
                )
                if not any(
                    signature_start <= line <= signature_end
                    for line in added_lines
                ):
                    continue
            records.append((file_name, signature))
    return records, ""


def _cpp_pure_virtual_replacement_matches_lineage(
    declaration: str,
    lineage: Mapping[str, Any],
    *,
    group: str,
) -> bool:
    """Require one owner- and signature-bound successor for an ABI waiver.

    A same-named pure virtual in another class in the same header is not a
    replacement.  Patch-only analysis cannot safely infer an omitted lexical
    owner, so an ABI waiver is deliberately unavailable unless the added
    declaration carries an explicit class/struct or qualified-owner witness.
    """
    predecessor = lineage.get("predecessor")
    successors = lineage.get("successors")
    if (
        not isinstance(predecessor, Mapping)
        or not isinstance(successors, list)
        or len(successors) != 1
        or not isinstance(successors[0], Mapping)
    ):
        return False
    successor = successors[0]
    predecessor_owner = str(predecessor.get("owner_qualified_name") or "")
    successor_owner = str(successor.get("owner_qualified_name") or "")
    declared_name = str(successor.get("declared_name") or "")
    replacement_owner = _cpp_declaration_owner_witness(declaration)
    if (
        not predecessor_owner
        or successor_owner != predecessor_owner
        or not replacement_owner
        or not _same_cpp_owner(replacement_owner, successor_owner)
        or _cpp_declaration_name(declaration) != declared_name
        or _text_contains_group(declaration, group)
    ):
        return False
    return _declaration_parameter_contract(declaration) == (
        _declaration_parameter_contract(
            str(successor.get("signature_text") or "")
        )
    ) and bool(_declaration_parameter_contract(declaration))


def _cpp_declaration_owner_witness(declaration: str) -> str:
    prefix = str(declaration or "").split("virtual", 1)[0]
    class_matches = re.findall(
        r"\b(?:class|struct)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\b[^{};]*\{",
        prefix,
    )
    if class_matches:
        return class_matches[-1]
    callable_prefix = str(declaration or "").split("(", 1)[0]
    qualified = re.findall(
        r"((?:[A-Za-z_]\w*::)+)~?[A-Za-z_]\w*\s*$",
        callable_prefix,
    )
    return qualified[-1].rstrip(":") if qualified else ""


def _same_cpp_owner(left: str, right: str) -> bool:
    # A simple-name suffix is not enough: ``left::A`` and ``right::A`` may be
    # unrelated ABI owners in the same header.  When the hunk does not expose
    # the complete lexical owner, the migration remains un-authorized.
    return bool(left and left == right)


def _declaration_parameter_contract(declaration: str) -> tuple[str, ...]:
    text = str(declaration or "")
    start = text.find("(")
    if start < 0:
        return ()
    depth = 0
    for index in range(start, len(text)):
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return tuple(_TOKEN.findall(text[start + 1:index]))
    return ()


def _identifier_present(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    return bool(re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
        text,
    ))


def _unit_line_text(unit: Mapping[str, Any], line_number: int) -> str:
    start_line = int(unit.get("start_line") or 0)
    offset = line_number - start_line
    lines = str(unit.get("body_text") or "").split("\n")
    return lines[offset] if 0 <= offset < len(lines) else ""


def _successor_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(value.get("file") or ""),
        int(value.get("begin_line") or 0),
        str(value.get("declared_name") or ""),
        str(value.get("owner_qualified_name") or ""),
        _normalized_declaration(str(value.get("signature_text") or "")),
    )


def _added_pure_virtuals(patch: str | None) -> dict[str, list[str]]:
    if patch is None:
        return {}
    changed = _changed_virtual_declarations(patch, "+")
    return {
        file_name: _pure_virtual_declarations(lines)
        for file_name, lines in changed.items()
    }


def _text_contains_group(text: str, group: str) -> bool:
    members = [
        item.rsplit(":", 1)[-1]
        for item in normalize_group(group).split("|")
        if item and item.rsplit(":", 1)[-1]
    ]
    tokens = set(re.findall(r"[A-Za-z_]\w*", text))
    return bool(members) and all(member in tokens for member in members)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value
