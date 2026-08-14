from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .analysis import (
    FunctionSignature,
    extract_snippet_candidates,
    function_signatures_in_text,
    iter_function_signatures,
    signature_parameter_fingerprints,
)
from .detector_utils import normalize_group, parse_group_from_evidence
from .compatibility_contract import evaluate_target_local_compatibility
from .data_clump_migration import (
    authorize_data_clump_compatibility_changes,
    evaluate_data_clump_declaration_migration,
)
from .location import LocationTarget
from .target_patch_identity import (
    added_blocks_from_target_hunk_units,
    ast_declaration_identity,
    current_target_hunk_units,
    evaluate_data_clump_target_patch_identity,
    validate_ast_declaration_identity,
)

DEFAULT_DATA_CLUMPS_OCCURRENCES = 3
_BODY_WINDOW_TOKENS = 12
_BODY_WINDOW_LIMIT = 3
_TOKEN = re.compile(
    r"[A-Za-z_]\w*|(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?)|"
    r"(?:==|!=|<=|>=|->|::|&&|\|\||\+\+|--|<<|>>)|[^\w\s]"
)


def data_clump_group_from_evidence(evidence: str) -> str:
    return normalize_group(parse_group_from_evidence(evidence))


def data_clump_occurrence_threshold() -> int:
    return DEFAULT_DATA_CLUMPS_OCCURRENCES


def detect_data_clump_occurrences(
    project_root: Path,
    *,
    language: str,
    evidence: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Discover a parameter group across a project for offline curation only.

    Runtime Target Guards must use :func:`evaluate_data_clump_targets` with
    caller-supplied locations.  Keeping discovery separate prevents a Guard
    from silently widening one target into a whole-project source scan.
    """
    group = data_clump_group_from_evidence(evidence)
    if not group:
        return {
            "success": False,
            "group": "",
            "occurrence_count": 0,
            "occurrences": [],
            "error": "missing group=... evidence",
        }
    try:
        signatures = iter_function_signatures(project_root, language)
    except Exception as exc:
        return {
            "success": False,
            "group": group,
            "occurrence_count": 0,
            "occurrences": [],
            "error": f"data clump group detection failed: {exc}",
        }
    occurrences = [
        _occurrence_payload(project_root, signature, group)
        for signature in signatures
        if signature_contains_group(signature, group)
    ]
    occurrences.sort(key=lambda item: (item["file"], item["begin_line"], item["method"]))
    return {
        "success": True,
        "group": group,
        "occurrence_count": len(occurrences),
        "occurrences": occurrences[:limit] if limit is not None else occurrences,
    }


def evaluate_data_clump_targets(
    project_root: Path,
    *,
    language: str,
    group: str,
    targets: Iterable[LocationTarget],
    baseline_occurrence_contract: Any = None,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    """Evaluate a frozen parameter group at explicit function locations only."""
    normalized_group = normalize_group(group)
    if not normalized_group:
        return {
            "success": False,
            "group": "",
            "occurrence_count": 0,
            "occurrences": [],
            "scope_files": [],
            "error": "missing target_context.group",
        }

    root = project_root.expanduser().resolve()
    occurrences: list[dict[str, Any]] = []
    target_snapshots: list[dict[str, Any]] = []
    scope_files: set[str] = set()
    seen_targets: set[tuple[str, int, str]] = set()
    resolved_identities: dict[tuple[str, int, int], int] = {}
    deferred_identity_collisions: list[dict[str, Any]] = []
    frozen_targets = {
        int(item.get("target_index")): dict(item)
        for item in (
            baseline_occurrence_contract
            if isinstance(baseline_occurrence_contract, list)
            else []
        )
        if isinstance(item, Mapping)
        and isinstance(item.get("target_index"), int)
    }
    for target_index, target in enumerate(targets):
        try:
            relative = target.file_path.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            return {
                "success": False,
                "group": normalized_group,
                "occurrence_count": 0,
                "occurrences": [],
                "scope_files": sorted(scope_files),
                "error": "explicit data-clumps target is outside project root",
            }
        scope_files.add(relative)
        identity = (relative, int(target.line or 0), str(target.method or ""))
        if identity in seen_targets:
            continue
        seen_targets.add(identity)
        if not target.file_path.is_file():
            target_snapshots.append({
                "target_index": target_index,
                "file": relative,
                "method": str(target.method or ""),
                "resolved": False,
                "parameter_fingerprints": [],
                "body_text": "",
            })
            continue
        try:
            candidates = extract_snippet_candidates(target, language)
        except Exception as exc:
            unresolved = {
                "target_index": target_index,
                "file": relative,
                "method": str(target.method or ""),
            }
            return {
                "success": False,
                "group": normalized_group,
                "occurrence_count": len(occurrences),
                "occurrences": occurrences,
                "scope_files": sorted(scope_files),
                "scope_mode": "explicit_target_locations",
                "target_snapshots": target_snapshots,
                "unresolved_targets": [unresolved],
                "occurrence_contract": [],
                "error": (
                    "explicit data-clumps target parse failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        if not candidates:
            target_snapshots.append({
                "target_index": target_index,
                "file": relative,
                "method": str(target.method or ""),
                "resolved": False,
                "parameter_fingerprints": [],
                "body_text": "",
            })
            continue
        snippet, target_parseable = candidates[0]
        frozen = frozen_targets.get(target_index)
        if frozen is not None and changed_patch is not None:
            identity_matches: list[tuple[Any, bool]] = []
            for candidate_snippet, candidate_parseable in candidates:
                candidate_identity = evaluate_data_clump_target_patch_identity(
                    [frozen],
                    [{
                        "target_index": target_index,
                        "file": relative,
                        "method": str(target.method or ""),
                        "resolved": True,
                        "begin_line": candidate_snippet.start_line,
                        "declaration_identity": ast_declaration_identity(
                            candidate_snippet.declared_name,
                            candidate_snippet.owner_qualified_name,
                        ),
                    }],
                    changed_patch=changed_patch,
                    language=language,
                )
                if candidate_identity.get("ok") is True:
                    identity_matches.append((
                        candidate_snippet,
                        candidate_parseable,
                    ))
            if len(identity_matches) == 1:
                snippet, target_parseable = identity_matches[0]
            elif len(identity_matches) > 1:
                return {
                    "success": False,
                    "group": normalized_group,
                    "occurrence_count": len(occurrences),
                    "occurrences": occurrences,
                    "scope_files": sorted(scope_files),
                    "scope_mode": "explicit_target_locations",
                    "target_snapshots": target_snapshots,
                    "unresolved_targets": [{
                        "target_index": target_index,
                        "file": relative,
                        "method": str(target.method or ""),
                        "reason": "target_identity_collision",
                    }],
                    "target_identity_collision": True,
                    "target_identity_collisions": [{
                        "file": relative,
                        "method": str(target.method or ""),
                        "target_indexes": [target_index],
                        "candidate_begin_lines": [
                            item[0].start_line for item in identity_matches
                        ],
                    }],
                    "occurrence_contract": [],
                    "error": "target_identity_collision",
                }
        if target_parseable is not True:
            return {
                "success": False,
                "group": normalized_group,
                "occurrence_count": len(occurrences),
                "occurrences": occurrences,
                "scope_files": sorted(scope_files),
                "scope_mode": "explicit_target_locations",
                "target_snapshots": target_snapshots,
                "unresolved_targets": [{
                    "target_index": target_index,
                    "file": relative,
                    "method": str(target.method or ""),
                    "reason": "target_parse_failed",
                }],
                "occurrence_contract": [],
                "error": "explicit data-clumps target parse failed",
            }
        resolved_identity = (relative, snippet.start_line, snippet.end_line)
        previous_target_index = resolved_identities.get(resolved_identity)
        if previous_target_index is not None:
            collision = {
                "file": relative,
                "method": str(target.method or ""),
                "begin_line": snippet.start_line,
                "end_line": snippet.end_line,
                "target_indexes": [previous_target_index, target_index],
            }
            # A parameter-object migration may consolidate several old
            # declarations into one successor. Defer only a real, non-empty
            # patch collision to the declaration-lineage gate below. Baseline
            # and no-patch collisions remain hard failures.
            if not changed_patch:
                unresolved = [
                    {
                        "target_index": index,
                        "file": relative,
                        "method": str(target.method or ""),
                        "reason": "target_identity_collision",
                    }
                    for index in collision["target_indexes"]
                ]
                return {
                    "success": False,
                    "group": normalized_group,
                    "occurrence_count": len(occurrences),
                    "occurrences": occurrences,
                    "scope_files": sorted(scope_files),
                    "scope_mode": "explicit_target_locations",
                    "target_snapshots": target_snapshots,
                    "unresolved_targets": unresolved,
                    "target_identity_collision": True,
                    "target_identity_collisions": [collision],
                    "occurrence_contract": [],
                    "error": "target_identity_collision",
                }
            deferred_identity_collisions.append(collision)
        resolved_identities[resolved_identity] = target_index
        parameter_fingerprints = signature_parameter_fingerprints(
            snippet.signature_text,
            language,
        )
        signature = FunctionSignature(
            file_path=target.file_path,
            start_line=snippet.start_line,
            end_line=snippet.end_line,
            name=str(target.method or ""),
            signature_text=snippet.signature_text,
            parameter_fingerprints=parameter_fingerprints,
        )
        occurrence = None
        if signature_contains_group(signature, normalized_group):
            occurrence = _occurrence_payload(root, signature, normalized_group)
            occurrences.append(occurrence)
        target_snapshots.append({
            "target_index": target_index,
            "file": relative,
            "method": str(target.method or ""),
            "resolved": True,
            "begin_line": snippet.start_line,
            "end_line": snippet.end_line,
            "signature_text": snippet.signature_text,
            "parameter_fingerprints": parameter_fingerprints,
            "body_text": snippet.body_text,
            "occurrence": occurrence,
            "declaration_identity": ast_declaration_identity(
                snippet.declared_name,
                snippet.owner_qualified_name,
            ),
        })

    occurrences.sort(key=lambda item: (item["file"], item["begin_line"], item["method"]))
    unresolved_targets = [
        {
            "target_index": int(item.get("target_index") or 0),
            "file": str(item.get("file") or ""),
            "method": str(item.get("method") or ""),
        }
        for item in target_snapshots
        if item.get("resolved") is not True
    ]
    occurrence_contract = _build_occurrence_contract(
        normalized_group,
        target_snapshots,
        language=language,
    )
    return {
        "success": True,
        "group": normalized_group,
        "occurrence_count": len(occurrences),
        "occurrences": occurrences,
        "scope_files": sorted(scope_files),
        "scope_mode": "explicit_target_locations",
        "target_snapshots": target_snapshots,
        "unresolved_targets": unresolved_targets,
        "deferred_target_identity_collisions": deferred_identity_collisions,
        "occurrence_contract": occurrence_contract,
    }


def evaluate_data_clump_checkpoint_contract(
    analysis: Mapping[str, Any],
    *,
    language: str,
    baseline_occurrence_contract: Any,
    changed_patch: str | None,
    compatibility_patch: str | None = None,
) -> dict[str, Any]:
    """Compare current explicit targets with the c000 Data Clumps witness.

    The frozen witness carries only caller-selected declarations.  Current
    analysis reads those same functions plus added hunks from their files; it
    never discovers or opens another source file.
    """

    current_occurrences = list(analysis.get("occurrences") or [])
    if not isinstance(baseline_occurrence_contract, list):
        return {
            "continuity_ok": False,
            "continuity_occurrence_count": len(current_occurrences),
            "continuity_occurrences": current_occurrences,
            "inline_copy_contract_available": False,
            "inline_copy_analysis_ok": False,
            "inline_copy_expansions": [],
            "checkpoint_contract_error": "baseline_occurrence_contract_unavailable",
        }

    baseline_records = [
        dict(item)
        for item in baseline_occurrence_contract
        if isinstance(item, Mapping)
    ]
    declaration_identity_errors = [
        error
        for record in baseline_records
        for _, error in [validate_ast_declaration_identity(
            record.get("declaration_identity")
        )]
        if error
    ]
    continuity_ok = (
        bool(baseline_records)
        and len(baseline_records) == len(baseline_occurrence_contract)
        and not list(analysis.get("unresolved_targets") or [])
        and not declaration_identity_errors
    )
    current_targets = {
        int(item.get("target_index")): item
        for item in list(analysis.get("target_snapshots") or [])
        if isinstance(item, Mapping) and isinstance(item.get("target_index"), int)
    }
    target_identity = evaluate_data_clump_target_patch_identity(
        baseline_records,
        current_targets.values(),
        changed_patch=changed_patch,
        language=language,
    )
    migration = evaluate_data_clump_declaration_migration(
        baseline_records,
        current_targets.values(),
        changed_patch=changed_patch,
        language=language,
        group=(
            str(baseline_records[0].get("group") or "")
            if baseline_records
            else ""
        ),
    )
    migrated_indexes = {
        int(value)
        for value in list(migration.get("migrated_target_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool)
    }
    target_identity_failures = [
        dict(item)
        for item in list(target_identity.get("failures") or [])
        if isinstance(item, Mapping)
    ]
    unauthorized_identity_failures = [
        item
        for item in target_identity_failures
        if not _identity_failure_covered_by_migration(item, migrated_indexes)
    ]
    migration_authorized = bool(
        migration.get("applicable") is True
        and migration.get("ok") is True
        and migration.get("old_group_entries_removed") is True
        and migration.get("project_full_required") is True
        and migration.get("closure_status") == "requires_project_full"
        and not unauthorized_identity_failures
        and not target_identity.get("error")
    )
    target_identity_ok = bool(
        target_identity.get("ok") is True or migration_authorized
    )
    continuity_ok = continuity_ok and target_identity_ok
    continuity: dict[tuple[str, str, int], dict[str, Any]] = {}
    for occurrence in current_occurrences:
        if not isinstance(occurrence, Mapping):
            continue
        key = (
            str(occurrence.get("file") or ""),
            str(occurrence.get("method") or ""),
            int(occurrence.get("begin_line") or 0),
        )
        continuity[key] = {**dict(occurrence), "match_mode": "exact_frozen_group"}

    for record in baseline_records:
        target_index = record.get("target_index")
        slots = record.get("parameter_slots")
        if not isinstance(target_index, int) or not isinstance(slots, list) or not slots:
            continuity_ok = False
            continue
        current = current_targets.get(target_index)
        if not isinstance(current, Mapping) or current.get("resolved") is not True:
            continue
        fingerprints = list(current.get("parameter_fingerprints") or [])
        if not _frozen_slots_retain_name_or_type(fingerprints, slots):
            continue
        payload = dict(current.get("occurrence") or {})
        if not payload:
            payload = {
                "file": str(current.get("file") or record.get("file") or ""),
                "location": (
                    f"{current.get('file') or record.get('file')}:"
                    f"method={current.get('method') or record.get('method')}|"
                    f"line={int(current.get('begin_line') or 0)}"
                ),
                "class": "",
                "method": str(current.get("method") or record.get("method") or ""),
                "begin_line": int(current.get("begin_line") or 0),
                "end_line": int(current.get("end_line") or 0),
                "signature_text": str(current.get("signature_text") or ""),
            }
        key = (
            str(payload.get("file") or ""),
            str(payload.get("method") or ""),
            int(payload.get("begin_line") or 0),
        )
        continuity[key] = {
            **payload,
            "match_mode": "frozen_parameter_slot_name_or_type",
        }

    record_windows = [_valid_body_windows(record) for record in baseline_records]
    inline_copy_contract_available = data_clump_body_window_contract_available(
        baseline_records
    )
    changed_units: list[dict[str, Any]] = []
    patch_error = ""
    if changed_patch is not None:
        changed_units, patch_error = current_target_hunk_units(changed_patch)
    else:
        patch_error = "changed_target_hunks_unavailable"

    added_group_error = ""
    if not patch_error:
        added_occurrences, added_group_error = _added_group_occurrences(
            added_blocks_from_target_hunk_units(changed_units),
            group=(
                str(baseline_records[0].get("group") or "")
                if baseline_records
                else ""
            ),
            language=language,
            explicit_spans={
                str(item.get("file") or ""): [
                    (
                        int(candidate.get("begin_line") or 0),
                        int(candidate.get("end_line") or 0),
                    )
                    for candidate in current_targets.values()
                    if candidate.get("resolved") is True
                    and str(candidate.get("file") or "")
                    == str(item.get("file") or "")
                ]
                for item in current_targets.values()
            },
        )
        if added_group_error:
            continuity_ok = False
        for occurrence in added_occurrences:
            key = (
                str(occurrence.get("file") or ""),
                str(occurrence.get("method") or ""),
                int(occurrence.get("begin_line") or 0),
            )
            continuity[key] = occurrence

    continuity_occurrences = sorted(
        continuity.values(),
        key=lambda item: (
            str(item.get("file") or ""),
            int(item.get("begin_line") or 0),
            str(item.get("method") or ""),
        ),
    )
    added_old_group_entries = [
        dict(item)
        for item in continuity_occurrences
        if item.get("match_mode") == "added_target_hunk_frozen_group"
    ]
    parallel_old_group_entries = [
        dict(item)
        for item in list(migration.get("parallel_old_group_entries") or [])
        if isinstance(item, Mapping)
    ]
    inline_error = (
        "baseline_body_window_contract_unavailable"
        if not inline_copy_contract_available
        else patch_error or added_group_error
    )
    inline_copy_analysis_ok = bool(
        inline_copy_contract_available and not inline_error
    )

    current_function_units_by_identity: dict[
        tuple[str, int, int], dict[str, Any]
    ] = {}
    for item in current_targets.values():
        if item.get("resolved") is not True:
            continue
        unit = {
            "file": str(item.get("file") or ""),
            "target_index": int(item.get("target_index") or 0),
            "method": str(item.get("method") or ""),
            "begin_line": int(item.get("begin_line") or 0),
            "end_line": int(item.get("end_line") or 0),
            "body_text": str(item.get("body_text") or ""),
        }
        current_function_units_by_identity.setdefault(
            (
                str(unit["file"]),
                int(unit["begin_line"]),
                int(unit["end_line"]),
            ),
            unit,
        )
    current_function_units = list(current_function_units_by_identity.values())
    explicit_spans: dict[str, list[tuple[int, int]]] = {}
    for item in current_function_units:
        explicit_spans.setdefault(str(item["file"]), []).append((
            int(item["begin_line"]),
            int(item["end_line"]),
        ))
    expansions: list[dict[str, Any]] = []
    if inline_copy_analysis_ok and inline_copy_contract_available:
        for record, valid_windows in zip(baseline_records, record_windows):
            expanded_windows: list[dict[str, Any]] = []
            for window in valid_windows:
                tokens = [str(item) for item in list(window.get("tokens") or [])]
                if not tokens:
                    continue
                current_counts = _window_occurrence_counts(
                    tokens,
                    language=language,
                    function_units=current_function_units,
                    changed_units=changed_units,
                    explicit_spans=explicit_spans,
                    source_target_index=int(record.get("target_index") or 0),
                )
                baseline_count = int(window.get("baseline_occurrences") or 1)
                baseline_source_count = int(
                    window.get("baseline_source_occurrences") or 1
                )
                baseline_outside_count = max(
                    0,
                    baseline_count - baseline_source_count,
                )
                current_count = int(current_counts["total"])
                source_relocated = bool(
                    int(current_counts["source"]) < baseline_source_count
                    and int(current_counts["outside"]) > baseline_outside_count
                )
                if current_count > baseline_count or source_relocated:
                    expanded_windows.append({
                        "sha256": str(window.get("sha256") or ""),
                        "baseline_occurrences": baseline_count,
                        "current_occurrences": current_count,
                        "baseline_source_occurrences": baseline_source_count,
                        "current_source_occurrences": int(
                            current_counts["source"]
                        ),
                        "baseline_outside_occurrences": baseline_outside_count,
                        "current_outside_occurrences": int(
                            current_counts["outside"]
                        ),
                        "reason": (
                            "source_window_relocated"
                            if source_relocated
                            else "window_occurrence_expanded"
                        ),
                    })
            if expanded_windows:
                strongest = max(
                    expanded_windows,
                    key=lambda item: (
                        int(item["current_occurrences"])
                        - int(item["baseline_occurrences"]),
                        int(item["current_occurrences"]),
                        -int(item["baseline_occurrences"]),
                    ),
                )
                expansions.append({
                    "source_file": str(record.get("file") or ""),
                    "source_method": str(record.get("method") or ""),
                    "baseline_occurrences": int(strongest["baseline_occurrences"]),
                    "current_occurrences": int(strongest["current_occurrences"]),
                    "window_sha256": sorted({
                        str(item["sha256"])
                        for item in expanded_windows
                    }),
                    "window_counts": expanded_windows,
                    "reason": str(strongest["reason"]),
                })

    if migration_authorized and not added_old_group_entries:
        migrated_predecessors = {
            (
                str(item.get("predecessor", {}).get("file") or ""),
                str(item.get("predecessor", {}).get("declared_name") or ""),
            )
            for item in list(migration.get("lineage") or [])
            if isinstance(item, Mapping)
            and isinstance(item.get("predecessor"), Mapping)
        }
        # Moving a frozen body into its declared no-old-group successor is the
        # intended parameter-object migration. Keep rejecting relocation for
        # every predecessor not covered by the audited lineage.
        expansions = [
            item
            for item in expansions
            if not (
                (
                    str(item.get("source_file") or ""),
                    str(item.get("source_method") or ""),
                )
                in migrated_predecessors
                and item.get("reason") == "source_window_relocated"
                and int(item.get("current_occurrences") or 0)
                == int(item.get("baseline_occurrences") or 0)
            )
        ]

    result = {
        "continuity_ok": continuity_ok,
        "continuity_occurrence_count": len(continuity_occurrences),
        "continuity_occurrences": continuity_occurrences,
        "inline_copy_contract_available": inline_copy_contract_available,
        "inline_copy_analysis_ok": inline_copy_analysis_ok,
        "inline_copy_expansions": expansions,
        "target_patch_identity_ok": target_identity_ok,
        "target_patch_identity_contract": str(
            (
                migration.get("contract")
                if migration_authorized
                else target_identity.get("contract")
            )
            or ""
        ),
        "target_patch_identity_failures": (
            target_identity_failures
            if target_identity.get("ok") is True
            else (
                unauthorized_identity_failures
                if migration_authorized
                else [
                    *unauthorized_identity_failures,
                    *list(migration.get("failures") or []),
                ]
            )
        ),
        "constructor_signature_reanchors": list(
            target_identity.get("constructor_signature_reanchors") or []
        ),
        "declaration_migration": migration,
        "declaration_lineage": list(migration.get("lineage") or []),
        "declaration_migration_mode": str(migration.get("mode") or ""),
        "project_full_required": bool(
            migration_authorized
            and migration.get("project_full_required") is True
        ),
        "parallel_old_group_entries": parallel_old_group_entries,
    }
    if compatibility_patch is not None:
        compatibility = evaluate_target_local_compatibility(
            language=language,
            baseline_targets=baseline_records,
            current_targets=current_targets.values(),
            production_patch=compatibility_patch,
        )
        authorization = authorize_data_clump_compatibility_changes(
            compatibility,
            migration if migration_authorized else {},
            production_patch=compatibility_patch,
            group=(
                str(baseline_records[0].get("group") or "")
                if baseline_records
                else ""
            ),
        )
        result["compatibility_contract"] = {
            **compatibility,
            "ok": authorization.get("ok") is True,
            "violations": list(authorization.get("violations") or []),
            "authorized_migrations": list(
                authorization.get("authorized") or []
            ),
        }
        if authorization.get("ok") is not True:
            result["guard_violations"] = list(
                authorization.get("violations") or []
            )
    if target_identity.get("error"):
        result["target_patch_identity_error"] = str(target_identity["error"])
    if declaration_identity_errors:
        result["checkpoint_contract_error"] = (
            "baseline_declaration_identity_contract_unavailable:"
            + declaration_identity_errors[0]
        )
    elif inline_error:
        result["checkpoint_contract_error"] = inline_error
    return result


def data_clump_body_window_contract_available(records: Any) -> bool:
    """Whether every frozen occurrence has a body-copy witness.

    A parser-confirmed empty function body has no behavior to inline or
    relocate, so its explicit empty-body marker is a complete witness rather
    than a missing fallback metric.  Non-empty short bodies still fail closed.
    """

    return bool(records) and isinstance(records, list) and all(
        isinstance(record, Mapping)
        and (
            bool(_valid_body_windows(record))
            or record.get("body_copy_not_applicable")
            == "empty_function_body"
        )
        for record in records
    )


def data_clump_declaration_identity_contract_available(records: Any) -> bool:
    """Whether every frozen occurrence has a valid parser-derived identity."""
    return bool(records) and isinstance(records, list) and all(
        isinstance(record, Mapping)
        and validate_ast_declaration_identity(
            record.get("declaration_identity")
        )[0]
        is not None
        for record in records
    )


def _valid_body_windows(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        window
        for window in list(record.get("body_windows") or [])
        if isinstance(window, Mapping)
        and isinstance(window.get("tokens"), list)
        and bool(window.get("tokens"))
        and isinstance(window.get("baseline_occurrences"), int)
        and not isinstance(window.get("baseline_occurrences"), bool)
        and int(window.get("baseline_occurrences")) > 0
        and isinstance(window.get("baseline_source_occurrences"), int)
        and not isinstance(window.get("baseline_source_occurrences"), bool)
        and int(window.get("baseline_source_occurrences")) > 0
    ]


def signature_contains_group(signature: FunctionSignature, group: str) -> bool:
    target = set(group.split("|"))
    params: set[str] = set()
    for item in signature.parameter_fingerprints:
        if not item.strip():
            continue
        normalized = normalize_group(item)
        params.add(normalized)
        if normalized.startswith(":"):
            params.add(normalized[1:])
    return bool(target) and target.issubset(params)


def _build_occurrence_contract(
    group: str,
    target_snapshots: list[dict[str, Any]],
    *,
    language: str,
) -> list[dict[str, Any]]:
    members = set(group.split("|"))
    matching = [
        item
        for item in target_snapshots
        if isinstance(item.get("occurrence"), Mapping)
    ]
    token_lists = {
        int(item["target_index"]): _normalized_tokens(
            str(item.get("body_text") or ""),
            language,
        )
        for item in matching
    }
    candidate_windows: dict[int, list[tuple[str, ...]]] = {}
    frequencies: dict[tuple[str, ...], int] = {}
    for target_index, tokens in token_lists.items():
        if not tokens:
            candidate_windows[target_index] = []
            continue
        width = min(_BODY_WINDOW_TOKENS, len(tokens))
        if width < 4:
            candidate_windows[target_index] = []
            continue
        candidates = [
            tuple(tokens[offset : offset + width])
            for offset in range(0, len(tokens) - width + 1)
        ]
        candidate_windows[target_index] = candidates
        for candidate in candidates:
            frequencies[candidate] = frequencies.get(candidate, 0) + 1

    contracts: list[dict[str, Any]] = []
    for item in matching:
        target_index = int(item["target_index"])
        fingerprints = [
            normalize_group(str(value))
            for value in list(item.get("parameter_fingerprints") or [])
        ]
        slots = [
            {
                "slot": slot,
                "type": _group_member_type(fingerprint),
                "member": fingerprint,
            }
            for slot, fingerprint in enumerate(fingerprints)
            if fingerprint in members
        ]
        available_windows = list(dict.fromkeys(
            candidate_windows.get(target_index, [])
        ))
        minimum_frequency = min(
            (frequencies.get(candidate, 0) for candidate in available_windows),
            default=0,
        )
        lowest_frequency_windows = [
            candidate
            for candidate in available_windows
            if frequencies.get(candidate) == minimum_frequency
        ]
        selected = _select_spread_windows(
            lowest_frequency_windows,
            _BODY_WINDOW_LIMIT,
        )
        occurrence = dict(item["occurrence"])
        contracts.append({
            "schema": "data_clump_explicit_occurrence/v1",
            "target_index": target_index,
            "file": str(item.get("file") or ""),
            "method": str(item.get("method") or ""),
            "begin_line": int(item.get("begin_line") or 0),
            "end_line": int(item.get("end_line") or 0),
            "signature_text": str(item.get("signature_text") or ""),
            "group": group,
            "parameter_count": len(fingerprints),
            "parameter_slots": slots,
            "declaration_identity": dict(
                item.get("declaration_identity") or {}
            ),
            "body_windows": [
                {
                    "sha256": hashlib.sha256(
                        "\0".join(window).encode("utf-8")
                    ).hexdigest(),
                    "tokens": list(window),
                    "token_count": len(window),
                    "baseline_occurrences": frequencies.get(window, 0),
                    "baseline_source_occurrences": _sequence_count(
                        token_lists.get(target_index, []),
                        list(window),
                    ),
                }
                for window in selected
            ],
            "body_copy_not_applicable": (
                "empty_function_body"
                if not token_lists.get(target_index)
                else ""
            ),
            "occurrence": occurrence,
        })
    contracts.sort(key=lambda item: int(item["target_index"]))
    return contracts


def _group_member_type(member: str) -> str:
    return member.rsplit(":", 1)[0] if ":" in member else ""


def _group_member_name(member: str) -> str:
    return member.rsplit(":", 1)[1] if ":" in member else member


def _frozen_slots_retain_name_or_type(
    current_fingerprints: list[Any],
    frozen_slots: list[Any],
) -> bool:
    for raw_slot in frozen_slots:
        if not isinstance(raw_slot, Mapping):
            return False
        slot = raw_slot.get("slot")
        expected_type = raw_slot.get("type")
        expected_member = raw_slot.get("member")
        if (
            not isinstance(slot, int)
            or not isinstance(expected_type, str)
            or not isinstance(expected_member, str)
        ):
            return False
        if slot < 0 or slot >= len(current_fingerprints):
            return False
        current_member = normalize_group(str(current_fingerprints[slot]))
        if (
            _group_member_type(current_member) != expected_type
            and _group_member_name(current_member)
            != _group_member_name(expected_member)
        ):
            return False
    return True


def _identity_failure_covered_by_migration(
    failure: Mapping[str, Any],
    migrated_indexes: set[int],
) -> bool:
    """Whether every target named by one strict failure has lineage."""
    indexes = [
        value
        for value in (
            failure.get("target_index"),
            failure.get("other_target_index"),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return bool(indexes) and all(value in migrated_indexes for value in indexes)


def _select_spread_windows(
    candidates: list[tuple[str, ...]],
    limit: int,
) -> list[tuple[str, ...]]:
    if len(candidates) <= limit:
        return candidates
    indexes = {
        round(index * (len(candidates) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [candidates[index] for index in sorted(indexes)]


def _strip_comments_preserving_lines(text: str, language: str) -> str:
    if language == "python":
        return "\n".join(re.sub(r"#.*", "", line) for line in text.split("\n"))
    without_blocks = re.sub(
        r"/\*.*?\*/",
        lambda match: "\n" * match.group(0).count("\n"),
        text,
        flags=re.DOTALL,
    )
    return "\n".join(re.sub(r"//.*", "", line) for line in without_blocks.split("\n"))


def _normalized_tokens(text: str, language: str) -> list[str]:
    return _TOKEN.findall(_strip_comments_preserving_lines(text, language))


def _added_group_occurrences(
    added_blocks: Iterable[Mapping[str, Any]],
    *,
    group: str,
    language: str,
    explicit_spans: Mapping[str, list[tuple[int, int]]],
) -> tuple[list[dict[str, Any]], str]:
    """Find frozen-group signatures only inside target-patch added blocks."""
    normalized_group = normalize_group(group)
    if not normalized_group:
        return [], "baseline_parameter_group_unavailable"
    occurrences: list[dict[str, Any]] = []
    for block in added_blocks:
        file_name = str(block.get("file") or "")
        body_text = str(block.get("body_text") or "")
        start_line = int(block.get("start_line") or 0)
        if not file_name or start_line < 1 or not body_text.strip():
            continue
        try:
            signatures = function_signatures_in_text(
                body_text,
                language,
                file_path=Path(file_name),
                start_line=start_line,
            )
        except Exception as exc:
            return [], (
                "added_target_hunk_signature_parse_failed:"
                f"{type(exc).__name__}:{exc}"
            )
        for signature in signatures:
            # A freshly introduced parameter-object constructor is the sink
            # that makes the holder refactoring possible, not a relocated
            # business-function occurrence of the clump.
            if _is_data_holder_constructor(signature, language):
                continue
            if any(
                signature.start_line <= end_line
                and signature.end_line >= begin_line
                for begin_line, end_line in explicit_spans.get(file_name, [])
            ):
                continue
            if not signature_contains_group(signature, normalized_group):
                continue
            occurrences.append({
                "file": file_name,
                "location": (
                    f"{file_name}:method={signature.name}|"
                    f"line={signature.start_line}"
                ),
                "class": "",
                "method": signature.name,
                "begin_line": signature.start_line,
                "end_line": signature.end_line,
                "score": len(normalized_group.split("|")),
                "rule_id": (
                    f"generic:data_clumps:{_short_hash(normalized_group)}"
                ),
                "evidence": (
                    f"group={normalized_group}; parameters="
                    f"{'|'.join(signature.parameter_fingerprints)}"
                ),
                "signature_text": signature.signature_text,
                "match_mode": "added_target_hunk_frozen_group",
            })
    return occurrences, ""


def _is_data_holder_constructor(
    signature: FunctionSignature,
    language: str,
) -> bool:
    if language == "python":
        return bool(
            signature.name in {"__init__", "__new__"}
            and signature.owner_kind == "class"
            and signature.owner_qualified_name
        )
    if language == "cpp":
        normalized = re.sub(r"\s+", " ", signature.signature_text).strip()
        return bool(
            signature.name
            and signature.owner_kind in {"class", "qualified"}
            and signature.owner_qualified_name.rsplit("::", 1)[-1]
            == signature.name
            and re.match(rf"^{re.escape(signature.name)}\s*\(", normalized)
        )
    return False


def _window_occurrence_counts(
    window: list[str],
    *,
    language: str,
    function_units: list[dict[str, Any]],
    changed_units: list[dict[str, Any]],
    explicit_spans: Mapping[str, list[tuple[int, int]]],
    source_target_index: int,
) -> dict[str, int]:
    source_count = 0
    outside_count = 0
    for unit in function_units:
        count = _sequence_count(
            _normalized_tokens(str(unit.get("body_text") or ""), language),
            window,
        )
        if int(unit.get("target_index") or 0) == source_target_index:
            source_count += count
        else:
            outside_count += count
    for unit in changed_units:
        text = str(unit.get("body_text") or "")
        cleaned = _strip_comments_preserving_lines(text, language)
        token_records: list[tuple[str, int]] = []
        start_line = int(unit.get("start_line") or 0)
        for offset, line in enumerate(cleaned.split("\n")):
            token_records.extend(
                (match.group(0), start_line + offset)
                for match in _TOKEN.finditer(line)
            )
        tokens = [token for token, _ in token_records]
        added_lines = {
            int(value)
            for value in list(unit.get("added_lines") or [])
            if isinstance(value, int) and not isinstance(value, bool)
        }
        added_declarations = _added_declaration_lines(
            unit,
            language=language,
            added_lines=added_lines,
        )
        for index in _sequence_offsets(tokens, window):
            line = token_records[index][1]
            spans = explicit_spans.get(str(unit.get("file") or ""), [])
            if any(begin <= line <= end for begin, end in spans):
                continue
            matched_lines = {
                token_line
                for _, token_line in token_records[index : index + len(window)]
            }
            added_match = bool(matched_lines.intersection(added_lines))
            added_declaration_owner = any(
                0 <= line - declaration_line <= 32
                for declaration_line in added_declarations
            )
            if not added_match and not added_declaration_owner:
                continue
            outside_count += 1
    return {
        "source": source_count,
        "outside": outside_count,
        "total": source_count + outside_count,
    }


def _added_declaration_lines(
    unit: Mapping[str, Any],
    *,
    language: str,
    added_lines: set[int],
) -> set[int]:
    start_line = int(unit.get("start_line") or 0)
    lines = str(unit.get("body_text") or "").split("\n")
    declarations: set[int] = set()
    for line_number in added_lines:
        offset = line_number - start_line
        if offset < 0 or offset >= len(lines):
            continue
        text = lines[offset].strip()
        if language == "python":
            if re.match(r"^(?:async\s+)?def\s+[A-Za-z_]\w*\s*\(", text):
                declarations.add(line_number)
            continue
        if (
            "(" in text
            and not re.match(
                r"^(?:if|for|while|switch|catch)\s*\(",
                text,
            )
            and re.search(r"[A-Za-z_]\w*\s*\(", text)
        ):
            declarations.add(line_number)
    return declarations


def _sequence_count(tokens: list[str], window: list[str]) -> int:
    return sum(1 for _ in _sequence_offsets(tokens, window))


def _sequence_offsets(tokens: list[str], window: list[str]):
    width = len(window)
    if width == 0 or width > len(tokens):
        return
    needle = tuple(window)
    for index in range(0, len(tokens) - width + 1):
        if tuple(tokens[index : index + width]) == needle:
            yield index


def _occurrence_payload(project_root: Path, signature: FunctionSignature, group: str) -> dict[str, Any]:
    rel = signature.file_path.resolve().relative_to(project_root.resolve()).as_posix()
    evidence = f"group={group}; parameters={'|'.join(signature.parameter_fingerprints)}"
    return {
        "file": rel,
        "location": f"{rel}:method={signature.name}|line={signature.start_line}",
        "class": "",
        "method": signature.name,
        "begin_line": signature.start_line,
        "end_line": signature.end_line,
        "score": len(group.split("|")),
        "rule_id": f"generic:data_clumps:{_short_hash(group)}",
        "evidence": evidence,
        "signature_text": signature.signature_text,
    }


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
