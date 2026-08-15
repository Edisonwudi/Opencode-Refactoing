"""Generic non-Java smell guards and build/test verification.

Java product smells are accepted only through their frozen checkpoint
contract.  The helpers in this module are the language-agnostic detector path
used by non-Java profiles; they are deliberately not a Java fallback.
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from ..analysis import (
    clone_normalized_token_score,
    count_meaningful_lines,
    count_parameters,
    estimate_nesting_depth,
    estimate_switch_branches,
    explicit_target_files_parseability,
    extract_snippet,
    method_basename,
    nonjava_finding_threshold,
    python_switch_metrics,
)
from ..config import CommandConfig, ResolvedRunConfig, interpolate_command_text
from ..data_clumps import (
    data_clump_occurrence_threshold,
    evaluate_data_clump_targets,
)
from ..feature_envy_target_contract import (
    FEATURE_ENVY_TARGET_CONTRACT,
)
from ..checkpoint_contract import checkpoint_gate_result
from ..java_test_attestation_runner import (
    ATTESTATION_ADAPTER_ID,
    ATTESTATION_SCHEMA,
)
from ..java_test_evidence import (
    declared_java_test_sources,
    prepare_java_sample_test_command,
    reset_java_sample_test_evidence,
)
from .context import GuardRunContext


SAMPLE_DEADLINE_EPOCH_MS_ENV = "SMELL_SAMPLE_DEADLINE_EPOCH_MS"


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
    sample_test = getattr(config, "sample_test", None)
    sample_command = str(getattr(sample_test, "command", "") or "").strip()
    sample_script = str(getattr(sample_test, "script", "") or "").strip()
    if (
        str(getattr(config, "verification_mode", "") or "").strip()
        == "sample_optimized"
        and not sample_command
        and not sample_script
    ):
        violations.append({
            "code": "JAVA_SAMPLE_TEST_COMMAND_MISSING",
            "message": (
                "Java sample_optimized verification requires a sample test command."
            ),
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
            if checkpoint_failure is not None or guard_type == "god_class":
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
            outcomes.append(_run_data_clumps_guard(config, guard, context))
        elif guard_type == "feature_envy" and config.language != "java":
            outcomes.append(
                _run_generic_feature_envy_guard(config, guard, context)
            )
        elif guard_type == "mysterious_name":
            outcomes.append(
                _run_generic_mysterious_name_guard(config, guard, context)
            )
        elif guard_type == "dead_code":
            outcomes.append(_run_dead_code_guard(config, guard, context))
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
    del config, guard
    checkpoint_ready = bool(
        context
        and context.checkpoint_required
        and context.checkpoint_smell == "god_class"
    )
    return {
        "type": "god_class",
        "success": False,
        "message": (
            "god_class verification requires the frozen checkpoint finding "
            "contract; no independent LOC or relative-reduction fallback is allowed."
        ),
        "details": {
            "checkpoint_required": checkpoint_ready,
            "guard": "checkpoint_contract",
        },
    }


def god_class_relative_reduction(context: Optional[GuardRunContext]) -> float:
    """Largest relative reduction among non-Java finding-predicate metrics."""
    delta = getattr(context, "metric_delta", None) if context is not None else None
    if not isinstance(delta, dict):
        return 0.0
    objectives = delta.get("objectives")
    if not isinstance(objectives, dict):
        return 0.0
    reductions = []
    for metric in ("nom", "wmc", "loc"):
        values = objectives.get(metric)
        if not isinstance(values, dict):
            continue
        reduction = values.get("relative_reduction")
        if isinstance(reduction, (int, float)) and not isinstance(reduction, bool):
            reductions.append(float(reduction))
    return max((0.0, *reductions))


def _has_command_config(config: CommandConfig) -> bool:
    return bool(
        str(getattr(config, "command", "") or "").strip()
        or str(getattr(config, "script", "") or "").strip()
    )


def _resolved_verification_cwd(config: ResolvedRunConfig) -> Path:
    value = getattr(config, "verification_cwd", None) or getattr(
        config, "cwd", None
    )
    if value is None:
        raise ValueError("Resolved verification configuration is missing verification_cwd")
    return Path(value)


def _sample_test_cwd(config: ResolvedRunConfig) -> Path:
    if str(getattr(config, "sample_test_source", "") or "") == "dataset":
        return Path(config.dataset_root)
    return _resolved_verification_cwd(config)


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
        details.update({"build": None, "test": None, "sample_test": None})
        return {**failure, **metadata, "details": details}
    build_result = None
    test_result = None
    sample_test_result = None
    if config.defaults.run_build:
        build_result = _run_command_config(
            config.build,
            cwd=_resolved_verification_cwd(config),
            env=config.env,
            label="build",
            project_root=config.project_root,
            source=config.build_source,
            timeout_seconds=config.defaults.shell_timeout,
        )
        if not build_result["success"]:
            return {
                "type": "build_test",
                "success": False,
                "message": f"Build failed. {build_result['summary_text']}",
                **metadata,
                "details": {
                    "build": build_result,
                    "test": None,
                    "sample_test": None,
                },
            }
    if require_test_execution and not config.defaults.run_tests:
        test_result = {
            "label": "test",
            "success": False,
            "status": "test_not_executed",
            "returncode": None,
            "command": "",
            "script": "",
            "cwd": str(_resolved_verification_cwd(config)),
            "source": config.test_source,
            "summary": [],
            "failure_highlights": [
                "Fresh test execution is required, but defaults.run_tests is false."
            ],
            "diagnostics": [],
            "tail": [],
            "summary_text": (
                "Fresh test execution is required, but test execution is disabled."
            ),
            "output": "",
        }
        return {
            "type": "build_test",
            "success": False,
            "reason": "TEST_EXECUTION_DISABLED",
            "message": str(test_result["summary_text"]),
            **metadata,
            "details": {
                "build": build_result,
                "test": test_result,
                "sample_test": None,
            },
        }
    if config.defaults.run_tests:
        test_cwd = (
            _sample_test_cwd(config)
            if config.verification_mode == "sample_optimized"
            else _resolved_verification_cwd(config)
        )
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
            timeout_seconds=config.defaults.shell_timeout,
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
                "details": {
                    "build": build_result,
                    "test": test_result,
                    "sample_test": None,
                },
            }
        if config.verification_mode == "sample_optimized" or require_test_execution:
            execution = (
                _sample_test_execution_evidence(
                    config,
                    test_started_ns,
                )
                if (
                    config.verification_mode == "sample_optimized"
                    and str(config.sample_test_location or "").strip()
                )
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
                    "details": {
                        "build": build_result,
                        "test": test_result,
                        "sample_test": None,
                    },
                }
        if (
            str(getattr(config, "language", "")).strip().lower() == "java"
            and config.verification_mode == "project_full"
            and _has_command_config(config.sample_test)
        ):
            reset_java_sample_test_evidence(config.project_root)
            sample_test_started_ns = time.time_ns()
            sample_test_command, evidence_adapter = (
                prepare_java_sample_test_command(config)
            )
            sample_test_result = _run_command_config(
                sample_test_command,
                cwd=_sample_test_cwd(config),
                env=config.env,
                label="sample_test",
                project_root=config.project_root,
                source=str(getattr(config, "sample_test_source", "") or ""),
                force_fresh_test_execution=True,
                timeout_seconds=config.defaults.shell_timeout,
            )
            sample_test_result["evidence_adapter"] = evidence_adapter
            if not sample_test_result["success"]:
                return {
                    "type": "build_test",
                    "success": False,
                    "message": (
                        "Sample test failed. "
                        f"{sample_test_result['summary_text']}"
                    ),
                    **metadata,
                    "details": {
                        "build": build_result,
                        "test": test_result,
                        "sample_test": sample_test_result,
                    },
                }
            sample_execution = (
                _sample_test_execution_evidence(
                    config,
                    sample_test_started_ns,
                )
                if str(config.sample_test_location or "").strip()
                else _project_test_execution_evidence(
                    config,
                    sample_test_started_ns,
                    sample_test_result,
                )
            )
            sample_test_result["execution_evidence"] = sample_execution
            if not sample_execution["success"]:
                sample_test_result["success"] = False
                sample_test_result["status"] = "test_not_executed"
                sample_test_result["failure_highlights"] = [
                    str(sample_execution["message"])
                ]
                sample_test_result["summary_text"] = str(
                    sample_execution["message"]
                )
                return {
                    "type": "build_test",
                    "success": False,
                    "message": (
                        "Declared sample test execution failed. "
                        f"{sample_execution['message']}"
                    ),
                    **metadata,
                    "details": {
                        "build": build_result,
                        "test": test_result,
                        "sample_test": sample_test_result,
                    },
                }
    return {
        "type": "build_test",
        "success": True,
        "message": _build_success_message(
            build_result,
            test_result,
            sample_test_result,
        ),
        **metadata,
        "details": {
            "build": build_result,
            "test": test_result,
            "sample_test": sample_test_result,
        },
    }


def run_focused_preflight(config: ResolvedRunConfig) -> Dict[str, object]:
    """Run a declared bounded preflight without accepting the sample.

    The command runs in the caller's project root, so a fresh project-full
    worktree can reuse its compiler outputs for the immediately following full
    build. Any focused test or behavior probe is a gate only: its evidence and
    result are never reused for final acceptance.
    """
    command = config.focused_preflight
    common: Dict[str, object] = {
        "schema_version": 1,
        "type": "focused_preflight",
        "acceptance": False,
        "project_full_executed": False,
        "cache_scope": "compiler_outputs_only",
        "test_result_reused": False,
        "pass_reused": False,
    }
    if not command.command and not command.script:
        return {
            **common,
            "success": True,
            "status": "NOT_APPLICABLE",
            "message": "No focused preflight is declared for this project.",
            "execution": None,
        }
    execution = _run_command_config(
        command,
        cwd=_resolved_verification_cwd(config),
        env=config.env,
        label="focused_preflight",
        project_root=config.project_root,
        source="project_manifest",
        timeout_seconds=config.defaults.shell_timeout,
    )
    ready = bool(execution["success"])
    return {
        **common,
        "success": ready,
        "status": "READY" if ready else "FAILED",
        "message": (
            "Focused preflight is ready; final project verification "
            "has not run."
            if ready
            else f"Focused preflight failed. {execution['summary_text']}"
        ),
        "execution": execution,
    }


def _verification_metadata(config: ResolvedRunConfig) -> Dict[str, object]:
    return {
        "verification_mode": config.verification_mode,
        "verification_command_source": str(
            getattr(config, "verification_command_source", "") or ""
        ),
        "verification_cwd": str(_resolved_verification_cwd(config)),
        "build_source": config.build_source,
        "test_source": config.test_source,
        "test_location": config.sample_test_location,
        "test_command_hash": _command_hash(config.sample_test_command),
        "sample_test_source": (
            str(getattr(config, "sample_test_source", "") or "")
            if str(config.sample_test_command or "").strip()
            else ""
        ),
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
    """Require native evidence for every explicitly declared Java test class.

    JUnit tests must produce a fresh non-empty XML report. A direct Java main
    test must produce a fresh attestation written only after that class exits
    successfully in its own JVM. Main executions are counted separately from
    JUnit tests; they are never represented as invented test cases.
    """
    locations = [
        part.strip()
        for part in str(config.sample_test_location or "").split(";")
        if part.strip() and Path(part.strip()).stem
    ]
    if not locations:
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
            "executions": 0,
        }

    entries: List[Dict[str, object]] = [
        {
            "location": location,
            "test_class": Path(location).stem,
            "declared_class": "",
            "source": None,
        }
        for location in locations
    ]
    try:
        declared_sources, source_error = declared_java_test_sources(config)
    except (AttributeError, OSError, ValueError):
        declared_sources, source_error = {}, "source_context_unavailable"
    if not source_error and len(declared_sources) == len(entries):
        for entry, (declared_class, source) in zip(
            entries,
            declared_sources.items(),
        ):
            entry["declared_class"] = declared_class
            entry["source"] = source

    test_classes = [str(entry["test_class"]) for entry in entries]
    fresh_reports = _fresh_test_reports(config.project_root, started_ns)
    attestations, invalid_attestations = _fresh_main_attestations(
        config,
        started_ns,
        declared_sources,
    )

    classes: List[Dict[str, object]] = []
    for entry in entries:
        test_class = str(entry["test_class"])
        declared_class = str(entry["declared_class"] or "")
        class_reports: List[str] = []
        class_executed = 0
        class_skipped = 0
        for report, root in fresh_reports:
            suite_names = [str(root.attrib.get("name") or "")]
            suite_names.extend(
                str(case.attrib.get("classname") or "")
                for case in root.findall(".//testcase")
            )
            if declared_class:
                matched = any(
                    name == declared_class or name.startswith(f"{declared_class}$")
                    for name in suite_names
                    if name
                )
            else:
                simple_names = {
                    name.rsplit(".", 1)[-1]
                    for name in suite_names
                    if name
                }
                matched = any(
                    name == test_class or name.startswith(f"{test_class}$")
                    for name in simple_names
                )
            if not matched:
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
            try:
                failures = int(str(root.attrib.get("failures") or "0"))
                errors = int(str(root.attrib.get("errors") or "0"))
            except ValueError:
                failures = len(root.findall(".//testcase/failure"))
                errors = len(root.findall(".//testcase/error"))
            non_skipped = max(tests - skipped, 0)
            if non_skipped <= 0 or failures > 0 or errors > 0:
                continue
            class_reports.append(
                str(
                    report.resolve().relative_to(
                        Path(config.project_root).expanduser().resolve()
                    )
                )
            )
            class_executed += non_skipped
            class_skipped += skipped

        class_attestations = attestations.get(declared_class, [])
        class_executions = sum(
            int(payload.get("executions") or 0)
            for _report, payload in class_attestations
        )
        if class_executed > 0:
            evidence_mode = "xml"
        elif class_executions > 0:
            evidence_mode = "declared_main_attestation"
            class_reports.extend(
                str(
                    report.resolve().relative_to(
                        Path(config.project_root).expanduser().resolve()
                    )
                )
                for report, _payload in class_attestations
            )
        else:
            evidence_mode = ""
        classes.append(
            {
                "test_class": test_class,
                "declared_class": declared_class,
                "success": class_executed > 0 or class_executions > 0,
                "reports": sorted(class_reports),
                "tests": class_executed,
                "skipped": class_skipped,
                "executions": class_executions,
                "evidence_mode": evidence_mode,
            }
        )

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
    executions = sum(int(item["executions"]) for item in classes)
    skipped_total = sum(int(item["skipped"]) for item in classes)
    evidence_units = executed + executions
    success = evidence_units > 0 and not missing
    return {
        "success": success,
        "message": (
            f"Pinned sample tests executed {executed} JUnit test(s) and "
            f"{executions} declared main program(s) across "
            f"{len(executed_classes)} declared class(es)"
            + (
                f"; no fresh report for {', '.join(missing)}."
                if missing
                else "."
            )
            if success
            else (
                "Pinned sample test evidence is incomplete; no fresh valid "
                "XML or main attestation for declared class(es): " + ", ".join(missing)
                if evidence_units > 0
                else "Pinned sample test evidence contains no fresh valid XML or "
                "main attestation "
                "for declared class(es): " + ", ".join(missing)
            )
        ),
        "test_class": test_classes[0] if len(test_classes) == 1 else "",
        "test_classes": test_classes,
        "classes": classes,
        "executed_test_classes": executed_classes,
        "missing_test_classes": missing,
        "reports": reports,
        "console_evidence": {},
        "tests": executed,
        "skipped": skipped_total,
        "executions": executions,
        "evidence_units": evidence_units,
        "invalid_attestations": invalid_attestations,
    }


def _fresh_main_attestations(
    config: ResolvedRunConfig,
    started_ns: int,
    declared_sources: Dict[str, Path],
) -> tuple[Dict[str, List[tuple[Path, Dict[str, object]]]], List[Dict[str, str]]]:
    valid: Dict[str, List[tuple[Path, Dict[str, object]]]] = {}
    invalid: List[Dict[str, str]] = []
    root = Path(config.project_root).expanduser().resolve()
    report_root = root / ".smell-artifacts" / "test-attestations"
    if not report_root.is_dir():
        return valid, invalid
    for report in sorted(report_root.glob("ATTEST-*.json")):
        try:
            stat = report.stat()
            if stat.st_mtime_ns < started_ns:
                continue
            if stat.st_size <= 0 or stat.st_size > 64 * 1024:
                raise ValueError("attestation_size_invalid")
            payload = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("attestation_not_object")
            declared_class = str(payload.get("declared_class") or "")
            source = declared_sources.get(declared_class)
            if source is None:
                raise ValueError("declared_class_not_frozen")
            _validate_main_attestation(
                payload,
                config=config,
                started_ns=started_ns,
                source=source,
            )
            valid.setdefault(declared_class, []).append((report, payload))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            invalid.append(
                {
                    "report": str(report.relative_to(root)),
                    "reason": str(exc),
                }
            )
    return valid, invalid[:20]


def _validate_main_attestation(
    payload: Dict[str, object],
    *,
    config: ResolvedRunConfig,
    started_ns: int,
    source: Path,
) -> None:
    expected = {
        "schema_version": ATTESTATION_SCHEMA,
        "adapter_id": ATTESTATION_ADAPTER_ID,
        "evidence_kind": "declared_main",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "contract_command_sha256": _command_hash(config.sample_test_command),
        "returncode": 0,
        "executions": 1,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ValueError(f"{field}_mismatch")

    argv_hash = str(payload.get("argv_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", argv_hash):
        raise ValueError("argv_sha256_invalid")
    for field in ("uid", "euid", "started_ns", "ended_ns"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field}_invalid")
    if payload["uid"] != os.getuid() or payload["euid"] != os.geteuid():
        raise ValueError("process_identity_mismatch")
    if int(payload["started_ns"]) < started_ns:
        raise ValueError("started_ns_stale")
    if int(payload["ended_ns"]) < int(payload["started_ns"]):
        raise ValueError("ended_ns_invalid")

    cwd_text = str(payload.get("cwd") or "").strip()
    if not cwd_text:
        raise ValueError("cwd_missing")
    cwd = Path(cwd_text).expanduser().resolve()
    project_root = Path(config.project_root).expanduser().resolve()
    try:
        cwd.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("cwd_outside_project") from exc


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


def _junit_report_count(root: ET.Element, attribute: str, marker: str) -> int:
    structural_count = (
        len(root.findall(".//testcase"))
        if attribute == "tests"
        else len(root.findall(f".//testcase/{marker}"))
    )
    raw_root = root.attrib.get(attribute)
    if raw_root is not None:
        try:
            return max(int(str(raw_root)), structural_count, 0)
        except ValueError:
            pass
    suite_counts: List[int] = []
    for suite in root.findall(".//testsuite"):
        raw_suite = suite.attrib.get(attribute)
        if raw_suite is None:
            continue
        try:
            suite_counts.append(max(int(str(raw_suite)), 0))
        except ValueError:
            continue
    if suite_counts:
        return max(sum(suite_counts), structural_count)
    return structural_count


def _project_test_execution_evidence(
    config: ResolvedRunConfig,
    started_ns: int,
    command_result: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Require fresh non-zero execution when no narrower test is pinned."""
    reports: List[str] = []
    failed_reports: List[str] = []
    executed = 0
    skipped_total = 0
    disabled_total = 0
    for report, root in _fresh_test_reports(config.project_root, started_ns):
        tests = _junit_report_count(root, "tests", "testcase")
        skipped = _junit_report_count(root, "skipped", "skipped")
        disabled = _junit_report_count(root, "disabled", "disabled")
        failures = _junit_report_count(root, "failures", "failure")
        errors = _junit_report_count(root, "errors", "error")
        skipped_total += skipped
        disabled_total += disabled
        relative_report = str(report.relative_to(config.project_root))
        if failures > 0 or errors > 0:
            failed_reports.append(relative_report)
            continue
        non_skipped = max(tests - skipped - disabled, 0)
        if non_skipped <= 0:
            continue
        reports.append(relative_report)
        executed += non_skipped

    output = str(command_result.get("output") or "") if isinstance(command_result, dict) else ""
    invocation = ""
    if isinstance(command_result, dict):
        invocation = "\n".join(
            str(command_result.get(field) or "") for field in ("command", "script")
        )
    maven_console_tests = 0
    if re.search(r"(?:^|[\s/])mvn(?:w)?(?:\s|$)|\bmaven\b", invocation):
        for match in re.finditer(
            r"Tests run:\s*(\d+),\s*Failures:\s*0,\s*Errors:\s*0,\s*Skipped:\s*(\d+)",
            output,
        ):
            maven_console_tests += max(
                int(match.group(1)) - int(match.group(2)), 0
            )
    junit_console_tests = 0
    if re.search(r"\b(?:JUnitCore|junit-platform|junit)\b", invocation, re.IGNORECASE):
        junit_match = re.search(r"\bOK\s+\((\d+)\s+tests?\)", output)
        if junit_match:
            junit_console_tests = int(junit_match.group(1))
    console_tests = maven_console_tests + junit_console_tests
    language = str(getattr(config, "language", "") or "").strip().lower()
    pytest_console_tests = (
        _pytest_console_test_count(output) if language == "python" else 0
    )
    unittest_console_tests = (
        _unittest_console_test_count(output) if language == "python" else 0
    )
    ctest_console_tests = (
        _ctest_console_test_count(output) if language in {"c", "cpp"} else 0
    )
    curl_runtests_console_tests = (
        _curl_runtests_console_test_count(invocation, output)
        if language == "c"
        else 0
    )
    redis_native_console_tests = (
        _redis_native_console_test_count(invocation, output)
        if language == "c"
        else 0
    )
    console_tests += (
        pytest_console_tests
        + unittest_console_tests
        + ctest_console_tests
        + curl_runtests_console_tests
        + redis_native_console_tests
    )
    executed = max(executed, console_tests)
    success = executed > 0 and not failed_reports
    return {
        "success": success,
        "mode": "project_full",
        "message": (
            f"Project-full verification executed {executed} non-skipped test(s)."
            if success
            else (
                "Project-full test command produced a fresh report containing "
                "test failures or errors."
                if failed_reports
                else "Project-full test command exited successfully but produced no "
                "fresh non-skipped test execution evidence."
            )
        ),
        "reports": sorted(reports),
        "failed_reports": sorted(failed_reports),
        "tests": executed,
        "skipped": skipped_total,
        "disabled": disabled_total,
        "console_tests": console_tests,
        "maven_console_tests": maven_console_tests,
        "junit_console_tests": junit_console_tests,
        "pytest_console_tests": pytest_console_tests,
        "unittest_console_tests": unittest_console_tests,
        "ctest_console_tests": ctest_console_tests,
        "curl_runtests_console_tests": curl_runtests_console_tests,
        "redis_native_console_tests": redis_native_console_tests,
    }


def _pytest_console_test_count(output: str) -> int:
    """Count only pytest's terminal success summaries.

    Collection/progress lines are deliberately insufficient: a successful
    project-full run must contain pytest's final ``N passed ... in Ns`` line.
    The caller has already established a zero command return code.
    """
    count = 0
    pattern = re.compile(
        r"^(?:=+\s*)?(?P<passed>\d+)\s+passed\b.*"
        r"\bin\s+\d+(?:\.\d+)?s"
        r"(?:\s+\(\d+:\d{2}(?::\d{2})?\))?(?:\s*=+)?$",
        re.IGNORECASE,
    )
    for raw_line in str(output or "").splitlines():
        match = pattern.fullmatch(_clean_log_line(raw_line).strip())
        if match:
            count += int(match.group("passed"))
    return count


def _unittest_console_test_count(output: str) -> int:
    """Count a completed Python unittest run from its strict terminal pair.

    Progress lines and a bare ``OK`` are insufficient.  A successful witness
    requires unittest's ``Ran N tests in ...s`` line followed by its terminal
    ``OK`` line.  Tests reported as skipped are excluded so an all-skipped run
    still fails closed.
    """

    cleaned_lines = [
        _clean_log_line(line).strip()
        for line in str(output or "").replace("\r", "\n").splitlines()
    ]
    for index, line in reversed(list(enumerate(cleaned_lines))):
        match = re.fullmatch(
            r"Ran\s+(?P<tests>[1-9][0-9]*)\s+tests?\s+in\s+"
            r"[0-9]+(?:\.[0-9]+)?s",
            line,
        )
        if not match:
            continue
        terminal = next(
            (
                later
                for later in cleaned_lines[index + 1 :]
                if re.fullmatch(r"OK(?:\s*\([^\n]*\))?", later)
            ),
            "",
        )
        if not terminal:
            return 0
        skipped_match = re.search(r"\bskipped=(\d+)\b", terminal)
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        return max(int(match.group("tests")) - skipped, 0)
    return 0


def _ctest_console_test_count(output: str) -> int:
    """Count ctest cases only when detailed passes and a clean summary agree."""
    cleaned_lines = [
        _clean_log_line(line).strip()
        for line in str(output or "").splitlines()
    ]
    passed_cases = sum(
        1
        for line in cleaned_lines
        if re.fullmatch(
            r"\d+/\d+\s+Test\s+#\d+:\s+.+?\s+\.{2,}\s+Passed\s+"
            r"\d+(?:\.\d+)?\s+sec",
            line,
            flags=re.IGNORECASE,
        )
    )
    clean_totals = [
        int(match.group("total"))
        for line in cleaned_lines
        if (
            match := re.fullmatch(
                r"100%\s+tests\s+passed,\s+0\s+tests\s+failed\s+out\s+of\s+"
                r"(?P<total>\d+)",
                line,
                flags=re.IGNORECASE,
            )
        )
    ]
    if passed_cases <= 0 or not clean_totals:
        return 0
    total = sum(clean_totals)
    return passed_cases if total >= passed_cases else 0


def _curl_runtests_console_test_count(invocation: str, output: str) -> int:
    """Count curl's native Perl suite only from its clean terminal summary."""
    if not re.search(
        r"(?:^|[\s/])tests/runtests\.pl[\"']?(?:\s|$)", invocation
    ):
        return 0
    counts = []
    for raw_line in str(output or "").splitlines():
        match = re.fullmatch(
            r"TESTDONE:\s+(?P<passed>[1-9][0-9]*)\s+tests\s+out\s+of\s+"
            r"(?P<total>[1-9][0-9]*)\s+reported\s+OK:\s+100%",
            _clean_log_line(raw_line).strip(),
        )
        if match and match.group("passed") == match.group("total"):
            counts.append(int(match.group("passed")))
    return counts[-1] if counts else 0


def _redis_native_console_test_count(invocation: str, output: str) -> int:
    """Count Redis' compiled-in C tests, never version/smoke output."""
    if not re.search(r"(?:^|[\s/])redis-server\s+test\s+all(?:\s|$)", invocation):
        return 0
    counts = []
    for raw_line in str(output or "").splitlines():
        match = re.fullmatch(
            r"Tests:\s+(?P<passed>[1-9][0-9]*)\s+passed,\s+0\s+failed,\s+"
            r"(?P<total>[1-9][0-9]*)\s+total",
            _clean_log_line(raw_line).strip(),
        )
        if match and match.group("passed") == match.group("total"):
            counts.append(int(match.group("passed")))
    return counts[-1] if counts else 0


def _run_long_method_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    max_lines = nonjava_finding_threshold(config.language, "long_method", 60) - 1
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
    max_params = nonjava_finding_threshold(config.language, "long_parameter_list", 6) - 1
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
    max_depth = nonjava_finding_threshold(config.language, "nested_complexity", 5) - 1
    snippet = extract_snippet(config.locations[0], config.language)
    if not snippet:
        return {
            "type": "nested_complexity",
            "success": False,
            "message": "Unable to resolve the target method or function body.",
            "details": None,
        }
    depth = estimate_nesting_depth(snippet, config.language)
    success = depth <= max_depth
    return {
        "type": "nested_complexity",
        "success": success,
        "message": f"Target has control-flow nesting depth {depth} (passing maximum {max_depth}).",
        "details": {"max_nesting_depth": depth, "passing_max": max_depth},
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


def _run_data_clumps_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Dict[str, object]:
    target_group = str((config.target_context or {}).get("group") or "").strip()
    if not target_group:
        return {
            "type": "data_clumps",
            "success": False,
            "message": "data_clumps guard: missing target_context.group; cannot validate explicit occurrences.",
            "details": {"detector": "explicit_target_parameter_group_guard"},
        }
    if (
        context is not None
        and context.checkpoint_required
        and context.checkpoint_smell == "data_clumps"
    ):
        current = context.current_metrics
        identity_ok = current.get("target_patch_identity_ok") is True
        finding_present = current.get("finding_present") is True
        migration_requires_project_full = bool(
            current.get("project_full_required")
        )
        migration_verification_ok = bool(
            not migration_requires_project_full
            or config.verification_mode == "project_full"
        )
        success = bool(
            current.get("ok") is True
            and identity_ok
            and current.get("target_missing") is not True
            and not finding_present
            and migration_verification_ok
        )
        return {
            "type": "data_clumps",
            "success": success,
            "message": (
                "data_clumps guard: reused the accepted checkpoint target-local verdict."
                if success
                else (
                    "data_clumps guard: controlled declaration migration requires "
                    "verification_mode=project_full."
                    if migration_requires_project_full
                    and not migration_verification_ok
                    else (
                        "data_clumps guard: checkpoint target identity or finding "
                        "closure is not satisfied."
                    )
                )
            ),
            "details": {
                "detector": "checkpoint_target_local_data_clumps_guard",
                "group": target_group,
                "target_missing": bool(current.get("target_missing")),
                "target_patch_identity_ok": identity_ok,
                "target_patch_identity_failures": list(
                    current.get("target_patch_identity_failures") or []
                ),
                "occurrence_count": int(
                    (current.get("objectives") or {}).get("occurrence_count") or 0
                ),
                "continuity_occurrence_count": int(
                    current.get("continuity_occurrence_count") or 0
                ),
                "inline_copy_expansions": list(
                    current.get("inline_copy_expansions") or []
                ),
                "declaration_migration_mode": current.get(
                    "declaration_migration_mode"
                ),
                "project_full_required": migration_requires_project_full,
                "verification_mode": config.verification_mode,
                "migration_verification_ok": migration_verification_ok,
                "scope_mode": "explicit_target_locations",
            },
        }
    analysis = evaluate_data_clump_targets(
        config.project_root,
        language=config.language,
        group=target_group,
        targets=config.locations,
    )
    if not analysis.get("success"):
        return {
            "type": "data_clumps",
            "success": False,
            "message": f"data_clumps guard: explicit target evaluation unavailable: {analysis.get('error', '')}",
            "details": {
                "detector": "explicit_target_parameter_group_guard",
                "group": target_group,
                "error": analysis.get("error", ""),
                "target_missing": bool(analysis.get("unresolved_targets")),
                "unresolved_targets": list(
                    analysis.get("unresolved_targets") or []
                ),
                "target_identity_collision": bool(
                    analysis.get("target_identity_collision")
                ),
                "target_identity_collisions": list(
                    analysis.get("target_identity_collisions") or []
                ),
                "scope_mode": "explicit_target_locations",
                "scope_files": list(analysis.get("scope_files") or []),
            },
        }
    unresolved_targets = list(analysis.get("unresolved_targets") or [])
    if unresolved_targets:
        first = unresolved_targets[0]
        return {
            "type": "data_clumps",
            "success": False,
            "message": (
                "data_clumps guard: a frozen explicit target cannot be resolved; "
                "an occurrence-count decrease is not accepted as repair."
            ),
            "details": {
                "detector": "explicit_target_parameter_group_guard",
                "group": target_group,
                "target_missing": True,
                "unresolved_targets": unresolved_targets,
                "file": first.get("file"),
                "method": first.get("method"),
                "scope_mode": "explicit_target_locations",
                "scope_files": list(analysis.get("scope_files") or []),
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
                "data_clumps guard: explicit target locations still report "
                f"group={target_group} across {occurrence_count} occurrence(s). "
                f"first remaining: {first.get('file')}#{first.get('method')}."
            ),
            "details": {
                "detector": "explicit_target_parameter_group_guard",
                "group": target_group,
                "occurrence_count": occurrence_count,
                "occurrence_threshold": threshold,
                "remaining_occurrences": remaining_occurrences,
                "remaining_occurrences_truncated": occurrence_count > len(remaining_occurrences),
                "file": first.get("file"),
                "method": first.get("method"),
                "begin_line": first.get("begin_line"),
                "evidence": first.get("evidence"),
                "scope_mode": "explicit_target_locations",
                "scope_files": list(analysis.get("scope_files") or []),
            },
        }
    if isinstance(config.finding_contract, dict) and config.finding_contract:
        return {
            "type": "data_clumps",
            "success": False,
            "message": (
                "data_clumps guard: a frozen checkpoint exists, but no matching "
                "target-patch identity verdict was supplied; resolution fails closed."
            ),
            "details": {
                "detector": "explicit_target_parameter_group_guard",
                "group": target_group,
                "occurrence_count": occurrence_count,
                "occurrence_threshold": threshold,
                "target_patch_identity_ok": False,
                "error": "checkpoint_target_patch_identity_unavailable",
                "scope_mode": "explicit_target_locations",
                "scope_files": list(analysis.get("scope_files") or []),
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
            "detector": "explicit_target_parameter_group_guard",
            "group": target_group,
            "occurrence_count": occurrence_count,
            "occurrence_threshold": threshold,
            "scope_mode": "explicit_target_locations",
            "scope_files": list(analysis.get("scope_files") or []),
        },
    }


def _run_generic_feature_envy_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Dict[str, object]:
    del guard
    target = config.locations[0] if config.locations else None
    if target is None:
        return {
            "type": "feature_envy",
            "success": False,
            "message": "feature_envy guard: missing target location.",
            "details": {
                "detector": "tree_sitter_generic",
                "contract": FEATURE_ENVY_TARGET_CONTRACT,
            },
        }
    if (
        context is None
        or not context.checkpoint_required
        or context.checkpoint_smell != "feature_envy"
    ):
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                "feature_envy guard: a frozen checkpoint target and target-local "
                "patch identity are required; no evidence/dominant-receiver "
                "fallback is allowed."
            ),
            "details": {
                "detector": "tree_sitter_generic",
                "contract": FEATURE_ENVY_TARGET_CONTRACT,
                "error": "feature_envy_checkpoint_contract_required",
            },
        }
    snapshot = dict(context.current_metrics or {})
    identity_ok = snapshot.get("target_patch_identity_ok") is True
    target_missing = snapshot.get("target_missing") is True
    identity_collision = snapshot.get("target_identity_collision") is True
    finding_present = snapshot.get("finding_present")
    snapshot_valid = bool(
        snapshot.get("ok") is True
        and identity_ok
        and not target_missing
        and not identity_collision
        and isinstance(finding_present, bool)
    )
    if not snapshot_valid:
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                "feature_envy guard: the frozen declaration and receiver could "
                "not be mapped one-to-one in the explicit target-file patch."
            ),
            "details": {
                "detector": "tree_sitter_generic",
                "contract": FEATURE_ENVY_TARGET_CONTRACT,
                "target_missing": target_missing,
                "target_identity_collision": identity_collision,
                "target_patch_identity_ok": identity_ok,
                "target_patch_identity_failures": list(
                    snapshot.get("target_patch_identity_failures") or []
                ),
                "error": snapshot.get("error", ""),
                "current_metrics": snapshot,
            },
        }
    dominant = str(
        snapshot.get("guard_receiver_name")
        or snapshot.get("dominant_receiver_type")
        or ""
    )
    dominant_count = int(
        snapshot.get("guard_receiver_access")
        or snapshot.get("dominant_receiver_access")
        or 0
    )
    ratio = float(
        snapshot.get("guard_receiver_ratio")
        or snapshot.get("dominant_receiver_ratio")
        or 0.0
    )
    access_reduction = int(
        snapshot.get("guard_receiver_access_required_reduction") or 0
    )
    ratio_reduction = int(
        snapshot.get("guard_receiver_ratio_required_access_reduction") or 0
    )
    required_reduction = int(
        snapshot.get("guard_required_receiver_access_reduction") or 0
    )
    pass_when = str(snapshot.get("guard_receiver_pass_when") or "")
    details = {
        "detector": "tree_sitter_generic",
        "contract": FEATURE_ENVY_TARGET_CONTRACT,
        "dominant_receiver": dominant,
        "dominant_receiver_access": dominant_count,
        "dominant_receiver_ratio": ratio,
        "receiver_access_required_reduction": access_reduction,
        "receiver_ratio_required_access_reduction": ratio_reduction,
        "required_receiver_access_reduction": required_reduction,
        "receiver_pass_when": pass_when,
        "expected_receiver": snapshot.get("expected_receiver_name"),
        "expected_receiver_access": snapshot.get("expected_receiver_access"),
        "method_loc": snapshot.get("method_loc"),
        "strict_detector_hit": bool(snapshot.get("strict_detector_hit")),
        "target_patch_identity_ok": identity_ok,
        "current_metrics": snapshot,
    }
    if finding_present:
        return {
            "type": "feature_envy",
            "success": False,
            "message": (
                f"feature_envy guard: target still accesses foreign receiver '{dominant}' "
                f"{dominant_count} time(s) at ratio {ratio:.3f}. "
                f"Reduce receiver accesses by at least {required_reduction}: "
                f"the access-count route needs {access_reduction}, while the ratio route "
                f"needs {ratio_reduction}. Passing condition: {pass_when}."
            ),
            "details": details,
        }
    return {
        "type": "feature_envy",
        "success": True,
        "message": (
            f"feature_envy guard: the frozen target declaration remains unique "
            f"and the strict detector no longer flags that code location "
            f"(dominant receiver '{dominant}' {dominant_count} access(es), ratio {ratio:.0%})."
        ),
        "details": details,
    }


def _run_generic_mysterious_name_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext],
) -> Dict[str, object]:
    target = config.locations[0] if config.locations else None
    if target is None:
        return {
            "type": "mysterious_name",
            "success": False,
            "message": "mysterious_name guard: missing target location.",
            "details": {"detector": "tree_sitter_generic"},
        }
    identity = (
        config.finding_contract.get("entity_identity")
        if isinstance(config.finding_contract, dict)
        and isinstance(config.finding_contract.get("entity_identity"), dict)
        else {}
    )
    selector = config.target_context if isinstance(config.target_context, dict) else {}
    kind = str(identity.get("symbol_kind") or selector.get("symbol_kind") or "")
    name = str(identity.get("symbol_name") or selector.get("symbol_name") or "")
    if (
        context is None
        or context.checkpoint_required is not True
        or context.checkpoint_smell != "mysterious_name"
        or not isinstance(context.checkpoint, dict)
    ):
        return {
            "type": "mysterious_name",
            "success": False,
            "message": (
                "mysterious_name guard: frozen checkpoint context is required; "
                "live spelling disappearance is not successor evidence."
            ),
            "details": {
                "detector": "checkpoint_contract",
                "error": "MN_FROZEN_CHECKPOINT_REQUIRED",
            },
        }
    snapshot = context.checkpoint.get("current_metrics")
    if not isinstance(snapshot, dict):
        snapshot = {}
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
    violations = list(snapshot.get("guard_violations") or [])
    successor = snapshot.get("successor_contract")
    successor_status = (
        str(successor.get("status") or "")
        if isinstance(successor, dict)
        else ""
    )
    if violations or successor_status != "accepted":
        return {
            "type": "mysterious_name",
            "success": False,
            "message": (
                "mysterious_name guard: the frozen symbol has no valid unique "
                "target-local successor."
            ),
            "details": {
                "detector": "checkpoint_contract",
                "target_kind": kind,
                "target_name": name,
                "successor_contract": successor,
                "guard_violations": violations,
                "current_metrics": snapshot,
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
            f"mysterious_name guard: {kind or 'identifier'} '{name}' has one "
            f"non-suspicious successor at {target.project_path}."
        ),
        "details": {
            "detector": "tree_sitter_generic",
            "target_kind": kind,
            "target_name": name,
            "successor_contract": successor,
            "current_metrics": snapshot,
        },
    }


def _run_dead_code_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Dict[str, object]:
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
    absence_allowed = dead_code_checkpoint_absence_allowed(context)
    if not target.file_path.exists():
        return _dead_code_target_removed_result(name, absence_allowed=absence_allowed)
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
        return _dead_code_target_removed_result(name, absence_allowed=absence_allowed)
    return {
        "type": "dead_code",
        "success": False,
        "message": f"dead_code guard: reported unused target `{name}` still exists.",
        "details": {
            "detector": "generic_dead_code_guard",
            "target": name,
            "target_found": True,
            "scope_mode": "explicit_target_locations",
        },
    }


def dead_code_checkpoint_absence_allowed(
    context: Optional[GuardRunContext],
) -> bool:
    if (
        context is None
        or not context.checkpoint_required
        or context.checkpoint_smell != "dead_code"
    ):
        return False
    evidence = context.current_metrics.get("target_absence_evidence")
    return bool(
        context.current_metrics.get("target_absence_allowed") is True
        and isinstance(evidence, dict)
        and evidence.get("contract") == "exact-target-declaration-deletion-v2"
        and evidence.get("allowed") is True
    )


def _dead_code_target_removed_result(
    name: str,
    *,
    absence_allowed: bool = False,
) -> Dict[str, object]:
    return {
        "type": "dead_code",
        "success": absence_allowed,
        "message": (
            f"dead_code guard: exact checkpoint deletion evidence authorizes `{name}` absence."
            if absence_allowed
            else (
                f"dead_code guard: target `{name}` no longer resolves, but no exact "
                "checkpoint deletion evidence authorizes its absence."
            )
        ),
        "details": {
            "detector": "generic_dead_code_guard",
            "target": name,
            "target_found": False,
            "target_absence_allowed": absence_allowed,
            "scope_mode": "explicit_target_locations",
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


def _run_code_clone_guard(
    config: ResolvedRunConfig,
    guard: Dict[str, object],
    context: Optional[GuardRunContext] = None,
) -> Dict[str, object]:
    if (
        context is not None
        and context.checkpoint_required
        and context.checkpoint_smell == "code_clone_type1"
    ):
        metrics = dict(context.current_metrics or {})
        identity_ok = metrics.get("target_patch_identity_ok") is True
        finding_present = metrics.get("finding_present")
        absence_allowed = metrics.get("target_absence_allowed") is True
        guard_violations = list(metrics.get("guard_violations") or [])
        target_resolved = (
            metrics.get("target_missing") is not True or absence_allowed
        )
        snapshot_valid = (
            metrics.get("ok") is True
            and identity_ok
            and isinstance(finding_present, bool)
            and target_resolved
            and not guard_violations
        )
        success = snapshot_valid and finding_present is False
        return {
            "type": "code_clone_type1",
            "success": success,
            "message": (
                (
                    "The identical frozen clone endpoints were consolidated into "
                    "one exact production-hunk implementation."
                    if absence_allowed
                    else "The checkpoint-authoritative clone endpoints retain their frozen "
                    "declaration identity and no longer form a type-1 clone."
                )
                if success
                else (
                    "The checkpoint-authoritative clone endpoints still form a "
                    "type-1 clone."
                    if snapshot_valid and finding_present is True
                    else "The frozen clone declaration identity could not be verified."
                )
            ),
            "details": {
                "detector": "checkpoint_current_metrics",
                "target_patch_identity_ok": identity_ok,
                "target_patch_identity_contract": metrics.get(
                    "target_patch_identity_contract"
                ),
                "target_patch_identity_failures": list(
                    metrics.get("target_patch_identity_failures") or []
                ),
                "target_missing": metrics.get("target_missing") is True,
                "target_absence_allowed": absence_allowed,
                "clone_consolidation": dict(
                    metrics.get("clone_consolidation") or {}
                ),
                "clone_related_occurrence_closure": dict(
                    metrics.get("clone_related_occurrence_closure") or {}
                ),
                "guard_violations": guard_violations,
                "finding_present": finding_present,
                "clone_token_count": (metrics.get("objectives") or {}).get(
                    "clone_token_count"
                ),
            },
        }
    if len(config.locations) >= 2:
        parseability = explicit_target_files_parseability(
            list(config.locations[:2]),
            config.language,
        )
        if parseability.get("ok") is not True:
            return {
                "type": "code_clone_type1",
                "success": False,
                "message": (
                    "One or more explicit clone target files are missing or "
                    "syntactically incomplete; clone disappearance is not accepted."
                ),
                "details": {
                    "detector": "tree_sitter_generic",
                    "target_resolution": "source_not_parseable",
                    "source_file_parseability": parseability,
                },
            }
        first_target, second_target = config.locations[:2]
        first = (
            extract_snippet(first_target, config.language)
            if first_target.file_path.is_file()
            else None
        )
        second = (
            extract_snippet(second_target, config.language)
            if second_target.file_path.is_file()
            else None
        )
    else:
        first, second = None, None
    if len(config.locations) >= 2 and (not first or not second):
        return {
            "type": "code_clone_type1",
            "success": False,
            "message": (
                "One or both frozen clone targets no longer resolve; target "
                "disappearance is not accepted without checkpoint identity evidence."
            ),
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
    first_normalized, second_normalized, clone_token_count = clone_normalized_token_score(
        first.body_text,
        second.body_text,
        config.language,
    )
    threshold = nonjava_finding_threshold(
        config.language,
        "code_clone_type1",
        30,
    )
    normalized_bodies_equal = bool(first_normalized) and first_normalized == second_normalized
    still_clone = clone_token_count >= threshold
    return {
        "type": "code_clone_type1",
        "success": not still_clone,
        "message": (
            "The target blocks still form a normalized type-1 clone at or above "
            f"the {threshold}-token threshold."
            if still_clone
            else (
                "The matching target blocks are below the normalized token threshold."
                if normalized_bodies_equal
                else "The target blocks no longer normalize to the same implementation."
            )
        ),
        "details": {
            "first_length": len(first_normalized),
            "second_length": len(second_normalized),
            "clone_token_count": clone_token_count,
            "finding_min_tokens": threshold,
            "normalized_bodies_equal": normalized_bodies_equal,
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
    timeout_seconds: int = 0,
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
    command_env = {**os.environ, **env}
    effective_timeout = _effective_command_timeout(
        max(1, int(timeout_seconds)) if timeout_seconds else None,
        command_env,
    )
    try:
        proc = _run_captured_command(
            command,
            cwd=str(cwd),
            env=command_env,
            shell=shell,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout or ""
        output = (
            captured.decode("utf-8", errors="replace")
            if isinstance(captured, bytes)
            else str(captured)
        )
        elapsed_limit = effective_timeout if effective_timeout is not None else timeout_seconds
        message = f"{label.capitalize()} timed out after {float(elapsed_limit):.1f} seconds."
        command_summary = _summarize_command_output(output, label=label, returncode=124)
        return {
            "label": label,
            "success": False,
            "status": "timeout",
            "returncode": 124,
            "command": rendered_command,
            "script": rendered_script,
            "cwd": str(cwd),
            "source": source,
            "summary": command_summary["summary"],
            "failure_highlights": [message, *command_summary["failure_highlights"]],
            "diagnostics": command_summary["diagnostics"],
            "tail": command_summary["tail"],
            "summary_text": message,
            "output": output,
        }
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


def _effective_command_timeout(
    configured_timeout: Optional[float],
    env: Dict[str, str],
) -> Optional[float]:
    raw_deadline = str(env.get(SAMPLE_DEADLINE_EPOCH_MS_ENV, "")).strip()
    if not raw_deadline.isdigit():
        return configured_timeout
    remaining = (int(raw_deadline) / 1000.0) - time.time()
    if configured_timeout is None:
        return max(0.0, remaining)
    return max(0.0, min(float(configured_timeout), remaining))


def _run_captured_command(
    command: object,
    *,
    cwd: str,
    env: Dict[str, str],
    shell: bool,
    timeout: Optional[float],
) -> subprocess.CompletedProcess[str]:
    """Run one build/test command and terminate its whole process group on timeout."""
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(proc)
        stdout, _ = proc.communicate()
        captured = exc.stdout or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", errors="replace")
        final_output = str(stdout or "")
        initial_output = str(captured)
        if final_output.startswith(initial_output):
            # ``communicate`` after termination normally returns the complete
            # stream, including bytes already attached to TimeoutExpired.
            captured = final_output
        elif not initial_output.startswith(final_output):
            # Be conservative for platform-specific pipe behavior where the
            # second read contains only the post-timeout tail.
            captured = f"{initial_output}{final_output}"
        raise subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=str(captured),
        ) from exc
    return subprocess.CompletedProcess(command, proc.returncode, stdout=stdout, stderr=None)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        # ``communicate`` can time out after the shell/group leader has
        # already exited when a background child still owns stdout.  The
        # process group may therefore remain alive even though ``poll`` is no
        # longer ``None``; always address the group by its original pgid.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait()
            return
        # Give every group member a bounded grace period to flush termination
        # diagnostics. Waiting only on the leader is insufficient: it may
        # already have exited while a background child still owns the pipe.
        grace_deadline = time.monotonic() + 1.0
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            proc.wait()
            return
        except PermissionError:
            # macOS can report EPERM briefly for an exited/zombie group.
            pass
        remaining_grace = grace_deadline - time.monotonic()
        if remaining_grace > 0:
            time.sleep(remaining_grace)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        return

    # Non-POSIX runtimes cannot address a process group by the leader's pid.
    # Retain the best-effort direct-child behavior for completeness.
    if proc.poll() is not None:  # pragma: no cover - delivery/runtime is POSIX
        return
    proc.terminate()  # pragma: no cover - delivery/runtime is POSIX
    try:  # pragma: no cover - delivery/runtime is POSIX
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:  # pragma: no cover - delivery/runtime is POSIX
        proc.kill()
        proc.wait()


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
    script_body = script if script.endswith("\n") else script + "\n"
    if os.name != "nt":
        # Project scripts are verification transactions.  A failed build/test
        # step must not be hidden by a later command (for example, a stamp or
        # report-writing command) that exits successfully.
        script_body = "set -e\n" + script_body
    script_path.write_text(script_body, encoding="utf-8")
    if os.name != "nt":
        script_path.chmod(0o700)
        return f"sh {script_path}", True
    return str(script_path), False

def _build_success_message(
    build_result: Optional[Dict[str, object]],
    test_result: Optional[Dict[str, object]],
    sample_test_result: Optional[Dict[str, object]] = None,
) -> str:
    parts = []
    if build_result and build_result["status"] != "skipped":
        parts.append("build passed")
    if test_result and test_result["status"] != "skipped":
        parts.append("project tests passed" if sample_test_result else "tests passed")
    if sample_test_result and sample_test_result["status"] != "skipped":
        parts.append("sample tests passed")
    if not parts:
        return "Build/test verification skipped."
    return " and ".join(parts).capitalize() + "."
