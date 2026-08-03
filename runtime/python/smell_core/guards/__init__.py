"""Generic non-Java smell guards and build/test verification.

Java product smells are accepted only through their frozen checkpoint
contract.  The helpers in this module are the language-agnostic detector path
used by non-Java profiles; they are deliberately not a Java fallback.
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
    python_switch_metrics,
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
from ..checkpoint_contract import checkpoint_gate_result
from .context import GuardRunContext


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

GRADLE_COMMAND_RE = re.compile(
    r"(?<![\w./-])"
    r"(?P<launcher>(?:\./)?gradlew(?:\.bat)?|gradle)"
    r"(?![\w.-])"
    r"(?P<body>(?:\\\r?\n|[^\n;&|])*)"
)
GRADLE_TEST_TASK_RE = re.compile(
    r"(?<!\S)"
    r"(?P<task>(?:(?::[A-Za-z0-9_.-]+)+:test|:test|test))"
    r"(?=\s|$)"
)


def validate_java_strict_verification_contract(
    config: ResolvedRunConfig,
) -> List[Dict[str, str]]:
    """Return Java-only configuration violations for strict verification.

    This validation intentionally lives below the bridge so direct Python
    callers cannot obtain a Java PASS by omitting a smell guard, disabling a
    build phase, or relying on ``_run_command_config``'s generic skipped result.
    Non-Java configurations retain their existing behavior.
    """
    if str(getattr(config, "language", "")).strip().lower() != "java":
        return []

    violations: List[Dict[str, str]] = []
    smell = str(getattr(config, "smell", "")).strip()
    profile = getattr(config, "profile", None)
    guards = list(getattr(profile, "guards", []) or [])
    if len(guards) != 1:
        violations.append({
            "code": "JAVA_GUARD_COUNT_INVALID",
            "message": f"Java requires exactly one smell guard; configured {len(guards)}.",
        })
    else:
        guard_type = str(guards[0].get("type", "")).strip()
        if not smell or guard_type != smell:
            violations.append({
                "code": "JAVA_GUARD_SMELL_MISMATCH",
                "message": (
                    f"Java guard type '{guard_type}' must equal configured smell '{smell}'."
                ),
            })

    defaults = getattr(config, "defaults", None)
    if getattr(defaults, "run_build", None) is not True:
        violations.append({
            "code": "JAVA_BUILD_DISABLED",
            "message": "Java strict verification requires defaults.run_build=true.",
        })
    if getattr(defaults, "run_tests", None) is not True:
        violations.append({
            "code": "JAVA_TESTS_DISABLED",
            "message": "Java strict verification requires defaults.run_tests=true.",
        })

    for phase, command_config, code in (
        ("build", getattr(config, "build", None), "JAVA_BUILD_COMMAND_MISSING"),
        ("test", getattr(config, "test", None), "JAVA_TEST_COMMAND_MISSING"),
    ):
        command = str(getattr(command_config, "command", "") or "").strip()
        script = str(getattr(command_config, "script", "") or "").strip()
        if not command and not script:
            violations.append({
                "code": code,
                "message": f"Java strict verification requires a non-empty {phase} command or script.",
            })
    return violations


def _java_verification_contract_failure(
    config: ResolvedRunConfig,
    result_type: str,
    violations: List[Dict[str, str]],
) -> Dict[str, object]:
    codes = ", ".join(item["code"] for item in violations)
    return {
        "type": result_type,
        "success": False,
        "message": f"Java strict verification contract is invalid: {codes}.",
        "details": {
            "detector": "java_strict_verification_contract",
            "reason": "JAVA_VERIFICATION_CONTRACT_INVALID",
            "smell": str(getattr(config, "smell", "") or ""),
            "violations": violations,
        },
    }


def run_smell_guards(config: ResolvedRunConfig, context: Optional[GuardRunContext] = None) -> List[Dict[str, object]]:
    strict_violations = validate_java_strict_verification_contract(config)
    if strict_violations:
        return [
            _java_verification_contract_failure(
                config,
                str(getattr(config, "smell", "") or "java_smell_guard"),
                strict_violations,
            )
        ]

    outcomes: List[Dict[str, object]] = []
    for guard in config.profile.guards:
        guard_type = str(guard.get("type", "")).strip()

        checkpoint_matches = (
            context is not None
            and context.checkpoint_required
            and context.checkpoint_smell == guard_type
        )

        if config.language == "java":
            if not checkpoint_matches:
                # Java verification has one authority: the target Guard frozen
                # in c000. Missing or mismatched context fails closed.
                missing = checkpoint_gate_result(
                    guard_type,
                    {
                        "required": False,
                        "reason": "baseline_checkpoint_missing",
                    },
                )
                if missing is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError("missing Java checkpoint unexpectedly passed")
                outcomes.append(missing)
                continue

            checkpoint_failure = checkpoint_gate_result(guard_type, context.checkpoint)
            if checkpoint_failure is not None:
                outcomes.append(checkpoint_failure)
            else:
                outcomes.append({
                    "type": guard_type,
                    "success": True,
                    "message": (
                        f"{guard_type} resolved: the frozen target smell is absent "
                        "and the changed-scope Guard contract passed."
                    ),
                    "details": {
                        "guard": "checkpoint_contract",
                        "checkpoint_id": context.checkpoint.get("checkpoint_id"),
                        "guard_contract": context.checkpoint.get("guard_contract"),
                        "current_metrics": context.checkpoint.get("current_metrics"),
                        "metric_delta": context.checkpoint.get("delta"),
                    },
                })
            continue

        if checkpoint_matches:
            checkpoint_failure = checkpoint_gate_result(guard_type, context.checkpoint)
            if checkpoint_failure is not None:
                outcomes.append(checkpoint_failure)
            else:
                outcomes.append({
                    "type": guard_type,
                    "success": True,
                    "message": (
                        f"{guard_type} resolved: the frozen target smell is absent "
                        "and the changed-scope Guard contract passed."
                    ),
                    "details": {
                        "guard": "checkpoint_contract",
                        "checkpoint_id": context.checkpoint.get("checkpoint_id"),
                        "guard_contract": context.checkpoint.get("guard_contract"),
                        "current_metrics": context.checkpoint.get("current_metrics"),
                        "metric_delta": context.checkpoint.get("delta"),
                    },
                })
            if checkpoint_failure is not None:
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


def run_build_test_guard(
    config: ResolvedRunConfig,
    *,
    require_test_execution: bool = False,
) -> Dict[str, object]:
    metadata = _verification_metadata(config)
    strict_violations = validate_java_strict_verification_contract(config)
    if strict_violations:
        failure = _java_verification_contract_failure(
            config,
            "build_test",
            strict_violations,
        )
        details = dict(failure.get("details") or {})
        details.update({"build": None, "test": None})
        return {**failure, **metadata, "details": details}
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
        fresh_execution_required = bool(
            config.verification_mode == "sample_optimized"
            or require_test_execution
        )
        test_result = _run_command_config(
            config.test,
            cwd=test_cwd,
            env=config.env,
            label="test",
            project_root=config.project_root,
            source=config.test_source,
            force_fresh_test_execution=fresh_execution_required,
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
        if (
            config.verification_mode == "sample_optimized"
            or require_test_execution
        ):
            execution = (
                _sample_test_execution_evidence(
                    config,
                    test_started_ns,
                    test_result,
                )
                if str(config.sample_test_location or "").strip()
                else _project_test_execution_evidence(
                    config,
                    test_started_ns,
                    test_result,
                )
            )
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
                    "message": (
                        "Declared test execution failed. "
                        f"{execution['message']}"
                    ),
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
    command_result: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Require fresh XML or explicit direct-Java evidence for a declared test."""
    test_classes = [
        Path(part.strip()).stem
        for part in str(config.sample_test_location or "").split(";")
        if part.strip() and Path(part.strip()).stem
    ]
    if not test_classes:
        return {
            "success": False,
            "message": "Pinned sample test location does not identify a test class.",
            "test_class": "",
            "test_classes": [],
            "classes": [],
            "executed_test_classes": [],
            "missing_test_classes": [],
            "reports": [],
            "tests": 0,
            "skipped": 0,
        }
    fresh_reports = _fresh_test_reports(config.project_root, started_ns)

    classes: List[Dict[str, object]] = []
    for test_class in test_classes:
        class_reports: List[str] = []
        class_executed = 0
        class_skipped = 0
        for report, root in fresh_reports:
            suite_names = [str(root.attrib.get("name") or "")]
            suite_names.extend(
                str(case.attrib.get("classname") or "")
                for case in root.findall(".//testcase")
            )
            simple_names = {
                name.rsplit(".", 1)[-1]
                for name in suite_names
                if name
            }
            if not any(
                name == test_class or name.startswith(f"{test_class}$")
                for name in simple_names
            ):
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
            class_reports.append(str(report.relative_to(config.project_root)))
            class_executed += non_skipped
            class_skipped += skipped
        classes.append(
            {
                "test_class": test_class,
                "success": class_executed > 0,
                "reports": sorted(class_reports),
                "tests": class_executed,
                "skipped": class_skipped,
                "evidence_mode": "xml" if class_executed > 0 else "",
            }
        )

    console_evidence: Dict[str, object] = {}
    rendered_command = ""
    output = ""
    if isinstance(command_result, dict):
        rendered_command = str(
            command_result.get("command")
            or command_result.get("script")
            or ""
        )
        output = str(command_result.get("output") or "")
    console_suite_counts: Dict[str, int] = {test_class: 0 for test_class in test_classes}
    for match in re.finditer(
        r"Tests run:\s*(\d+),\s*Failures:\s*0,\s*Errors:\s*0,\s*"
        r"Skipped:\s*(\d+).*?\bin\s+([A-Za-z0-9_.$]+)",
        output,
    ):
        tests = int(match.group(1))
        skipped = int(match.group(2))
        simple_name = match.group(3).rsplit(".", 1)[-1]
        non_skipped = max(tests - skipped, 0)
        if non_skipped <= 0:
            continue
        for test_class in test_classes:
            if simple_name == test_class or simple_name.startswith(f"{test_class}$"):
                console_suite_counts[test_class] += non_skipped
    console_suite_classes = [
        test_class
        for test_class, count in console_suite_counts.items()
        if count > 0
    ]
    if console_suite_classes:
        for item in classes:
            test_class = str(item["test_class"])
            if bool(item["success"]) or console_suite_counts[test_class] <= 0:
                continue
            item["success"] = True
            item["tests"] = console_suite_counts[test_class]
            item["evidence_mode"] = "test_runner_console"
        console_evidence = {
            "mode": "test_runner_console",
            "invoked_test_classes": console_suite_classes,
            "tests": sum(console_suite_counts.values()),
        }
    direct_java = bool(
        re.search(r"(?:^|[;&|]\s*)java(?:\s|$)", rendered_command)
    )
    if direct_java:
        invoked = [
            test_class
            for test_class in test_classes
            if re.search(
                rf"(?<![\w$])(?:[A-Za-z_$][\w$]*\.)*"
                rf"{re.escape(test_class)}(?![\w$])",
                rendered_command,
            )
        ]
        junit_match = re.search(r"\bOK\s+\((\d+)\s+tests?\)", output)
        junit_core = "org.junit.runner.JUnitCore" in rendered_command
        console_tests = (
            int(junit_match.group(1))
            if junit_core and junit_match
            else (1 if invoked and not junit_core else 0)
        )
        if console_tests > 0:
            for item in classes:
                if str(item["test_class"]) not in invoked or bool(item["success"]):
                    continue
                item["success"] = True
                item["tests"] = console_tests if len(invoked) == 1 else 1
                item["evidence_mode"] = (
                    "junit_console" if junit_core else "java_main_exit_zero"
                )
            console_evidence = {
                "mode": "junit_console" if junit_core else "java_main_exit_zero",
                "invoked_test_classes": invoked,
                "tests": console_tests,
            }
    missing = [
        str(item["test_class"])
        for item in classes
        if not bool(item["success"])
    ]
    executed_classes = [
        str(item["test_class"])
        for item in classes
        if bool(item["success"])
    ]
    reports = sorted(
        str(report)
        for item in classes
        for report in item["reports"]  # type: ignore[union-attr]
    )
    executed = sum(int(item["tests"]) for item in classes)
    skipped_total = sum(int(item["skipped"]) for item in classes)
    success = executed > 0
    return {
        "success": success,
        "message": (
            f"Pinned sample tests executed {executed} test(s) across "
            f"{len(executed_classes)} declared class(es)"
            + (
                f"; no fresh report for {', '.join(missing)}."
                if missing
                else "."
            )
            if success
            else "Pinned sample test evidence contains no fresh non-empty report "
            "for declared class(es): "
            + ", ".join(missing)
        ),
        "test_class": test_classes[0] if len(test_classes) == 1 else "",
        "test_classes": test_classes,
        "classes": classes,
        "executed_test_classes": executed_classes,
        "missing_test_classes": missing,
        "reports": reports,
        "console_evidence": console_evidence,
        "tests": executed,
        "skipped": skipped_total,
    }


def _fresh_test_reports(
    project_root: Path,
    started_ns: int,
) -> List[tuple[Path, ET.Element]]:
    reports: List[tuple[Path, ET.Element]] = []
    for report in project_root.rglob("TEST-*.xml"):
        try:
            if report.stat().st_mtime_ns < started_ns:
                continue
            reports.append((report, ET.parse(report).getroot()))
        except (OSError, ET.ParseError):
            continue
    return reports


def _project_test_execution_evidence(
    config: ResolvedRunConfig,
    started_ns: int,
    command_result: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Require fresh non-zero execution when no narrower test is pinned."""
    reports: List[str] = []
    executed = 0
    skipped_total = 0
    for report, root in _fresh_test_reports(config.project_root, started_ns):
        try:
            tests = int(str(root.attrib.get("tests") or ""))
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

    output = str(command_result.get("output") or "") if isinstance(command_result, dict) else ""
    console_tests = 0
    for match in re.finditer(
        r"Tests run:\s*(\d+),\s*Failures:\s*0,\s*Errors:\s*0,\s*Skipped:\s*(\d+)",
        output,
    ):
        console_tests += max(int(match.group(1)) - int(match.group(2)), 0)
    junit_match = re.search(r"\bOK\s+\((\d+)\s+tests?\)", output)
    if junit_match:
        console_tests += int(junit_match.group(1))
    executed = max(executed, console_tests)
    success = executed > 0
    return {
        "success": success,
        "mode": "project_full",
        "message": (
            f"Project-full verification executed {executed} non-skipped test(s)."
            if success
            else "Project-full test command exited successfully but produced no "
            "fresh non-skipped test execution evidence."
        ),
        "reports": sorted(reports),
        "tests": executed,
        "skipped": skipped_total,
        "console_tests": console_tests,
    }


def _run_long_method_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_lines = int(guard.get("max_lines", 60))
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
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "switch_statements",
            "success": False,
            "message": "Unable to resolve the target method or function body.",
            "details": None,
        }
    branch_count = estimate_switch_branches(snippet, config.language)
    if config.language == "python":
        switch_count, _, _ = python_switch_metrics(snippet)
    else:
        switch_count = len(re.findall(r"\bswitch\s*\(", snippet.body_text))
    success = switch_count == 0
    return {
        "type": "switch_statements",
        "success": success,
        "message": f"Target has {switch_count} switch construct(s); branch count is {branch_count}.",
        "details": {"switch_count": switch_count, "branch_count": branch_count},
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
    target = config.locations[0] if config.locations else None
    if target is None:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": "mysterious_name guard: missing target location.",
            "details": {"detector": "tree_sitter_generic"},
        }
    from ..checkpoint_adapters import capture_metric_snapshot

    identity = (
        config.finding_contract.get("entity_identity")
        if isinstance(config.finding_contract, dict)
        and isinstance(config.finding_contract.get("entity_identity"), dict)
        else {}
    )
    selector = config.target_context if isinstance(config.target_context, dict) else {}
    kind = str(identity.get("symbol_kind") or selector.get("symbol_kind") or "")
    name = str(identity.get("symbol_name") or selector.get("symbol_name") or "")
    snapshot = capture_metric_snapshot(config, "")
    if not snapshot.get("ok"):
        return {
            "type": "mysterious_name",
            "success": False,
            "message": f"mysterious_name guard: detector unavailable: {snapshot.get('error', '')}",
            "details": {
                "detector": "tree_sitter_generic",
                "error": snapshot.get("error", ""),
            },
        }
    if snapshot.get("finding_present") is True:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": (
                f"mysterious_name guard: detector still reports {kind or 'identifier'} "
                f"'{name}' at {target.project_path}."
            ),
            "details": {
                "detector": "tree_sitter_generic",
                "target_kind": kind,
                "target_name": name,
                "current_metrics": snapshot,
            },
        }
    return {
        "type": "mysterious_name",
        "success": True,
        "message": (
            f"mysterious_name guard: detector no longer reports {kind or 'identifier'} "
            f"'{name}' at {target.project_path}."
        ),
        "details": {
            "detector": "tree_sitter_generic",
            "target_kind": kind,
            "target_name": name,
        },
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
    force_fresh_test_execution: bool = False,
) -> Dict[str, object]:
    rendered_command = ""
    rendered_script = ""
    if command_config.script:
        rendered_script = interpolate_command_text(command_config.script, project_root)
        if force_fresh_test_execution:
            rendered_script = _force_fresh_gradle_test_execution(rendered_script)
        command, shell = _build_script_command(
            rendered_script,
            label,
        )
    elif command_config.command:
        rendered_command = interpolate_command_text(command_config.command, project_root)
        if force_fresh_test_execution:
            rendered_command = _force_fresh_gradle_test_execution(rendered_command)
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


def _force_fresh_gradle_test_execution(command_text: str) -> str:
    """Precede explicit Gradle ``test`` tasks with their matching clean task.

    ``--no-build-cache`` does not disable Gradle's local up-to-date checks.  A
    configured test command can therefore exit zero without executing a test
    after the build phase (or a prior verification) has produced the same task
    outputs.  When fresh evidence is required, cleaning only the named test
    tasks invalidates those outputs without forcing compilation and dependency
    tasks to rerun as the broader ``--rerun-tasks`` option would.

    Commands without an explicit Gradle ``test`` task are left unchanged and
    continue to fail closed if they produce no fresh execution evidence.
    """

    def add_clean_tasks(match: re.Match[str]) -> str:
        launcher = match.group("launcher")
        body = match.group("body")
        clean_tasks: List[str] = []
        for task_match in GRADLE_TEST_TASK_RE.finditer(body):
            test_task = task_match.group("task")
            clean_task = f"{test_task[:-4]}cleanTest"
            if clean_task in clean_tasks:
                continue
            if re.search(
                rf"(?<!\S){re.escape(clean_task)}(?=\s|$)",
                body,
            ):
                continue
            clean_tasks.append(clean_task)
        if not clean_tasks:
            return match.group(0)
        return f"{launcher} {' '.join(clean_tasks)}{body}"

    return GRADLE_COMMAND_RE.sub(add_clean_tasks, str(command_text or ""))


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
