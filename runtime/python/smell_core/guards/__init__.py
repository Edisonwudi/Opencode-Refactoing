"""Generic smell guards and build/test verification.

Language-specific guard implementations register themselves via the
``registry`` module.  This module owns the top-level dispatch and
the language-agnostic text-analysis fallback path.
"""
from __future__ import annotations

import os
import re
import hashlib
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from ..analysis import (
    LANGUAGE_EXTENSIONS,
    count_meaningful_lines,
    count_parameters,
    estimate_complexity,
    estimate_switch_branches,
    extract_class_text,
    extract_pair_snippets,
    extract_snippet,
    method_basename,
    normalize_for_clone,
)
from ..config import CommandConfig, ResolvedRunConfig, interpolate_command_text
from ..data_clumps import (
    data_clump_group_from_evidence,
    data_clump_occurrence_threshold,
    detect_data_clump_occurrences,
)
from ..feature_envy import (
    analyze_feature_envy_target as analyze_generic_feature_envy_target,
    feature_envy_receiver_from_evidence,
)
from ..mysterious_name import (
    detect_mysterious_names as detect_generic_mysterious_names,
    find_matching_name_finding,
)
from ..checkpoint_contract import checkpoint_gate_result
from .context import GuardRunContext
from .registry import get_clone_guard, get_smell_guard, get_syntactic_guard

# Language-specific registrations are loaded lazily on first use to avoid
# pulling in optional heavy dependencies (e.g. tree_sitter) at import time.
_JAVA_REGISTERED = False


def _ensure_java_registered() -> None:
    global _JAVA_REGISTERED
    if not _JAVA_REGISTERED:
        from . import java_registration  # noqa: F401
        _JAVA_REGISTERED = True


SUMMARY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"BUILD FAILURE",
        r"FAILURE",
        r"There are test failures",
        r"Tests run:\s*\d+",
        r"Failed tests:",
        r"Exception",
        r"error:",
    ]
]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAILED_TEST_RE = re.compile(r"^\s*(?P<test>.+\s>\s.+)\s+FAILED\s*$")
MAVEN_TEST_FAILURE_RE = re.compile(
    r"^\[ERROR\]\s+(?P<test>[A-Za-z0-9_.$]+(?:Test|IT)[A-Za-z0-9_.$]*\.[^\s]+.*(?:»|:).*)$"
)
MAVEN_JAVAC_DIAGNOSTIC_RE = re.compile(
    r"^\[ERROR\]\s+(?P<file>.+?\.java):\[(?P<line>\d+),(?P<column>\d+)\]\s+(?P<message>.+)$"
)
PLAIN_JAVAC_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>.+?\.java):(?P<line>\d+):\s+(?P<message>.+)$"
)
JAVAC_CONTEXT_PREFIXES = (
    "需要:",
    "找到:",
    "原因:",
    "符号:",
    "位置:",
    "方法 ",
    "required:",
    "found:",
    "reason:",
    "symbol:",
    "location:",
    "method ",
    "class ",
)
CRITICAL_FAILURE_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"\b(?:NoSuchMethodException|NoSuchMethodError|ClassNotFoundException)\b",
        r"\b(?:AssertionError|ComparisonFailure)\b",
        r"\b(?:NullPointerException|IllegalArgumentException|IllegalStateException)\b",
        r"\bCompilation failed\b",
        r"\berror:",
        r"错误:",
        r"^\s*Caused by:\s+",
    ]
]


def run_smell_guards(config: ResolvedRunConfig, context: Optional[GuardRunContext] = None) -> List[Dict[str, object]]:
    if config.language == "java":
        _ensure_java_registered()
    outcomes: List[Dict[str, object]] = []
    for guard in config.profile.guards:
        guard_type = str(guard.get("type", "")).strip()

        if (
            context is not None
            and context.checkpoint_required
            and context.checkpoint_smell == guard_type
        ):
            checkpoint_failure = checkpoint_gate_result(guard_type, context.checkpoint)
            if checkpoint_failure is not None:
                outcomes.append(checkpoint_failure)
                continue

        # --- Language-specific smell guard (registered via registry) ---
        smell_handler = get_smell_guard(config.language)
        if smell_handler is not None:
            result = smell_handler(config, guard, context)
            if result is not None:
                outcomes.append(result)
                continue

        # --- Language-agnostic smell types ---
        if guard_type == "long_method":
            outcomes.append(_run_long_method_guard(config, guard))
        elif guard_type == "long_parameter_list":
            outcomes.append(_run_long_parameter_list_guard(config, guard))
        elif guard_type == "nested_complexity":
            outcomes.append(_run_nested_complexity_guard(config, guard))
        elif guard_type == "switch_statements":
            outcomes.append(_run_switch_statements_guard(config, guard))
        elif guard_type == "code_clone_type1":
            outcomes.append(_run_code_clone_guard(config, guard, context))
        elif guard_type == "data_clumps":
            outcomes.append(_run_data_clumps_guard(config, guard))
        elif guard_type == "feature_envy" and config.language != "java":
            outcomes.append(_run_generic_feature_envy_guard(config, guard))
        elif guard_type == "mysterious_name":
            # Java is intercepted by the registered smell handler above; this
            # generic branch only ever serves non-Java languages.
            outcomes.append(_run_generic_mysterious_name_guard(config, guard))
        elif guard_type == "dead_code":
            outcomes.append(_run_dead_code_guard(config, guard))
        elif guard_type == "god_class" and config.language != "java":
            outcomes.append(_run_generic_god_class_guard(config, guard, context))
        else:
            outcomes.append(
                {
                    "type": guard_type or "unknown",
                    "success": False,
                    "message": f"Unknown guard type '{guard_type}'.",
                    "details": None,
                }
            )
    return outcomes


def _run_generic_god_class_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext],
) -> Dict[str, object]:
    text = extract_class_text(config.locations[0], config.language) if config.locations else None
    loc = count_meaningful_lines(text or "", config.language)
    checkpoint_ready = bool(context and context.checkpoint_required)
    # A god-class repair must move a meaningful share of the class out; a token
    # extraction of a few lines is not a repair.  The reduction is the one the
    # checkpoint contract computed against the immutable baseline.
    min_reduction = float(guard.get("min_relative_reduction", 0.05))
    relative_reduction = god_class_relative_reduction(context)
    reduction_ok = relative_reduction >= min_reduction
    success = text is not None and checkpoint_ready and reduction_ok
    if success:
        message = (
            f"Class metric reduced to {loc} LOC "
            f"(-{relative_reduction:.1%} vs baseline, threshold {min_reduction:.0%})."
        )
    elif not reduction_ok:
        message = (
            f"god_class guard: class metric reduction {relative_reduction:.1%} is below the "
            f"required {min_reduction:.0%}; move a cohesive responsibility cluster out of the class."
        )
    else:
        message = "God-class verification requires a measurable target and the checkpoint reduction contract."
    return {
        "type": "god_class",
        "success": success,
        "message": message,
        "details": {
            "class_loc": loc,
            "checkpoint_required": checkpoint_ready,
            "relative_reduction": round(relative_reduction, 6),
            "min_relative_reduction": min_reduction,
        },
    }


def god_class_relative_reduction(context: Optional[GuardRunContext]) -> float:
    """Relative class_loc reduction from the checkpoint delta (0 when unavailable)."""
    delta = getattr(context, "metric_delta", None) if context is not None else None
    if not isinstance(delta, dict):
        return 0.0
    objectives = delta.get("objectives")
    if not isinstance(objectives, dict):
        return 0.0
    values = objectives.get("class_loc")
    if not isinstance(values, dict):
        return 0.0
    reduction = values.get("relative_reduction")
    if isinstance(reduction, bool) or not isinstance(reduction, (int, float)):
        return 0.0
    return max(0.0, float(reduction))


def run_build_test_guard(config: ResolvedRunConfig) -> Dict[str, object]:
    metadata = _verification_metadata(config)
    if config.verification_mode == "sample_optimized" and not str(config.sample_test_command or "").strip():
        return {
            "type": "build_test",
            "success": False,
            "message": "Sample-level test command is required for sample_optimized verification.",
            **metadata,
            "details": {
                "build": None,
                "test": {
                    "label": "test",
                    "success": False,
                    "status": "missing",
                    "returncode": None,
                    "summary": [],
                    "failure_highlights": ["Sample-level test command is missing."],
                    "diagnostics": [],
                    "tail": [],
                    "summary_text": "Sample-level test command is missing.",
                    "output": "",
                    "source": config.test_source,
                },
            },
        }
    build_result = None
    test_result = None
    if config.defaults.run_build:
        build_result = _run_command_config(
            config.build,
            cwd=config.cwd,
            env=config.env,
            label="build",
            project_root=config.project_root,
            source=config.build_source,
        )
        if not build_result["success"]:
            return {
                "type": "build_test",
                "success": False,
                "message": f"Build failed. {build_result['summary_text']}",
                **metadata,
                "details": {"build": build_result, "test": None},
            }
    if config.defaults.run_tests:
        test_cwd = config.dataset_root if config.test_source == "dataset" else config.cwd
        test_started_ns = time.time_ns()
        test_result = _run_command_config(
            config.test,
            cwd=test_cwd,
            env=config.env,
            label="test",
            project_root=config.project_root,
            source=config.test_source,
        )
        if not test_result["success"]:
            message = f"Tests failed. {test_result['summary_text']}"
            if config.verification_mode == "sample_optimized":
                message = f"Sample test failed. {test_result['summary_text']}"
            return {
                "type": "build_test",
                "success": False,
                "message": message,
                **metadata,
                "details": {"build": build_result, "test": test_result},
            }
        if config.verification_mode == "sample_optimized":
            execution = _sample_test_execution_evidence(config, test_started_ns)
            test_result["execution_evidence"] = execution
            if not execution["success"]:
                test_result["success"] = False
                test_result["status"] = "test_not_executed"
                test_result["failure_highlights"] = [
                    str(execution["message"])
                ]
                test_result["summary_text"] = str(execution["message"])
                return {
                    "type": "build_test",
                    "success": False,
                    "message": f"Sample test failed. {execution['message']}",
                    **metadata,
                    "details": {"build": build_result, "test": test_result},
                }
    return {
        "type": "build_test",
        "success": True,
        "message": _build_success_message(build_result, test_result),
        **metadata,
        "details": {"build": build_result, "test": test_result},
    }


def _verification_metadata(config: ResolvedRunConfig) -> Dict[str, object]:
    return {
        "verification_mode": config.verification_mode,
        "build_source": config.build_source,
        "test_source": config.test_source,
        "test_location": config.sample_test_location if config.test_source == "dataset" else "",
        "test_command_hash": _command_hash(config.sample_test_command)
        if config.test_source == "dataset"
        else "",
    }


def _command_hash(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sample_test_execution_evidence(
    config: ResolvedRunConfig,
    started_ns: int,
) -> Dict[str, object]:
    """Require a fresh JUnit XML report for the pinned sample test class."""
    test_class = Path(str(config.sample_test_location or "")).stem
    if not test_class:
        return {
            "success": False,
            "message": "Pinned sample test location does not identify a test class.",
            "test_class": "",
            "reports": [],
            "tests": 0,
        }
    reports: List[str] = []
    executed = 0
    skipped_total = 0
    for report in config.project_root.rglob(f"TEST-*{test_class}.xml"):
        try:
            if report.stat().st_mtime_ns < started_ns:
                continue
            root = ET.parse(report).getroot()
        except (OSError, ET.ParseError):
            continue
        tests_text = str(root.attrib.get("tests") or "").strip()
        try:
            tests = int(tests_text)
        except ValueError:
            tests = len(root.findall(".//testcase"))
        try:
            skipped = int(str(root.attrib.get("skipped") or "0"))
        except ValueError:
            skipped = len(root.findall(".//testcase/skipped"))
        non_skipped = max(tests - skipped, 0)
        if non_skipped <= 0:
            continue
        reports.append(str(report.relative_to(config.project_root)))
        executed += non_skipped
        skipped_total += skipped
    return {
        "success": executed > 0,
        "message": (
            f"Pinned sample test {test_class} executed {executed} test(s)."
            if executed > 0
            else f"Pinned sample test {test_class} produced no fresh non-empty JUnit report."
        ),
        "test_class": test_class,
        "reports": sorted(reports),
        "tests": executed,
        "skipped": skipped_total,
    }


def _run_long_method_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_lines = int(guard.get("max_lines", 60))
    syntactic_handler = get_syntactic_guard(config.language)
    if syntactic_handler is not None:
        syntactic = syntactic_handler(
            config,
            "long_method",
            {"long_method_ncss": max_lines},
            str(guard.get("evidence", "")),
        )
        if syntactic is not None:
            return syntactic
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "long_method",
            "success": False,
            "message": "Unable to resolve the target method or function.",
            "details": None,
        }
    line_count = count_meaningful_lines(snippet.body_text, config.language)
    success = line_count <= max_lines
    return {
        "type": "long_method",
        "success": success,
        "message": f"Target has {line_count} meaningful lines (threshold {max_lines}).",
        "details": {"line_count": line_count, "max_lines": max_lines},
    }


def _run_long_parameter_list_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_params = int(guard.get("max_params", 5))
    syntactic_handler = get_syntactic_guard(config.language)
    if syntactic_handler is not None:
        syntactic = syntactic_handler(
            config,
            "long_parameter_list",
            {"long_parameter_list": max_params},
            str(guard.get("evidence", "")),
        )
        if syntactic is not None:
            return syntactic
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "long_parameter_list",
            "success": False,
            "message": "Unable to resolve the target method or function signature.",
            "details": None,
        }
    param_count = count_parameters(snippet.signature_text, config.language)
    success = param_count <= max_params
    return {
        "type": "long_parameter_list",
        "success": success,
        "message": f"Target has {param_count} parameters (threshold {max_params}).",
        "details": {"param_count": param_count, "max_params": max_params},
    }


def _run_nested_complexity_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_complexity = int(guard.get("max_complexity", 20))
    syntactic_handler = get_syntactic_guard(config.language)
    if syntactic_handler is not None:
        syntactic = syntactic_handler(
            config,
            "nested_complexity",
            {"cognitive_complexity": max_complexity},
            str(guard.get("evidence", "")),
        )
        if syntactic is not None:
            return syntactic
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "nested_complexity",
            "success": False,
            "message": "Unable to resolve the target method or function body.",
            "details": None,
        }
    complexity = estimate_complexity(snippet, config.language)
    success = complexity <= max_complexity
    return {
        "type": "nested_complexity",
        "success": success,
        "message": f"Target has estimated complexity {complexity} (threshold {max_complexity}).",
        "details": {"complexity": complexity, "max_complexity": max_complexity},
    }


def _run_switch_statements_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_branches = int(guard.get("max_branches", 12))
    syntactic_handler = get_syntactic_guard(config.language)
    if syntactic_handler is not None:
        syntactic = syntactic_handler(
            config,
            "switch_statements",
            {
                "switch_case_count": max_branches,
                "switch_density": float(guard.get("max_density", 10.0)),
            },
            str(guard.get("evidence", "")),
        )
        if syntactic is not None:
            return syntactic
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "switch_statements",
            "success": False,
            "message": "Unable to resolve the target method or function body.",
            "details": None,
        }
    branch_count = estimate_switch_branches(snippet, config.language)
    success = branch_count <= max_branches
    return {
        "type": "switch_statements",
        "success": success,
        "message": f"Target has switch-style branch count {branch_count} (threshold {max_branches}).",
        "details": {"branch_count": branch_count, "max_branches": max_branches},
    }


def _run_data_clumps_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    evidence = str(guard.get("evidence") or "").strip()
    target_group = data_clump_group_from_evidence(evidence)
    if not target_group:
        return {
            "type": "data_clumps",
            "success": False,
            "message": "data_clumps guard: missing group=... evidence; cannot validate the clump family.",
            "details": {"detector": "generic_parameter_group_detector"},
        }
    analysis = detect_data_clump_occurrences(
        config.project_root,
        language=config.language,
        evidence=evidence,
        limit=20,
    )
    if not analysis.get("success"):
        return {
            "type": "data_clumps",
            "success": False,
            "message": f"data_clumps guard: generic detector unavailable: {analysis.get('error', '')}",
            "details": {
                "detector": "generic_parameter_group_detector",
                "group": target_group,
                "error": analysis.get("error", ""),
            },
        }
    occurrence_count = int(analysis.get("occurrence_count") or 0)
    threshold = int(guard.get("min_occurrences") or data_clump_occurrence_threshold())
    if occurrence_count >= threshold:
        remaining_occurrences = list(analysis.get("occurrences") or [])
        first = remaining_occurrences[0] if remaining_occurrences else {}
        return {
            "type": "data_clumps",
            "success": False,
            "message": (
                "data_clumps guard: generic parameter detector still reports "
                f"group={target_group} across {occurrence_count} occurrence(s). "
                f"first remaining: {first.get('file')}#{first.get('method')}."
            ),
            "details": {
                "detector": "generic_parameter_group_detector",
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
        "type": "data_clumps",
        "success": True,
        "message": (
            f"data_clumps guard: group={target_group} is below the repeated-occurrence threshold "
            f"({occurrence_count}/{threshold})."
        ),
        "details": {
            "detector": "generic_parameter_group_detector",
            "group": target_group,
            "occurrence_count": occurrence_count,
            "occurrence_threshold": threshold,
        },
    }


def _run_generic_feature_envy_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    target = config.locations[0] if config.locations else None
    if target is None:
        return {
            "type": "feature_envy",
            "success": False,
            "message": "feature_envy guard: missing target location.",
            "details": {"detector": "tree_sitter_generic"},
        }
    evidence = str(guard.get("evidence") or "")
    expected_receiver = feature_envy_receiver_from_evidence(evidence)
    try:
        profile = analyze_generic_feature_envy_target(
            config.project_root,
            language=config.language,
            target_file=target.file_path,
            method=target.method,
            line=target.line,
            expected_receiver=expected_receiver,
        )
    except Exception as exc:
        return {
            "type": "feature_envy",
            "success": False,
            "message": f"feature_envy guard: generic detector unavailable: {exc}",
            "details": {"detector": "tree_sitter_generic", "error": str(exc)},
        }
    if not profile.get("ok"):
        return {
            "type": "feature_envy",
            "success": True,
            "message": "feature_envy guard: the reported target no longer resolves.",
            "details": {"detector": "tree_sitter_generic", "error": profile.get("error", "")},
        }
    dominant = str(profile.get("dominant_receiver_type") or "")
    dominant_count = int(profile.get("dominant_receiver_access") or 0)
    ratio = float(profile.get("dominant_receiver_ratio") or 0.0)
    details = {
        "detector": "tree_sitter_generic",
        "dominant_receiver": dominant,
        "dominant_receiver_access": dominant_count,
        "dominant_receiver_ratio": ratio,
        "method_loc": profile.get("method_loc"),
        "strict_detector_hit": bool(profile.get("strict_detector_hit")),
    }
    if profile.get("strict_detector_hit"):
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                f"feature_envy guard: target still accesses foreign receiver '{dominant}' "
                f"{dominant_count} time(s) ({ratio:.0%} of member accesses); move that logic "
                "to the envied receiver or reduce the accesses below the detector thresholds."
            ),
            "details": details,
        }
    return {
        "type": "feature_envy",
        "success": True,
        "message": (
            f"feature_envy guard: strict detector no longer flags the target "
            f"(dominant receiver '{dominant}' {dominant_count} access(es), ratio {ratio:.0%})."
        ),
        "details": details,
    }


def _run_generic_mysterious_name_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    from ..java.syntactic_detector import parse_mysterious_evidence

    evidence = str(guard.get("evidence") or "")
    kind, name = parse_mysterious_evidence(evidence)
    if not name:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": "mysterious_name guard: missing kind=...; name=... evidence; cannot validate the rename.",
            "details": {"detector": "tree_sitter_generic"},
        }
    target = config.locations[0] if config.locations else None
    if target is None or not target.file_path.is_file():
        return {
            "type": "mysterious_name",
            "success": True,
            "message": f"mysterious_name guard: the target of reported {kind or 'name'} '{name}' no longer resolves.",
            "details": {"detector": "tree_sitter_generic", "target_kind": kind, "target_name": name},
        }
    try:
        findings = detect_generic_mysterious_names(target.file_path, language=config.language)
    except Exception as exc:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": f"mysterious_name guard: generic detector unavailable: {exc}",
            "details": {"detector": "tree_sitter_generic", "error": str(exc)},
        }
    try:
        snippet = extract_snippet(target, config.language)
    except Exception:
        snippet = None
    if snippet is None:
        return {
            "type": "mysterious_name",
            "success": True,
            "message": f"mysterious_name guard: the function owning reported {kind or 'name'} '{name}' no longer resolves.",
            "details": {"detector": "tree_sitter_generic", "target_kind": kind, "target_name": name},
        }
    match = find_matching_name_finding(
        findings,
        kind=kind,
        name=name,
        scope=(snippet.start_line, snippet.end_line),
    )
    if match is not None:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": (
                f"mysterious_name guard: reported {match.kind} '{name}' is still present "
                f"({match.reason}); rename it to a descriptive identifier."
            ),
            "details": {
                "detector": "tree_sitter_generic",
                "target_kind": kind,
                "target_name": name,
                "finding": match.evidence,
                "line": match.line,
            },
        }
    return {
        "type": "mysterious_name",
        "success": True,
        "message": f"mysterious_name guard: reported {kind or 'name'} '{name}' no longer appears in the target.",
        "details": {"detector": "tree_sitter_generic", "target_kind": kind, "target_name": name},
    }


def _run_dead_code_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    target = config.locations[0] if config.locations else None
    if target is None:
        return {
            "type": "dead_code",
            "success": False,
            "message": "dead_code guard: missing target location.",
            "details": {"detector": "generic_dead_code_guard"},
        }
    name = _dead_code_target_name(config, guard)
    if not name:
        return {
            "type": "dead_code",
            "success": False,
            "message": "dead_code guard: unable to resolve the reported member name.",
            "details": {"detector": "generic_dead_code_guard", "target_found": None},
        }
    if not target.file_path.exists():
        return _dead_code_target_removed_result(name)
    try:
        snippet = extract_snippet(target, config.language)
    except Exception as exc:
        return {
            "type": "dead_code",
            "success": False,
            "message": f"dead_code guard: unable to inspect target: {exc}",
            "details": {"detector": "generic_dead_code_guard", "target": name, "target_found": None},
        }
    if snippet is None:
        return _dead_code_target_removed_result(name)
    references = _find_dead_code_references(
        config.project_root,
        config.language,
        target.file_path,
        name,
        snippet.start_line,
        snippet.end_line,
    )
    if references:
        return {
            "type": "dead_code",
            "success": False,
            "message": (
                f"dead_code guard: reported target `{name}` still exists and has "
                f"{len(references)} project-local reference(s); safe delete is blocked."
            ),
            "details": {
                "detector": "generic_dead_code_guard",
                "target": name,
                "target_found": True,
                "reference_count": len(references),
                "references": references[:20],
                "references_truncated": len(references) > 20,
            },
        }
    return {
        "type": "dead_code",
        "success": False,
        "message": f"dead_code guard: reported unused target `{name}` still exists.",
        "details": {
            "detector": "generic_dead_code_guard",
            "target": name,
            "target_found": True,
            "reference_count": 0,
            "references": [],
        },
    }


def _dead_code_target_removed_result(name: str) -> Dict[str, object]:
    return {
        "type": "dead_code",
        "success": True,
        "message": f"dead_code guard: reported target `{name}` no longer resolves.",
        "details": {
            "detector": "generic_dead_code_guard",
            "target": name,
            "target_found": False,
            "reference_count": 0,
            "references": [],
        },
    }


def _dead_code_target_name(config: ResolvedRunConfig, guard: Dict[str, object]) -> str:
    if config.locations:
        name = method_basename(config.locations[0].method)
        if name:
            return name
    evidence = str(guard.get("evidence") or "")
    for key in ("method", "function", "member", "name"):
        match = re.search(rf"(?:^|;\s*){key}=([^;]+)", evidence)
        if match:
            name = method_basename(match.group(1).strip())
            if name:
                return name
    return ""


def _find_dead_code_references(
    project_root: Path,
    language: str,
    target_file: Path,
    target_name: str,
    target_start_line: int,
    target_end_line: int,
) -> list[dict[str, object]]:
    references: list[dict[str, object]] = []
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(target_name)}(?![A-Za-z0-9_])")
    for source_path in _iter_dead_code_source_files(project_root, language):
        try:
            raw_text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_number, line in enumerate(_strip_comments_preserving_lines(raw_text, language), start=1):
            if source_path == target_file and target_start_line <= line_number <= target_end_line:
                continue
            if not pattern.search(line):
                continue
            references.append(
                {
                    "file": str(source_path.relative_to(project_root)),
                    "line": line_number,
                    "text": line.strip(),
                }
            )
    return references


def _iter_dead_code_source_files(project_root: Path, language: str):
    extensions = LANGUAGE_EXTENSIONS.get(language, set())
    if not extensions:
        return
    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".pytest_cache",
        "node_modules",
        "build",
        "dist",
        "target",
    }
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in ignored_dirs for part in path.relative_to(project_root).parts[:-1]):
            continue
        yield path


def _strip_comments_preserving_lines(text: str, language: str) -> list[str]:
    if language == "python":
        return [line.split("#", 1)[0] for line in text.splitlines()]
    lines: list[str] = []
    in_block = False
    for raw_line in text.splitlines():
        index = 0
        cleaned = ""
        while index < len(raw_line):
            if in_block:
                end = raw_line.find("*/", index)
                if end < 0:
                    index = len(raw_line)
                    continue
                in_block = False
                index = end + 2
                continue
            line_comment = raw_line.find("//", index)
            block_comment = raw_line.find("/*", index)
            if line_comment >= 0 and (block_comment < 0 or line_comment < block_comment):
                cleaned += raw_line[index:line_comment]
                break
            if block_comment >= 0:
                cleaned += raw_line[index:block_comment]
                in_block = True
                index = block_comment + 2
                continue
            cleaned += raw_line[index:]
            break
        lines.append(cleaned)
    return lines


def _run_code_clone_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Dict[str, object]:
    clone_handler = get_clone_guard(config.language)
    if clone_handler is not None:
        syntactic = clone_handler(config, guard, context)
        if syntactic is not None:
            return syntactic
    first, second = extract_pair_snippets(config.locations, config.language)
    if len(config.locations) >= 2 and (not first or not second):
        return {
            "type": "code_clone_type1",
            "success": True,
            "message": "One or both original clone targets no longer resolve after refactoring.",
            "details": {
                "target_resolution": "partial" if first or second else "none",
                "first_found": first is not None,
                "second_found": second is not None,
            },
        }
    if not first or not second:
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": "Unable to resolve both clone targets.",
            "details": {
                "target_resolution": "invalid_location",
                "target_count": len(config.locations),
                "first_found": first is not None,
                "second_found": second is not None,
            },
        }
    first_normalized = normalize_for_clone(first.body_text, config.language)
    second_normalized = normalize_for_clone(second.body_text, config.language)
    still_clone = bool(first_normalized) and first_normalized == second_normalized
    return {
        "type": "code_clone_type1",
        "success": not still_clone,
        "message": (
            "The target blocks still normalize to the same implementation."
            if still_clone
            else "The target blocks no longer normalize to the same implementation."
        ),
        "details": {
            "first_length": len(first_normalized),
            "second_length": len(second_normalized),
        },
    }


def _run_command_config(
    command_config: CommandConfig,
    *,
    cwd: Path,
    env: Dict[str, str],
    label: str,
    project_root: Path,
    source: str = "",
) -> Dict[str, object]:
    rendered_command = ""
    rendered_script = ""
    if command_config.script:
        rendered_script = interpolate_command_text(command_config.script, project_root)
        command, shell = _build_script_command(
            rendered_script,
            label,
        )
    elif command_config.command:
        rendered_command = interpolate_command_text(command_config.command, project_root)
        command, shell = rendered_command, True
    else:
        return {
            "label": label,
            "success": True,
            "status": "skipped",
            "returncode": 0,
            "command": "",
            "script": "",
            "cwd": str(cwd),
            "source": source,
            "summary": [],
            "failure_highlights": [],
            "diagnostics": [],
            "tail": [],
            "summary_text": f"No configured {label} command.",
            "output": "",
        }
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        env={**os.environ, **env},
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    output = proc.stdout or ""
    command_summary = _summarize_command_output(output, label=label, returncode=proc.returncode)
    return {
        "label": label,
        "success": proc.returncode == 0,
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "command": rendered_command,
        "script": rendered_script,
        "cwd": str(cwd),
        "source": source,
        "summary": command_summary["summary"],
        "failure_highlights": command_summary["failure_highlights"],
        "diagnostics": command_summary["diagnostics"],
        "tail": command_summary["tail"],
        "summary_text": command_summary["summary_text"],
        "output": output,
    }


def _summarize_command_output(output: str, *, label: str, returncode: int) -> Dict[str, object]:
    lines = [_clean_log_line(line) for line in (output or "").splitlines()]
    summary = [line for line in lines if line and any(pattern.search(line) for pattern in SUMMARY_PATTERNS)]
    diagnostics = _extract_javac_diagnostics(lines)
    diagnostic_highlights = [str(diagnostic["highlight"]) for diagnostic in diagnostics]
    failure_highlights = _dedupe_lines(diagnostic_highlights + _extract_failure_highlights(lines))
    prioritized = _dedupe_lines(failure_highlights + summary)
    tail = lines[-20:]
    if diagnostic_highlights:
        summary_text = " | ".join(diagnostic_highlights[:3])
    elif failure_highlights:
        summary_text = " | ".join(failure_highlights[:3])
    elif summary:
        summary_text = summary[-1]
    else:
        summary_text = tail[-1] if tail else f"{label} command returned {returncode}"
    return {
        "summary": prioritized[:8],
        "failure_highlights": failure_highlights,
        "diagnostics": diagnostics,
        "tail": tail,
        "summary_text": summary_text,
    }


def _extract_javac_diagnostics(lines: List[str]) -> List[Dict[str, object]]:
    diagnostics: List[Dict[str, object]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        diagnostic = _parse_javac_diagnostic_line(stripped)
        if diagnostic is None:
            continue
        diagnostic["context"] = _javac_context_lines(lines, index + 1)
        diagnostic["highlight"] = _format_javac_diagnostic(diagnostic)
        diagnostics.append(diagnostic)
    return _dedupe_diagnostics(diagnostics)[:12]


def _parse_javac_diagnostic_line(line: str) -> Optional[Dict[str, object]]:
    maven = MAVEN_JAVAC_DIAGNOSTIC_RE.match(line)
    if maven:
        return {
            "tool": "javac",
            "format": "maven",
            "file": maven.group("file"),
            "line": int(maven.group("line")),
            "column": int(maven.group("column")),
            "message": maven.group("message").strip(),
        }
    plain = PLAIN_JAVAC_DIAGNOSTIC_RE.match(line)
    if plain:
        return {
            "tool": "javac",
            "format": "plain",
            "file": plain.group("file"),
            "line": int(plain.group("line")),
            "column": None,
            "message": plain.group("message").strip(),
        }
    return None


def _javac_context_lines(lines: List[str], start_index: int) -> List[str]:
    context: List[str] = []
    for raw_line in lines[start_index : start_index + 6]:
        stripped = raw_line.strip()
        if not stripped:
            if context:
                break
            continue
        if _parse_javac_diagnostic_line(stripped) is not None:
            break
        text = stripped
        if text.startswith("[ERROR]"):
            text = text[len("[ERROR]") :].strip()
        lowered = text.lower()
        if (
            text == "^"
            or text.startswith("^")
            or text.startswith(JAVAC_CONTEXT_PREFIXES)
            or lowered.startswith(JAVAC_CONTEXT_PREFIXES)
        ):
            context.append(text)
        elif context:
            break
        else:
            break
        if len(context) >= 4:
            break
    return context


def _format_javac_diagnostic(diagnostic: Dict[str, object]) -> str:
    location = f"{diagnostic['file']}:{diagnostic['line']}:"
    if diagnostic.get("column") is not None:
        location = f"{diagnostic['file']}:[{diagnostic['line']},{diagnostic['column']}]"
    parts = [f"{location} {diagnostic['message']}"]
    parts.extend(str(item) for item in diagnostic.get("context", []))
    return " | ".join(parts)


def _dedupe_diagnostics(diagnostics: List[Dict[str, object]]) -> List[Dict[str, object]]:
    seen = set()
    deduped: List[Dict[str, object]] = []
    for diagnostic in diagnostics:
        key = (
            diagnostic.get("file"),
            diagnostic.get("line"),
            diagnostic.get("column"),
            diagnostic.get("message"),
            tuple(diagnostic.get("context", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)
    return deduped


def _extract_failure_highlights(lines: List[str]) -> List[str]:
    failed_test_highlights: List[str] = []
    standalone_highlights: List[str] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        failed_test = FAILED_TEST_RE.match(line)
        if failed_test:
            failed_test_highlights.append(line.strip())
            failed_test_highlights.extend(_nearby_failure_causes(lines, index + 1))
            continue
        maven_failure = MAVEN_TEST_FAILURE_RE.match(line.strip())
        if maven_failure:
            failed_test_highlights.append(maven_failure.group("test").strip())
            continue
        if any(pattern.search(line) for pattern in CRITICAL_FAILURE_PATTERNS):
            standalone_highlights.append(line.strip())
    return _dedupe_lines(failed_test_highlights + standalone_highlights)[:12]


def _nearby_failure_causes(lines: List[str], start_index: int) -> List[str]:
    causes: List[str] = []
    for line in lines[start_index : start_index + 20]:
        stripped = line.strip()
        if not stripped:
            if causes:
                break
            continue
        if (
            any(pattern.search(stripped) for pattern in CRITICAL_FAILURE_PATTERNS)
            or stripped.startswith("java.")
            or stripped.startswith("org.")
            or stripped.startswith("com.")
            or stripped.startswith("at ")
        ):
            causes.append(stripped)
        if len(causes) >= 5:
            break
    return causes


def _dedupe_lines(lines: List[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for line in lines:
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _clean_log_line(line: str) -> str:
    return ANSI_ESCAPE_RE.sub("", line).rstrip()


def _build_script_command(script: str, label: str) -> tuple:
    suffix = ".cmd" if os.name == "nt" else ".sh"
    temp_dir = Path(tempfile.mkdtemp(prefix=f"smell-core-{label}-"))
    script_path = temp_dir / f"{label}{suffix}"
    script_path.write_text(script if script.endswith("\n") else script + "\n", encoding="utf-8")
    if os.name != "nt":
        script_path.chmod(0o700)
        return f"sh {script_path}", True
    return str(script_path), False

def _build_success_message(build_result: Optional[Dict[str, object]], test_result: Optional[Dict[str, object]]) -> str:
    parts = []
    if build_result and build_result["status"] != "skipped":
        parts.append("build passed")
    if test_result and test_result["status"] != "skipped":
        parts.append("tests passed")
    if not parts:
        return "Build/test verification skipped."
    return " and ".join(parts).capitalize() + "."
