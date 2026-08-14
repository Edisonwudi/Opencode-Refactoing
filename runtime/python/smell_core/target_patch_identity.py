"""Target-local declaration identity from caller-scoped unified diffs.

This module never discovers or opens source files. Callers supply frozen target
records, current records parsed from those same explicit targets, and a patch
already restricted to the target paths.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any


TARGET_PATCH_IDENTITY_CONTRACT = "target-old-to-current-hunk-anchor-v1"
DATA_CLUMP_CONSTRUCTOR_REANCHOR_CONTRACT = (
    "data-clump-cpp-constructor-same-hunk-owner-name-bijection-v1"
)
FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT = (
    "feature-envy-wrapper-same-hunk-owner-name-signature-bijection-v1"
)
CLONE_RETAINED_ENDPOINT_REANCHOR_CONTRACT = (
    "clone-retained-endpoint-same-hunk-owner-name-signature-bijection-v1"
)
SAME_HUNK_IDENTIFIER_REPLACEMENT_CONTRACT = (
    "target-old-new-lines-identifiers-same-unique-hunk-v1"
)
TARGET_ANCHOR_DELETION_CONTRACT = (
    "target-old-anchor-exact-declaration-deletion-v2"
)
TARGET_DECLARATION_DELETION_WITNESS_CONTRACT = (
    "target-declaration-line-hashes-v1"
)
AST_DECLARATION_IDENTITY_CONTRACT = "ast-declared-name-and-owner-v1"
MAX_TARGET_PATCH_BYTES = 8 * 1024 * 1024
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def evaluate_target_patch_identity(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Verify that each current declaration maps from its frozen old anchor."""

    baseline = [dict(item) for item in baseline_targets]
    current = {
        int(item.get("target_index")): dict(item)
        for item in current_targets
        if isinstance(item.get("target_index"), int)
    }
    result = {
        "contract": TARGET_PATCH_IDENTITY_CONTRACT,
        "ok": False,
        "failures": [],
        "patch_available": changed_patch is not None,
        "error": "",
    }
    if not baseline:
        result["error"] = "baseline_target_anchors_unavailable"
        return result
    parsed = _parse_target_patch(changed_patch, max_patch_bytes=max_patch_bytes)
    if not parsed["ok"]:
        result["error"] = parsed["error"]
        return result

    failures: list[dict[str, Any]] = []
    hunks_by_file = parsed["hunks_by_file"]
    for frozen in baseline:
        target_index = frozen.get("target_index")
        file_name = str(frozen.get("file") or "")
        old_line = _positive_integer(frozen.get("begin_line"))
        if not isinstance(target_index, int) or not file_name or old_line is None:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "baseline_target_anchor_invalid",
            })
            continue
        candidate = current.get(target_index)
        if not isinstance(candidate, Mapping) or candidate.get("resolved") is not True:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "method": str(frozen.get("method") or ""),
                "reason": "current_target_unresolved",
            })
            continue
        current_file = str(candidate.get("file") or "")
        current_line = _positive_integer(candidate.get("begin_line"))
        if current_file != file_name or current_line is None:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "current_file": current_file,
                "reason": "current_target_anchor_invalid",
            })
            continue
        frozen_declaration, frozen_declaration_error = _declaration_identity(
            frozen
        )
        if frozen_declaration_error:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "baseline_target_declaration_identity_invalid",
                "error": frozen_declaration_error,
            })
        elif frozen_declaration is not None:
            current_declaration, current_declaration_error = _declaration_identity(
                candidate
            )
            if current_declaration_error or current_declaration is None:
                failures.append({
                    "target_index": target_index,
                    "file": file_name,
                    "reason": "current_target_declaration_identity_invalid",
                    "error": (
                        current_declaration_error
                        or "current_declaration_identity_unavailable"
                    ),
                })
            elif current_declaration != frozen_declaration:
                failures.append({
                    "target_index": target_index,
                    "file": file_name,
                    "reason": "target_declaration_identity_changed",
                    "baseline_declaration_identity": frozen_declaration,
                    "current_declaration_identity": current_declaration,
                })
        allowed = _mapped_current_lines(
            list(hunks_by_file.get(file_name) or []),
            old_line,
        )
        if current_line not in allowed:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "method": str(frozen.get("method") or ""),
                "baseline_begin_line": old_line,
                "current_begin_line": current_line,
                "allowed_current_lines": sorted(allowed),
                "reason": (
                    "target_declaration_deleted"
                    if not allowed
                    else "target_anchor_not_preserved"
                ),
            })
    result["failures"] = failures
    result["ok"] = not failures
    return result


def evaluate_data_clump_target_patch_identity(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    language: str,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Evaluate Data Clumps identity with one narrow C++ constructor route.

    The shared target identity remains strict.  Data Clumps may additionally
    re-anchor a C++ constructor whose signature is replaced while removing the
    frozen parameter group, but only when the old declaration anchor and the
    new declaration anchor are added/removed in the same unified-diff hunk and
    their parser-derived owner and declared name are identical.  Callers still
    have to supply one selected current declaration per frozen target; this
    function enforces the resulting mapping is one-to-one.
    """

    baseline = [dict(item) for item in baseline_targets]
    current_list = [dict(item) for item in current_targets]
    result = {
        "contract": DATA_CLUMP_CONSTRUCTOR_REANCHOR_CONTRACT,
        "ok": False,
        "failures": [],
        "patch_available": changed_patch is not None,
        "error": "",
        "constructor_signature_reanchors": [],
    }
    if not baseline:
        result["error"] = "baseline_target_anchors_unavailable"
        return result

    current_by_index: dict[int, dict[str, Any]] = {}
    duplicate_indexes: set[int] = set()
    for item in current_list:
        target_index = item.get("target_index")
        if not isinstance(target_index, int):
            continue
        if target_index in current_by_index:
            duplicate_indexes.add(target_index)
        current_by_index[target_index] = item
    if duplicate_indexes:
        result["failures"] = [
            {
                "target_index": target_index,
                "reason": "current_target_index_not_unique",
            }
            for target_index in sorted(duplicate_indexes)
        ]
        return result

    parsed = _parse_target_patch(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if not parsed["ok"]:
        result["error"] = parsed["error"]
        return result

    failures: list[dict[str, Any]] = []
    reanchors: list[dict[str, Any]] = []
    resolved_locations: dict[tuple[str, int], int] = {}
    for frozen in baseline:
        target_index = frozen.get("target_index")
        current = (
            current_by_index.get(target_index)
            if isinstance(target_index, int)
            else None
        )
        if current is None:
            failures.append({
                "target_index": target_index,
                "reason": "current_target_unresolved",
            })
            continue

        strict = evaluate_target_patch_identity(
            [frozen],
            [current],
            changed_patch=changed_patch,
            max_patch_bytes=max_patch_bytes,
        )
        if strict.get("ok") is not True:
            frozen_identity, frozen_identity_error = _declaration_identity(
                frozen
            )
            if (
                language != "cpp"
                or frozen_identity_error
                or frozen_identity is None
                or not _is_cpp_constructor_identity(frozen_identity)
            ):
                failures.extend(list(strict.get("failures") or []))
                continue
            reanchor, reason = _cpp_constructor_same_hunk_reanchor(
                frozen,
                current,
                language=language,
                hunks_by_file=parsed["hunks_by_file"],
            )
            if reanchor is None:
                failures.append({
                    "target_index": target_index,
                    "file": str(frozen.get("file") or ""),
                    "method": str(frozen.get("method") or ""),
                    "reason": reason,
                    "strict_failures": list(strict.get("failures") or []),
                })
                continue
            reanchors.append(reanchor)

        current_file = str(current.get("file") or "")
        current_line = _positive_integer(current.get("begin_line"))
        if not current_file or current_line is None:
            failures.append({
                "target_index": target_index,
                "reason": "current_target_anchor_invalid",
            })
            continue
        location = (current_file, current_line)
        previous = resolved_locations.get(location)
        if previous is not None and previous != target_index:
            failures.append({
                "target_index": target_index,
                "other_target_index": previous,
                "file": current_file,
                "current_begin_line": current_line,
                "reason": "constructor_signature_reanchor_not_one_to_one",
            })
            continue
        resolved_locations[location] = int(target_index)

    result["failures"] = failures
    result["constructor_signature_reanchors"] = reanchors
    result["ok"] = not failures
    return result


def evaluate_clone_target_patch_identity(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Map retained clone endpoints through one narrow same-hunk move.

    Clone removal commonly leaves the selected method in place as a small
    delegate while moving its declaration within the surrounding class.  The
    strict old-line mapping remains the primary route.  A moved endpoint is
    accepted only when the old declaration line is removed and exactly one
    current declaration line is added in the same hunk, with identical
    parser-derived owner/name and normalized signature.  Body equality is not
    required because reducing that body is the requested clone repair.
    """

    baseline = [dict(item) for item in baseline_targets]
    current_list = [dict(item) for item in current_targets]
    result = {
        "contract": CLONE_RETAINED_ENDPOINT_REANCHOR_CONTRACT,
        "ok": False,
        "failures": [],
        "patch_available": changed_patch is not None,
        "error": "",
        "retained_endpoint_reanchors": [],
    }
    if not baseline:
        result["error"] = "baseline_target_anchors_unavailable"
        return result

    current_by_index: dict[int, dict[str, Any]] = {}
    duplicate_indexes: set[int] = set()
    for item in current_list:
        target_index = item.get("target_index")
        if not isinstance(target_index, int):
            continue
        if target_index in current_by_index:
            duplicate_indexes.add(target_index)
        current_by_index[target_index] = item
    if duplicate_indexes:
        result["failures"] = [
            {
                "target_index": target_index,
                "reason": "current_target_index_not_unique",
            }
            for target_index in sorted(duplicate_indexes)
        ]
        return result

    parsed = _parse_target_patch(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if not parsed["ok"]:
        result["error"] = parsed["error"]
        return result

    failures: list[dict[str, Any]] = []
    reanchors: list[dict[str, Any]] = []
    resolved_locations: dict[tuple[str, int], int] = {}
    for frozen in baseline:
        target_index = frozen.get("target_index")
        current = (
            current_by_index.get(target_index)
            if isinstance(target_index, int)
            else None
        )
        if current is None:
            failures.append({
                "target_index": target_index,
                "reason": "current_target_unresolved",
            })
            continue
        strict = evaluate_target_patch_identity(
            [frozen],
            [current],
            changed_patch=changed_patch,
            max_patch_bytes=max_patch_bytes,
        )
        if strict.get("ok") is not True:
            reanchor, reason = _clone_same_hunk_retained_endpoint_reanchor(
                frozen,
                current,
                hunks_by_file=parsed["hunks_by_file"],
            )
            if reanchor is None:
                failures.append({
                    "target_index": target_index,
                    "file": str(frozen.get("file") or ""),
                    "method": str(frozen.get("method") or ""),
                    "reason": reason,
                    "strict_failures": list(strict.get("failures") or []),
                })
                continue
            reanchors.append(reanchor)

        current_file = str(current.get("file") or "")
        current_line = _positive_integer(current.get("begin_line"))
        if not current_file or current_line is None:
            failures.append({
                "target_index": target_index,
                "reason": "current_target_anchor_invalid",
            })
            continue
        location = (current_file, current_line)
        previous = resolved_locations.get(location)
        if previous is not None and previous != target_index:
            failures.append({
                "target_index": target_index,
                "other_target_index": previous,
                "file": current_file,
                "current_begin_line": current_line,
                "reason": "clone_endpoint_reanchor_not_one_to_one",
            })
            continue
        resolved_locations[location] = int(target_index)

    result["failures"] = failures
    result["retained_endpoint_reanchors"] = reanchors
    result["ok"] = not failures
    return result


def evaluate_feature_envy_target_patch_identity(
    baseline_target: Mapping[str, Any],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Map one frozen Feature Envy declaration to exactly one current wrapper.

    Strict old-to-current line mapping remains the primary route.  A retained
    wrapper may re-anchor only when its declaration line is added in the same
    hunk that removed the frozen declaration line, with identical parser owner,
    declared name, and complete parameter fingerprints.  No nearest or
    cross-hunk candidate is considered.
    """
    frozen = dict(baseline_target)
    candidates = [dict(item) for item in current_targets]
    result = {
        "contract": FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT,
        "ok": False,
        "failures": [],
        "patch_available": changed_patch is not None,
        "error": "",
        "strict_target_mappings": [],
        "wrapper_reanchors": [],
    }
    parsed = _parse_target_patch(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if not parsed["ok"]:
        result["error"] = parsed["error"]
        return result

    eligible: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    for candidate in candidates:
        strict = evaluate_target_patch_identity(
            [frozen],
            [candidate],
            changed_patch=changed_patch,
            max_patch_bytes=max_patch_bytes,
        )
        if strict.get("ok") is True:
            eligible.append((candidate, "strict", {
                "contract": TARGET_PATCH_IDENTITY_CONTRACT,
                "target_index": candidate.get("target_index"),
                "file": str(candidate.get("file") or ""),
                "current_begin_line": candidate.get("begin_line"),
            }))
            continue
        reanchor, reason = _feature_envy_same_hunk_wrapper_reanchor(
            frozen,
            candidate,
            hunks_by_file=parsed["hunks_by_file"],
        )
        if reanchor is not None:
            eligible.append((candidate, "wrapper", reanchor))
            continue
        failures.append({
            "target_index": candidate.get("target_index"),
            "file": str(frozen.get("file") or ""),
            "current_begin_line": candidate.get("begin_line"),
            "reason": reason,
            "strict_failures": list(strict.get("failures") or []),
        })

    if len(eligible) != 1:
        failures.append({
            "reason": (
                "feature_envy_target_mapping_missing"
                if not eligible
                else "feature_envy_target_mapping_not_one_to_one"
            ),
            "eligible_candidate_count": len(eligible),
        })
        result["failures"] = failures
        return result
    _, mode, mapping = eligible[0]
    if mode == "strict":
        result["strict_target_mappings"] = [mapping]
    else:
        result["wrapper_reanchors"] = [mapping]
    result["ok"] = True
    return result


def _feature_envy_same_hunk_wrapper_reanchor(
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    hunks_by_file: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    file_name = str(frozen.get("file") or "")
    current_file = str(current.get("file") or "")
    old_line = _positive_integer(frozen.get("begin_line"))
    new_line = _positive_integer(current.get("begin_line"))
    if (
        not file_name
        or current_file != file_name
        or old_line is None
        or new_line is None
        or current.get("resolved") is not True
    ):
        return None, "feature_envy_wrapper_anchor_invalid"
    frozen_identity, frozen_error = _declaration_identity(frozen)
    current_identity, current_error = _declaration_identity(current)
    if frozen_error or frozen_identity is None:
        return None, "feature_envy_wrapper_baseline_identity_invalid"
    if current_error or current_identity is None:
        return None, "feature_envy_wrapper_current_identity_invalid"
    if current_identity != frozen_identity:
        return None, "feature_envy_wrapper_owner_or_name_changed"
    frozen_parameters = frozen.get("parameter_fingerprints")
    current_parameters = current.get("parameter_fingerprints")
    if (
        not isinstance(frozen_parameters, list)
        or not isinstance(current_parameters, list)
        or frozen_parameters != current_parameters
    ):
        return None, "feature_envy_wrapper_parameter_identity_changed"
    matching_hunks = [
        hunk
        for hunk in list(hunks_by_file.get(file_name) or [])
        if old_line in set(hunk.get("removed_lines") or set())
        and new_line in set(hunk.get("added_lines") or set())
    ]
    if len(matching_hunks) != 1:
        return None, "feature_envy_wrapper_not_same_unique_hunk"
    hunk = matching_hunks[0]
    return {
        "contract": FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT,
        "target_index": frozen.get("target_index"),
        "file": file_name,
        "baseline_begin_line": old_line,
        "current_begin_line": new_line,
        "declared_name": str(frozen_identity["declared_name"]),
        "owner_qualified_name": str(frozen_identity["owner_qualified_name"]),
        "parameter_fingerprints": list(frozen_parameters),
        "hunk_old_start": int(hunk.get("old_start") or 0),
        "hunk_new_start": int(hunk.get("new_start") or 0),
    }, ""


def _clone_same_hunk_retained_endpoint_reanchor(
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    hunks_by_file: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    file_name = str(frozen.get("file") or "")
    current_file = str(current.get("file") or "")
    old_line = _positive_integer(frozen.get("begin_line"))
    new_line = _positive_integer(current.get("begin_line"))
    if (
        not file_name
        or current_file != file_name
        or old_line is None
        or new_line is None
        or current.get("resolved") is not True
    ):
        return None, "clone_endpoint_reanchor_anchor_invalid"
    frozen_identity, frozen_error = _declaration_identity(frozen)
    current_identity, current_error = _declaration_identity(current)
    if frozen_error or frozen_identity is None:
        return None, "clone_endpoint_reanchor_baseline_identity_invalid"
    if current_error or current_identity is None:
        return None, "clone_endpoint_reanchor_current_identity_invalid"
    if current_identity != frozen_identity:
        return None, "clone_endpoint_reanchor_owner_or_name_changed"
    frozen_signature = str(frozen.get("signature_sha256") or "")
    current_signature = str(current.get("signature_sha256") or "")
    if (
        re.fullmatch(r"[0-9a-f]{64}", frozen_signature) is None
        or current_signature != frozen_signature
    ):
        return None, "clone_endpoint_reanchor_signature_changed"
    matching_hunks = [
        hunk
        for hunk in list(hunks_by_file.get(file_name) or [])
        if old_line in set(hunk.get("removed_lines") or set())
        and new_line in set(hunk.get("added_lines") or set())
    ]
    if len(matching_hunks) != 1:
        return None, "clone_endpoint_reanchor_not_same_unique_hunk"
    hunk = matching_hunks[0]
    return {
        "contract": CLONE_RETAINED_ENDPOINT_REANCHOR_CONTRACT,
        "target_index": frozen.get("target_index"),
        "file": file_name,
        "baseline_begin_line": old_line,
        "current_begin_line": new_line,
        "declared_name": str(frozen_identity["declared_name"]),
        "owner_qualified_name": str(frozen_identity["owner_qualified_name"]),
        "signature_sha256": frozen_signature,
        "hunk_old_start": int(hunk.get("old_start") or 0),
        "hunk_new_start": int(hunk.get("new_start") or 0),
    }, ""


def _cpp_constructor_same_hunk_reanchor(
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    language: str,
    hunks_by_file: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    """Authorize exactly one parser-proven C++ constructor replacement."""

    if language != "cpp":
        return None, "constructor_signature_reanchor_language_not_cpp"
    target_index = frozen.get("target_index")
    file_name = str(frozen.get("file") or "")
    current_file = str(current.get("file") or "")
    old_line = _positive_integer(frozen.get("begin_line"))
    new_line = _positive_integer(current.get("begin_line"))
    if (
        not isinstance(target_index, int)
        or not file_name
        or current_file != file_name
        or old_line is None
        or new_line is None
        or current.get("resolved") is not True
    ):
        return None, "constructor_signature_reanchor_anchor_invalid"

    frozen_identity, frozen_error = _declaration_identity(frozen)
    current_identity, current_error = _declaration_identity(current)
    if frozen_error or frozen_identity is None:
        return None, "constructor_signature_reanchor_baseline_identity_invalid"
    if current_error or current_identity is None:
        return None, "constructor_signature_reanchor_current_identity_invalid"
    if current_identity != frozen_identity:
        return None, "constructor_signature_reanchor_owner_or_name_changed"

    declared_name = str(frozen_identity["declared_name"])
    owner = str(frozen_identity["owner_qualified_name"])
    if not _is_cpp_constructor_identity(frozen_identity):
        return None, "constructor_signature_reanchor_not_constructor"

    matching_hunks = [
        hunk
        for hunk in list(hunks_by_file.get(file_name) or [])
        if old_line in set(hunk.get("removed_lines") or set())
        and new_line in set(hunk.get("added_lines") or set())
    ]
    if len(matching_hunks) != 1:
        return None, "constructor_signature_reanchor_not_same_unique_hunk"
    hunk = matching_hunks[0]
    return {
        "contract": DATA_CLUMP_CONSTRUCTOR_REANCHOR_CONTRACT,
        "target_index": target_index,
        "file": file_name,
        "baseline_begin_line": old_line,
        "current_begin_line": new_line,
        "declared_name": declared_name,
        "owner_qualified_name": owner,
        "hunk_old_start": int(hunk.get("old_start") or 0),
        "hunk_new_start": int(hunk.get("new_start") or 0),
    }, ""


def _is_cpp_constructor_identity(identity: Mapping[str, str]) -> bool:
    declared_name = str(identity.get("declared_name") or "")
    owner = str(identity.get("owner_qualified_name") or "")
    return bool(
        declared_name
        and owner
        and owner.rsplit("::", 1)[-1] == declared_name
    )


def ast_declaration_identity(
    declared_name: str,
    owner_qualified_name: str,
) -> dict[str, str]:
    """Build the shared parser-derived declaration identity payload."""
    return {
        "contract": AST_DECLARATION_IDENTITY_CONTRACT,
        "declared_name": str(declared_name),
        "owner_qualified_name": str(owner_qualified_name),
    }


def validate_ast_declaration_identity(
    value: Any,
) -> tuple[dict[str, str] | None, str]:
    """Validate and canonicalize one frozen parser-derived identity."""
    if not isinstance(value, Mapping):
        return None, "declaration_identity_not_object"
    contract = str(value.get("contract") or "")
    declared_name = value.get("declared_name")
    owner = value.get("owner_qualified_name")
    if contract != AST_DECLARATION_IDENTITY_CONTRACT:
        return None, "declaration_identity_contract_invalid"
    if not isinstance(declared_name, str) or not declared_name:
        return None, "declared_name_unavailable"
    if not isinstance(owner, str):
        return None, "owner_qualified_name_invalid"
    return ast_declaration_identity(declared_name, owner), ""


def current_target_hunk_units(
    changed_patch: str | None,
    *,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> tuple[list[dict[str, Any]], str]:
    """Return current-side text units from caller-scoped target hunks."""

    parsed = _parse_target_patch(changed_patch, max_patch_bytes=max_patch_bytes)
    if not parsed["ok"]:
        return [], str(parsed["error"] or "target_patch_parse_failed")
    return list(parsed["current_hunk_units"]), ""


def same_hunk_identifier_replacement(
    changed_patch: str | None,
    *,
    old_line: int,
    current_line: int,
    old_identifier: str,
    current_identifier: str,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Prove one old/new identifier declaration pair is in one unique hunk.

    The caller supplies already-frozen old/current declaration lines and the
    two identifier spellings.  This helper only projects the caller-scoped
    unified diff; it neither opens source files nor searches a project.  Both
    lines must be explicit ``-``/``+`` lines in the same unique hunk, and each
    line must contain its expected identifier as a complete token.
    """
    result: dict[str, Any] = {
        "contract": SAME_HUNK_IDENTIFIER_REPLACEMENT_CONTRACT,
        "ok": False,
        "error": "",
        "old_hunks": [],
        "new_hunks": [],
    }
    if (
        _positive_integer(old_line) is None
        or _positive_integer(current_line) is None
        or not isinstance(old_identifier, str)
        or not old_identifier
        or not isinstance(current_identifier, str)
        or not current_identifier
    ):
        result["error"] = "target_identifier_replacement_witness_invalid"
        return result
    parsed = _parse_target_patch(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if not parsed["ok"]:
        result["error"] = str(parsed["error"] or "target_patch_parse_failed")
        return result
    hunks_by_file = parsed["hunks_by_file"]
    if parsed.get("diff_file_count") != 1 or len(hunks_by_file) != 1:
        result["error"] = "target_identifier_replacement_patch_scope_invalid"
        return result

    old_pattern = _identifier_pattern(old_identifier)
    current_pattern = _identifier_pattern(current_identifier)
    old_hunks: list[dict[str, Any]] = []
    new_hunks: list[dict[str, Any]] = []
    for file_name, hunks in hunks_by_file.items():
        for hunk_index, hunk in enumerate(hunks, start=1):
            removed_text = hunk.get("removed_text")
            added_text = hunk.get("added_text")
            if (
                isinstance(removed_text, Mapping)
                and isinstance(removed_text.get(old_line), str)
                and old_pattern.search(str(removed_text[old_line]))
            ):
                old_hunks.append({
                    "file": str(file_name),
                    "hunk_index": hunk_index,
                })
            if (
                isinstance(added_text, Mapping)
                and isinstance(added_text.get(current_line), str)
                and current_pattern.search(str(added_text[current_line]))
            ):
                new_hunks.append({
                    "file": str(file_name),
                    "hunk_index": hunk_index,
                })
    result["old_hunks"] = old_hunks
    result["new_hunks"] = new_hunks
    if (
        len(old_hunks) != 1
        or len(new_hunks) != 1
        or old_hunks[0] != new_hunks[0]
    ):
        result["error"] = "target_identifier_replacement_not_same_unique_hunk"
        return result
    result.update({
        "ok": True,
        "file": old_hunks[0]["file"],
        "hunk_index": old_hunks[0]["hunk_index"],
    })
    return result


def current_target_added_blocks(
    changed_patch: str | None,
    *,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> tuple[list[dict[str, Any]], str]:
    """Return only contiguous current-side added blocks from target hunks.

    Context lines remain available through :func:`current_target_hunk_units`
    for old-to-current declaration mapping, but consumers that detect newly
    introduced declarations or copied bodies must not treat hunk context as
    new code.
    """
    units, error = current_target_hunk_units(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if error:
        return [], error
    return added_blocks_from_target_hunk_units(units), ""


def previous_target_removed_blocks(
    changed_patch: str | None,
    *,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> tuple[list[dict[str, Any]], str]:
    """Return contiguous old-side removed blocks from caller-scoped hunks.

    Consumers can inspect declarations that the submitted production patch
    actually removed without opening or discovering any project source.  Only
    ``-`` lines are projected; unchanged hunk context is deliberately omitted
    so an untouched related declaration cannot be mistaken for a deletion.
    """

    parsed = _parse_target_patch(
        changed_patch,
        max_patch_bytes=max_patch_bytes,
    )
    if not parsed["ok"]:
        return [], str(parsed["error"] or "target_patch_parse_failed")

    blocks: list[dict[str, Any]] = []
    for file_name, hunks in parsed["hunks_by_file"].items():
        for hunk in hunks:
            removed_text = hunk.get("removed_text")
            if not isinstance(removed_text, Mapping):
                continue
            block_start = 0
            previous_line = 0
            block_lines: list[str] = []

            def flush_block() -> None:
                nonlocal block_start, previous_line, block_lines
                if block_lines:
                    blocks.append({
                        "file": str(file_name),
                        "start_line": block_start,
                        "end_line": previous_line,
                        "body_text": "\n".join(block_lines),
                        "removed_lines": list(
                            range(block_start, previous_line + 1)
                        ),
                    })
                block_start = 0
                previous_line = 0
                block_lines = []

            for line_number, line_text in sorted(removed_text.items()):
                if (
                    isinstance(line_number, bool)
                    or not isinstance(line_number, int)
                    or not isinstance(line_text, str)
                ):
                    flush_block()
                    continue
                if block_lines and line_number != previous_line + 1:
                    flush_block()
                if not block_lines:
                    block_start = line_number
                block_lines.append(line_text)
                previous_line = line_number
            flush_block()
    return blocks, ""


def evaluate_target_anchor_deletions(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    *,
    changed_patch: str | None,
    max_patch_bytes: int = MAX_TARGET_PATCH_BYTES,
) -> dict[str, Any]:
    """Verify exact deletion of unresolved frozen declarations.

    This is deliberately narrower than source discovery: callers provide the
    frozen declarations, current results from those same explicit targets,
    and the already-bounded production patch.  Every frozen declaration line
    must be removed byte-for-byte without a replacement mapping.  A still-
    resolved declaration is never authorized as absent.
    """

    baseline = [dict(item) for item in baseline_targets]
    current = {
        int(item.get("target_index")): dict(item)
        for item in current_targets
        if isinstance(item.get("target_index"), int)
    }
    result = {
        "contract": TARGET_ANCHOR_DELETION_CONTRACT,
        "ok": False,
        "failures": [],
        "patch_available": changed_patch is not None,
        "error": "",
    }
    if not baseline:
        result["error"] = "baseline_target_anchors_unavailable"
        return result
    parsed = _parse_target_patch(changed_patch, max_patch_bytes=max_patch_bytes)
    if not parsed["ok"]:
        result["error"] = parsed["error"]
        return result

    failures: list[dict[str, Any]] = []
    hunks_by_file = parsed["hunks_by_file"]
    for frozen in baseline:
        target_index = frozen.get("target_index")
        file_name = str(frozen.get("file") or "")
        old_line = _positive_integer(frozen.get("begin_line"))
        if not isinstance(target_index, int) or not file_name or old_line is None:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "baseline_target_anchor_invalid",
            })
            continue
        candidate = current.get(target_index)
        if isinstance(candidate, Mapping) and candidate.get("resolved") is True:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "current_target_still_resolved",
            })
            continue
        witness, witness_error = _validate_declaration_deletion_witness(
            frozen.get("declaration_deletion_witness")
        )
        if witness is None:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "baseline_declaration_deletion_witness_invalid",
                "error": witness_error,
            })
            continue
        if not (
            int(witness["start_line"])
            <= old_line
            <= int(witness["end_line"])
        ):
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "baseline_begin_line": old_line,
                "witness_start_line": int(witness["start_line"]),
                "reason": "baseline_declaration_deletion_witness_mismatch",
            })
            continue
        matching_hunks = [
            hunk
            for hunk in list(hunks_by_file.get(file_name) or [])
            if _hunk_exactly_deletes_declaration(hunk, witness)
        ]
        if len(matching_hunks) != 1:
            failures.append({
                "target_index": target_index,
                "file": file_name,
                "baseline_begin_line": old_line,
                "declaration_end_line": int(witness["end_line"]),
                "matching_deletion_hunk_count": len(matching_hunks),
                "reason": "baseline_declaration_not_exactly_deleted",
            })
    result["failures"] = failures
    result["ok"] = not failures
    return result


def target_declaration_deletion_witness(
    source_bytes: bytes,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    """Freeze byte-exact line evidence for one already-selected declaration."""

    lines = source_bytes.splitlines()
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("declaration span is outside the selected target file")
    line_hashes = [
        hashlib.sha256(line).hexdigest()
        for line in lines[start_line - 1 : end_line]
    ]
    return {
        "contract": TARGET_DECLARATION_DELETION_WITNESS_CONTRACT,
        "start_line": start_line,
        "end_line": end_line,
        "line_hashes": line_hashes,
        "declaration_sha256": _line_hashes_digest(line_hashes),
    }


def validate_target_declaration_deletion_witness(
    value: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Validate one frozen complete-declaration line witness."""

    return _validate_declaration_deletion_witness(value)


def _validate_declaration_deletion_witness(
    value: Any,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, Mapping):
        return None, "declaration_deletion_witness_not_object"
    if str(value.get("contract") or "") != TARGET_DECLARATION_DELETION_WITNESS_CONTRACT:
        return None, "declaration_deletion_witness_contract_invalid"
    start_line = _positive_integer(value.get("start_line"))
    end_line = _positive_integer(value.get("end_line"))
    line_hashes = value.get("line_hashes")
    if start_line is None or end_line is None or end_line < start_line:
        return None, "declaration_deletion_witness_span_invalid"
    if (
        not isinstance(line_hashes, list)
        or len(line_hashes) != end_line - start_line + 1
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in line_hashes
        )
    ):
        return None, "declaration_deletion_witness_line_hashes_invalid"
    if str(value.get("declaration_sha256") or "") != _line_hashes_digest(line_hashes):
        return None, "declaration_deletion_witness_digest_invalid"
    return {
        "contract": TARGET_DECLARATION_DELETION_WITNESS_CONTRACT,
        "start_line": start_line,
        "end_line": end_line,
        "line_hashes": list(line_hashes),
        "declaration_sha256": str(value["declaration_sha256"]),
    }, ""


def _hunk_exactly_deletes_declaration(
    hunk: Mapping[str, Any],
    witness: Mapping[str, Any],
) -> bool:
    start_line = int(witness["start_line"])
    end_line = int(witness["end_line"])
    removed_lines = set(hunk.get("removed_lines") or set())
    removed_text = hunk.get("removed_text")
    if not isinstance(removed_text, Mapping):
        return False
    actual_hashes: list[str] = []
    for line_number in range(start_line, end_line + 1):
        if (
            line_number not in removed_lines
            or _mapped_current_lines([hunk], line_number)
        ):
            return False
        line = removed_text.get(line_number)
        if not isinstance(line, str):
            return False
        actual_hashes.append(
            hashlib.sha256(
                line.encode("utf-8", errors="surrogateescape")
            ).hexdigest()
        )
    return actual_hashes == list(witness["line_hashes"])


def _line_hashes_digest(line_hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(line_hashes).encode("ascii")).hexdigest()


def added_blocks_from_target_hunk_units(
    units: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the parsed target hunk units to contiguous added-line blocks."""
    blocks: list[dict[str, Any]] = []
    for unit in units:
        file_name = str(unit.get("file") or "")
        start_line = int(unit.get("start_line") or 0)
        added_lines = {
            int(value)
            for value in list(unit.get("added_lines") or [])
            if isinstance(value, int) and not isinstance(value, bool)
        }
        block_start = 0
        block_lines: list[str] = []

        def flush_block() -> None:
            nonlocal block_start, block_lines
            if block_lines:
                blocks.append({
                    "file": file_name,
                    "start_line": block_start,
                    "end_line": block_start + len(block_lines) - 1,
                    "body_text": "\n".join(block_lines),
                    "added_lines": list(
                        range(block_start, block_start + len(block_lines))
                    ),
                })
            block_start = 0
            block_lines = []

        for offset, line in enumerate(str(unit.get("body_text") or "").split("\n")):
            line_number = start_line + offset
            if line_number not in added_lines:
                flush_block()
                continue
            if not block_lines:
                block_start = line_number
            block_lines.append(line)
        flush_block()
    return blocks


def _parse_target_patch(
    changed_patch: str | None,
    *,
    max_patch_bytes: int,
) -> dict[str, Any]:
    if changed_patch is None:
        return {
            "ok": False,
            "error": "changed_target_hunks_unavailable",
            "hunks_by_file": {},
            "current_hunk_units": [],
        }
    encoded = changed_patch.encode("utf-8", errors="surrogateescape")
    if len(encoded) > max_patch_bytes:
        return {
            "ok": False,
            "error": "changed_target_hunks_exceed_byte_limit",
            "hunks_by_file": {},
            "current_hunk_units": [],
        }

    hunks_by_file: dict[str, list[dict[str, Any]]] = {}
    current_units: list[dict[str, Any]] = []
    current_file = ""
    old_file = ""
    hunk: dict[str, Any] | None = None
    old_line = 0
    new_line = 0
    edit_old: list[int] = []
    edit_new: list[int] = []
    current_text: list[str] = []
    current_text_start = 0
    saw_diff_header = False
    diff_file_count = 0

    def flush_edit() -> None:
        nonlocal edit_old, edit_new
        if hunk is not None:
            line_map = hunk["line_map"]
            for offset, removed_line in enumerate(edit_old):
                # Preserve position inside the contiguous replacement block.
                # Mapping every removed line to every addition lets a later,
                # unrelated same-name declaration impersonate the frozen
                # target after its real declaration was deleted or renamed.
                line_map[removed_line] = (
                    {edit_new[offset]}
                    if offset < len(edit_new)
                    else set()
                )
        edit_old = []
        edit_new = []

    def flush_hunk() -> None:
        nonlocal hunk, current_text, current_text_start
        if hunk is None:
            return
        flush_edit()
        hunks_by_file.setdefault(str(hunk["file"]), []).append(hunk)
        if current_text:
            current_units.append({
                "file": str(hunk["file"]),
                "start_line": current_text_start,
                "body_text": "\n".join(current_text),
                "added_lines": sorted(
                    int(value) for value in hunk.get("added_lines", set())
                ),
            })
        hunk = None
        current_text = []
        current_text_start = 0

    for line in changed_patch.splitlines():
        if line.startswith("diff --git "):
            flush_hunk()
            saw_diff_header = True
            diff_file_count += 1
            current_file = ""
            old_file = ""
            continue
        if line.startswith("@@ "):
            flush_hunk()
            match = _HUNK_HEADER.match(line)
            if match is None or not current_file:
                return {
                    "ok": False,
                    "error": "changed_target_hunk_parse_failed",
                    "hunks_by_file": {},
                    "current_hunk_units": [],
                }
            old_start = int(match.group("old_start"))
            new_start = int(match.group("new_start"))
            hunk = {
                "file": current_file,
                "old_start": old_start,
                "old_count": int(match.group("old_count") or 1),
                "new_start": new_start,
                "new_count": int(match.group("new_count") or 1),
                "line_map": {},
                "removed_lines": set(),
                "removed_text": {},
                "added_lines": set(),
                "added_text": {},
            }
            old_line = old_start
            new_line = new_start
            continue
        if hunk is None and line.startswith("--- "):
            path = line[4:].strip()
            old_file = "" if path == "/dev/null" else (
                path[2:] if path.startswith("a/") else path
            )
            continue
        if hunk is None and line.startswith("+++ "):
            path = line[4:].strip()
            current_file = old_file if path == "/dev/null" else (
                path[2:] if path.startswith("b/") else path
            )
            continue
        if hunk is None:
            if line == "GIT binary patch" or line.startswith("Binary files "):
                return {
                    "ok": False,
                    "error": "changed_target_hunk_is_binary",
                    "hunks_by_file": {},
                    "current_hunk_units": [],
                }
            continue
        if line.startswith(" "):
            flush_edit()
            hunk["line_map"][old_line] = {new_line}
            if not current_text:
                current_text_start = new_line
            current_text.append(line[1:])
            old_line += 1
            new_line += 1
        elif line.startswith("-"):
            edit_old.append(old_line)
            hunk["removed_lines"].add(old_line)
            hunk["removed_text"][old_line] = line[1:]
            old_line += 1
        elif line.startswith("+"):
            edit_new.append(new_line)
            hunk["added_lines"].add(new_line)
            hunk["added_text"][new_line] = line[1:]
            if not current_text:
                current_text_start = new_line
            current_text.append(line[1:])
            new_line += 1
        elif line.startswith("\\ No newline"):
            continue
        else:
            flush_hunk()
    flush_hunk()
    if changed_patch.strip() and not saw_diff_header:
        return {
            "ok": False,
            "error": "changed_target_patch_format_invalid",
            "hunks_by_file": {},
            "current_hunk_units": [],
        }
    for hunks in hunks_by_file.values():
        hunks.sort(key=lambda item: (int(item["old_start"]), int(item["new_start"])))
    return {
        "ok": True,
        "error": "",
        "hunks_by_file": hunks_by_file,
        "current_hunk_units": current_units,
        "diff_file_count": diff_file_count,
    }


def _mapped_current_lines(hunks: list[Mapping[str, Any]], old_line: int) -> set[int]:
    delta = 0
    for hunk in hunks:
        old_start = int(hunk.get("old_start") or 0)
        old_count = int(hunk.get("old_count") or 0)
        new_start = int(hunk.get("new_start") or 0)
        new_count = int(hunk.get("new_count") or 0)
        if old_line < old_start:
            return {old_line + delta}
        if old_count > 0 and old_line < old_start + old_count:
            mapped = hunk.get("line_map")
            if not isinstance(mapped, Mapping):
                return set()
            # A removed anchor may map only to additions in the same contiguous
            # replacement block.  Unrelated additions elsewhere in this hunk
            # are not evidence that the frozen declaration survived.
            return {int(value) for value in mapped.get(old_line, set())}
        delta = (new_start + new_count) - (old_start + old_count)
    return {old_line + delta}


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _identifier_pattern(identifier: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])"
    )


def _declaration_identity(
    target: Mapping[str, Any],
) -> tuple[dict[str, str] | None, str]:
    if "declaration_identity" not in target:
        return None, ""
    return validate_ast_declaration_identity(target.get("declaration_identity"))
