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
from pathlib import Path
from typing import Dict, List, Optional

from ..analysis import (
    LANGUAGE_EXTENSIONS,
    count_meaningful_lines,
    count_parameters,
    estimate_complexity,
    estimate_switch_branches,
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
            outcomes.append(_run_code_clone_guard(config, guard))
        elif guard_type == "data_clumps":
            outcomes.append(_run_data_clumps_guard(config, guard))
        elif guard_type == "dead_code":
            outcomes.append(_run_dead_code_guard(config, guard))
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


def _run_code_clone_guard(config: ResolvedRunConfig, guard: Dict[str, object]) -> Dict[str, object]:
    clone_handler = get_clone_guard(config.language)
    if clone_handler is not None:
        syntactic = clone_handler(config, guard)
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
