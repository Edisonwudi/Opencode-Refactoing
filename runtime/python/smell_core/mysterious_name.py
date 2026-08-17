"""Generic Mysterious Name detection for non-Java languages.

Ports the Java heuristics (``java/syntactic_detector.py``) onto the
tree-sitter facilities in ``analysis.py``.  Findings keep the strict
``kind=...; name=...; reason=...; len=N`` diagnostic format. Java product
selection uses typed finding fields and never parses this text as an input.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .analysis import (
    function_declaration_boundary_complete,
    function_signatures_in_file,
    iter_local_variable_names,
    method_basename,
    parse_function_nodes,
    source_syntax_issue_witnesses,
)
from .target_patch_identity import (
    ast_declaration_identity,
    evaluate_target_patch_identity,
    same_hunk_identifier_replacement,
)

DEFAULT_MYSTERIOUS_NAME_MIN_LEN = 2

DEFAULT_LOW_INFO_NAMES = frozenset({
    "tmp",
    "temp",
    "data",
    "foo",
    "bar",
    "aaa",
    "bbb",
    "ccc",
    "obj",
    "var",
    "misc",
    "util",
})
TOO_SHORT_ALLOWLIST = frozenset({"i", "j", "k", "x", "y", "z", "id", "ok"})


@dataclass(frozen=True)
class MysteriousNameFinding:
    file_path: Path
    line: int
    kind: str
    name: str
    reason: str
    evidence: str
    func_start_line: int = 0
    func_end_line: int = 0


MYSTERIOUS_NAME_SUCCESSOR_CONTRACT = (
    "frozen-container-symbol-slot-cohort-same-hunk-successor-v3"
)
MYSTERIOUS_NAME_SOURCE_PARSEABILITY_CONTRACT = (
    "selected-container-complete-boundary-with-frozen-file-recovery-v3"
)
MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT = (
    "complete-container-cohort-old-current-target-patch-bijection-v1"
)
MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT = (
    "complete-parser-declaration-boundaries-conditional-anchor-and-sha256-v2"
)


@dataclass(frozen=True)
class _LocalDeclaration:
    name: str
    line: int


@dataclass(frozen=True)
class _Container:
    declared_name: str
    owner_qualified_name: str
    owner_kind: str
    start_line: int
    end_line: int
    declaration_start_line: int
    parameter_names: tuple[str, ...]
    parameter_shapes: tuple[str, ...]
    parameter_lines: tuple[int, ...]
    locals: tuple[_LocalDeclaration, ...]
    identifier_counts: Mapping[str, int]
    declaration_sha256: str
    boundary_complete: bool
    preprocessor_guard_start_line: int


def suspicious_name_reason(
    name: str,
    *,
    min_len: int = DEFAULT_MYSTERIOUS_NAME_MIN_LEN,
    allow_too_short: bool = True,
    low_info_names: frozenset = DEFAULT_LOW_INFO_NAMES,
) -> Optional[str]:
    lowered = name.lower()
    if lowered in low_info_names:
        return "low_info_name"
    if allow_too_short and len(name) <= min_len and lowered not in TOO_SHORT_ALLOWLIST:
        return "too_short"
    if re.fullmatch(r"[a-zA-Z]\d+", name):
        return "letter_digit"
    if re.fullmatch(r"([a-zA-Z])\1{2,}", name):
        return "repeated_char"
    return None


def detect_mysterious_names(
    file_path: Path,
    *,
    language: str,
    min_len: int = DEFAULT_MYSTERIOUS_NAME_MIN_LEN,
) -> list[MysteriousNameFinding]:
    file_path = Path(file_path).expanduser().resolve()
    findings: list[MysteriousNameFinding] = []
    signatures = function_signatures_in_file(file_path, language)
    for signature in signatures:
        reason = suspicious_name_reason(signature.name, min_len=min_len, allow_too_short=True)
        if reason:
            findings.append(
                _finding(
                    file_path,
                    signature.start_line,
                    "method",
                    signature.name,
                    reason,
                    func_start_line=signature.start_line,
                    func_end_line=signature.end_line,
                )
            )
        for fingerprint in signature.parameter_fingerprints:
            param_name = fingerprint.rsplit(":", 1)[-1].strip()
            if not param_name:
                continue
            reason = suspicious_name_reason(param_name, min_len=min_len, allow_too_short=True)
            if reason:
                findings.append(
                    _finding(
                        file_path,
                        signature.start_line,
                        "param",
                        param_name,
                        reason,
                        func_start_line=signature.start_line,
                        func_end_line=signature.end_line,
                    )
                )
    for function_node, source_bytes in parse_function_nodes(file_path, language):
        body_node = function_node.child_by_field_name("body")
        if body_node is None:
            continue
        func_start_line = function_node.start_point[0] + 1
        func_end_line = function_node.end_point[0] + 1
        for name, line in iter_local_variable_names(body_node, source_bytes, language):
            reason = suspicious_name_reason(name, min_len=min_len, allow_too_short=True)
            if reason:
                findings.append(
                    _finding(
                        file_path,
                        line,
                        "local",
                        name,
                        reason,
                        func_start_line=func_start_line,
                        func_end_line=func_end_line,
                    )
                )
    return findings


def evaluate_mysterious_name_target(
    target: Any,
    *,
    language: str,
    selector: Mapping[str, Any],
    frozen_identity: Mapping[str, Any] | None = None,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    """Capture or evaluate one explicitly selected suspicious symbol.

    The evaluator parses only ``target.file_path``.  At c000 it freezes the
    parser-declared container owner/name, the selected symbol kind/name, and a
    declaration slot.  Verification admits a successor only when a parameter
    keeps the same declaration slot, or a local has one unique declaration
    replacement in the same caller-supplied target-file hunk.  The new name
    must leave the detector's suspicious-name domain.

    ``changed_patch=None`` is intentionally a detection-only mode used by the
    legacy post-checkpoint presentation Guard.  It can report that the frozen
    spelling disappeared, but the authoritative checkpoint path always passes
    the bounded target patch and owns successor acceptance.
    """
    source_path = Path(target.file_path).expanduser().resolve()
    kind = str(selector.get("symbol_kind") or "").strip().lower()
    name = str(selector.get("symbol_name") or "").strip()
    declaration_lines = _selector_declaration_lines(selector)
    if kind not in {"param", "local"} or not name or declaration_lines is None:
        return _snapshot_error(
            "MYSTERIOUS_NAME_SELECTOR_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    if not source_path.is_file():
        return _missing_container_snapshot(
            target,
            kind=kind,
            name=name,
            identity=dict(frozen_identity or {}),
            code="MN_TARGET_FILE_MISSING",
        )
    syntax_witnesses = source_syntax_issue_witnesses(source_path, language)
    if language == "python" and syntax_witnesses:
        return _with_source_parseability(
            _snapshot_error(
                "MN_TARGET_FILE_SYNTAX_INVALID",
                target=target,
                kind=kind,
                name=name,
            ),
            syntax_witnesses,
        )

    containers = _containers_in_file(source_path, language)
    frozen = dict(frozen_identity or {})
    if not frozen:
        snapshot = _capture_target_symbol(
            target,
            language=language,
            containers=containers,
            kind=kind,
            name=name,
            declaration_lines=declaration_lines,
        )
    else:
        snapshot = _evaluate_target_symbol(
            target,
            language=language,
            containers=containers,
            kind=kind,
            name=name,
            declaration_lines=declaration_lines,
            frozen=frozen,
            changed_patch=changed_patch,
        )
    return _with_source_parseability(snapshot, syntax_witnesses)


def _capture_target_symbol(
    target: Any,
    *,
    language: str,
    containers: list[_Container],
    kind: str,
    name: str,
    declaration_lines: tuple[int, ...],
) -> dict[str, Any]:
    candidates = _baseline_container_candidates(target, containers)
    if len(candidates) != 1:
        return _snapshot_error(
            "MN_CONTAINER_AMBIGUOUS" if candidates else "MN_CONTAINER_NOT_FOUND",
            target=target,
            kind=kind,
            name=name,
            candidate_count=len(candidates),
            target_missing=not candidates,
        )
    container = candidates[0]
    if not container.boundary_complete:
        return _snapshot_error(
            "MN_TARGET_CONTAINER_SYNTAX_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    cohort = _container_identity_cohort(container, containers)
    duplicate_identities = _duplicate_complete_container_identities(cohort)
    if duplicate_identities:
        return _snapshot_error(
            "MN_BASELINE_CONTAINER_IDENTITY_AMBIGUOUS",
            target=target,
            kind=kind,
            name=name,
            candidate_count=len(duplicate_identities),
        )
    target_cohort_indexes = [
        index for index, item in enumerate(cohort) if item is container
    ]
    if len(target_cohort_indexes) != 1:
        return _snapshot_error(
            "MN_BASELINE_CONTAINER_COHORT_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    declarations = _symbol_declarations(container, kind, name)
    if declaration_lines:
        declarations = [
            item for item in declarations if item[1] in declaration_lines
        ]
        if tuple(sorted(item[1] for item in declarations)) != declaration_lines:
            return _snapshot_error(
                "MN_DECLARATION_SELECTOR_NOT_FOUND",
                target=target,
                kind=kind,
                name=name,
                candidate_count=len(declarations),
            )
    reason = suspicious_name_reason(name)
    if not declarations or (len(declarations) != 1 and not declaration_lines):
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {
                "target_suspicious_name_present": 1 if declarations and reason else 0
            },
            "finding_present": bool(declarations and reason),
            "candidate_count": len(declarations),
            "finding_identity": _base_identity(target, kind=kind, name=name),
            "target_kind": kind,
            "target_name": name,
            "error": "MN_SYMBOL_AMBIGUOUS" if declarations else "MN_SYMBOL_NOT_FOUND",
        }
    if reason is None:
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 0},
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": _base_identity(target, kind=kind, name=name),
            "target_kind": kind,
            "target_name": name,
            "error": "MN_SELECTED_NAME_NOT_SUSPICIOUS",
        }

    symbol_slots = [slot for slot, _line in declarations]
    selected_declaration_lines = [line for _slot, line in declarations]
    if any(line < 1 for line in selected_declaration_lines):
        return _snapshot_error(
            "MN_DECLARATION_LINE_UNAVAILABLE",
            target=target,
            kind=kind,
            name=name,
        )
    occurrence_count = int(container.identifier_counts.get(name, 0))
    if occurrence_count < 1:
        return _snapshot_error(
            "MN_REFERENCE_WITNESS_UNAVAILABLE",
            target=target,
            kind=kind,
            name=name,
        )
    identity = {
        **_base_identity(target, kind=kind, name=name),
        "mysterious_name_contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
        "container": _container_identity(container),
        "container_continuity_contract": (
            MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT
        ),
        "container_cohort": [_container_identity(item) for item in cohort],
        "target_container_cohort_index": target_cohort_indexes[0],
        "symbol_slots": symbol_slots,
        "declaration_lines": selected_declaration_lines,
        "baseline_reference_count": occurrence_count,
    }
    return {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {"target_suspicious_name_present": 1},
        "finding_present": True,
        "candidate_count": 1,
        "finding_identity": identity,
        "target_kind": kind,
        "target_name": name,
        "target_reason": reason,
        "successor_contract": {
            "contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
            "status": "frozen",
            "symbol_slots": symbol_slots,
            "declaration_lines": selected_declaration_lines,
            "baseline_reference_count": occurrence_count,
            "container_cohort_size": len(cohort),
        },
    }


def _evaluate_target_symbol(
    target: Any,
    *,
    language: str,
    containers: list[_Container],
    kind: str,
    name: str,
    declaration_lines: tuple[int, ...],
    frozen: Mapping[str, Any],
    changed_patch: str | None,
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    if str(frozen.get("mysterious_name_contract") or "") != MYSTERIOUS_NAME_SUCCESSOR_CONTRACT:
        return _snapshot_error(
            "MN_FROZEN_CONTRACT_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    if (
        str(frozen.get("symbol_kind") or "") != kind
        or str(frozen.get("symbol_name") or "") != name
        or tuple(frozen.get("declaration_lines") or ()) != declaration_lines
        or str(frozen.get("file") or "").replace("\\", "/")
        != str(target.project_path).replace("\\", "/")
    ):
        return _snapshot_error(
            "MN_FROZEN_IDENTITY_MISMATCH",
            target=target,
            kind=kind,
            name=name,
        )
    frozen_container, _frozen_container_error = _validate_container_identity(
        frozen.get("container")
    )
    if frozen_container is None:
        return _snapshot_error(
            "MN_FROZEN_CONTAINER_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    if (
        str(frozen.get("container_continuity_contract") or "")
        != MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT
    ):
        return _snapshot_error(
            "MN_FROZEN_CONTAINER_CONTINUITY_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    frozen_cohort, target_cohort_index, cohort_error = (
        _validate_frozen_container_cohort(frozen, frozen_container)
    )
    if cohort_error:
        return _snapshot_error(
            cohort_error,
            target=target,
            kind=kind,
            name=name,
        )

    if changed_patch is None:
        exact, drift_code = _current_container_candidates(
            containers,
            frozen_container,
        )
        container = exact[0] if len(exact) == 1 else None
        continuity_code = (
            "MN_CONTAINER_AMBIGUOUS" if len(exact) > 1 else drift_code
        )
        continuity_candidate_count = len(exact)
        continuity_result = {
            "contract": MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT,
            "status": "not_evaluated_without_target_patch",
        }
    else:
        (
            container,
            continuity_code,
            continuity_candidate_count,
            continuity_result,
        ) = _resolve_current_container_from_cohort(
            target,
            containers=containers,
            frozen_container=frozen_container,
            frozen_cohort=frozen_cohort,
            target_cohort_index=target_cohort_index,
            changed_patch=changed_patch,
        )
    if container is None:
        snapshot = _missing_container_snapshot(
            target,
            kind=kind,
            name=name,
            identity=dict(frozen),
            code=continuity_code,
            candidate_count=continuity_candidate_count,
        )
        snapshot["successor_contract"]["container_continuity"] = (
            continuity_result
        )
        return snapshot
    if not container.boundary_complete:
        return _snapshot_error(
            "MN_TARGET_CONTAINER_SYNTAX_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    current_old = _symbol_declarations(container, kind, name)
    if current_old:
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 1},
            "finding_present": True,
            "candidate_count": len(current_old),
            "finding_identity": dict(frozen),
            "target_kind": kind,
            "target_name": name,
            "successor_contract": {
                "contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
                "status": "original_symbol_still_declared",
                "container_continuity": continuity_result,
            },
        }

    # Presentation-only detector calls happen after the authoritative
    # checkpoint result has already been evaluated.  They do not possess the
    # controller-owned patch, so they may report spelling absence but never
    # manufacture a successor verdict.
    if changed_patch is None:
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 0},
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": dict(frozen),
            "target_kind": kind,
            "target_name": name,
            "successor_contract": {
                "contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
                "status": "not_evaluated_without_target_patch",
            },
        }

    raw_symbol_slots = frozen.get("symbol_slots")
    raw_declaration_lines = frozen.get("declaration_lines")
    reference_count = frozen.get("baseline_reference_count")
    if (
        not isinstance(raw_symbol_slots, list)
        or not raw_symbol_slots
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in raw_symbol_slots
        )
        or len(set(raw_symbol_slots)) != len(raw_symbol_slots)
        or not isinstance(raw_declaration_lines, list)
        or len(raw_declaration_lines) != len(raw_symbol_slots)
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 1
            for item in raw_declaration_lines
        )
        or isinstance(reference_count, bool)
        or not isinstance(reference_count, int)
        or reference_count < 1
    ):
        return _snapshot_error(
            "MN_FROZEN_SYMBOL_WITNESS_INVALID",
            target=target,
            kind=kind,
            name=name,
        )
    symbol_slots = [int(item) for item in raw_symbol_slots]
    declaration_lines = [int(item) for item in raw_declaration_lines]

    baseline_parameters = _string_tuple(frozen_container.get("parameter_names"))
    baseline_locals = _string_tuple(frozen_container.get("local_names"))
    current_parameters = container.parameter_names
    current_locals = tuple(item.name for item in container.locals)
    current_lines = (
        container.parameter_lines if kind == "param"
        else tuple(item.line for item in container.locals)
    )
    baseline_symbols = baseline_parameters if kind == "param" else baseline_locals
    current_symbols = current_parameters if kind == "param" else current_locals
    if any(
        slot >= len(baseline_symbols) or slot >= len(current_symbols)
        for slot in symbol_slots
    ):
        violations.append(_violation("MN_SYMBOL_SLOT_MISSING", symbol_slots=symbol_slots))
        successor = ""
        successor_lines: list[int] = []
    else:
        successor_names = {current_symbols[slot] for slot in symbol_slots}
        successor = next(iter(successor_names)) if len(successor_names) == 1 else ""
        successor_lines = [
            current_lines[slot] if slot < len(current_lines) else 0
            for slot in symbol_slots
        ]
        if len(successor_names) != 1:
            violations.append(
                _violation(
                    "MN_SUCCESSOR_COHORT_NOT_UNIFORM",
                    successor_names=sorted(successor_names),
                )
            )

    if kind == "param":
        changed_slots = _changed_slots(baseline_parameters, current_parameters)
        if changed_slots != symbol_slots:
            violations.append(
                _violation(
                    "MN_PARAMETER_SLOT_MAPPING_NOT_UNIQUE",
                    expected_slots=symbol_slots,
                    changed_slots=changed_slots,
                )
            )
        if baseline_locals != current_locals:
            violations.append(_violation("MN_UNRELATED_LOCAL_DECLARATION_CHANGED"))
    else:
        changed_slots = _changed_slots(baseline_locals, current_locals)
        if changed_slots != symbol_slots:
            violations.append(
                _violation(
                    "MN_LOCAL_MAPPING_NOT_UNIQUE",
                    expected_slots=symbol_slots,
                    changed_slots=changed_slots,
                )
            )
        if baseline_parameters != current_parameters:
            violations.append(_violation("MN_UNRELATED_PARAMETER_CHANGED"))

    if not successor or successor == name:
        violations.append(_violation("MN_SUCCESSOR_NAME_MISSING"))
    else:
        successor_reason = suspicious_name_reason(successor)
        if successor_reason is not None:
            violations.append(
                _violation(
                    "MN_SUCCESSOR_NAME_STILL_SUSPICIOUS",
                    successor_name=successor,
                    suspicious_reason=successor_reason,
                )
            )
        baseline_other_declarations = [
            item
            for index, item in enumerate((*baseline_parameters, *baseline_locals))
            if index not in {
                slot if kind == "param" else len(baseline_parameters) + slot
                for slot in symbol_slots
            }
        ]
        if successor in baseline_other_declarations:
            violations.append(
                _violation(
                    "MN_SUCCESSOR_COLLIDES_WITH_EXISTING_DECLARATION",
                    successor_name=successor,
                )
            )

    old_references = int(container.identifier_counts.get(name, 0))
    successor_references = int(container.identifier_counts.get(successor, 0)) if successor else 0
    if old_references != 0:
        violations.append(
            _violation("MN_STALE_REFERENCE_REMAINS", stale_reference_count=old_references)
        )
    if successor and successor_references != reference_count:
        violations.append(
            _violation(
                "MN_REFERENCE_CLOSURE_MISMATCH",
                baseline_reference_count=reference_count,
                successor_reference_count=successor_references,
            )
        )

    patch_witnesses = [
        _same_hunk_declaration_successor(
            changed_patch,
            old_line=old_line,
            current_line=current_line,
            old_name=name,
            successor_name=successor,
        )
        for old_line, current_line in zip(declaration_lines, successor_lines)
    ]
    patch_result = (
        patch_witnesses[0]
        if len(patch_witnesses) == 1
        else {
            "ok": bool(patch_witnesses)
            and all(item.get("ok") is True for item in patch_witnesses),
            "contract": "all-selected-declarations-same-hunk-successor-v1",
            "witnesses": patch_witnesses,
        }
    )
    for witness in patch_witnesses:
        if witness.get("ok") is True:
            continue
        violations.append(
            _violation(
                str(witness.get("code") or "MN_TARGET_PATCH_INVALID"),
                **{
                    key: value
                    for key, value in witness.items()
                    if key not in {"ok", "code"}
                },
            )
        )

    return {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {"target_suspicious_name_present": 0},
        "finding_present": False,
        "candidate_count": 0,
        "finding_identity": dict(frozen),
        "target_kind": kind,
        "target_name": name,
        "successor_name": successor,
        "target_missing": False,
        "successor_contract": {
            "contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
            "status": "accepted" if not violations else "rejected",
            "symbol_slots": symbol_slots,
            "old_declaration_lines": declaration_lines,
            "current_declaration_lines": successor_lines,
            "successor_name": successor,
            "baseline_reference_count": reference_count,
            "successor_reference_count": successor_references,
            "same_hunk": patch_result,
            "container_continuity": continuity_result,
        },
        "guard_violations": violations,
    }


def _containers_in_file(file_path: Path, language: str) -> list[_Container]:
    signatures = function_signatures_in_file(file_path, language)
    parsed = parse_function_nodes(file_path, language)
    nodes_by_span: dict[tuple[int, int], list[tuple[Any, bytes]]] = {}
    for node, source_bytes in parsed:
        key = (node.start_point[0] + 1, node.end_point[0] + 1)
        nodes_by_span.setdefault(key, []).append((node, source_bytes))

    containers: list[_Container] = []
    for signature in signatures:
        node_candidates = nodes_by_span.get((signature.start_line, signature.end_line), [])
        if len(node_candidates) != 1:
            continue
        node, source_bytes = node_candidates[0]
        parameter_parts = [_parameter_parts(item) for item in signature.parameter_fingerprints]
        parameter_names = tuple(name for _shape, name in parameter_parts)
        parameter_shapes = tuple(shape for shape, _name in parameter_parts)
        parameter_lines = tuple(
            _name_line_in_signature(signature.signature_text, signature.start_line, name)
            for name in parameter_names
        )
        local_declarations = _local_declarations(node, source_bytes, language)
        containers.append(
            _Container(
                declared_name=signature.name,
                owner_qualified_name=signature.owner_qualified_name,
                owner_kind=signature.owner_kind,
                start_line=signature.start_line,
                end_line=signature.end_line,
                declaration_start_line=signature.declaration_start_line,
                parameter_names=parameter_names,
                parameter_shapes=parameter_shapes,
                parameter_lines=parameter_lines,
                locals=local_declarations,
                identifier_counts=_identifier_counts(node, source_bytes, language),
                declaration_sha256=hashlib.sha256(
                    signature.declaration_text.encode(
                        "utf-8", errors="surrogateescape"
                    )
                ).hexdigest(),
                boundary_complete=function_declaration_boundary_complete(
                    node,
                    source_bytes,
                    language,
                ),
                preprocessor_guard_start_line=(
                    _preprocessor_guard_start_line(node)
                ),
            )
        )
    return containers


def _preprocessor_guard_start_line(node: Any) -> int:
    parent = node.parent
    while parent is not None:
        if str(parent.type).startswith("preproc_"):
            return int(parent.start_point[0]) + 1
        parent = parent.parent
    return 0


def _baseline_container_candidates(target: Any, containers: list[_Container]) -> list[_Container]:
    declared_name = method_basename(str(target.method or "")) or ""
    candidates = [item for item in containers if not declared_name or item.declared_name == declared_name]
    target_line = int(target.line or 0)
    if target_line > 0:
        candidates = [
            item for item in candidates if item.start_line <= target_line <= item.end_line
        ]
    return candidates


def _container_identity_cohort(
    target: _Container,
    containers: list[_Container],
) -> list[_Container]:
    family = _container_family_key(target)
    return sorted(
        (item for item in containers if _container_family_key(item) == family),
        key=lambda item: (
            item.declaration_start_line,
            item.start_line,
            item.end_line,
            item.declaration_sha256,
        ),
    )


def _container_family_key(value: _Container | Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(value, _Container):
        return (
            value.declared_name,
            value.owner_qualified_name,
            value.owner_kind,
            value.parameter_shapes,
        )
    return (
        str(value.get("declared_name") or ""),
        str(value.get("owner_qualified_name") or ""),
        str(value.get("owner_kind") or ""),
        _string_tuple(value.get("parameter_shapes")),
    )


def _complete_container_identity_key(
    value: _Container | Mapping[str, Any],
) -> tuple[Any, ...]:
    digest = (
        value.declaration_sha256
        if isinstance(value, _Container)
        else str(value.get("declaration_sha256") or "")
    )
    guard_start = (
        value.preprocessor_guard_start_line
        if isinstance(value, _Container)
        else int(value.get("preprocessor_guard_start_line") or 0)
    )
    return (*_container_family_key(value), digest, guard_start)


def _duplicate_complete_container_identities(
    containers: Iterable[_Container | Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    counts: Counter[tuple[Any, ...]] = Counter(
        _complete_container_identity_key(item) for item in containers
    )
    return sorted(key for key, count in counts.items() if count > 1)


def _current_container_candidates(
    containers: list[_Container],
    frozen: Mapping[str, Any],
) -> tuple[list[_Container], str]:
    declared_name = str(frozen.get("declared_name") or "")
    owner = str(frozen.get("owner_qualified_name") or "")
    owner_kind = str(frozen.get("owner_kind") or "")
    shapes = _string_tuple(frozen.get("parameter_shapes"))
    exact = [
        item
        for item in containers
        if item.declared_name == declared_name
        and item.owner_qualified_name == owner
        and item.owner_kind == owner_kind
        and item.parameter_shapes == shapes
    ]
    if exact:
        return exact, ""
    same_declaration = [
        item
        for item in containers
        if item.declared_name == declared_name and item.parameter_shapes == shapes
    ]
    if same_declaration:
        return [], "MN_CONTAINER_OWNER_CHANGED"
    same_owner = [
        item
        for item in containers
        if item.owner_qualified_name == owner
        and item.owner_kind == owner_kind
        and item.parameter_shapes == shapes
    ]
    if same_owner:
        return [], "MN_CONTAINER_DECLARED_NAME_CHANGED"
    return [], "MN_CONTAINER_NOT_FOUND"


def _validate_container_identity(
    value: Any,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, Mapping):
        return None, "container_identity_not_object"
    if str(value.get("contract") or "") != MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT:
        return None, "container_identity_contract_invalid"

    declared_name = value.get("declared_name")
    owner = value.get("owner_qualified_name")
    owner_kind = value.get("owner_kind")
    if not isinstance(declared_name, str) or not declared_name:
        return None, "container_declared_name_invalid"
    if not isinstance(owner, str) or not isinstance(owner_kind, str):
        return None, "container_owner_identity_invalid"

    sequence_fields: dict[str, list[str]] = {}
    for field in ("parameter_names", "parameter_shapes", "local_names"):
        raw = value.get(field)
        if not isinstance(raw, list) or not all(
            isinstance(item, str) for item in raw
        ):
            return None, f"container_{field}_invalid"
        sequence_fields[field] = list(raw)
    if len(sequence_fields["parameter_names"]) != len(
        sequence_fields["parameter_shapes"]
    ):
        return None, "container_parameter_identity_invalid"

    line_fields: dict[str, int] = {}
    for field in (
        "capture_start_line",
        "capture_end_line",
        "capture_declaration_start_line",
        "capture_declaration_end_line",
    ):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            return None, f"container_{field}_invalid"
        line_fields[field] = raw
    guard_start = value.get("preprocessor_guard_start_line")
    if (
        isinstance(guard_start, bool)
        or not isinstance(guard_start, int)
        or guard_start < 0
    ):
        return None, "container_preprocessor_guard_start_line_invalid"
    if not (
        line_fields["capture_declaration_start_line"]
        <= line_fields["capture_start_line"]
        <= line_fields["capture_end_line"]
        <= line_fields["capture_declaration_end_line"]
    ):
        return None, "container_declaration_boundaries_invalid"

    digest = value.get("declaration_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None, "container_declaration_sha256_invalid"
    return {
        "contract": MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT,
        "declared_name": declared_name,
        "owner_qualified_name": owner,
        "owner_kind": owner_kind,
        **sequence_fields,
        **line_fields,
        "preprocessor_guard_start_line": guard_start,
        "declaration_sha256": digest,
    }, ""


def _validate_frozen_container_cohort(
    frozen: Mapping[str, Any],
    frozen_container: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, str]:
    raw_cohort = frozen.get("container_cohort")
    target_index = frozen.get("target_container_cohort_index")
    if (
        not isinstance(raw_cohort, list)
        or not raw_cohort
        or isinstance(target_index, bool)
        or not isinstance(target_index, int)
        or target_index < 0
        or target_index >= len(raw_cohort)
    ):
        return [], -1, "MN_FROZEN_CONTAINER_COHORT_INVALID"

    cohort: list[dict[str, Any]] = []
    for raw in raw_cohort:
        item, error = _validate_container_identity(raw)
        if item is None:
            return [], -1, "MN_FROZEN_CONTAINER_COHORT_INVALID"
        cohort.append(item)
    family = _container_family_key(frozen_container)
    if any(_container_family_key(item) != family for item in cohort):
        return [], -1, "MN_FROZEN_CONTAINER_COHORT_INVALID"
    if cohort[target_index] != dict(frozen_container):
        return [], -1, "MN_FROZEN_CONTAINER_COHORT_INVALID"
    if _duplicate_complete_container_identities(cohort):
        return [], -1, "MN_BASELINE_CONTAINER_IDENTITY_AMBIGUOUS"
    return cohort, target_index, ""


def _resolve_current_container_from_cohort(
    target: Any,
    *,
    containers: list[_Container],
    frozen_container: Mapping[str, Any],
    frozen_cohort: list[Mapping[str, Any]],
    target_cohort_index: int,
    changed_patch: str,
) -> tuple[_Container | None, str, int, dict[str, Any]]:
    candidates, drift_code = _current_container_candidates(
        containers,
        frozen_container,
    )
    result: dict[str, Any] = {
        "contract": MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT,
        "status": "rejected",
        "baseline_cohort_size": len(frozen_cohort),
        "current_candidate_count": len(candidates),
        "mapping": [],
    }
    if not candidates:
        result["error"] = drift_code
        return None, drift_code, 0, result
    if len(candidates) != len(frozen_cohort):
        code = "MN_CONTAINER_COHORT_CARDINALITY_CHANGED"
        result["error"] = code
        return None, code, len(candidates), result
    if _duplicate_complete_container_identities(candidates):
        code = "MN_CURRENT_CONTAINER_IDENTITY_AMBIGUOUS"
        result["error"] = code
        return None, code, len(candidates), result

    file_name = str(target.project_path).replace("\\", "/")
    possible: list[list[int]] = []
    edge_failures: list[dict[str, Any]] = []
    for old_index, frozen_item in enumerate(frozen_cohort):
        current_indexes: list[int] = []
        for current_index, candidate in enumerate(candidates):
            if (
                old_index != target_cohort_index
                and candidate.declaration_sha256
                != str(frozen_item.get("declaration_sha256") or "")
            ):
                edge_failures.append({
                    "baseline_index": old_index,
                    "current_index": current_index,
                    "error": "non_target_complete_declaration_changed",
                    "failures": [],
                })
                continue
            edge = _container_patch_identity_edge(
                file_name=file_name,
                frozen=frozen_item,
                current=candidate,
                changed_patch=changed_patch,
            )
            if edge.get("ok") is True:
                current_indexes.append(current_index)
            else:
                edge_failures.append({
                    "baseline_index": old_index,
                    "current_index": current_index,
                    "error": str(edge.get("error") or ""),
                    "failures": list(edge.get("failures") or []),
                })
        possible.append(current_indexes)

    if any(len(indexes) != 1 for indexes in possible):
        code = "MN_CONTAINER_PATCH_IDENTITY_NOT_UNIQUE"
        result.update({
            "error": code,
            "possible_current_indexes": possible,
            "edge_failures": edge_failures,
        })
        return None, code, len(candidates), result
    selected_indexes = [indexes[0] for indexes in possible]
    if len(set(selected_indexes)) != len(selected_indexes):
        code = "MN_CONTAINER_PATCH_IDENTITY_NOT_BIJECTIVE"
        result.update({
            "error": code,
            "possible_current_indexes": possible,
        })
        return None, code, len(candidates), result

    result.update({
        "status": "accepted",
        "mapping": [
            {
                "baseline_index": old_index,
                "current_index": current_index,
                "old_declaration_start_line": int(
                    frozen_cohort[old_index]["capture_declaration_start_line"]
                ),
                "current_declaration_start_line": (
                    candidates[current_index].declaration_start_line
                ),
            }
            for old_index, current_index in enumerate(selected_indexes)
        ],
    })
    return (
        candidates[selected_indexes[target_cohort_index]],
        "",
        len(candidates),
        result,
    )


def _container_patch_identity_edge(
    *,
    file_name: str,
    frozen: Mapping[str, Any],
    current: _Container,
    changed_patch: str,
) -> dict[str, Any]:
    old_identity = ast_declaration_identity(
        str(frozen.get("declared_name") or ""),
        str(frozen.get("owner_qualified_name") or ""),
    )
    current_identity = ast_declaration_identity(
        current.declared_name,
        current.owner_qualified_name,
    )
    baseline_targets = [{
        "target_index": 0,
        "file": file_name,
        "begin_line": int(frozen["capture_declaration_start_line"]),
        "declaration_identity": old_identity,
    }]
    current_targets = [{
        "target_index": 0,
        "file": file_name,
        "begin_line": current.declaration_start_line,
        "resolved": True,
        "declaration_identity": current_identity,
    }]
    return evaluate_target_patch_identity(
        baseline_targets,
        current_targets,
        changed_patch=changed_patch,
    )


def _container_identity(container: _Container) -> dict[str, Any]:
    return {
        "contract": MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT,
        "declared_name": container.declared_name,
        "owner_qualified_name": container.owner_qualified_name,
        "owner_kind": container.owner_kind,
        "parameter_names": list(container.parameter_names),
        "parameter_shapes": list(container.parameter_shapes),
        "local_names": [item.name for item in container.locals],
        "capture_start_line": container.start_line,
        "capture_end_line": container.end_line,
        "capture_declaration_start_line": container.declaration_start_line,
        "capture_declaration_end_line": container.end_line,
        "preprocessor_guard_start_line": (
            container.preprocessor_guard_start_line
        ),
        "declaration_sha256": container.declaration_sha256,
    }


def _symbol_declarations(
    container: _Container,
    kind: str,
    name: str,
) -> list[tuple[int, int]]:
    if kind == "param":
        return [
            (index, container.parameter_lines[index])
            for index, item in enumerate(container.parameter_names)
            if item == name
        ]
    return [
        (index, item.line)
        for index, item in enumerate(container.locals)
        if item.name == name
    ]


def _local_declarations(
    function_node: Any,
    source_bytes: bytes,
    language: str,
) -> tuple[_LocalDeclaration, ...]:
    body = function_node.child_by_field_name("body")
    if body is None:
        return ()
    declarations: list[_LocalDeclaration] = []
    if language == "python":
        first_binding: dict[str, _LocalDeclaration] = {}
        for node in _walk_container_scope(body, language):
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            if left is None or left.type != "identifier":
                continue
            name = _node_text(source_bytes, left).strip()
            if name and name not in first_binding:
                first_binding[name] = _LocalDeclaration(
                    name,
                    node.start_point[0] + 1,
                )
        return tuple(first_binding.values())
    if language in {"c", "cpp"}:
        for node in _walk_container_scope(body, language):
            if node.type != "declaration":
                continue
            type_node = node.child_by_field_name("type")
            type_id = type_node.id if type_node is not None else None
            for child in node.named_children:
                if type_id is not None and child.id == type_id:
                    continue
                name_node = _declarator_name_node(child)
                if name_node is None:
                    continue
                name = _node_text(source_bytes, name_node).strip()
                if name:
                    declarations.append(
                        _LocalDeclaration(name, name_node.start_point[0] + 1)
                    )
        return tuple(declarations)
    return ()


def _identifier_counts(
    function_node: Any,
    source_bytes: bytes,
    language: str,
) -> Mapping[str, int]:
    counts: Counter[str] = Counter()
    for node in _walk_container_scope(function_node, language, include_root=True):
        if node.type != "identifier" or _identifier_is_non_symbol_field(node):
            continue
        name = _node_text(source_bytes, node).strip()
        if name:
            counts[name] += 1
    return dict(counts)


def _walk_container_scope(
    node: Any,
    language: str,
    *,
    include_root: bool = False,
) -> Iterable[Any]:
    stop_types = {
        "python": {"function_definition", "class_definition", "lambda"},
        "cpp": {"lambda_expression"},
        "c": set(),
    }.get(language, set())

    def walk(current: Any, root: bool) -> Iterable[Any]:
        if root or current.type not in stop_types:
            if include_root or not root:
                yield current
            for child in current.named_children:
                if child.type in stop_types:
                    continue
                yield from walk(child, False)

    yield from walk(node, True)


def _identifier_is_non_symbol_field(node: Any) -> bool:
    parent = node.parent
    if parent is None:
        return False
    for field in ("attribute", "field", "name"):
        selected = parent.child_by_field_name(field)
        if selected is not None and selected.id == node.id and parent.type in {
            "attribute",
            "field_expression",
            "keyword_argument",
            "function_definition",
        }:
            return True
    return parent.type in {"label_statement", "goto_statement"}


def _declarator_name_node(node: Any) -> Any | None:
    if node.type in {"identifier", "field_identifier", "destructor_name", "operator_name"}:
        return node
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        nested = _declarator_name_node(declarator)
        if nested is not None:
            return nested
    for child in reversed(node.named_children):
        nested = _declarator_name_node(child)
        if nested is not None:
            return nested
    return None


def _parameter_parts(fingerprint: str) -> tuple[str, str]:
    shape, separator, name = str(fingerprint).rpartition(":")
    if not separator:
        return "", str(fingerprint).strip()
    return shape.strip(), name.strip()


def _name_line_in_signature(signature_text: str, start_line: int, name: str) -> int:
    pattern = _name_pattern(name)
    matches = [
        start_line + offset
        for offset, line in enumerate(signature_text.splitlines())
        if pattern.search(line)
    ]
    return matches[0] if len(matches) == 1 else 0


def _same_hunk_declaration_successor(
    patch: str,
    *,
    old_line: int,
    current_line: int,
    old_name: str,
    successor_name: str,
) -> dict[str, Any]:
    if not patch.strip():
        return {"ok": False, "code": "MN_TARGET_PATCH_UNAVAILABLE"}
    witness = same_hunk_identifier_replacement(
        patch,
        old_line=old_line,
        current_line=current_line,
        old_identifier=old_name,
        current_identifier=successor_name,
    )
    if witness.get("ok") is True:
        return {
            "ok": True,
            "contract": witness.get("contract"),
            "file": witness.get("file"),
            "hunk_index": witness.get("hunk_index"),
        }

    patch_error = str(witness.get("error") or "")
    if patch_error == "target_identifier_replacement_patch_scope_invalid":
        code = "MN_TARGET_PATCH_SCOPE_INVALID"
    elif patch_error in {
        "changed_target_hunks_unavailable",
        "changed_target_hunks_exceed_byte_limit",
        "changed_target_hunk_parse_failed",
        "changed_target_hunk_is_binary",
        "changed_target_patch_format_invalid",
        "target_patch_parse_failed",
    }:
        code = "MN_TARGET_PATCH_HUNK_UNAVAILABLE"
    else:
        code = "MN_DECLARATION_SUCCESSOR_NOT_UNIQUE_IN_SAME_HUNK"
    return {
        "ok": False,
        "code": code,
        "shared_contract": witness.get("contract"),
        "patch_error": patch_error,
        "old_hunks": witness.get("old_hunks", []),
        "new_hunks": witness.get("new_hunks", []),
    }


def _changed_slots(before: tuple[str, ...], after: tuple[str, ...]) -> list[int]:
    if len(before) != len(after):
        return list(range(max(len(before), len(after))))
    return [index for index, (old, new) in enumerate(zip(before, after)) if old != new]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _selector_declaration_lines(
    selector: Mapping[str, Any],
) -> tuple[int, ...] | None:
    raw = selector.get("declaration_lines", [])
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        return None
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1
        for item in raw
    ):
        return None
    lines = tuple(sorted(int(item) for item in raw))
    return lines if len(set(lines)) == len(lines) else None


def _name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _node_text(source_bytes: bytes, node: Any) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="surrogateescape"
    )


def _base_identity(target: Any, *, kind: str, name: str) -> dict[str, Any]:
    return {
        "smell": "mysterious_name",
        "file": str(target.project_path).replace("\\", "/"),
        "method": str(target.method or ""),
        "class": str(target.class_name or ""),
        "symbol_kind": kind,
        "symbol_name": name,
    }


def _violation(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _snapshot_error(
    code: str,
    *,
    target: Any,
    kind: str,
    name: str,
    candidate_count: int = 0,
    target_missing: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "detector": "tree_sitter_generic",
        "objectives": {},
        "finding_present": False,
        "candidate_count": candidate_count,
        "finding_identity": _base_identity(target, kind=kind, name=name),
        "target_kind": kind,
        "target_name": name,
        "target_missing": target_missing,
        "error": code,
    }


def _with_source_parseability(
    snapshot: dict[str, Any],
    syntax_witnesses: list[dict[str, object]],
) -> dict[str, Any]:
    """Attach bounded parser-recovery evidence to one target snapshot.

    C/C++ translation units often contain tree-sitter recovery nodes around
    unrelated macros.  The selected function still has to be complete, while
    the checkpoint contract freezes this file-level witness multiset and
    rejects any newly introduced recovery during verification.
    """
    result = dict(snapshot)
    result["target_file_parseable"] = not syntax_witnesses
    result["parser_recovery_required"] = bool(syntax_witnesses)
    result["target_syntax_issue_witnesses"] = list(syntax_witnesses)
    error = str(result.get("error") or "")
    if error == "MN_TARGET_CONTAINER_SYNTAX_INVALID":
        result["target_container_boundary_complete"] = False
    elif result.get("ok") is True and result.get("target_missing") is not True:
        result["target_container_boundary_complete"] = True
    return result


def _missing_container_snapshot(
    target: Any,
    *,
    kind: str,
    name: str,
    identity: Mapping[str, Any],
    code: str,
    candidate_count: int = 0,
) -> dict[str, Any]:
    return {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {"target_suspicious_name_present": 0},
        "finding_present": False,
        "candidate_count": candidate_count,
        "finding_identity": dict(identity) or _base_identity(target, kind=kind, name=name),
        "target_kind": kind,
        "target_name": name,
        "target_missing": True,
        "guard_violations": [_violation(code)],
        "successor_contract": {
            "contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
            "status": "container_unresolved",
        },
    }


def _finding(
    file_path: Path,
    line: int,
    kind: str,
    name: str,
    reason: str,
    *,
    func_start_line: int = 0,
    func_end_line: int = 0,
) -> MysteriousNameFinding:
    return MysteriousNameFinding(
        file_path=file_path,
        line=line,
        kind=kind,
        name=name,
        reason=reason,
        evidence=f"kind={kind}; name={name}; reason={reason}; len={len(name)}",
        func_start_line=func_start_line,
        func_end_line=func_end_line,
    )
