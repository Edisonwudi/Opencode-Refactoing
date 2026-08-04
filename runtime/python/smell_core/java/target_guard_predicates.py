"""Target-file predicates for the lightweight Java smell guard.

These helpers deliberately do not perform finding discovery.  They resolve one
source entity inside the file named by ``location`` and evaluate exactly one
product predicate.  Cross-file impact-cone and anti-relocation checks belong to
the caller; this module is the local predicate layer used at both capture and
verification time.

All functions return the same compact mapping.  ``target_match_count`` counts
source entities (or, for Mysterious Name, matching symbol findings), not all
findings in the project.  A missing target is a valid verification result, but
it is rejected by the capture helpers because c000 must freeze a real finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..location import LocationTarget, parse_location_descriptor
from .ast_ncss import run_ast_ncss
from . import syntactic_detector as syntax


LONG_METHOD_THRESHOLD = 60
NESTED_COMPLEXITY_THRESHOLD = int(
    syntax.DEFAULT_THRESHOLDS["cognitive_complexity"]
)

_PREDICATE_IDS = {
    "long_method": "java-target/long-method/ast-ncss-v1",
    "nested_complexity": "java-target/nested-complexity/cognitive-v1",
    "switch_statements": "java-target/switch-statements/presence-v1",
    "mysterious_name": "java-target/mysterious-name/strict-v1",
}


@dataclass(frozen=True)
class _TargetFile:
    root: Path
    file_path: Path
    relative_file: str
    method: str
    class_name: str
    line: int | None


TargetGuardResult = dict[str, Any]
TargetPredicate = Callable[
    [Path | str, Any, Mapping[str, Any] | None],
    TargetGuardResult,
]


def capture_target_guard_predicate(
    smell: str,
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    """Capture one uniquely matched, currently present target finding."""
    helper = _CAPTURE_HELPERS.get(str(smell))
    if helper is None:
        return _unsupported_result(str(smell))
    return helper(project_root, location, selector)


def evaluate_target_guard_predicate(
    smell: str,
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    """Evaluate one frozen target without enumerating project findings."""
    helper = _EVALUATE_HELPERS.get(str(smell))
    if helper is None:
        return _unsupported_result(str(smell))
    return helper(project_root, location, selector)


def capture_long_method(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    return _capture_result(evaluate_long_method(project_root, location, selector))


def evaluate_long_method(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    predicate_id = _PREDICATE_IDS["long_method"]
    prepared = _prepare_method_target(
        project_root,
        location,
        selector,
        predicate_id=predicate_id,
    )
    if isinstance(prepared, dict):
        return prepared
    target, methods, matches = prepared
    base = _method_selection_result(target, matches, predicate_id)
    if len(matches) != 1:
        return base

    metric = run_ast_ncss(target.file_path, target.root, -1)
    if not metric.ok:
        return _error_result(
            predicate_id,
            target,
            str(metric.error or "TARGET_METRIC_UNAVAILABLE"),
            target_match_count=1,
        )
    method = matches[0]
    score = _ast_ncss_for_method(method, methods, metric.findings)
    if score is None:
        return _error_result(
            predicate_id,
            target,
            "TARGET_METRIC_UNAVAILABLE",
            target_match_count=1,
        )
    return _matched_method_result(
        target,
        method,
        predicate_id=predicate_id,
        objectives={"ast_ncss": float(score)},
        present=score >= LONG_METHOD_THRESHOLD,
        metric_witness={
            "metric": "ast_ncss",
            "operator": ">=",
            "threshold": LONG_METHOD_THRESHOLD,
            "value": score,
        },
    )


def capture_nested_complexity(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    return _capture_result(
        evaluate_nested_complexity(project_root, location, selector)
    )


def evaluate_nested_complexity(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    predicate_id = _PREDICATE_IDS["nested_complexity"]
    prepared = _prepare_method_target(
        project_root,
        location,
        selector,
        predicate_id=predicate_id,
    )
    if isinstance(prepared, dict):
        return prepared
    target, _, matches = prepared
    base = _method_selection_result(target, matches, predicate_id)
    if len(matches) != 1:
        return base
    method = matches[0]
    score = syntax.compute_cognitive_complexity(
        method.body_text,
        method.method_name,
    )
    return _matched_method_result(
        target,
        method,
        predicate_id=predicate_id,
        objectives={"cognitive_complexity": float(score)},
        present=score >= NESTED_COMPLEXITY_THRESHOLD,
        metric_witness={
            "metric": "cognitive_complexity",
            "operator": ">=",
            "threshold": NESTED_COMPLEXITY_THRESHOLD,
            "value": score,
        },
    )


def capture_switch_statements(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    return _capture_result(
        evaluate_switch_statements(project_root, location, selector)
    )


def evaluate_switch_statements(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    predicate_id = _PREDICATE_IDS["switch_statements"]
    prepared = _prepare_method_target(
        project_root,
        location,
        selector,
        predicate_id=predicate_id,
    )
    if isinstance(prepared, dict):
        return prepared
    target, _, matches = prepared
    base = _method_selection_result(target, matches, predicate_id)
    if len(matches) != 1:
        return base
    method = matches[0]
    switch_count, case_count, density = syntax.compute_switch_metrics(
        method.body_text
    )
    return _matched_method_result(
        target,
        method,
        predicate_id=predicate_id,
        objectives={
            "switch_count": float(switch_count),
            "switch_case_count": float(case_count),
            "switch_density": round(float(density), 6),
        },
        present=switch_count > 0,
        metric_witness={
            "metric": "switch_count",
            "operator": ">",
            "threshold": 0,
            "value": switch_count,
            "case_count": case_count,
            "density": round(float(density), 6),
        },
    )


def capture_mysterious_name(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    return _capture_result(
        evaluate_mysterious_name(project_root, location, selector)
    )


def evaluate_mysterious_name(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None = None,
) -> TargetGuardResult:
    predicate_id = _PREDICATE_IDS["mysterious_name"]
    try:
        target = _coerce_target(project_root, location)
    except (OSError, TypeError, ValueError) as exc:
        return _input_error_result(predicate_id, str(exc))
    if not target.file_path.is_file():
        return _missing_result(predicate_id, target)
    selector_map = dict(selector or {})
    if not _selector_file_matches(target, selector_map):
        return _missing_result(predicate_id, target)
    try:
        classes, methods = syntax.load_project_model(
            target.root,
            [target.file_path],
        )
        findings = list(
            syntax._detect_mysterious_name(
                methods,
                int(syntax.DEFAULT_THRESHOLDS["mysterious_name_min_len"]),
                syntax.DEFAULT_LOW_INFO_NAMES,
                profile="strict",
                exclude_tests=True,
            )
        )
        findings.extend(
            syntax._detect_mysterious_names_outside_methods(
                target.root,
                [target.file_path],
                classes,
                methods,
                int(syntax.DEFAULT_THRESHOLDS["mysterious_name_min_len"]),
                syntax.DEFAULT_LOW_INFO_NAMES,
                profile="strict",
            )
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return _error_result(predicate_id, target, str(exc))

    matches = _select_mysterious_findings(findings, target, selector_map)
    selection = _selection(len(matches))
    if len(matches) != 1:
        return {
            "ok": len(matches) == 0,
            "target_match_count": len(matches),
            "target_smell_present": False,
            "target_missing": len(matches) == 0,
            "objectives": {"target_suspicious_name_present": 0.0},
            "entity_identity": {},
            "witness": _witness(
                predicate_id,
                target,
                selection,
                **(
                    {"error": "TARGET_AMBIGUOUS"}
                    if len(matches) > 1
                    else {}
                ),
            ),
        }
    match = matches[0]
    identity = {
        "kind": "symbol",
        "file": target.relative_file,
        "class": str(match.class_name or ""),
        "method": str(match.method or ""),
        "container_method": str(match.method or ""),
        "parameter_type_fingerprint": (
            syntax.method_parameter_type_fingerprint(match.method)
        ),
        "symbol_kind": str(match.symbol_kind or ""),
        "symbol_name": str(match.symbol_name or ""),
        "rule_id": str(match.rule_id or ""),
    }
    return {
        "ok": True,
        "target_match_count": 1,
        "target_smell_present": True,
        "target_missing": False,
        "objectives": {"target_suspicious_name_present": 1.0},
        "entity_identity": identity,
        "witness": _witness(
            predicate_id,
            target,
            "MATCHED",
            metric={
                "metric": "target_suspicious_name_present",
                "operator": "==",
                "threshold": 1,
                "value": 1,
            },
            matched_range={
                "begin_line": int(match.begin_line or 0),
                "end_line": int(match.end_line or match.begin_line or 0),
            },
            symbol={
                "kind": str(match.symbol_kind or ""),
                "name": str(match.symbol_name or ""),
                "scope_starts": list(match.scope_starts or ()),
            },
        ),
    }


def _prepare_method_target(
    project_root: Path | str,
    location: Any,
    selector: Mapping[str, Any] | None,
    *,
    predicate_id: str,
) -> tuple[_TargetFile, list[syntax.JavaMethodInfo], list[syntax.JavaMethodInfo]] | TargetGuardResult:
    try:
        target = _coerce_target(project_root, location)
    except (OSError, TypeError, ValueError) as exc:
        return _input_error_result(predicate_id, str(exc))
    if not target.file_path.is_file():
        return _missing_result(predicate_id, target)
    selector_map = dict(selector or {})
    if not _selector_file_matches(target, selector_map):
        return _missing_result(predicate_id, target)
    try:
        _, methods = syntax.load_project_model(target.root, [target.file_path])
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return _error_result(predicate_id, target, str(exc))
    matches = _select_methods(methods, target, selector_map)
    return target, methods, matches


def _select_methods(
    methods: Sequence[syntax.JavaMethodInfo],
    target: _TargetFile,
    selector: Mapping[str, Any],
) -> list[syntax.JavaMethodInfo]:
    method_anchor = str(
        selector.get("source_signature")
        or selector.get("method")
        or selector.get("container_method")
        or target.method
        or ""
    ).strip()
    class_anchor = str(
        selector.get("fq_owner")
        or selector.get("class")
        or selector.get("target_class")
        or target.class_name
        or ""
    ).strip()
    method_name = _method_name(method_anchor)
    fingerprint = selector.get("parameter_type_fingerprint")
    if fingerprint is None:
        fingerprint = syntax.method_parameter_type_fingerprint(method_anchor)
    candidates = [
        method
        for method in methods
        if (not class_anchor or _same_simple_class(method.class_name, class_anchor))
        and (not method_name or method.method_name == method_name)
        and (
            fingerprint is None
            or syntax.method_parameter_type_fingerprint(method.signature)
            == str(fingerprint)
        )
    ]
    has_identity = bool(method_name or class_anchor or fingerprint is not None)
    # ``source_signature`` is emitted only by a captured entity identity; it
    # cannot come from mutable target_context.  Once that frozen identity
    # resolves a unique current declaration, the original source line is only
    # historical evidence and must not make ordinary line drift look like a
    # deleted target. Capture still uses the caller's line as a hard
    # disambiguator. During verify, retain it only as a soft disambiguator for
    # structurally identical declarations and never collapse multiple valid
    # identity matches to NOT_FOUND merely because every declaration moved.
    frozen_identity = bool(str(selector.get("source_signature") or "").strip())
    if target.line and not frozen_identity:
        candidates = [
            method
            for method in candidates
            if method.begin_line <= int(target.line) <= method.end_line
        ]
    elif target.line and len(candidates) > 1:
        containing = [
            method
            for method in candidates
            if method.begin_line <= int(target.line) <= method.end_line
        ]
        if containing:
            candidates = containing
    if not has_identity and not target.line:
        return []
    return sorted(
        candidates,
        key=lambda method: (
            method.begin_line,
            method.end_line,
            method.class_name,
            method.signature,
        ),
    )


def _select_mysterious_findings(
    findings: Sequence[syntax.JavaSyntacticFinding],
    target: _TargetFile,
    selector: Mapping[str, Any],
) -> list[syntax.JavaSyntacticFinding]:
    kind = str(selector.get("symbol_kind") or "").strip()
    name = str(selector.get("symbol_name") or "").strip()
    class_anchor = str(
        selector.get("fq_owner")
        or selector.get("class")
        or selector.get("target_class")
        or target.class_name
        or ""
    ).strip()
    container = str(
        selector.get("container_method")
        or selector.get("method")
        or target.method
        or ""
    ).strip()
    container_name = _method_name(container)
    container_fingerprint = selector.get("parameter_type_fingerprint")
    if container_fingerprint is None:
        container_fingerprint = syntax.method_parameter_type_fingerprint(container)
    candidates = [
        item
        for item in findings
        if (not kind or str(item.symbol_kind or "") == kind)
        and (not name or str(item.symbol_name or "") == name)
        and (not class_anchor or _same_simple_class(item.class_name, class_anchor))
        and (
            not container_name
            or _method_name(item.method) == container_name
        )
        and (
            container_fingerprint is None
            or syntax.method_parameter_type_fingerprint(item.method)
            == str(container_fingerprint)
        )
    ]
    has_scope_identity = bool(
        kind
        or name
        or class_anchor
        or container_name
        or container_fingerprint is not None
    )
    if target.line and (len(candidates) > 1 or not has_scope_identity):
        line = int(target.line)
        candidates = [
            item
            for item in candidates
            if int(item.begin_line or 0) <= line <= int(item.end_line or item.begin_line or 0)
            or (
                str(item.method or "").startswith("<initializer:")
                and line in set(item.scope_starts or ())
            )
        ]
    if not has_scope_identity and not target.line:
        return []
    return sorted(
        candidates,
        key=lambda item: (
            int(item.begin_line or 0),
            str(item.class_name or ""),
            str(item.method or ""),
            str(item.symbol_kind or ""),
            str(item.symbol_name or ""),
            str(item.rule_id or ""),
        ),
    )


def _ast_ncss_for_method(
    method: syntax.JavaMethodInfo,
    methods: Sequence[syntax.JavaMethodInfo],
    findings: Sequence[syntax.JavaSyntacticFinding],
) -> int | None:
    """Map an AST-NCSS row to an overload by source order, never by nearest line."""
    key = (method.method_name, method.end_line)
    source_peers = [
        item for item in methods
        if (item.method_name, item.end_line) == key
    ]
    metric_peers = [
        item for item in findings
        if (_method_name(item.method), int(item.end_line or 0)) == key
    ]
    if method not in source_peers or len(source_peers) != len(metric_peers):
        return None
    ordinal = source_peers.index(method)
    return int(metric_peers[ordinal].score)


def _method_selection_result(
    target: _TargetFile,
    matches: Sequence[syntax.JavaMethodInfo],
    predicate_id: str,
) -> TargetGuardResult:
    count = len(matches)
    return {
        "ok": count <= 1,
        "target_match_count": count,
        "target_smell_present": False,
        "target_missing": count == 0,
        "objectives": {},
        "entity_identity": {},
        "witness": _witness(
            predicate_id,
            target,
            _selection(count),
            **({"error": "TARGET_AMBIGUOUS"} if count > 1 else {}),
        ),
    }


def _matched_method_result(
    target: _TargetFile,
    method: syntax.JavaMethodInfo,
    *,
    predicate_id: str,
    objectives: Mapping[str, float],
    present: bool,
    metric_witness: Mapping[str, Any],
) -> TargetGuardResult:
    return {
        "ok": True,
        "target_match_count": 1,
        "target_smell_present": bool(present),
        "target_missing": False,
        "objectives": dict(objectives),
        "entity_identity": _method_identity(target, method),
        "witness": _witness(
            predicate_id,
            target,
            "MATCHED",
            metric=dict(metric_witness),
            matched_range={
                "begin_line": method.begin_line,
                "end_line": method.end_line,
            },
        ),
    }


def _method_identity(
    target: _TargetFile,
    method: syntax.JavaMethodInfo,
) -> dict[str, Any]:
    return {
        "kind": "method",
        "file": target.relative_file,
        "class": str(method.class_name or ""),
        "method": str(method.signature or ""),
        "source_signature": str(method.signature or ""),
        "parameter_type_fingerprint": (
            syntax.method_parameter_type_fingerprint(method.signature)
        ),
    }


def _capture_result(result: TargetGuardResult) -> TargetGuardResult:
    captured = {
        **result,
        "witness": dict(result.get("witness") or {}),
    }
    if captured.get("ok") is not True:
        return captured
    if int(captured.get("target_match_count") or 0) != 1:
        captured["ok"] = False
        captured["witness"]["error"] = "BASELINE_FINDING_NOT_FOUND"
        return captured
    if captured.get("target_smell_present") is not True:
        captured["ok"] = False
        captured["witness"]["error"] = "BASELINE_FINDING_NOT_FOUND"
    return captured


def _coerce_target(project_root: Path | str, location: Any) -> _TargetFile:
    root = Path(project_root).expanduser().resolve()
    parsed: Any
    if isinstance(location, LocationTarget):
        parsed = location
    elif isinstance(location, str):
        parsed = parse_location_descriptor(location, root)
    elif isinstance(location, Path):
        parsed = {
            "file_path": location,
        }
    elif isinstance(location, Mapping):
        parsed = location
    elif hasattr(location, "file_path"):
        parsed = location
    else:
        raise TypeError("location must be a LocationTarget, descriptor, path, or mapping")

    def read(name: str, default: Any = None) -> Any:
        if isinstance(parsed, Mapping):
            return parsed.get(name, default)
        return getattr(parsed, name, default)

    raw_path = read("file_path") or read("project_path") or read("file")
    if raw_path is None:
        raise ValueError("location does not contain a target file")
    declared = Path(str(raw_path)).expanduser()
    file_path = declared.resolve() if declared.is_absolute() else (root / declared).resolve()
    try:
        relative = file_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("target file must be inside project_root") from exc
    raw_line = read("line")
    line = int(raw_line) if raw_line not in (None, "") else None
    if line is not None and line <= 0:
        raise ValueError("target line must be a positive integer")
    return _TargetFile(
        root=root,
        file_path=file_path,
        relative_file=relative,
        method=str(read("method") or "").strip(),
        class_name=str(read("class_name") or read("class") or "").strip(),
        line=line,
    )


def _selector_file_matches(
    target: _TargetFile,
    selector: Mapping[str, Any],
) -> bool:
    frozen = str(selector.get("file") or "").replace("\\", "/").lstrip("./")
    return not frozen or frozen == target.relative_file


def _selection(count: int) -> str:
    if count == 1:
        return "MATCHED"
    return "NOT_FOUND" if count == 0 else "AMBIGUOUS"


def _method_name(signature: Any) -> str:
    text = str(signature or "").strip()
    return text.split("(", 1)[0].strip().rsplit(".", 1)[-1]


def _same_simple_class(left: Any, right: Any) -> bool:
    return (
        str(left or "").strip().rsplit(".", 1)[-1].lower()
        == str(right or "").strip().rsplit(".", 1)[-1].lower()
    )


def _witness(
    predicate_id: str,
    target: _TargetFile,
    selection: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "predicate_id": predicate_id,
        "selection": selection,
        "source_scope": [target.relative_file],
        "parsed_file_count": 1 if target.file_path.is_file() else 0,
        **extra,
    }


def _missing_result(
    predicate_id: str,
    target: _TargetFile,
) -> TargetGuardResult:
    return {
        "ok": True,
        "target_match_count": 0,
        "target_smell_present": False,
        "target_missing": True,
        "objectives": {},
        "entity_identity": {},
        "witness": _witness(predicate_id, target, "NOT_FOUND"),
    }


def _error_result(
    predicate_id: str,
    target: _TargetFile,
    error: str,
    *,
    target_match_count: int = 0,
) -> TargetGuardResult:
    return {
        "ok": False,
        "target_match_count": int(target_match_count),
        "target_smell_present": False,
        "target_missing": target_match_count == 0,
        "objectives": {},
        "entity_identity": {},
        "witness": _witness(
            predicate_id,
            target,
            "ERROR",
            error=str(error),
        ),
    }


def _input_error_result(predicate_id: str, error: str) -> TargetGuardResult:
    return {
        "ok": False,
        "target_match_count": 0,
        "target_smell_present": False,
        "target_missing": True,
        "objectives": {},
        "entity_identity": {},
        "witness": {
            "predicate_id": predicate_id,
            "selection": "ERROR",
            "source_scope": [],
            "parsed_file_count": 0,
            "error": str(error),
        },
    }


def _unsupported_result(smell: str) -> TargetGuardResult:
    return {
        "ok": False,
        "target_match_count": 0,
        "target_smell_present": False,
        "target_missing": True,
        "objectives": {},
        "entity_identity": {},
        "witness": {
            "predicate_id": "",
            "selection": "ERROR",
            "source_scope": [],
            "parsed_file_count": 0,
            "error": f"UNSUPPORTED_TARGET_PREDICATE:{smell}",
        },
    }


_CAPTURE_HELPERS: dict[str, TargetPredicate] = {
    "long_method": capture_long_method,
    "nested_complexity": capture_nested_complexity,
    "switch_statements": capture_switch_statements,
    "mysterious_name": capture_mysterious_name,
}

_EVALUATE_HELPERS: dict[str, TargetPredicate] = {
    "long_method": evaluate_long_method,
    "nested_complexity": evaluate_nested_complexity,
    "switch_statements": evaluate_switch_statements,
    "mysterious_name": evaluate_mysterious_name,
}
