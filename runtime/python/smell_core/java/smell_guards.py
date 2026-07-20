"""Java-specific smell guard implementations.

These functions are called from the generic ``guards.run_smell_guards``
dispatcher when the target language is Java or the smell type requires
a Java-specific detector.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..analysis import count_meaningful_lines, extract_snippet
from ..config import ResolvedRunConfig
from ..guards.context import GuardRunContext
from .detector_utils import (
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
    parse_parent_from_evidence as _parse_parent_from_evidence,
)
from .data_clumps import (
    data_clump_group_from_evidence,
    data_clump_occurrence_threshold,
    detect_data_clump_occurrences,
)
from .semantic_detector import (
    SemanticFinding,
    _build_project_model,
    find_matching_semantic_finding,
    run_java_semantic_detector,
)
from .ast_ncss import run_ast_ncss
from .syntactic_detector import (
    _finding,
    find_matching_clone_pair,
    find_matching_syntactic_finding,
    load_project_model,
    parse_mysterious_evidence,
    run_java_syntactic_detector,
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


def run_java_clone_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Optional[Dict[str, object]]:
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
    return {
        "type": "code_clone_type1",
        "success": True,
        "message": "code_clone_type1 guard: Java syntactic detector no longer reports the target clone pair.",
        "details": {
            "detector": "java_syntactic_detector",
            "target_resolution": target_resolution,
            "left_clone_finding_found": left_finding is not None,
            "right_clone_finding_found": right_finding is not None,
        },
    }


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


def _run_semantic_guard(
    config: ResolvedRunConfig,
    guard_type: str,
    evidence: str,
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
    target = config.locations[0]
    if not target.file_path.exists() or target.file_path.suffix != ".java":
        return {
            "type": guard_type,
            "success": False,
            "message": f"{guard_type} guard: target file not found or not a .java file: {target.file_path}",
            "details": {"detector": "python_semantic_detector", "file": str(target.file_path)},
        }
    if guard_type == "data_clumps":
        return _run_data_clumps_group_guard(config, guard_type, evidence)
    detection = run_java_semantic_detector(config.project_root)
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
    if guard_type == "refused_bequest" and _requires_unsupported_throw_removal(evidence):
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
    if guard_type == "refused_bequest" and _requires_empty_override_removal(evidence):
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
