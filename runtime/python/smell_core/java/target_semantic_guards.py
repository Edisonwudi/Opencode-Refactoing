"""Target-scoped semantic Guard capture and verification.

This module deliberately is not a project detector.  It builds one semantic
model from caller-supplied production files, runs exactly one requested smell
evaluator, and returns a bounded target decision.  Build/test and checkpoint
status remain the responsibility of the outer Guard contract.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..analysis import method_basename
from ..location import LocationTarget, parse_location_descriptor
from ..target_context import validate_target_context
from . import semantic_detector as semantic
from .catalog_identity import stable_java_method_signature
from .detector_utils import normalize_path


SUPPORTED_TARGET_SEMANTIC_GUARDS = frozenset(
    {"feature_envy", "god_class", "refused_bequest", "dead_code"}
)
WITNESS_LIMIT = 6
VIOLATION_LIMIT = 8
TEXT_LIMIT = 180

_EVALUATOR_NAMES = {
    "feature_envy": "_detect_feature_envy",
    "god_class": "_detect_god_class",
    "refused_bequest": "_detect_refused_bequest",
    "dead_code": "_detect_dead_code",
}


def capture_target_semantic_guard(
    smell: str,
    project_root: str | Path,
    location: str,
    selector: Mapping[str, Any] | None,
    analysis_files: Iterable[str | Path],
    classpath: str = "",
) -> dict[str, Any]:
    """Capture exactly one target finding from an explicit analysis scope."""
    normalized_smell = _supported_smell(smell)
    root = Path(project_root).expanduser().resolve()
    normalized_selector = validate_target_context(selector)
    try:
        target = parse_location_descriptor(location, root)
        model = semantic.build_scoped_project_model(
            root,
            tuple(analysis_files),
            classpath,
        )
        findings = _run_evaluator(normalized_smell, model)
        matches = _capture_matches(
            normalized_smell,
            findings,
            model,
            target,
            normalized_selector,
        )
        declarations = _capture_declarations(
            normalized_smell,
            model,
            target,
            normalized_selector,
        )
    except Exception as exc:
        return _analysis_failed(exc)

    target_missing = len(declarations) == 0
    if len(matches) != 1:
        violation = "TARGET_FINDING_NOT_FOUND" if not matches else "TARGET_AMBIGUOUS"
        return _snapshot(
            ok=False,
            target_match_count=len(matches),
            target_smell_present=bool(matches),
            target_missing=target_missing,
            objectives=_objectives(
                normalized_smell,
                model,
                None,
                declarations,
            ),
            entity_identity={},
            witness=[
                _finding_witness(normalized_smell, item, model, role="candidate")
                for item in matches[:WITNESS_LIMIT]
            ],
            guard_violations=[violation],
        )

    match = matches[0]
    identity = _finding_identity(normalized_smell, match, model)
    peers = [
        item
        for item in sorted(findings, key=_finding_sort_key)
        if id(item) != id(match)
    ]
    target_witness = _finding_witness(
        normalized_smell,
        match,
        model,
        role="target",
    )
    target_witness["baseline_peer_count"] = len(peers)
    target_witness["baseline_peer_witness_truncated"] = (
        len(peers) > WITNESS_LIMIT - 1
    )
    return _snapshot(
        ok=True,
        target_match_count=1,
        target_smell_present=True,
        target_missing=False,
        objectives=_objectives(normalized_smell, model, match, declarations),
        entity_identity=identity,
        witness=[target_witness]
        + [
            _finding_witness(
                normalized_smell,
                item,
                model,
                role="baseline_peer",
            )
            for item in peers[: WITNESS_LIMIT - 1]
        ],
        guard_violations=[],
    )


def evaluate_target_semantic_guard(
    smell: str,
    project_root: str | Path,
    location: str,
    selector: Mapping[str, Any] | None,
    analysis_files: Iterable[str | Path],
    baseline: Mapping[str, Any],
    classpath: str = "",
    changed_line_ranges: Mapping[
        str | Path,
        Sequence[Sequence[int] | Mapping[str, int]],
    ]
    | None = None,
) -> dict[str, Any]:
    """Re-evaluate a frozen target inside its target-plus-change scope.

    ``changed_line_ranges`` maps current relative or absolute Java file paths
    to inclusive ``(start, end)`` pairs (or ``{"start": n, "end": m}``).
    Feature Envy findings whose method intersects one of those ranges are
    treated as changed-scope relocation candidates independently of lineage.
    """
    normalized_smell = _supported_smell(smell)
    root = Path(project_root).expanduser().resolve()
    validate_target_context(selector)
    baseline_identity = baseline.get("entity_identity")
    if (
        baseline.get("ok") is not True
        or not isinstance(baseline_identity, Mapping)
        or baseline_identity.get("smell") != normalized_smell
    ):
        return _snapshot(
            ok=False,
            guard_violations=["BASELINE_TARGET_INVALID"],
        )

    try:
        # Parse the current location as an admission check, but frozen identity
        # is authoritative after capture and is intentionally line-independent.
        parse_location_descriptor(location, root)
        normalized_changed_ranges = _normalize_changed_line_ranges(
            root,
            changed_line_ranges,
        )
        model = semantic.build_scoped_project_model(
            root,
            tuple(analysis_files),
            classpath,
        )
        findings = _run_evaluator(normalized_smell, model)
        declarations = _identity_declarations(
            normalized_smell,
            model,
            baseline_identity,
        )
        matches = [
            item
            for item in findings
            if _same_identity(
                normalized_smell,
                baseline_identity,
                _finding_identity(normalized_smell, item, model),
            )
        ]
    except Exception as exc:
        return _analysis_failed(exc, entity_identity=dict(baseline_identity))

    violations: list[str] = []
    if matches:
        violations.append("TARGET_SMELL_REMAINS")
    if len(matches) > 1:
        violations.append("TARGET_AMBIGUOUS")
    if normalized_smell == "god_class" and not declarations:
        violations.append("TARGET_ENTITY_MISSING")

    relocations = _relocations(
        normalized_smell,
        findings,
        matches,
        model,
        baseline_identity,
        baseline,
        normalized_changed_ranges,
    )
    violations.extend(
        f"{_relocation_code(normalized_smell)}:{_finding_label(item)}"
        for item in relocations
    )

    target_match = matches[0] if len(matches) == 1 else None
    witness = [
        _finding_witness(normalized_smell, item, model, role="target")
        for item in matches[:1]
    ]
    if not witness and declarations:
        witness.append(
            _declaration_witness(
                normalized_smell,
                declarations[0],
                model,
                role="target_declaration",
            )
        )
    witness.extend(
        _finding_witness(normalized_smell, item, model, role="relocation")
        for item in relocations[: max(0, WITNESS_LIMIT - len(witness))]
    )
    return _snapshot(
        ok=len(matches) <= 1,
        target_match_count=len(matches),
        target_smell_present=bool(matches),
        target_missing=not declarations,
        objectives=_objectives(
            normalized_smell,
            model,
            target_match,
            declarations,
        ),
        entity_identity=dict(baseline_identity),
        witness=witness,
        guard_violations=violations,
    )


def _supported_smell(smell: str) -> str:
    normalized = str(smell or "").strip().lower()
    if normalized not in SUPPORTED_TARGET_SEMANTIC_GUARDS:
        raise ValueError(f"unsupported target semantic guard: {smell}")
    return normalized


def _run_evaluator(smell: str, model: semantic.ProjectModel) -> list[semantic.SemanticFinding]:
    evaluator = getattr(semantic, _EVALUATOR_NAMES[smell])
    return list(evaluator(model))


def _capture_matches(
    smell: str,
    findings: Sequence[semantic.SemanticFinding],
    model: semantic.ProjectModel,
    target: LocationTarget,
    selector: Mapping[str, Any],
) -> list[semantic.SemanticFinding]:
    candidates = [
        item
        for item in findings
        if _same_file(item.file, target.project_path)
    ]
    wanted_class = _selector_class(target, selector)
    if wanted_class:
        candidates = [
            item
            for item in candidates
            if _same_class(item.class_name, wanted_class)
        ]
    if smell != "god_class":
        wanted_method = _selector_method(target, selector)
        if wanted_method:
            candidates = [
                item
                for item in candidates
                if _same_method(item.method, wanted_method)
            ]
    if smell == "refused_bequest" and selector.get("parent"):
        candidates = [
            item
            for item in candidates
            if _same_class(
                str(item.attributes.get("parent") or ""),
                str(selector["parent"]),
            )
        ]
    parameter_count = selector.get("target_parameter_count")
    if parameter_count is not None:
        candidates = [
            item
            for item in candidates
            if int(item.attributes.get("parameter_count") or -1)
            == int(parameter_count)
        ]
    if target.line:
        containing = [
            item
            for item in candidates
            if int(item.begin_line) <= int(target.line) <= int(item.end_line)
        ]
        candidates = containing
    return sorted(candidates, key=_finding_sort_key)


def _capture_declarations(
    smell: str,
    model: semantic.ProjectModel,
    target: LocationTarget,
    selector: Mapping[str, Any],
) -> list[Any]:
    if smell == "god_class":
        candidates = [
            item
            for item in model.classes.values()
            if _same_file(item.file, target.project_path)
        ]
        wanted_class = _selector_class(target, selector)
        if wanted_class:
            candidates = [
                item for item in candidates if _same_class(item.qualified_name, wanted_class)
            ]
        if target.line:
            candidates = [
                item
                for item in candidates
                if int(item.begin_line) <= int(target.line) <= int(item.end_line)
            ]
        return candidates

    candidates = [
        item
        for item in model.methods
        if _same_file(item.file, target.project_path)
    ]
    wanted_class = _selector_class(target, selector)
    if wanted_class:
        candidates = [
            item for item in candidates if _same_class(item.owner_qualified_name, wanted_class)
        ]
    wanted_method = _selector_method(target, selector)
    if wanted_method:
        candidates = [
            item for item in candidates if _same_method(item.method_signature, wanted_method)
        ]
    parameter_count = selector.get("target_parameter_count")
    if parameter_count is not None:
        candidates = [
            item
            for item in candidates
            if len(item.parameter_descriptors) == int(parameter_count)
        ]
    if target.line:
        candidates = [
            item
            for item in candidates
            if int(item.begin_line) <= int(target.line) <= int(item.end_line)
        ]
    return candidates


def _identity_declarations(
    smell: str,
    model: semantic.ProjectModel,
    identity: Mapping[str, Any],
) -> list[Any]:
    if smell == "god_class":
        return [
            item
            for item in model.classes.values()
            if _same_file(item.file, identity.get("file"))
            and _same_class(item.qualified_name, identity.get("class"))
        ]
    return [
        item
        for item in model.methods
        if _same_file(item.file, identity.get("file"))
        and _same_class(item.owner_qualified_name, identity.get("class"))
        and _same_method(item.method_signature, identity.get("method"))
    ]


def _finding_identity(
    smell: str,
    finding: semantic.SemanticFinding,
    model: semantic.ProjectModel,
) -> dict[str, Any]:
    if smell == "god_class":
        record = _class_record_for_finding(model, finding)
        return {
            "smell": smell,
            "file": _clean_path(finding.file),
            "class": str(
                record.qualified_name if record is not None else finding.class_name
            ),
        }
    record = _method_record_for_finding(model, finding)
    identity: dict[str, Any] = {
        "smell": smell,
        "file": _clean_path(finding.file),
        "class": str(
            record.owner_qualified_name if record is not None else finding.class_name
        ),
        "method": _stable_method(finding.method),
    }
    if smell == "refused_bequest":
        identity["parent"] = str(finding.attributes.get("parent") or "")
    if smell == "dead_code":
        identity["kind"] = str(finding.attributes.get("kind") or "")
    return identity


def _same_identity(
    smell: str,
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    if not _same_file(baseline.get("file"), current.get("file")):
        return False
    if not _same_class(baseline.get("class"), current.get("class")):
        return False
    if smell == "god_class":
        return True
    if not _same_method(baseline.get("method"), current.get("method")):
        return False
    if smell == "refused_bequest" and not _same_class(
        baseline.get("parent"), current.get("parent")
    ):
        return False
    return True


def _objectives(
    smell: str,
    model: semantic.ProjectModel,
    finding: semantic.SemanticFinding | None,
    declarations: Sequence[Any],
) -> dict[str, float]:
    if smell == "feature_envy":
        record = declarations[0] if len(declarations) == 1 else None
        if record is not None:
            profile = semantic._designite_feature_envy_profile(model, record)
            return {
                "envy_access_diff": float(profile.envy_access_diff),
                "envy_access": float(profile.envy_access_count),
                "self_access": float(profile.self_access_count),
            }
        return {"envy_access_diff": 0.0, "envy_access": 0.0, "self_access": 0.0}
    if smell == "god_class":
        record = declarations[0] if len(declarations) == 1 else None
        if record is None:
            return {name: 0.0 for name in ("nom", "nof", "wmc", "loc", "atfd")}
        methods = list(record.methods)
        return {
            "nom": float(len(methods) + len(record.bodyless_method_declarations)),
            "nof": float(len(record.fields)),
            "wmc": float(
                sum(semantic._god_class_method_complexity(item) for item in methods)
                + len(record.bodyless_method_declarations)
            ),
            "loc": float(max(0, record.end_line - record.begin_line + 1)),
            "atfd": float(
                semantic._god_class_atfd(
                    methods,
                    record.bodyless_method_declarations,
                )
            ),
        }
    if smell == "refused_bequest":
        present = 1.0 if finding is not None else 0.0
        return {
            "refusal_finding_present": present,
            "rejection_signals": present,
        }
    record = declarations[0] if len(declarations) == 1 else None
    refs = 0
    loc = 0
    if record is not None:
        refs = int(
            semantic._method_reference_counts(model).get(
                semantic._method_identity(record),
                0,
            )
        )
        loc = int(record.loc)
    return {
        "unused_private_finding_present": 1.0 if finding is not None else 0.0,
        "refs": float(refs),
        "loc": float(loc),
    }


def _relocations(
    smell: str,
    findings: Sequence[semantic.SemanticFinding],
    target_matches: Sequence[semantic.SemanticFinding],
    model: semantic.ProjectModel,
    baseline_identity: Mapping[str, Any],
    baseline: Mapping[str, Any],
    changed_line_ranges: Mapping[str, Sequence[tuple[int, int]]],
) -> list[semantic.SemanticFinding]:
    excluded = {id(item) for item in target_matches}
    baseline_lineage = _baseline_lineage(baseline)
    baseline_method = str(baseline_identity.get("method") or "")
    baseline_parent = str(baseline_identity.get("parent") or "")
    baseline_type = _baseline_witness_value(baseline, "envied_type")
    baseline_peers = _baseline_peer_witnesses(baseline)
    output: list[semantic.SemanticFinding] = []
    for finding in sorted(findings, key=_finding_sort_key):
        if id(finding) in excluded:
            continue
        identity = _finding_identity(smell, finding, model)
        feature_envy_method_changed = bool(
            smell == "feature_envy"
            and _finding_intersects_changed_ranges(
                finding,
                changed_line_ranges,
            )
        )
        is_baseline_peer = any(
            _same_witness_identity(smell, peer, identity)
            for peer in baseline_peers
        )
        if is_baseline_peer and not feature_envy_method_changed:
            continue
        record = _method_record_for_finding(model, finding)
        lineage = _method_lineage(record)
        relocated = False
        if smell == "feature_envy":
            relocated = bool(
                feature_envy_method_changed
                or (
                    baseline_method
                    and _same_method(baseline_method, identity.get("method"))
                )
                or (baseline_lineage and lineage == baseline_lineage)
                or (
                    baseline_type
                    and _same_method_name(baseline_method, identity.get("method"))
                    and _same_class(
                        baseline_type,
                        finding.attributes.get("envied_type"),
                    )
                )
            )
        elif smell == "god_class":
            relocated = True
        elif smell == "refused_bequest":
            same_parent = _same_class(baseline_parent, identity.get("parent"))
            relocated = bool(
                same_parent
                and (
                    _same_method(baseline_method, identity.get("method"))
                    or (baseline_lineage and lineage == baseline_lineage)
                )
            )
        elif smell == "dead_code":
            relocated = bool(
                _same_method(baseline_method, identity.get("method"))
                or (baseline_lineage and lineage == baseline_lineage)
            )
        if relocated:
            output.append(finding)
        if len(output) >= WITNESS_LIMIT:
            break
    return output


def _normalize_changed_line_ranges(
    root: Path,
    changed_line_ranges: Mapping[
        str | Path,
        Sequence[Sequence[int] | Mapping[str, int]],
    ]
    | None,
) -> dict[str, tuple[tuple[int, int], ...]]:
    if changed_line_ranges is None:
        return {}
    if not isinstance(changed_line_ranges, Mapping):
        raise ValueError("CHANGED_LINE_RANGES_INVALID")

    normalized: dict[str, tuple[tuple[int, int], ...]] = {}
    for raw_file, raw_ranges in changed_line_ranges.items():
        candidate = Path(raw_file).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            relative = candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"CHANGED_LINE_RANGE_OUTSIDE_PROJECT: {raw_file}"
            ) from exc
        if isinstance(raw_ranges, (str, bytes)) or not isinstance(
            raw_ranges,
            Sequence,
        ):
            raise ValueError(f"CHANGED_LINE_RANGES_INVALID: {raw_file}")

        ranges: list[tuple[int, int]] = []
        for raw_range in raw_ranges:
            if isinstance(raw_range, Mapping):
                if set(raw_range) != {"start", "end"}:
                    raise ValueError(
                        f"CHANGED_LINE_RANGE_INVALID: {raw_file}"
                    )
                start = raw_range["start"]
                end = raw_range["end"]
            elif (
                isinstance(raw_range, Sequence)
                and not isinstance(raw_range, (str, bytes))
                and len(raw_range) == 2
            ):
                start, end = raw_range
            else:
                raise ValueError(f"CHANGED_LINE_RANGE_INVALID: {raw_file}")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 1
                or end < start
            ):
                raise ValueError(f"CHANGED_LINE_RANGE_INVALID: {raw_file}")
            ranges.append((start, end))

        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        normalized[normalize_path(relative.as_posix())] = tuple(merged)
    return normalized


def _finding_intersects_changed_ranges(
    finding: semantic.SemanticFinding,
    changed_line_ranges: Mapping[str, Sequence[tuple[int, int]]],
) -> bool:
    ranges = changed_line_ranges.get(normalize_path(_clean_path(finding.file)), ())
    begin = int(finding.begin_line)
    end = int(finding.end_line)
    return any(begin <= range_end and end >= range_start for range_start, range_end in ranges)


def _finding_witness(
    smell: str,
    finding: semantic.SemanticFinding,
    model: semantic.ProjectModel,
    *,
    role: str,
) -> dict[str, Any]:
    record = _method_record_for_finding(model, finding)
    witness: dict[str, Any] = {
        "role": role,
        "file": _clean_path(finding.file),
        "class": _bounded(finding.class_name),
        "line": int(finding.begin_line),
    }
    if finding.method:
        witness["method"] = _bounded(_stable_method(finding.method))
    lineage = _method_lineage(record)
    if lineage:
        witness["lineage_sha256"] = lineage
    selected_attributes = {
        "feature_envy": ("envied_type", "envied_field", "envy_access_diff"),
        "god_class": ("nom", "wmc", "loc", "atfd"),
        "refused_bequest": ("parent", "rejection_kind"),
        "dead_code": ("kind", "refs", "loc"),
    }[smell]
    for name in selected_attributes:
        value = finding.attributes.get(name)
        if value not in (None, ""):
            witness[name] = _bounded(value) if isinstance(value, str) else value
    return witness


def _declaration_witness(
    smell: str,
    declaration: Any,
    model: semantic.ProjectModel,
    *,
    role: str,
) -> dict[str, Any]:
    del model
    if smell == "god_class":
        return {
            "role": role,
            "file": _clean_path(declaration.file),
            "class": _bounded(declaration.qualified_name),
            "line": int(declaration.begin_line),
        }
    witness = {
        "role": role,
        "file": _clean_path(declaration.file),
        "class": _bounded(declaration.owner_qualified_name),
        "method": _bounded(_stable_method(declaration.method_signature)),
        "line": int(declaration.begin_line),
    }
    lineage = _method_lineage(declaration)
    if lineage:
        witness["lineage_sha256"] = lineage
    return witness


def _method_record_for_finding(
    model: semantic.ProjectModel,
    finding: semantic.SemanticFinding,
) -> semantic.MethodRecord | None:
    candidates = [
        item
        for item in model.methods
        if _same_file(item.file, finding.file)
        and _same_class(item.class_name, finding.class_name)
        and int(item.begin_line) == int(finding.begin_line)
        and _same_method(item.method_signature, finding.method)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _class_record_for_finding(
    model: semantic.ProjectModel,
    finding: semantic.SemanticFinding,
) -> semantic.ClassRecord | None:
    candidates = [
        item
        for item in model.classes.values()
        if _same_file(item.file, finding.file)
        and _same_class(item.class_name, finding.class_name)
        and int(item.begin_line) == int(finding.begin_line)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _selector_class(target: LocationTarget, selector: Mapping[str, Any]) -> str:
    if selector.get("target_class"):
        return str(selector["target_class"])
    if target.class_name:
        return str(target.class_name)
    if str(selector.get("symbol_kind") or "").lower() in {
        "class",
        "interface",
        "enum",
        "record",
    }:
        return str(selector.get("symbol_name") or "")
    return ""


def _selector_method(target: LocationTarget, selector: Mapping[str, Any]) -> str:
    if target.method:
        return str(target.method)
    if selector.get("container_method"):
        return str(selector["container_method"])
    if str(selector.get("symbol_kind") or "").lower() in {
        "method",
        "constructor",
    }:
        return str(selector.get("symbol_name") or "")
    return ""


def _method_lineage(method: semantic.MethodRecord | None) -> str:
    if method is None:
        return ""
    normalized = re.sub(r"\s+", "", str(method.body_text or ""))
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _baseline_lineage(baseline: Mapping[str, Any]) -> str:
    return _baseline_witness_value(baseline, "lineage_sha256")


def _baseline_witness_value(baseline: Mapping[str, Any], key: str) -> str:
    witness = baseline.get("witness")
    if not isinstance(witness, Sequence) or isinstance(witness, (str, bytes)):
        return ""
    for item in witness:
        if isinstance(item, Mapping) and item.get(key):
            return str(item[key])
    return ""


def _baseline_peer_witnesses(
    baseline: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    witness = baseline.get("witness")
    if not isinstance(witness, Sequence) or isinstance(witness, (str, bytes)):
        return []
    return [
        item
        for item in witness[:WITNESS_LIMIT]
        if isinstance(item, Mapping) and item.get("role") == "baseline_peer"
    ]


def _same_witness_identity(
    smell: str,
    witness: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> bool:
    if not _same_file(witness.get("file"), identity.get("file")):
        return False
    if not _same_class(witness.get("class"), identity.get("class")):
        return False
    if smell == "god_class":
        return True
    if not _same_method(witness.get("method"), identity.get("method")):
        return False
    if smell == "refused_bequest":
        return _same_class(witness.get("parent"), identity.get("parent"))
    if smell == "dead_code" and witness.get("kind"):
        return str(witness.get("kind")) == str(identity.get("kind"))
    return True


def _relocation_code(smell: str) -> str:
    return {
        "feature_envy": "FEATURE_ENVY_RELOCATED",
        "god_class": "GOD_CLASS_RELOCATED",
        "refused_bequest": "REFUSED_BEQUEST_RELOCATED",
        "dead_code": "DEAD_CODE_RELOCATED",
    }[smell]


def _finding_label(finding: semantic.SemanticFinding) -> str:
    parts = [
        _clean_path(finding.file),
        str(finding.class_name or ""),
        _stable_method(finding.method) if finding.method else "",
    ]
    return _bounded("#".join(item for item in parts if item))


def _finding_sort_key(finding: semantic.SemanticFinding) -> tuple[Any, ...]:
    return (
        _clean_path(finding.file),
        int(finding.begin_line),
        str(finding.class_name or ""),
        str(finding.method or ""),
    )


def _stable_method(value: Any) -> str:
    return stable_java_method_signature(value)


def _same_method(left: Any, right: Any) -> bool:
    left_text = _stable_method(left)
    right_text = _stable_method(right)
    if not left_text or not right_text:
        return False
    if "(" in str(left or "") and "(" in str(right or ""):
        return left_text == right_text
    return _same_method_name(left_text, right_text)


def _same_method_name(left: Any, right: Any) -> bool:
    left_name = str(method_basename(str(left or "")) or "").lower()
    right_name = str(method_basename(str(right or "")) or "").lower()
    return bool(left_name and left_name == right_name)


def _same_class(left: Any, right: Any) -> bool:
    left_text = str(left or "").replace("$", ".").strip().lower()
    right_text = str(right or "").replace("$", ".").strip().lower()
    if not left_text or not right_text:
        return False
    return left_text == right_text or left_text.rsplit(".", 1)[-1] == right_text.rsplit(".", 1)[-1]


def _same_file(left: Any, right: Any) -> bool:
    return bool(left and right and normalize_path(str(left)) == normalize_path(str(right)))


def _clean_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def _bounded(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


def _snapshot(
    *,
    ok: bool,
    target_match_count: int = 0,
    target_smell_present: bool = False,
    target_missing: bool = True,
    objectives: Mapping[str, Any] | None = None,
    entity_identity: Mapping[str, Any] | None = None,
    witness: Sequence[Mapping[str, Any]] = (),
    guard_violations: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "target_match_count": max(0, int(target_match_count)),
        "target_smell_present": bool(target_smell_present),
        "target_missing": bool(target_missing),
        "objectives": {
            str(key): float(value)
            for key, value in dict(objectives or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
        "entity_identity": {
            str(key): value
            for key, value in dict(entity_identity or {}).items()
            if value not in (None, "", [], {})
        },
        "witness": [dict(item) for item in list(witness)[:WITNESS_LIMIT]],
        "guard_violations": [
            _bounded(item) for item in list(guard_violations)[:VIOLATION_LIMIT]
        ],
    }


def _analysis_failed(
    exc: Exception,
    *,
    entity_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _snapshot(
        ok=False,
        entity_identity=entity_identity,
        witness=[{"role": "analysis_error", "message": _bounded(exc)}],
        guard_violations=["ANALYSIS_FAILED"],
    )
