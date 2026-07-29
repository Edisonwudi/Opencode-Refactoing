"""Java-specific smell guard implementations.

These functions are called from the generic ``guards.run_smell_guards``
dispatcher when the target language is Java or the smell type requires
a Java-specific detector.
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..analysis import count_meaningful_lines, extract_snippet, strip_comments
from ..config import ResolvedRunConfig
from ..guards.context import GuardRunContext
from .detector_utils import (
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
    parse_expected_state_field as _parse_expected_state_field,
    parse_parent_from_evidence as _parse_parent_from_evidence,
    parse_structural_expectation as _parse_structural_expectation,
    parse_target_class as _parse_target_class,
    parse_target_parameter_count as _parse_target_parameter_count,
)
from .data_clumps import (
    data_clump_group_from_evidence,
    data_clump_occurrence_threshold,
    detect_data_clump_occurrences,
)
from .semantic_detector import (
    SemanticFinding,
    _build_project_model,
    analyze_refused_bequest_target,
    find_matching_semantic_finding,
    run_java_semantic_detector,
)
from .ast_ncss import run_ast_ncss
from .syntactic_detector import (
    JavaClassInfo,
    JavaMethodInfo,
    _finding,
    find_matching_clone_pair,
    find_matching_syntactic_finding,
    load_java_source_model,
    load_project_model,
    parse_mysterious_evidence,
    run_java_syntactic_detector,
    tokenize_clone,
)


# ---------------------------------------------------------------------------
# Public dispatch: called by guards.run_smell_guards for Java-only smell types
# ---------------------------------------------------------------------------

def run_java_smell_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Optional[Dict[str, object]]:
    """Dispatch a single Java-only smell guard.  Returns ``None`` for unknown types."""
    guard_type = str(guard.get("type", "")).strip()
    if guard_type == "feature_envy":
        return _run_feature_envy_guard(config, guard, context)
    handler = _JAVA_GUARD_DISPATCH.get(guard_type)
    if handler is None:
        return None
    return handler(config, guard)


# ---------------------------------------------------------------------------
# Java early-return hooks for the five language-agnostic guards
# ---------------------------------------------------------------------------

def run_java_syntactic_guard(
    config: ResolvedRunConfig,
    guard_type: str,
    thresholds: Dict[str, object],
    evidence: str = "",
) -> Optional[Dict[str, object]]:
    """Early-return hook for long_method / long_parameter_list / nested_complexity / switch_statements."""
    if config.language != "java":
        return None
    if not config.locations:
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard: target location is missing.",
            "details": {"detector": "java_syntactic_detector"},
        }
    target = config.locations[0]
    if not target.file_path.exists() or target.file_path.suffix != ".java":
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard: target file not found or not a .java file: {target.file_path}",
            "details": {"detector": "java_syntactic_detector", "file": str(target.file_path)},
        }
    if guard_type == "long_method":
        return _run_java_ast_ncss_guard(config, target, thresholds, evidence)
    detection = run_java_syntactic_detector(
        config.project_root,
        target_files=[target.file_path],
        thresholds=thresholds,
        include_code_clone=False,
        include_mysterious_name=False,
    )
    if not detection.ok:
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard: Java syntactic detector unavailable: {detection.error}",
            "details": {"detector": "java_syntactic_detector", "error": detection.error},
        }
    match = find_matching_syntactic_finding(
        detection.findings.get(guard_type, []),
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
        original_start_line=target.start_line,
        original_param_count=target.parameter_count if guard_type == "long_parameter_list" else None,
        original_param_type_fingerprint=target.param_type_fingerprint if guard_type == "long_parameter_list" else None,
        evidence=evidence,
    )
    if not match and guard_type == "long_parameter_list":
        match = _find_lingering_lpl_signature(config, target, thresholds)
    if match:
        return {
            "type": guard_type,
            "success": False,
            "message": (
                f"{guard_type} guard: Java syntactic detector still reports "
                f"{target.project_path}#{target.method or target.line}. evidence: {match.evidence}"
            ),
            "details": {
                "detector": "java_syntactic_detector",
                "file": match.file,
                "method": match.method,
                "begin_line": match.begin_line,
                "end_line": match.end_line,
                "score": match.score,
                "rule_id": match.rule_id,
                "evidence": match.evidence,
            },
        }
    return {
        "type": guard_type,
        "success": True,
        "message": (
            f"{guard_type} guard: Java syntactic detector no longer reports "
            f"{target.project_path}#{target.method or target.line}."
        ),
        "details": {"detector": "java_syntactic_detector"},
    }


def _find_lingering_lpl_signature(
    config: ResolvedRunConfig,
    target: Any,
    thresholds: Dict[str, object],
) -> Optional[Any]:
    """Fail-closed fallback for long_parameter_list.

    The finding matcher anchors on the original arity, so an agent can make
    the target "unfindable" (line drift, added overloads, signature edits)
    while the original long signature still exists in the file. When the
    matcher comes back empty, rescan the target file directly: any same-name
    method whose parameter count still exceeds the LPL threshold means the
    smell was never repaired.
    """
    try:
        _, methods = load_project_model(config.project_root, [target.file_path])
    except Exception:
        return None
    threshold = int(thresholds.get("long_parameter_list", 5) or 5)
    target_method = _normalize_method(target.method)
    lingering = [
        method
        for method in methods
        if (not target_method or _normalize_method(method.method_name) == target_method)
        and len(method.parameter_names or []) > threshold
    ]
    if not lingering:
        return None
    worst = max(lingering, key=lambda item: len(item.parameter_names or []))
    count = len(worst.parameter_names or [])
    return _finding(
        "long_parameter_list",
        worst,
        float(count),
        "custom:long_parameter_list_lingering",
        f"param_count={count}; threshold={threshold}; matcher_fallback=lingering-signature",
    )


def _run_java_ast_ncss_guard(
    config: ResolvedRunConfig,
    target: Any,
    thresholds: Dict[str, object],
    evidence: str,
) -> Dict[str, object]:
    threshold = int(thresholds.get("long_method_ncss", 60))
    result = run_ast_ncss(target.file_path, config.project_root, threshold)
    if not result.ok:
        return {
            "type": "long_method",
            "success": False,
            "message": f"long_method guard: Java AST-NCSS unavailable: {result.error}",
            "details": {"detector": "java_ast_ncss", "metric": "PMD-compatible AST-NCSS", "error": result.error},
        }
    match = find_matching_syntactic_finding(
        result.findings,
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
        original_start_line=target.start_line,
        evidence=evidence,
    )
    if match:
        return {
            "type": "long_method",
            "success": False,
            "message": (
                f"long_method guard: Java AST still reports {target.project_path}#"
                f"{target.method or target.line} with AST-NCSS {match.score:g} "
                f"(threshold {threshold})."
            ),
            "details": {
                "detector": "java_ast_ncss",
                "metric": "PMD-compatible AST-NCSS",
                "file": match.file,
                "method": match.method,
                "begin_line": match.begin_line,
                "score": match.score,
                "threshold": threshold,
                "rule_id": match.rule_id,
                "evidence": match.evidence,
            },
        }
    return {
        "type": "long_method",
        "success": True,
        "message": (
            f"long_method guard: Java AST no longer reports {target.project_path}#"
            f"{target.method or target.line} at or above AST-NCSS threshold {threshold}."
        ),
        "details": {
            "detector": "java_ast_ncss",
            "metric": "PMD-compatible AST-NCSS",
            "threshold": threshold,
        },
    }


def run_java_clone_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Optional[Dict[str, object]]:
    """Early-return hook for code_clone_type1 on Java projects."""
    if config.language != "java":
        return None
    if len(config.locations) < 2:
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": "code_clone_type1 guard: clone location did not resolve to two targets.",
            "details": {
                "detector": "java_syntactic_detector",
                "target_resolution": "invalid_location",
                "target_count": len(config.locations),
            },
        }
    first, second = config.locations[0], config.locations[1]
    for loc in (first, second):
        if not loc.file_path.exists() or loc.file_path.suffix != ".java":
            return {
                "type": "code_clone_type1",
                "success": False,
                "message": f"code_clone_type1 guard: target file not found or not a .java file: {loc.file_path}",
                "details": {"detector": "java_syntactic_detector", "file": str(loc.file_path)},
            }
    detection = run_java_syntactic_detector(
        config.project_root,
        target_files=[first.file_path, second.file_path],
        thresholds={"code_clone_min_tokens": int(guard.get("min_tokens", 80))},
        include_mysterious_name=False,
    )
    if not detection.ok:
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": f"code_clone_type1 guard: Java syntactic detector unavailable: {detection.error}",
            "details": {"detector": "java_syntactic_detector", "error": detection.error},
        }
    clone_findings = detection.findings.get("code_clone_type1", [])
    match = find_matching_clone_pair(
        clone_findings,
        left_file=first.file_path,
        right_file=second.file_path,
        project_root=config.project_root,
        left_method=first.method,
        right_method=second.method,
        left_line=first.line,
        right_line=second.line,
    )
    if match:
        left, right = match
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": (
                "code_clone_type1 guard: Java syntactic detector still reports "
                f"the clone pair {first.project_path} and {second.project_path}."
            ),
            "details": {
                "detector": "java_syntactic_detector",
                "left": {"file": left.file, "method": left.method, "begin_line": left.begin_line, "evidence": left.evidence},
                "right": {"file": right.file, "method": right.method, "begin_line": right.begin_line, "evidence": right.evidence},
                "rule_id": left.rule_id,
            },
        }
    left_finding = find_matching_syntactic_finding(
        clone_findings,
        target_file=first.file_path,
        project_root=config.project_root,
        method=first.method,
        line=first.line,
    )
    right_finding = find_matching_syntactic_finding(
        clone_findings,
        target_file=second.file_path,
        project_root=config.project_root,
        method=second.method,
        line=second.line,
    )
    if left_finding is None and right_finding is None:
        target_resolution = "no_clone_findings_for_targets"
    elif left_finding is None or right_finding is None:
        target_resolution = "partial_clone_target_changed"
    else:
        target_resolution = "clone_pair_changed"
    structural = _verify_clone_structural_resolution(
        config,
        first,
        second,
        min_tokens=int(guard.get("min_tokens", 80)),
        expected_group_size=_clone_group_size(_guard_evidence(guard)),
        changed_java_files=context.changed_java_files if context else [],
    )
    if not structural["success"]:
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": f"code_clone_type1 guard: {structural['message']}",
            "details": {
                "detector": "java_syntactic_detector",
                "target_resolution": target_resolution,
                "left_clone_finding_found": left_finding is not None,
                "right_clone_finding_found": right_finding is not None,
                **structural["details"],
            },
        }
    return {
        "type": "code_clone_type1",
        "success": True,
        "message": (
            "code_clone_type1 guard: the target clone pair is gone and the "
            "shared structural resolution is proven."
        ),
        "details": {
            "detector": "java_syntactic_detector",
            "target_resolution": target_resolution,
            "left_clone_finding_found": left_finding is not None,
            "right_clone_finding_found": right_finding is not None,
            **structural["details"],
        },
    }


_CALL_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_QUALIFIED_CALL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_$]*)\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
_NON_DELEGATION_CALLS = {
    "if", "for", "while", "switch", "catch", "synchronized", "try",
    "do", "return", "throw", "new", "assert", "this",
}


def _verify_clone_structural_resolution(
    config: ResolvedRunConfig,
    first: Any,
    second: Any,
    *,
    min_tokens: int,
    expected_group_size: int,
    changed_java_files: List[Path],
) -> Dict[str, object]:
    baseline = _load_clone_baseline(config, (first, second))
    if baseline is None:
        return {
            "success": False,
            "message": "structural proof unavailable because the Git baseline targets could not be resolved.",
            "details": {"structural_resolution": "baseline_unavailable"},
        }
    baseline_classes, baseline_methods, baseline_targets = baseline
    relevant_files = _clone_relevant_files(config, (first, second), changed_java_files)
    baseline_classes, baseline_methods = _extend_clone_baseline_scope(
        config,
        baseline_classes,
        baseline_methods,
        relevant_files,
    )
    try:
        current_classes, current_methods = load_project_model(config.project_root, relevant_files)
    except Exception as exc:
        return {
            "success": False,
            "message": f"structural proof unavailable: {exc}",
            "details": {"structural_resolution": "current_model_unavailable", "error": str(exc)},
        }
    current_targets = [
        _find_clone_target_method(current_methods, loc, baseline_target)
        for loc, baseline_target in zip((first, second), baseline_targets)
    ]

    scope_expansion = _find_unreported_overload_changes(
        baseline_methods,
        baseline_targets,
        current_methods,
        expected_group_size=expected_group_size,
    )
    if scope_expansion:
        return {
            "success": False,
            "message": (
                "the dataset reports a two-member overload clone, but additional "
                "overloads were changed; restore unrelated family members."
            ),
            "details": {
                "structural_resolution": "unreported_overload_scope_expansion",
                "changed_unreported_overloads": scope_expansion,
            },
        }

    new_parameterized_casts = _find_new_parameterized_casts(
        baseline_methods,
        current_methods,
    )
    if new_parameterized_casts:
        return {
            "success": False,
            "message": (
                "the refactoring introduced parameterized casts; preserve concrete "
                "generic types through typed adapters instead."
            ),
            "details": {
                "structural_resolution": "new_parameterized_casts",
                "parameterized_casts": new_parameterized_casts,
            },
        }

    new_reflective_array_calls = _find_new_reflective_array_calls(
        baseline_methods,
        current_methods,
    )
    if new_reflective_array_calls:
        return {
            "success": False,
            "message": (
                "the refactoring introduced reflective array dispatch; preserve "
                "typed access through adapters instead."
            ),
            "details": {
                "structural_resolution": "new_reflective_array_dispatch",
                "reflective_array_calls": new_reflective_array_calls,
            },
        }

    new_primitive_dispatch_adapters = _find_new_primitive_dispatch_adapters(
        baseline_methods,
        current_methods,
    )
    if new_primitive_dispatch_adapters:
        return {
            "success": False,
            "message": (
                "the refactoring introduced an Object/type-switch or boxing adapter "
                "for primitive variants; keep primitive access typed."
            ),
            "details": {
                "structural_resolution": "new_primitive_dispatch_adapter",
                "primitive_dispatch_adapters": new_primitive_dispatch_adapters,
            },
        }

    retained_clone_fallbacks = _find_retained_clone_fallbacks(
        baseline_targets,
        current_targets,
    )
    if retained_clone_fallbacks:
        return {
            "success": False,
            "message": (
                "a target delegates to a shared owner but still retains the original "
                "clone body as a fallback; remove the superseded implementation."
            ),
            "details": {
                "structural_resolution": "delegating_fallback_retains_clone",
                "retained_clone_fallbacks": retained_clone_fallbacks,
            },
        }

    new_service_locator_calls = _find_new_service_locator_calls(
        baseline_targets,
        current_targets,
    )
    if new_service_locator_calls:
        return {
            "success": False,
            "message": (
                "the refactoring introduced runtime service-locator delegation; "
                "preserve the project's explicit dependency-injection route."
            ),
            "details": {
                "structural_resolution": "new_service_locator_delegation",
                "service_locator_calls": new_service_locator_calls,
            },
        }

    moved = _find_moved_clone_occurrences(
        baseline_methods,
        current_methods,
        baseline_targets,
        min_tokens=min_tokens,
    )
    if len(moved) >= 2:
        return {
            "success": False,
            "message": "the original clone body was copied or moved to multiple methods instead of centralized.",
            "details": {
                "structural_resolution": "moved_clone_still_duplicated",
                "moved_clone_occurrences": moved,
            },
        }

    parallel_helpers = _find_parallel_new_helpers(
        baseline_methods,
        current_methods,
        current_targets,
        min_tokens=min_tokens,
    )
    if parallel_helpers:
        return {
            "success": False,
            "message": (
                "new parallel helpers still duplicate one another; centralize the "
                "shared behavior and remove superseded wrappers."
            ),
            "details": {
                "structural_resolution": "parallel_new_helpers_duplicated",
                "parallel_new_helpers": parallel_helpers,
            },
        }

    proof = _shared_clone_route_proof(
        baseline_classes,
        baseline_methods,
        baseline_targets,
        current_classes,
        current_methods,
        current_targets,
    )
    if not proof["proven"]:
        return {
            "success": False,
            "message": (
                "the original pair changed, but no shared helper, owner delegation, "
                "or inherited implementation proves clone elimination."
            ),
            "details": {
                "structural_resolution": "shared_route_unproven",
                "left_target_found": current_targets[0] is not None,
                "right_target_found": current_targets[1] is not None,
                **proof,
            },
        }
    return {
        "success": True,
        "message": "shared structural resolution proven.",
        "details": {
            "structural_resolution": proof["route"],
            "shared_calls": proof.get("shared_calls", []),
            "moved_clone_occurrences": moved,
        },
    }


def _load_clone_baseline(
    config: ResolvedRunConfig,
    locations: Tuple[Any, Any],
) -> Optional[Tuple[List[JavaClassInfo], List[JavaMethodInfo], List[JavaMethodInfo]]]:
    classes: List[JavaClassInfo] = []
    methods: List[JavaMethodInfo] = []
    seen: set[str] = set()
    for loc in locations:
        rel_path = str(loc.project_path).replace("\\", "/")
        if rel_path in seen:
            continue
        seen.add(rel_path)
        result = subprocess.run(
            ["git", "-C", str(config.project_root), "show", f"HEAD:{rel_path}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return None
        parsed_classes, parsed_methods = load_java_source_model(loc.file_path, rel_path, result.stdout)
        classes.extend(parsed_classes)
        methods.extend(parsed_methods)
    targets = [
        _find_clone_target_method(methods, loc, None)
        for loc in locations
    ]
    if any(target is None for target in targets):
        return None
    return classes, methods, [target for target in targets if target is not None]


def _clone_relevant_files(
    config: ResolvedRunConfig,
    locations: Tuple[Any, Any],
    changed_java_files: List[Path],
) -> List[Path]:
    files: Dict[str, Path] = {}
    for candidate in [loc.file_path for loc in locations] + list(changed_java_files):
        path = Path(candidate)
        if not path.is_absolute():
            path = config.project_root / path
        if path.exists() and path.suffix == ".java":
            files[str(path.resolve())] = path.resolve()
    unresolved_parents: set[str] = set()
    for _ in range(16):
        classes, _ = load_project_model(config.project_root, list(files.values()))
        known_classes = {item.class_name for item in classes}
        unresolved_parents = {
            item.parent_name
            for item in classes
            if item.parent_name and item.parent_name not in known_classes
        }
        added = False
        for parent_name in sorted(unresolved_parents):
            for candidate in config.project_root.rglob(f"{parent_name}.java"):
                resolved = candidate.resolve()
                key = str(resolved)
                if key not in files:
                    files[key] = resolved
                    added = True
        if not added:
            break
    return list(files.values())


def _extend_clone_baseline_scope(
    config: ResolvedRunConfig,
    baseline_classes: List[JavaClassInfo],
    baseline_methods: List[JavaMethodInfo],
    relevant_files: List[Path],
) -> Tuple[List[JavaClassInfo], List[JavaMethodInfo]]:
    """Load HEAD versions of added context files so relational checks stay symmetric."""
    classes = list(baseline_classes)
    methods = list(baseline_methods)
    loaded_paths = {_normalize_path(item.rel_path) for item in classes}
    for file_path in relevant_files:
        try:
            rel_path = file_path.resolve().relative_to(config.project_root.resolve()).as_posix()
        except ValueError:
            continue
        if _normalize_path(rel_path) in loaded_paths:
            continue
        result = subprocess.run(
            ["git", "-C", str(config.project_root), "show", f"HEAD:{rel_path}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            continue
        parsed_classes, parsed_methods = load_java_source_model(file_path, rel_path, result.stdout)
        classes.extend(parsed_classes)
        methods.extend(parsed_methods)
        loaded_paths.add(_normalize_path(rel_path))
    return classes, methods


def _find_clone_target_method(
    methods: List[JavaMethodInfo],
    location: Any,
    baseline_target: Optional[JavaMethodInfo],
) -> Optional[JavaMethodInfo]:
    expected_path = _normalize_path(str(location.project_path))
    expected_name = _normalize_method(location.method) or (
        _normalize_method(baseline_target.method_name) if baseline_target else ""
    )
    candidates = [
        method
        for method in methods
        if _normalize_path(method.rel_path) == expected_path
        and (not expected_name or _normalize_method(method.method_name) == expected_name)
    ]
    if not candidates:
        return None
    if baseline_target:
        same_class = [method for method in candidates if method.class_name == baseline_target.class_name]
        if not same_class:
            return None
        candidates = same_class
        same_arity = [
            method for method in candidates
            if len(method.parameter_names) == len(baseline_target.parameter_names)
        ]
        if same_arity:
            candidates = same_arity
    line = int(location.line or location.start_line or 0)
    if line:
        return min(candidates, key=lambda method: abs(method.begin_line - line))
    return candidates[0]


def _method_calls(method: Optional[JavaMethodInfo]) -> set[str]:
    if method is None:
        return set()
    return {
        name
        for name in _CALL_RE.findall(method.body_text)
        if name not in _NON_DELEGATION_CALLS
    }


def _find_moved_clone_occurrences(
    baseline_methods: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
    baseline_targets: List[JavaMethodInfo],
    *,
    min_tokens: int,
) -> List[Dict[str, object]]:
    baseline_tokens = tokenize_clone(baseline_targets[0].body_text)
    if len(baseline_tokens) < min_tokens:
        return []
    unchanged_baseline_occurrences = {
        _method_identity(method)
        for method in baseline_methods
        if tokenize_clone(method.body_text) == baseline_tokens
    }
    occurrences = []
    for method in current_methods:
        if tokenize_clone(method.body_text) != baseline_tokens:
            continue
        if _method_identity(method) in unchanged_baseline_occurrences:
            continue
        occurrences.append({
            "file": method.rel_path,
            "class": method.class_name,
            "method": method.signature,
            "begin_line": method.begin_line,
        })
    return occurrences


def _method_identity(method: JavaMethodInfo) -> Tuple[str, str, str]:
    return (
        _normalize_path(method.rel_path),
        method.class_name,
        method.signature,
    )


def _clone_group_size(evidence: str) -> int:
    match = re.search(r"\bgroup_size=(\d+)\b", str(evidence or ""))
    return int(match.group(1)) if match else 0


def _find_unreported_overload_changes(
    baseline_methods: List[JavaMethodInfo],
    baseline_targets: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
    *,
    expected_group_size: int,
) -> List[Dict[str, object]]:
    """Keep a reported two-overload clone repair scoped to that exact pair."""
    if expected_group_size != 2:
        return []
    left, right = baseline_targets
    if (
        left.rel_path != right.rel_path
        or left.class_name != right.class_name
        or left.method_name != right.method_name
    ):
        return []
    target_identities = {_method_identity(method) for method in baseline_targets}
    current_by_identity = {
        _method_identity(method): method
        for method in current_methods
    }
    changed: List[Dict[str, object]] = []
    for baseline_method in baseline_methods:
        identity = _method_identity(baseline_method)
        if identity in target_identities:
            continue
        if (
            baseline_method.rel_path != left.rel_path
            or baseline_method.class_name != left.class_name
            or baseline_method.method_name != left.method_name
        ):
            continue
        current_method = current_by_identity.get(identity)
        if (
            current_method is not None
            and tokenize_clone(current_method.body_text) == tokenize_clone(baseline_method.body_text)
        ):
            continue
        changed.append({
            "file": baseline_method.rel_path,
            "class": baseline_method.class_name,
            "method": baseline_method.signature,
            "change": "removed" if current_method is None else "body_changed",
        })
    return changed


_PARAMETERIZED_CAST_RE = re.compile(
    r"\(\s*[A-Za-z_$][A-Za-z0-9_$.]*\s*<[^()\n]+>\s*\)"
)


def _find_new_parameterized_casts(
    baseline_methods: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
) -> List[Dict[str, object]]:
    baseline_count = sum(
        len(_PARAMETERIZED_CAST_RE.findall(method.body_text))
        for method in baseline_methods
    )
    current_hits: List[Dict[str, object]] = []
    for method in current_methods:
        casts = _PARAMETERIZED_CAST_RE.findall(method.body_text)
        if not casts:
            continue
        current_hits.extend({
            "file": method.rel_path,
            "class": method.class_name,
            "method": method.signature,
            "cast": " ".join(cast.split()),
        } for cast in casts)
    if len(current_hits) <= baseline_count:
        return []
    return current_hits[baseline_count:]


_REFLECTIVE_ARRAY_CALL_RE = re.compile(
    r"\b(?:java\s*\.\s*lang\s*\.\s*reflect\s*\.\s*)?"
    r"Array\s*\.\s*(?:get|set|getLength)\s*\("
)

_PRIMITIVE_ARRAY_TYPE_SWITCH_RE = re.compile(
    r"\binstanceof\s+(?:boolean|byte|char|short|int|long|float|double)\s*\[\s*\]"
)
_PRIMITIVE_ARRAY_CAST_RE = re.compile(
    r"\(\s*(?:boolean|byte|char|short|int|long|float|double)\s*\[\s*\]\s*\)"
)
_BOXING_INDEX_ADAPTER_RE = re.compile(
    r"\b(?:java\s*\.\s*util\s*\.\s*function\s*\.\s*)?IntFunction\s*<\s*"
    r"(?:Object|Boolean|Byte|Character|Short|Integer|Long|Float|Double)\s*>"
)


def _find_new_primitive_dispatch_adapters(
    baseline_methods: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
) -> List[Dict[str, object]]:
    """Reject new primitive paths that erase type or box each indexed value."""
    def hits(method: JavaMethodInfo) -> List[str]:
        text = f"{method.signature}\n{method.body_text}"
        reasons = []
        if (
            re.search(r"\bObject\b", method.signature)
            and (
                _PRIMITIVE_ARRAY_TYPE_SWITCH_RE.search(method.body_text)
                or _PRIMITIVE_ARRAY_CAST_RE.search(method.body_text)
            )
        ):
            reasons.append("object_primitive_array_type_switch")
        if _BOXING_INDEX_ADAPTER_RE.search(text):
            reasons.append("boxing_int_function")
        return reasons

    baseline_hits = set()
    for method in baseline_methods:
        for reason in hits(method):
            baseline_hits.add((*_method_identity(method), reason))
    introduced = []
    for method in current_methods:
        for reason in hits(method):
            identity = (*_method_identity(method), reason)
            if identity in baseline_hits:
                continue
            introduced.append({
                "file": method.rel_path,
                "class": method.class_name,
                "method": method.signature,
                "reason": reason,
            })
    return introduced


def _find_retained_clone_fallbacks(
    baseline_targets: List[JavaMethodInfo],
    current_targets: List[Optional[JavaMethodInfo]],
) -> List[Dict[str, object]]:
    """Detect a new delegation wrapped around an otherwise retained clone body."""
    retained = []
    for baseline_target, current_target in zip(baseline_targets, current_targets):
        if current_target is None:
            continue
        baseline_tokens = tokenize_clone(baseline_target.body_text)
        current_tokens = tokenize_clone(current_target.body_text)
        if len(baseline_tokens) < 20 or current_tokens == baseline_tokens:
            continue
        baseline_core = (
            baseline_tokens[1:-1]
            if len(baseline_tokens) >= 2
            and baseline_tokens[0] == "{"
            and baseline_tokens[-1] == "}"
            else baseline_tokens
        )
        if _contains_token_subsequence(current_tokens, baseline_core):
            retained.append({
                "file": current_target.rel_path,
                "class": current_target.class_name,
                "method": current_target.signature,
            })
    return retained


def _contains_token_subsequence(tokens: List[str], candidate: List[str]) -> bool:
    if not candidate or len(candidate) > len(tokens):
        return False
    width = len(candidate)
    return any(tokens[index:index + width] == candidate for index in range(len(tokens) - width + 1))


_SERVICE_LOCATOR_CALL_RE = re.compile(
    r"\b(?:ApplicationContextProvider|ApplicationContextHolder|SpringContext|"
    r"ServiceLocator|BeanFactory)\s*\.\s*(?:getBean|getService|resolve)\s*\("
)


def _find_new_service_locator_calls(
    baseline_targets: List[JavaMethodInfo],
    current_targets: List[Optional[JavaMethodInfo]],
) -> List[Dict[str, object]]:
    introduced = []
    for baseline_target, current_target in zip(baseline_targets, current_targets):
        if current_target is None:
            continue
        before = len(_SERVICE_LOCATOR_CALL_RE.findall(baseline_target.body_text))
        after = len(_SERVICE_LOCATOR_CALL_RE.findall(current_target.body_text))
        if after <= before:
            continue
        introduced.append({
            "file": current_target.rel_path,
            "class": current_target.class_name,
            "method": current_target.signature,
            "count": after - before,
        })
    return introduced


def _find_new_reflective_array_calls(
    baseline_methods: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
) -> List[Dict[str, object]]:
    baseline_count = sum(
        len(_REFLECTIVE_ARRAY_CALL_RE.findall(method.body_text))
        for method in baseline_methods
    )
    current_hits: List[Dict[str, object]] = []
    for method in current_methods:
        calls = _REFLECTIVE_ARRAY_CALL_RE.findall(method.body_text)
        if not calls:
            continue
        current_hits.extend({
            "file": method.rel_path,
            "class": method.class_name,
            "method": method.signature,
        } for _ in calls)
    if len(current_hits) <= baseline_count:
        return []
    return current_hits[baseline_count:]


def _find_parallel_new_helpers(
    baseline_methods: List[JavaMethodInfo],
    current_methods: List[JavaMethodInfo],
    current_targets: List[Optional[JavaMethodInfo]],
    *,
    min_tokens: int,
) -> List[List[Dict[str, object]]]:
    """Reject exact helper clones introduced by the refactoring itself."""
    baseline_identities = {_method_identity(method) for method in baseline_methods}
    target_identities = {
        _method_identity(method)
        for method in current_targets
        if method is not None
    }
    groups: Dict[Tuple[str, ...], List[JavaMethodInfo]] = {}
    for method in current_methods:
        identity = _method_identity(method)
        if identity in baseline_identities or identity in target_identities:
            continue
        tokens = tuple(tokenize_clone(method.body_text))
        parallel_min_tokens = max(20, min(min_tokens, 30))
        if len(tokens) < parallel_min_tokens:
            continue
        groups.setdefault(tokens, []).append(method)

    duplicated: List[List[Dict[str, object]]] = []
    for methods in groups.values():
        if len(methods) < 2:
            continue
        duplicated.append([
            {
                "file": method.rel_path,
                "class": method.class_name,
                "method": method.signature,
                "begin_line": method.begin_line,
            }
            for method in methods
        ])
    return duplicated


def _shared_clone_route_proof(
    baseline_classes: List[JavaClassInfo],
    baseline_methods: List[JavaMethodInfo],
    baseline_targets: List[JavaMethodInfo],
    current_classes: List[JavaClassInfo],
    current_methods: List[JavaMethodInfo],
    current_targets: List[Optional[JavaMethodInfo]],
) -> Dict[str, object]:
    left, right = current_targets
    baseline_common = _method_calls(baseline_targets[0]) & _method_calls(baseline_targets[1])
    if left is not None and right is not None:
        left_calls = _method_calls(left)
        right_calls = _method_calls(right)
        current_common = left_calls & right_calls
        introduced_common = _proven_shared_calls(
            current_common - baseline_common,
            current_methods,
        )
        if introduced_common:
            return {"proven": True, "route": "shared_callee", "shared_calls": introduced_common}
        if (
            "super" in current_common
            and "super" not in baseline_common
            and _share_parent_constructor(
                left,
                right,
                current_classes,
                current_methods,
            )
        ):
            return {
                "proven": True,
                "route": "shared_parent_constructor_delegation",
                "shared_calls": ["super"],
            }
        left_new_calls = _method_calls(left) - _method_calls(baseline_targets[0])
        right_new_calls = _method_calls(right) - _method_calls(baseline_targets[1])
        one_hop_common = _proven_one_hop_shared_calls(
            left_new_calls,
            right_new_calls,
            current_methods,
            excluded_calls=baseline_common,
        )
        if one_hop_common:
            return {
                "proven": True,
                "route": "typed_adapter_to_shared_callee",
                "shared_calls": one_hop_common,
            }
        if baseline_targets[1].method_name in left_new_calls or baseline_targets[0].method_name in right_new_calls:
            return {"proven": True, "route": "existing_owner_delegation", "shared_calls": []}
        qualified_left = set(_QUALIFIED_CALL_RE.findall(left.body_text))
        qualified_right = set(_QUALIFIED_CALL_RE.findall(right.body_text))
        shared_qualified = _proven_qualified_owner_calls(
            qualified_left & qualified_right,
            current_methods,
        )
        if shared_qualified:
            return {
                "proven": True,
                "route": "qualified_owner_delegation",
                "shared_calls": shared_qualified,
            }
        owner_delegation = _proven_target_owner_delegation(
            left,
            right,
            qualified_left,
            qualified_right,
            current_methods,
        )
        if owner_delegation:
            return {
                "proven": True,
                "route": "qualified_owner_delegation",
                "shared_calls": owner_delegation,
            }

    missing_indexes = [
        index for index, target in enumerate(current_targets)
        if target is None
    ]
    if len(missing_indexes) == 1:
        missing_index = missing_indexes[0]
        surviving_index = 1 - missing_index
        surviving_target = current_targets[surviving_index]
        missing_class = baseline_targets[missing_index].class_name
        baseline_class_calls = set().union(*(
            _method_calls(method)
            for method in baseline_methods
            if method.class_name == missing_class
        ))
        current_class_calls = set().union(*(
            _method_calls(method)
            for method in current_methods
            if method.class_name == missing_class
        ))
        replacement_calls = (
            current_class_calls
            - baseline_class_calls
        ) & _method_calls(surviving_target)
        proven_replacements = _proven_shared_calls(
            replacement_calls,
            current_methods,
        )
        if proven_replacements:
            return {
                "proven": True,
                "route": "removed_target_to_shared_callee",
                "shared_calls": proven_replacements,
            }

    inherited = []
    for baseline_target, current_target in zip(baseline_targets, current_targets):
        if current_target is not None:
            continue
        baseline_class = next(
            (item for item in baseline_classes if item.class_name == baseline_target.class_name),
            None,
        )
        current_class = next(
            (item for item in current_classes if item.class_name == baseline_target.class_name),
            None,
        )
        if baseline_class is None or current_class is None or not current_class.parent_name:
            continue
        inherited_method, inheritance_path = _find_inherited_method(
            current_class,
            baseline_target,
            current_classes,
            current_methods,
        )
        if inherited_method is not None:
            inherited.append(
                f"{'->'.join(inheritance_path)}.{inherited_method.signature}"
            )
    missing_count = sum(target is None for target in current_targets)
    if missing_count and len(inherited) == missing_count:
        return {"proven": True, "route": "shared_parent_inheritance", "shared_calls": inherited}
    return {
        "proven": False,
        "route": "none",
        "baseline_common_calls": sorted(baseline_common),
        "current_left_calls": sorted(_method_calls(left)),
        "current_right_calls": sorted(_method_calls(right)),
    }


def _find_inherited_method(
    current_class: JavaClassInfo,
    baseline_target: JavaMethodInfo,
    current_classes: List[JavaClassInfo],
    current_methods: List[JavaMethodInfo],
) -> Tuple[Optional[JavaMethodInfo], List[str]]:
    classes_by_name = {item.class_name: item for item in current_classes}
    path = [current_class.class_name]
    parent_name = current_class.parent_name
    visited = set(path)
    while parent_name and parent_name not in visited:
        visited.add(parent_name)
        path.append(parent_name)
        parent_method = next(
            (
                method for method in current_methods
                if method.class_name == parent_name
                and _normalize_method(method.method_name) == _normalize_method(baseline_target.method_name)
                and len(method.parameter_names) == len(baseline_target.parameter_names)
            ),
            None,
        )
        if parent_method is not None:
            return parent_method, path
        parent_class = classes_by_name.get(parent_name)
        parent_name = parent_class.parent_name if parent_class else None
    return None, path


def _share_parent_constructor(
    left: JavaMethodInfo,
    right: JavaMethodInfo,
    current_classes: List[JavaClassInfo],
    current_methods: List[JavaMethodInfo],
) -> bool:
    classes_by_name = {item.class_name: item for item in current_classes}
    left_class = classes_by_name.get(left.class_name)
    right_class = classes_by_name.get(right.class_name)
    if (
        left_class is None
        or right_class is None
        or not left_class.parent_name
        or left_class.parent_name != right_class.parent_name
    ):
        return False
    parent_name = left_class.parent_name
    return any(
        method.class_name == parent_name
        and _normalize_method(method.method_name) == _normalize_method(parent_name)
        for method in current_methods
    )


def _proven_qualified_owner_calls(
    calls: set[Tuple[str, str]],
    current_methods: List[JavaMethodInfo],
) -> List[str]:
    proven = []
    for owner, call_name in sorted(calls):
        if any(
            method.class_name == owner and method.method_name == call_name
            for method in current_methods
        ):
            proven.append(f"{owner}.{call_name}")
    return proven


def _proven_target_owner_delegation(
    left: JavaMethodInfo,
    right: JavaMethodInfo,
    qualified_left: set[Tuple[str, str]],
    qualified_right: set[Tuple[str, str]],
    current_methods: List[JavaMethodInfo],
) -> List[str]:
    proven = []
    for owner_target, qualified_calls in (
        (right, qualified_left),
        (left, qualified_right),
    ):
        owner_calls = _method_calls(owner_target)
        for owner, call_name in qualified_calls:
            if owner != owner_target.class_name or call_name not in owner_calls:
                continue
            if any(
                method.class_name == owner and method.method_name == call_name
                for method in current_methods
            ):
                proven.append(f"{owner}.{call_name}")
    return sorted(set(proven))


def _proven_shared_calls(
    call_names: set[str],
    current_methods: List[JavaMethodInfo],
) -> List[str]:
    """Keep calls that resolve to one owner, not same-named per-class helpers."""
    proven = []
    for call_name in sorted(call_names):
        definitions = [
            method
            for method in current_methods
            if method.method_name == call_name
        ]
        if len(definitions) == 1:
            proven.append(call_name)
    return proven


def _proven_one_hop_shared_calls(
    left_calls: set[str],
    right_calls: set[str],
    current_methods: List[JavaMethodInfo],
    *,
    excluded_calls: set[str],
) -> List[str]:
    """Allow distinct typed adapters when both immediately reach one shared owner."""
    if not left_calls or not right_calls or left_calls == right_calls:
        return []
    left_adapter_calls: set[str] = set()
    right_adapter_calls: set[str] = set()
    for call_name in left_calls:
        definitions = [method for method in current_methods if method.method_name == call_name]
        if len(definitions) == 1:
            left_adapter_calls.update(_method_calls(definitions[0]))
    for call_name in right_calls:
        definitions = [method for method in current_methods if method.method_name == call_name]
        if len(definitions) == 1:
            right_adapter_calls.update(_method_calls(definitions[0]))
    return _proven_shared_calls(
        (left_adapter_calls & right_adapter_calls) - excluded_calls,
        current_methods,
    )


# ---------------------------------------------------------------------------
# Java-only guard implementations
# ---------------------------------------------------------------------------

def _run_mysterious_name_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    evidence = _guard_evidence(guard)
    if config.language != "java" or not config.locations:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": "mysterious_name guard only supports Java detector-backed validation.",
            "details": {"detector": "java_syntactic_detector", "language": config.language},
        }
    target = config.locations[0]
    if not target.file_path.exists() or target.file_path.suffix != ".java":
        return {
            "type": "mysterious_name",
            "success": False,
            "message": f"mysterious_name guard: target file not found or not a .java file: {target.file_path}",
            "details": {"detector": "java_syntactic_detector", "file": str(target.file_path)},
        }
    detection = run_java_syntactic_detector(
        config.project_root,
        target_files=[target.file_path],
        thresholds={"mysterious_name_min_len": int(guard.get("min_len", 2))},
        include_code_clone=False,
        include_mysterious_name=True,
    )
    if not detection.ok:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": f"mysterious_name guard: detector unavailable: {detection.error}",
            "details": {"detector": "java_syntactic_detector", "error": detection.error},
        }
    match = find_matching_syntactic_finding(
        detection.findings.get("mysterious_name", []),
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
        original_start_line=target.start_line,
        original_param_count=target.parameter_count,
        original_param_type_fingerprint=target.param_type_fingerprint,
        evidence=evidence,
    )
    kind, name = parse_mysterious_evidence(evidence)
    if match:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": (
                f"mysterious_name guard: detector still reports {kind or 'identifier'} "
                f"'{name or _extract_mysterious_name(evidence)}' at {target.project_path}."
            ),
            "details": {
                "detector": "java_syntactic_detector",
                "file": match.file,
                "method": match.method,
                "begin_line": match.begin_line,
                "rule_id": match.rule_id,
                "evidence": match.evidence,
            },
        }
    return {
        "type": "mysterious_name",
        "success": True,
        "message": (
            f"mysterious_name guard: detector no longer reports {kind or 'identifier'} "
            f"'{name or _extract_mysterious_name(evidence)}' at {target.project_path}."
        ),
        "details": {"detector": "java_syntactic_detector"},
    }


def _group_evidence_values(evidence: str, field: str) -> List[str]:
    match = re.search(
        rf"(?:^|;\s*){re.escape(field)}=([^;]+)",
        str(evidence or ""),
        flags=re.IGNORECASE,
    )
    return [part.strip() for part in match.group(1).split("|")] if match else []


def _run_semantic_guard(
    config: ResolvedRunConfig,
    guard_type: str,
    evidence: str,
    *,
    _shared_detection=None,
    _shared_project_model=None,
    _group_member: bool = False,
) -> Dict[str, object]:
    """Run a Java semantic guard (feature_envy / data_clumps / refused_bequest / dead_code).

    Uses the Python+tree-sitter implementation (``semantic_detector``)
    to avoid depending on the legacy Java-based SemanticSmellSolver.  The
    detector key ``"python_semantic_detector"`` distinguishes this
    implementation from the syntactic one (``"java_syntactic_detector"``).
    """
    if config.language != "java" or not config.locations:
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard only supports Java detector-backed validation.",
            "details": {"detector": "python_semantic_detector", "language": config.language},
        }
    invalid_targets = [
        target
        for target in config.locations
        if not target.file_path.exists() or target.file_path.suffix != ".java"
    ]
    if invalid_targets:
        return {
            "type": guard_type,
            "success": False,
            "message": (
                f"{guard_type} guard: target file not found or not a .java file: "
                f"{invalid_targets[0].file_path}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": str(invalid_targets[0].file_path),
            },
        }
    if guard_type == "data_clumps":
        return _run_data_clumps_group_guard(config, guard_type, evidence)
    if guard_type == "refused_bequest" and len(config.locations) > 1 and not _group_member:
        detection = run_java_semantic_detector(config.project_root)
        if not detection.ok:
            return {
                "type": guard_type,
                "success": False,
                "message": f"{guard_type} guard: semantic detector unavailable: {detection.error}",
                "details": {"detector": "python_semantic_detector", "error": detection.error},
            }
        project_model = _build_project_model(config.project_root, include_tests=False)
        group_arities = _group_evidence_values(evidence, "target_parameter_counts")
        group_classes = _group_evidence_values(evidence, "target_classes")
        member_results = []
        for index, target in enumerate(config.locations):
            member_config = copy.copy(config)
            member_config.locations = [target]
            member_evidence = evidence
            if len(group_arities) == len(config.locations):
                member_evidence += f"; target_parameter_count={group_arities[index]}"
            if len(group_classes) == len(config.locations):
                member_evidence += f"; target_class={group_classes[index]}"
            member_results.append(
                _run_semantic_guard(
                    member_config,
                    guard_type,
                    member_evidence,
                    _shared_detection=detection,
                    _shared_project_model=project_model,
                    _group_member=True,
                )
            )
        failed_members = [item for item in member_results if not item.get("success")]
        first_failure = (
            f" First unresolved target: {failed_members[0].get('message', 'unknown failure')}"
            if failed_members
            else ""
        )
        return {
            "type": guard_type,
            "success": not failed_members,
            "message": (
                f"refused_bequest group guard: {len(member_results) - len(failed_members)}/"
                f"{len(member_results)} target methods satisfy the structural contract."
                f"{first_failure}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "target_count": len(member_results),
                "failure_count": len(failed_members),
                "member_results": member_results,
            },
        }
    target = config.locations[0]
    detection = _shared_detection or run_java_semantic_detector(config.project_root)
    if not detection.ok:
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard: semantic detector unavailable: {detection.error}",
            "details": {"detector": "python_semantic_detector", "error": detection.error},
        }
    match = find_matching_semantic_finding(
        detection.findings.get(guard_type, []),
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
        evidence_group="",
        evidence_parent=_parse_parent_from_evidence(evidence) if guard_type == "refused_bequest" else "",
    )
    if match:
        return {
            "type": guard_type,
            "success": False,
            "message": (
                f"{guard_type} guard: Python semantic detector still reports "
                f"{target.project_path}#{target.method or target.line}. evidence: {match.evidence}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": match.file,
                "method": match.method,
                "begin_line": match.begin_line,
                "evidence": match.evidence,
            },
        }
    structural_target_removed = False
    if guard_type == "refused_bequest":
        structural_expectation = _parse_structural_expectation(evidence)
        if structural_expectation:
            if structural_expectation == "state_getter":
                expected_state_field = _parse_expected_state_field(evidence)
                if not expected_state_field:
                    return {
                        "type": guard_type,
                        "success": False,
                        "message": (
                            "refused_bequest guard: state_getter requires "
                            "expected_state_field evidence."
                        ),
                        "details": {
                            "detector": "python_semantic_detector",
                            "structural_expectation": structural_expectation,
                        },
                    }
                if not _target_method_returns_expected_state(
                    config,
                    target,
                    expected_state_field,
                ):
                    return {
                        "type": guard_type,
                        "success": False,
                        "message": (
                            "refused_bequest guard: state getter must directly return "
                            f"the declared backing field {expected_state_field!r}."
                        ),
                        "details": {
                            "detector": "python_semantic_detector",
                            "structural_expectation": structural_expectation,
                            "expected_state_field": expected_state_field,
                        },
                    }
            elif structural_expectation not in {
                "capability_split",
                "rejecting_override_removed",
            }:
                return {
                    "type": guard_type,
                    "success": False,
                    "message": (
                        "refused_bequest guard: unsupported structural expectation "
                        f"{structural_expectation!r}."
                    ),
                    "details": {
                        "detector": "python_semantic_detector",
                        "structural_expectation": structural_expectation,
                    },
                }
            else:
                profile = analyze_refused_bequest_target(
                    config.project_root,
                    target_file=target.file_path,
                    method=target.method,
                    line=target.line,
                    reported_parent=_parse_parent_from_evidence(evidence),
                    target_parameter_count=_parse_target_parameter_count(evidence),
                    target_class_name=_parse_target_class(evidence),
                    project_model=_shared_project_model,
                )
                if not profile.get("ok"):
                    return {
                        "type": guard_type,
                        "success": False,
                        "message": (
                            "refused_bequest guard: capability-split profile could not "
                            f"be resolved: {profile.get('error', 'unknown error')}."
                        ),
                        "details": {
                            "detector": "python_semantic_detector",
                            "structural_expectation": structural_expectation,
                            "capability_profile": profile,
                        },
                    }
                expectation_satisfied = (
                    profile.get("capability_split_satisfied")
                    if structural_expectation == "capability_split"
                    else profile.get("rejecting_override_removed")
                )
                if not expectation_satisfied:
                    if structural_expectation == "rejecting_override_removed":
                        failure_message = (
                            "refused_bequest guard: the rejecting child override is still "
                            "declared, or the reported safe parent method cannot be resolved."
                        )
                    elif profile.get("inherited_rejecting_owners"):
                        failure_message = (
                            "refused_bequest guard: the rejected capability was relocated "
                            "to an inherited ancestor instead of being split. Rejecting "
                            f"ancestor(s): {', '.join(profile['inherited_rejecting_owners'])}."
                        )
                    else:
                        failure_message = (
                            "refused_bequest guard: the reported parent capability is "
                            "still inherited and still exposes the target method."
                        )
                    return {
                        "type": guard_type,
                        "success": False,
                        "message": failure_message,
                        "details": {
                            "detector": "python_semantic_detector",
                            "structural_expectation": structural_expectation,
                            "capability_profile": profile,
                        },
                    }
                structural_target_removed = True
    if (
        guard_type == "refused_bequest"
        and not structural_target_removed
        and _requires_unsupported_throw_removal(evidence)
    ):
        unsupported_throw = _target_method_unsupported_throw(config, target)
        if unsupported_throw:
            return {
                "type": guard_type,
                "success": False,
                "message": (
                    "refused_bequest guard: target method still directly throws "
                    "UnsupportedOperationException. Remove or replace the rejecting override "
                    "instead of hiding it from override-based detection."
                ),
                "details": {
                    "detector": "python_semantic_detector",
                    "file": str(target.project_path),
                    "method": target.method,
                    "begin_line": target.line,
                    "explicit_unsupported_throw": True,
                },
            }
    if (
        guard_type == "refused_bequest"
        and not structural_target_removed
        and _requires_empty_override_removal(evidence)
    ):
        empty_override = _target_method_empty_override(config, target)
        if empty_override:
            return {
                "type": guard_type,
                "success": False,
                "message": (
                    "refused_bequest guard: target method is still an empty inherited "
                    "contract implementation. Implement, delegate, or remove the empty "
                    "override according to the reported refactor path."
                ),
                "details": {
                    "detector": "python_semantic_detector",
                    "file": str(target.project_path),
                    "method": target.method,
                    "begin_line": target.line,
                    "empty_override": True,
                },
            }
    return {
        "type": guard_type,
        "success": True,
        "message": (
            f"{guard_type} guard: Python semantic detector no longer reports "
            f"{target.project_path}#{target.method or target.line}."
        ),
        "details": {"detector": "python_semantic_detector"},
    }


def _run_data_clumps_group_guard(
    config: ResolvedRunConfig,
    guard_type: str,
    evidence: str,
) -> Dict[str, object]:
    target = config.locations[0]
    target_group = data_clump_group_from_evidence(evidence)
    if not target_group:
        return {
            "type": guard_type,
            "success": False,
            "message": "data_clumps guard: missing group=... evidence; cannot validate the clump family.",
            "details": {"detector": "python_semantic_detector"},
        }
    analysis = detect_data_clump_occurrences(config.project_root, evidence=evidence, limit=20)
    if not analysis.get("success"):
        return {
            "type": guard_type,
            "success": False,
            "message": f"data_clumps guard: semantic detector unavailable: {analysis.get('error', '')}",
            "details": {
                "detector": "python_semantic_detector",
                "group": target_group,
                "error": analysis.get("error", ""),
            },
        }
    occurrence_count = int(analysis.get("occurrence_count") or 0)
    threshold = data_clump_occurrence_threshold()
    if occurrence_count >= threshold:
        remaining_occurrences = list(analysis.get("occurrences") or [])
        first = remaining_occurrences[0] if remaining_occurrences else {}
        return {
            "type": guard_type,
            "success": False,
            "message": (
                "data_clumps guard: Python semantic detector still reports "
                f"group={target_group} across {occurrence_count} occurrence(s). "
                f"first remaining: {first.get('file')}#{first.get('method')}. "
                "Update the repeated parameter group across the remaining occurrence family."
            ),
            "details": {
                "detector": "python_semantic_detector",
                "group": target_group,
                "occurrence_count": occurrence_count,
                "occurrence_threshold": threshold,
                "remaining_occurrences": remaining_occurrences,
                "remaining_occurrences_truncated": occurrence_count > len(remaining_occurrences),
                "file": first.get("file"),
                "method": first.get("method"),
                "begin_line": first.get("begin_line"),
                "evidence": first.get("evidence"),
            },
        }
    return {
        "type": guard_type,
        "success": True,
        "message": (
            f"data_clumps guard: group={target_group} is below the repeated-occurrence threshold "
            f"for {target.project_path} ({occurrence_count}/{threshold})."
        ),
        "details": {
            "detector": "python_semantic_detector",
            "group": target_group,
            "occurrence_count": occurrence_count,
            "occurrence_threshold": threshold,
        },
    }


def _run_god_class_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    if config.language != "java" or not config.locations:
        return {
            "type": "god_class",
            "success": False,
            "message": "god_class guard only supports Java detector-backed validation.",
            "details": {"detector": "python_semantic_detector", "language": config.language},
        }
    target = config.locations[0]
    if not target.file_path.exists() or target.file_path.suffix != ".java":
        return {
            "type": "god_class",
            "success": False,
            "message": (
                "god_class guard: target file is missing or not a .java file; deleting "
                "the target Java file is not accepted as a god_class fix."
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": str(target.file_path),
                "old_target_missing": True,
                "target_file_missing": True,
            },
        }
    evidence = _guard_evidence(guard)
    baseline_metrics = _parse_god_class_metrics(evidence)
    target_class = target.class_name or _class_from_evidence(evidence)
    target_class_exists = _target_class_exists(config, target.file_path, target_class) if target_class else True
    if target_class_exists is False:
        return {
            "type": "god_class",
            "success": False,
            "message": (
                "god_class guard: target class is missing; deleting the reported class "
                "is not accepted as a god_class fix."
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": str(target.project_path),
                "class_name": target_class,
                "old_target_missing": True,
                "target_class_missing": True,
            },
        }
    detection = run_java_semantic_detector(config.project_root)
    if not detection.ok:
        return {
            "type": "god_class",
            "success": False,
            "message": f"god_class guard: semantic detector unavailable: {detection.error}",
            "details": {"detector": "python_semantic_detector", "error": detection.error},
        }
    match = _find_matching_god_class_finding(
        detection.findings.get("god_class", []),
        target_file=target.file_path,
        project_root=config.project_root,
        class_name=target_class,
        line=target.line,
    )
    if match:
        current_metrics = _parse_god_class_metrics(match.evidence)
        metric_delta = {
            name: current_metrics[name] - baseline_metrics[name]
            for name in sorted(set(baseline_metrics).intersection(current_metrics))
        }
        return {
            "type": "god_class",
            "success": False,
            "message": (
                "god_class guard: Python semantic detector still reports "
                f"{target.project_path}#{match.class_name}. evidence: {match.evidence}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": match.file,
                "class_name": match.class_name,
                "begin_line": match.begin_line,
                "end_line": match.end_line,
                "score": match.score,
                "rule_id": match.rule_id,
                "evidence": match.evidence,
                "baseline_metrics": baseline_metrics,
                "current_metrics": current_metrics,
                "metric_delta": metric_delta,
            },
        }
    return {
        "type": "god_class",
        "success": True,
        "message": (
            "god_class guard: Python semantic detector no longer reports "
            f"{target.project_path}#{target_class or target.line}."
        ),
        "details": {
            "detector": "python_semantic_detector",
            "file": str(target.project_path),
            "class_name": target_class,
            "baseline_metrics": baseline_metrics,
        },
    }


def _run_feature_envy_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext],
) -> Dict[str, object]:
    if config.language != "java" or not config.locations:
        return {
            "type": "feature_envy",
            "success": False,
            "message": "feature_envy guard only supports Java detector-backed validation.",
            "details": {"detector": "python_semantic_detector", "language": config.language},
        }
    target = config.locations[0]
    detection = run_java_semantic_detector(config.project_root)
    if not detection.ok:
        return {
            "type": "feature_envy",
            "success": False,
            "message": f"feature_envy guard: semantic detector unavailable: {detection.error}",
            "details": {"detector": "python_semantic_detector", "error": detection.error},
        }
    findings = detection.findings.get("feature_envy", [])
    match = find_matching_semantic_finding(
        findings,
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
    )
    if match:
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                "feature_envy guard: Python semantic detector still reports "
                f"{target.project_path}#{target.method or target.line}. evidence: {match.evidence}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "file": match.file,
                "method": match.method,
                "begin_line": match.begin_line,
                "evidence": match.evidence,
                "old_target_missing": False,
            },
        }
    if _target_method_exists(config, target):
        return {
            "type": "feature_envy",
            "success": True,
            "message": (
                "feature_envy guard: Python semantic detector no longer reports "
                f"{target.project_path}#{target.method or target.line}."
            ),
            "details": {"detector": "python_semantic_detector", "old_target_missing": False},
        }

    if not target.file_path.exists():
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                "feature_envy guard: old target file is missing; deleting the target Java file "
                "is not accepted as a feature_envy fix."
            ),
            "details": {
                "detector": "python_semantic_detector",
                "old_target_missing": True,
                "target_file_missing": True,
                "file": target.project_path,
            },
        }

    if context is None:
        return _feature_envy_missing_baseline_failure(
            "feature_envy guard: old target method is missing, but no baseline context is available."
        )
    if not context.feature_envy_baseline_ok:
        error = context.feature_envy_baseline_error or "unknown baseline error"
        return _feature_envy_missing_baseline_failure(
            f"feature_envy guard: old target method is missing, but baseline capture failed: {error}"
        )

    changed_rel_paths = _changed_java_rel_paths(config.project_root, context.changed_java_files)
    baseline_keys = {_feature_envy_finding_key(item) for item in context.feature_envy_baseline_findings}
    new_findings = [
        finding
        for finding in findings
        if _normalize_path(finding.file) in changed_rel_paths
        and _feature_envy_finding_key(finding) not in baseline_keys
    ]
    if new_findings:
        first = new_findings[0]
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                "feature_envy guard: old target method is missing, but changed Java files contain "
                f"a new feature_envy finding at {first.file}#{first.method}. evidence: {first.evidence}"
            ),
            "details": {
                "detector": "python_semantic_detector",
                "old_target_missing": True,
                "changed_java_files": sorted(changed_rel_paths),
                "new_findings": [_semantic_finding_to_dict(item) for item in new_findings],
            },
        }
    return {
        "type": "feature_envy",
        "success": True,
        "message": (
            "feature_envy guard: old target method missing; no new feature_envy finding "
            "in changed Java files."
        ),
        "details": {
            "detector": "python_semantic_detector",
            "old_target_missing": True,
            "changed_java_files": sorted(changed_rel_paths),
            "baseline_finding_count": len(context.feature_envy_baseline_findings),
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guard_evidence(guard: Dict[str, object]) -> str:
    return str(guard.get("evidence") or "").strip()


def _class_from_evidence(evidence: str) -> str:
    match = re.search(r"\bclass=([^;,\s]+)", str(evidence or ""))
    return match.group(1).strip() if match else ""


def _parse_god_class_metrics(evidence: str) -> Dict[str, int]:
    return {
        name: int(value)
        for name, value in re.findall(r"\b(nom|nof|wmc|loc|atfd)=(\d+)\b", str(evidence or ""))
    }


def _normalize_class_name(value: str) -> str:
    return str(value or "").strip().rsplit(".", 1)[-1].lower()


def _find_matching_god_class_finding(
    findings: List[SemanticFinding],
    *,
    target_file: Path,
    project_root: Path,
    class_name: str,
    line: Optional[int],
) -> Optional[SemanticFinding]:
    target_rel = _normalize_rel_path(target_file, project_root)
    target_class = _normalize_class_name(class_name)
    candidates: List[SemanticFinding] = []
    for finding in findings:
        if _normalize_path(finding.file) != target_rel:
            continue
        finding_class = _normalize_class_name(finding.class_name or _class_from_evidence(finding.evidence))
        if target_class and finding_class != target_class:
            continue
        candidates.append(finding)
    if not candidates:
        return None
    if line:
        return min(candidates, key=lambda item: _class_line_distance(line, item))
    return candidates[0]


def _line_in_class_finding(line: int, finding: SemanticFinding) -> bool:
    return bool(finding.begin_line and finding.end_line and finding.begin_line <= line <= finding.end_line)


def _class_line_distance(line: int, finding: SemanticFinding) -> int:
    if _line_in_class_finding(line, finding):
        return 0
    return abs((finding.begin_line or 0) - line)


def _target_class_exists(config: ResolvedRunConfig, target_file: Path, class_name: str) -> Optional[bool]:
    target_rel = _normalize_rel_path(target_file, config.project_root)
    target_class = _normalize_class_name(class_name)
    if not target_class:
        return True
    try:
        model = _build_project_model(config.project_root, include_tests=True)
    except Exception:
        return None
    for cls in model.classes.values():
        if _normalize_path(cls.file) != target_rel:
            continue
        if _normalize_class_name(cls.class_name) == target_class:
            return True
    return False


def _feature_envy_missing_baseline_failure(message: str) -> Dict[str, object]:
    return {
        "type": "feature_envy",
        "success": False,
        "message": message,
        "details": {"detector": "python_semantic_detector", "old_target_missing": True},
    }


def _target_method_exists(config: ResolvedRunConfig, target) -> bool:
    if not target.file_path.exists() or target.file_path.suffix != ".java":
        return False
    try:
        return extract_snippet(target, config.language) is not None
    except Exception:
        return False


def _changed_java_rel_paths(project_root: Path, changed_java_files: List[Path]) -> set[str]:
    return {
        _normalize_rel_path(path, project_root)
        for path in changed_java_files
        if path.exists() and path.suffix == ".java"
    }


def _feature_envy_finding_key(finding: SemanticFinding | Dict[str, Any]) -> Tuple[str, str, str]:
    if isinstance(finding, dict):
        file = str(finding.get("file") or "")
        class_name = str(finding.get("class_name") or "")
        method = str(finding.get("method") or "")
    else:
        file = finding.file
        class_name = finding.class_name
        method = finding.method
    return (_normalize_path(file), class_name.strip().lower(), _normalize_method(method))


def _semantic_finding_to_dict(finding: SemanticFinding) -> Dict[str, object]:
    return {
        "smell_type": finding.smell_type,
        "file": finding.file,
        "class_name": finding.class_name,
        "method": finding.method,
        "begin_line": finding.begin_line,
        "end_line": finding.end_line,
        "score": finding.score,
        "rule_id": finding.rule_id,
        "evidence": finding.evidence,
    }


def _extract_mysterious_name(evidence: str) -> str:
    for key in ("param", "local", "name"):
        match = re.search(rf"\b{key}=([^;,\s]+)", evidence)
        if match:
            return match.group(1).strip()
    return ""


def _requires_unsupported_throw_removal(evidence: str) -> bool:
    return bool(
        re.search(
            r"\bexplicit_unsupported_throw(?:\s*=\s*(?:true|1|yes))?\b",
            evidence,
            flags=re.IGNORECASE,
        )
    )


def _requires_empty_override_removal(evidence: str) -> bool:
    return bool(
        re.search(
            r"(?:empty_override|unimplemented_contract|unimplemented_loader|resource_leak_contract)"
            r"(?:\s*=\s*(?:true|1|yes))?",
            evidence,
            flags=re.IGNORECASE,
        )
    )


def _target_method_unsupported_throw(config: ResolvedRunConfig, target) -> bool:
    try:
        snippet = extract_snippet(target, config.language)
    except Exception:
        return False
    if snippet is None:
        return False
    return bool(re.search(r"\bthrow\s+new\s+UnsupportedOperationException\b", snippet.body_text))


def _target_method_empty_override(config: ResolvedRunConfig, target) -> bool:
    try:
        snippet = extract_snippet(target, config.language)
    except Exception:
        return False
    if snippet is None:
        return False
    return count_meaningful_lines(snippet.body_text, config.language) == 0


def _target_method_returns_expected_state(
    config: ResolvedRunConfig,
    target,
    expected_state_field: str,
) -> bool:
    try:
        snippet = extract_snippet(target, config.language)
    except Exception:
        return False
    if snippet is None:
        return False
    body = strip_comments(snippet.body_text, config.language).strip()
    field = re.escape(expected_state_field)
    return bool(
        re.fullmatch(
            rf"\{{?\s*return\s+(?:this\.)?{field}\s*;\s*\}}?",
            body,
        )
    )


# ---------------------------------------------------------------------------
# Dispatch table (must come after all handler definitions)
# ---------------------------------------------------------------------------

_JAVA_GUARD_DISPATCH = {
    "data_clumps": lambda c, g: _run_semantic_guard(c, "data_clumps", _guard_evidence(g)),
    "dead_code": lambda c, g: _run_semantic_guard(c, "dead_code", _guard_evidence(g)),
    "god_class": _run_god_class_guard,
    "mysterious_name": _run_mysterious_name_guard,
    "refused_bequest": lambda c, g: _run_semantic_guard(c, "refused_bequest", _guard_evidence(g)),
}
