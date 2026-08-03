"""Shared Java source model plus the remaining syntactic product detectors.

Nested complexity, switch statements, and mysterious names are detected here.
Long methods use ``ast_ncss``; long parameter lists use the semantic signature
model; exact clones use ``clone_closure``.  Keeping those product definitions in
one place avoids subtly different shadow findings in this fast source scanner.
"""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tree_sitter_language_pack import get_parser

from ..analysis import java_cognitive_complexity_from_text
from .source_layout import (
    JavaSourceLayoutError,
    discover_java_source_layout,
    standard_test_root,
)
from .detector_utils import (
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
)


JAVA_CONTROL_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "synchronized",
    "try",
    "do",
    "return",
    "throw",
    "new",
    "assert",
    "case",
}

JAVA_KEYWORDS = {
    "abstract",
    "assert",
    "boolean",
    "break",
    "byte",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extends",
    "final",
    "finally",
    "float",
    "for",
    "goto",
    "if",
    "implements",
    "import",
    "instanceof",
    "int",
    "interface",
    "long",
    "native",
    "new",
    "package",
    "private",
    "protected",
    "public",
    "return",
    "short",
    "static",
    "strictfp",
    "super",
    "switch",
    "synchronized",
    "this",
    "throw",
    "throws",
    "transient",
    "try",
    "void",
    "volatile",
    "while",
    "record",
    "sealed",
    "permits",
    "non-sealed",
    "var",
    "yield",
}

IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
CLASS_DECL_RE = re.compile(
    r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)(?:\s+extends\s+([A-Za-z_$][A-Za-z0-9_$\.]*))?"
)
VAR_DECL_RE = re.compile(
    r"\b(?:(?:byte|short|int|long|float|double|boolean|char|String|var)(?:\s*\[\s*\])*|[A-Z][A-Za-z0-9_$<>\[\],.?\s]*)\s+([a-zA-Z_$][A-Za-z0-9_$]*)\s*(?:=|;|,)"
)

DEFAULT_THRESHOLDS = {
    "cognitive_complexity": 20,
    "mysterious_name_min_len": 2,
    "mysterious_name_profile": "strict",
}

DEFAULT_EXCLUDE_PATHS = [
    ".git",
    ".gradle",
    ".idea",
    ".settings",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "build",
    "out",
    "bin",
    "dist",
    "node_modules",
    "venv",
    "env",
]

DEFAULT_LOW_INFO_NAMES = {
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
}


@dataclass(frozen=True)
class JavaMethodInfo:
    file_path: Path
    rel_path: str
    class_name: str
    method_name: str
    signature: str
    begin_line: int
    end_line: int
    body_begin_line: int
    body_text: str
    is_constructor: bool
    parameter_names: List[str]
    parameter_tokens: List[str]


@dataclass(frozen=True)
class JavaClassInfo:
    file_path: Path
    rel_path: str
    class_name: str
    parent_name: Optional[str]
    begin_line: int
    end_line: int


@dataclass(frozen=True)
class JavaSyntacticFinding:
    smell_type: str
    file: str
    class_name: str
    method: str
    begin_line: int
    end_line: int
    score: float
    rule_id: str
    evidence: str
    symbol_kind: str = ""
    symbol_name: str = ""
    scope_starts: Tuple[int, ...] = ()
    switch_count: int = 0
    switch_case_count: int = 0
    switch_density: float = 0.0


@dataclass(frozen=True)
class JavaSyntacticDetectionResult:
    ok: bool
    findings: Dict[str, List[JavaSyntacticFinding]]
    error: str = ""
    unavailable: Optional[Dict[str, object]] = None


def run_java_syntactic_detector(
    project_root: Path,
    *,
    include_tests: bool = False,
    target_files: Optional[Sequence[Path]] = None,
    thresholds: Optional[Dict[str, object]] = None,
    include_mysterious_name: bool = True,
) -> JavaSyntacticDetectionResult:
    config = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    project_root = project_root.expanduser().resolve()
    try:
        java_files = _resolve_java_files(project_root, include_tests=include_tests, target_files=target_files)
        classes, methods = load_project_model(project_root, java_files)
        mysterious_findings = (
            _detect_mysterious_name(
                methods,
                int(config["mysterious_name_min_len"]),
                DEFAULT_LOW_INFO_NAMES,
                profile=str(config["mysterious_name_profile"]),
                exclude_tests=True,
            )
            if include_mysterious_name
            else []
        )
        if include_mysterious_name:
            mysterious_findings.extend(
                _detect_mysterious_names_outside_methods(
                    project_root,
                    java_files,
                    classes,
                    methods,
                    int(config["mysterious_name_min_len"]),
                    DEFAULT_LOW_INFO_NAMES,
                    profile=str(config["mysterious_name_profile"]),
                )
            )
        findings = {
            "nested_complexity": _detect_nested_complexity(methods, int(config["cognitive_complexity"])),
            "switch_statements": _detect_switch_statements(methods),
            "mysterious_name": mysterious_findings,
        }
        return JavaSyntacticDetectionResult(ok=True, findings={k: _sort_findings(v) for k, v in findings.items()})
    except JavaSourceLayoutError as exc:
        return JavaSyntacticDetectionResult(
            ok=False,
            findings=_empty_findings(),
            error="DETECTOR_UNAVAILABLE",
            unavailable=exc.to_unavailable(),
        )
    except Exception as exc:
        return JavaSyntacticDetectionResult(ok=False, findings=_empty_findings(), error=str(exc))


def find_matching_syntactic_findings(
    findings: Sequence[JavaSyntacticFinding],
    *,
    target_file: Path,
    project_root: Path,
    method: Optional[str],
    line: Optional[int],
    class_name: Optional[str] = None,
    original_param_type_fingerprint: Optional[str] = None,
) -> List[JavaSyntacticFinding]:
    """Return every detector finding compatible with the stable target.

    File, class, method name, and parameter types are identity filters.  A
    containing source line may disambiguate otherwise identical candidates,
    but a stale line never overrides an already-unique stable identity.  In
    particular, this function deliberately has no nearest/first fallback.
    """
    target_rel = _normalize_rel_path(target_file, project_root)
    target_method = _normalize_method(method)
    target_class = _normalize_class_name(class_name)
    has_identity_anchor = bool(target_method or target_class)
    candidates: List[JavaSyntacticFinding] = []
    for finding in findings:
        if _normalize_path(finding.file) != target_rel:
            continue
        if target_class and _normalize_class_name(finding.class_name) != target_class:
            continue
        if target_method and _normalize_method(finding.method) != target_method:
            continue
        candidates.append(finding)

    signature_fingerprint = (
        original_param_type_fingerprint
        if original_param_type_fingerprint is not None
        else method_parameter_type_fingerprint(method)
    )
    if signature_fingerprint is not None:
        same_signature = [
            finding
            for finding in candidates
            if _finding_parameter_type_fingerprint(finding) == signature_fingerprint
        ]
        if not same_signature:
            return []
        candidates = same_signature
    if not candidates:
        return []

    line_matched = False
    if len(candidates) > 1 or not has_identity_anchor:
        for anchor_line in _distinct_positive_lines(line):
            containing = [
                finding
                for finding in candidates
                if finding.begin_line
                and finding.begin_line
                <= anchor_line
                <= (finding.end_line or finding.begin_line)
            ]
            if containing:
                candidates = containing
                line_matched = True
                break
    if not has_identity_anchor and line and not line_matched:
        return []
    return sorted(
        candidates,
        key=lambda item: (
            _normalize_path(item.file),
            _normalize_class_name(item.class_name),
            _normalize_method_signature(item.method),
            item.begin_line,
            item.end_line,
            item.rule_id,
        ),
    )

def method_parameter_type_fingerprint(method: Optional[str]) -> Optional[str]:
    """Canonical Java parameter types from a declaration-like signature."""
    signature = str(method or "")
    if "(" not in signature or ")" not in signature:
        return None
    inner = signature.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inner:
        return ""
    normalized: List[str] = []
    for raw in _split_top_level_commas(inner):
        part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
        part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
        part = re.sub(r"\s+", " ", part).strip()
        if not part:
            continue
        chunks = part.split(" ")
        # Detector findings contain declaration parameter names.  A caller may
        # also provide a Java-style type-only signature such as ``run(int)``.
        type_text = part if len(chunks) == 1 else " ".join(chunks[:-1]).strip()
        normalized.append(_normalize_type_name(type_text))
    return ",".join(normalized)


def _normalize_method_signature(method: Optional[str]) -> str:
    name = _normalize_method(method)
    fingerprint = method_parameter_type_fingerprint(method)
    return name if fingerprint is None else f"{name}({fingerprint})"


def _normalize_class_name(value: Optional[str]) -> str:
    return str(value or "").strip().rsplit(".", 1)[-1].lower()


def _distinct_positive_lines(*values: Optional[int]) -> List[int]:
    lines: List[int] = []
    for value in values:
        if value is None or int(value) <= 0 or int(value) in lines:
            continue
        lines.append(int(value))
    return lines


def _finding_parameter_type_fingerprint(finding: JavaSyntacticFinding) -> Optional[str]:
    return method_parameter_type_fingerprint(finding.method)


def load_project_model(project_root: Path, java_files: Sequence[Path]) -> Tuple[List[JavaClassInfo], List[JavaMethodInfo]]:
    all_classes: List[JavaClassInfo] = []
    all_methods: List[JavaMethodInfo] = []
    for file_path in java_files:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(file_path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
        line_starts = _build_line_starts(text)
        classes = _extract_class_ranges(file_path, rel_path, text, line_starts)
        methods = _scan_java_methods(file_path, rel_path, text, line_starts, classes)
        all_classes.extend(classes)
        all_methods.extend(methods)
    return all_classes, all_methods


def load_java_source_model(
    file_path: Path,
    rel_path: str,
    text: str,
) -> Tuple[List[JavaClassInfo], List[JavaMethodInfo]]:
    """Parse an in-memory Java source snapshot with the regular detector model."""
    line_starts = _build_line_starts(text)
    classes = _extract_class_ranges(file_path, rel_path, text, line_starts)
    methods = _scan_java_methods(file_path, rel_path, text, line_starts, classes)
    return classes, methods


def _detect_nested_complexity(methods: Sequence[JavaMethodInfo], threshold: int) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        score = compute_cognitive_complexity(method.body_text, method.method_name)
        if score < threshold:
            continue
        rows.append(_finding("nested_complexity", method, float(score), "custom:cognitive_complexity", f"complexity={score}; threshold={threshold}"))
    return rows


def _detect_switch_statements(
    methods: Sequence[JavaMethodInfo],
) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        switch_count, case_count, density = compute_switch_metrics(method.body_text)
        if switch_count == 0:
            continue
        score = max(float(case_count), float(density))
        rows.append(
            _finding(
                "switch_statements",
                method,
                score,
                "custom:target_method_contains_switch",
                f"switch_count={switch_count}; case_count={case_count}; density={density:.2f}",
                switch_count=switch_count,
                switch_case_count=case_count,
                switch_density=density,
            )
        )
    return rows


def is_thin_forwarder(body_text: str) -> bool:
    """Exclude one-statement delegation shells from type-1 clone findings."""
    compact = re.sub(r"\s+", " ", mask_comments_and_strings(body_text)).strip()
    return bool(
        re.fullmatch(
            r"\{\s*(?:return\s+)?(?:super|this|[A-Za-z_$][A-Za-z0-9_$.]*)"
            r"\s*\([^;{}]*\)\s*;\s*\}",
            compact,
        )
    )


_is_thin_forwarder = is_thin_forwarder


def _detect_mysterious_name(
    methods: Sequence[JavaMethodInfo],
    min_len: int,
    low_info_names: Iterable[str],
    *,
    profile: str = "legacy",
    exclude_tests: bool = False,
    exclude_generated: bool = True,
) -> List[JavaSyntacticFinding]:
    low_info = {name.lower() for name in low_info_names}
    strict_mode = profile == "strict"
    rows = []
    for method in methods:
        if _should_exclude_mysterious_path(method.rel_path, profile, exclude_tests, exclude_generated):
            continue
        if not method.is_constructor and _is_valid_java_identifier(method.method_name):
            reason = _suspicious_name_reason(method.method_name, min_len, low_info, allow_too_short=True)
            if reason:
                evidence = _mysterious_evidence("method", method.method_name, reason) if strict_mode else f"name={method.method_name}; reason={reason}"
                rows.append(
                    _finding(
                        "mysterious_name",
                        method,
                        1.0,
                        "custom:mysterious_method_name",
                        evidence,
                        begin_line=method.begin_line,
                        end_line=method.begin_line,
                        symbol_kind="method",
                        symbol_name=method.method_name,
                    )
                )
        for pname in method.parameter_names:
            if not _is_valid_java_identifier(pname):
                continue
            reason = _suspicious_name_reason(pname, min_len, low_info, allow_too_short=not strict_mode)
            if reason:
                evidence = _mysterious_evidence("param", pname, reason) if strict_mode else f"param={pname}; reason={reason}"
                rows.append(
                    _finding(
                        "mysterious_name",
                        method,
                        1.0,
                        "custom:mysterious_parameter_name",
                        evidence,
                        begin_line=method.begin_line,
                        end_line=method.begin_line,
                        symbol_kind="param",
                        symbol_name=pname,
                    )
                )
        masked_body = mask_comments_and_strings(method.body_text)
        seen_local_names: set[str] = set()
        for declaration in VAR_DECL_RE.finditer(masked_body):
            var = declaration.group(1)
            if not _is_valid_java_identifier(var):
                continue
            if var in seen_local_names:
                continue
            reason = _suspicious_name_reason(var, min_len, low_info, allow_too_short=not strict_mode)
            if reason:
                seen_local_names.add(var)
                evidence = (
                    _mysterious_evidence("local", var, reason)
                    if strict_mode
                    else f"local={var}; reason={reason}"
                )
                declaration_line = method.body_begin_line + masked_body.count(
                    "\n", 0, declaration.start(1)
                )
                rows.append(
                    _finding(
                        "mysterious_name",
                        method,
                        1.0,
                        "custom:mysterious_local_name",
                        evidence,
                        begin_line=declaration_line,
                        end_line=declaration_line,
                        symbol_kind="local",
                        symbol_name=var,
                    )
                )
    return rows


def _detect_mysterious_names_outside_methods(
    project_root: Path,
    java_files: Sequence[Path],
    classes: Sequence[JavaClassInfo],
    methods: Sequence[JavaMethodInfo],
    min_len: int,
    low_info_names: Iterable[str],
    *,
    profile: str,
) -> List[JavaSyntacticFinding]:
    """Detect locals in static, instance, lambda, and anonymous initializer scopes."""
    low_info = {name.lower() for name in low_info_names}
    strict_mode = profile == "strict"
    rows: List[JavaSyntacticFinding] = []
    methods_by_file: Dict[str, List[JavaMethodInfo]] = defaultdict(list)
    classes_by_file: Dict[str, List[JavaClassInfo]] = defaultdict(list)
    for method in methods:
        methods_by_file[method.rel_path].append(method)
    for cls in classes:
        classes_by_file[cls.rel_path].append(cls)

    for file_path in java_files:
        rel_path = str(file_path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
        if _should_exclude_mysterious_path(rel_path, profile, True, True):
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        masked = mask_comments_and_strings(text)
        line_starts = _build_line_starts(text)
        brace_ranges = _brace_ranges(masked)
        seen: Set[Tuple[str, str]] = set()
        for declaration in VAR_DECL_RE.finditer(masked):
            name = declaration.group(1)
            if not _is_valid_java_identifier(name):
                continue
            line = _idx_to_line(line_starts, declaration.start(1))
            if any(method.begin_line <= line <= method.end_line for method in methods_by_file[rel_path]):
                continue
            reason = _suspicious_name_reason(name, min_len, low_info, allow_too_short=not strict_mode)
            if not reason:
                continue
            cls = _find_enclosing_class(classes_by_file[rel_path], line)
            if cls is None:
                continue
            containing = [
                (start, end)
                for start, end in brace_ranges
                if start < declaration.start(1) < end
            ]
            if not containing:
                continue
            scope_start, scope_end = min(containing, key=lambda item: item[1] - item[0])
            class_only = (
                _idx_to_line(line_starts, scope_start) == cls.begin_line
                and _idx_to_line(line_starts, scope_end) == cls.end_line
            )
            kind = "field" if class_only else "local"
            if kind != "local":
                continue
            scope_id = _initializer_scope_id(masked, containing)
            if (scope_id, name) in seen:
                continue
            seen.add((scope_id, name))
            evidence = (
                _mysterious_evidence(kind, name, reason)
                if strict_mode
                else f"local={name}; reason={reason}"
            )
            if strict_mode:
                structural_scopes = sorted(
                    containing,
                    key=lambda item: item[1] - item[0],
                )[:3]
                structural_starts = sorted({
                    _idx_to_line(line_starts, item[0])
                    for item in containing
                })
                evidence += (
                    f"; scope_begin={min(_idx_to_line(line_starts, item[0]) for item in structural_scopes)}"
                    f"; scope_end={max(_idx_to_line(line_starts, item[1]) for item in structural_scopes)}"
                    f"; scope_starts={'|'.join(str(item) for item in structural_starts)}"
                )
            rows.append(
                JavaSyntacticFinding(
                    smell_type="mysterious_name",
                    file=rel_path,
                    class_name=cls.class_name,
                    method=f"<initializer:{scope_id}>",
                    begin_line=line,
                    end_line=line,
                    score=1.0,
                    rule_id="custom:mysterious_initializer_local_name",
                    evidence=evidence,
                    symbol_kind=kind,
                    symbol_name=name,
                    scope_starts=tuple(structural_starts) if strict_mode else (),
                )
            )
    return rows


def _brace_ranges(masked_text: str) -> List[Tuple[int, int]]:
    stack: List[int] = []
    ranges: List[Tuple[int, int]] = []
    for index, char in enumerate(masked_text):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            ranges.append((stack.pop(), index))
    return ranges


def _initializer_scope_id(masked_text: str, containing: Sequence[Tuple[int, int]]) -> str:
    fragments: List[str] = []
    for start, _ in sorted(containing, key=lambda item: item[1] - item[0])[:3]:
        context_start = max(0, masked_text.rfind("\n", 0, start - 1) + 1)
        fragment = re.sub(r"\s+", " ", masked_text[context_start:start]).strip()
        fragments.append(fragment[-120:])
    return hashlib.sha1("|".join(fragments).encode("utf-8")).hexdigest()[:16]


def _finding(
    smell_type: str,
    method: JavaMethodInfo,
    score: float,
    rule_id: str,
    evidence: str,
    *,
    begin_line: Optional[int] = None,
    end_line: Optional[int] = None,
    symbol_kind: str = "",
    symbol_name: str = "",
    switch_count: int = 0,
    switch_case_count: int = 0,
    switch_density: float = 0.0,
) -> JavaSyntacticFinding:
    return JavaSyntacticFinding(
        smell_type=smell_type,
        file=method.rel_path,
        class_name=method.class_name,
        method=method.signature,
        begin_line=method.begin_line if begin_line is None else begin_line,
        end_line=method.end_line if end_line is None else end_line,
        score=score,
        rule_id=rule_id,
        evidence=evidence,
        symbol_kind=symbol_kind,
        symbol_name=symbol_name,
        switch_count=switch_count,
        switch_case_count=switch_case_count,
        switch_density=switch_density,
    )


def count_non_comment_loc(block_text: str) -> int:
    in_block = False
    count = 0
    for raw_line in block_text.splitlines():
        i = 0
        out: List[str] = []
        while i < len(raw_line):
            ch = raw_line[i]
            nxt = raw_line[i + 1] if i + 1 < len(raw_line) else ""
            if in_block:
                if ch == "*" and nxt == "/":
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                in_block = True
                i += 2
                continue
            if ch == "/" and nxt == "/":
                break
            out.append(ch)
            i += 1
        if "".join(out).strip():
            count += 1
    return count


def compute_cognitive_complexity(block_text: str, method_name: str = "") -> int:
    return java_cognitive_complexity_from_text(block_text, method_name)


def compute_switch_metrics(block_text: str) -> Tuple[int, int, float]:
    masked = mask_comments_and_strings(block_text)
    switch_count = len(re.findall(r"\bswitch\b", masked))
    case_count = len(re.findall(r"\bcase\b", masked))
    density = (case_count / switch_count) if switch_count else 0.0
    return switch_count, case_count, density


def tokenize_clone(block_text: str) -> List[str]:
    """Return Java Type-1 clone tokens.

    Type-1 equality ignores only layout and comments. Identifier names,
    literal values, and every Java operator remain part of the fingerprint.
    Tree-sitter leaf ranges provide the lexer contract, avoiding a second,
    incomplete regular-expression implementation of Java tokens.
    """
    return list(_tokenize_clone_cached(str(block_text or "")))


def tokenize_clone_node(
    node: object,
    *,
    exclude_nodes: Sequence[object] = (),
) -> List[str]:
    """Return Type-1 tokens from an existing Java tree-sitter node."""
    excluded = {
        (
            str(getattr(item, "type", "")),
            int(getattr(item, "start_byte", -1)),
            int(getattr(item, "end_byte", -1)),
        )
        for item in exclude_nodes
    }
    tokens: List[str] = []

    def visit(current: object) -> None:
        identity = (
            str(getattr(current, "type", "")),
            int(getattr(current, "start_byte", -1)),
            int(getattr(current, "end_byte", -1)),
        )
        if identity in excluded or "comment" in identity[0]:
            return
        children = list(getattr(current, "children", ()) or ())
        if children:
            for child in children:
                visit(child)
            return
        raw = getattr(current, "text", None)
        if isinstance(raw, bytes) and raw:
            tokens.append(raw.decode("utf-8", errors="strict"))

    visit(node)
    return tokens


def tokenize_structural_window(block_text: str) -> List[str]:
    """Return the versioned normalized stream used by Data Clumps windows."""
    masked = mask_comments_and_strings(block_text)
    raw_tokens = re.findall(
        r"[A-Za-z_$][A-Za-z0-9_$]*|\d+|==|!=|<=|>=|&&|\|\||::|"
        r"[{}()\[\];,.+\-*/%<>?:=]",
        masked,
    )
    normalized: List[str] = []
    for token in raw_tokens:
        if re.fullmatch(r"\d+", token):
            normalized.append("NUM")
        elif re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token):
            normalized.append(token if token in JAVA_KEYWORDS else "ID")
        else:
            normalized.append(token)
    return normalized


@lru_cache(maxsize=8192)
def _tokenize_clone_cached(block_text: str) -> Tuple[str, ...]:
    prefix = "class __CloneLex { void __cloneLex() {\n"
    suffix = "\n} }"
    snippet = block_text.encode("utf-8")
    prefix_bytes = prefix.encode("utf-8")
    source = prefix_bytes + snippet + suffix.encode("utf-8")
    root = get_parser("java").parse(source).root_node
    start = len(prefix_bytes)
    end = start + len(snippet)
    tokens: List[str] = []

    def visit(node: object) -> None:
        node_type = str(getattr(node, "type", ""))
        if "comment" in node_type:
            return
        children = list(getattr(node, "children", ()) or ())
        if children:
            for child in children:
                visit(child)
            return
        node_start = int(getattr(node, "start_byte", 0))
        node_end = int(getattr(node, "end_byte", 0))
        if node_end <= node_start or node_start < start or node_end > end:
            return
        tokens.append(source[node_start:node_end].decode("utf-8", errors="strict"))

    visit(root)
    return tuple(tokens)


def mask_comments_and_strings(text: str) -> str:
    chars = list(text)
    i = 0
    state = "normal"
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "normal":
            if ch == "/" and nxt == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "line_comment"
                continue
            if ch == "/" and nxt == "*":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "block_comment"
                continue
            if ch == '"':
                nxt2 = chars[i + 2] if i + 2 < len(chars) else ""
                if nxt == '"' and nxt2 == '"':
                    chars[i] = chars[i + 1] = chars[i + 2] = " "
                    i += 3
                    state = "text_block"
                    continue
                chars[i] = " "
                i += 1
                state = "string"
                continue
            if ch == "'":
                chars[i] = " "
                i += 1
                state = "char"
                continue
            i += 1
            continue
        if state == "line_comment":
            if ch == "\n":
                state = "normal"
            else:
                chars[i] = " "
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "normal"
            else:
                if ch != "\n":
                    chars[i] = " "
                i += 1
            continue
        if state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if ch == "\\":
                chars[i] = " "
                if i + 1 < len(chars) and chars[i + 1] != "\n":
                    chars[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                chars[i] = " "
                state = "normal"
            elif ch != "\n":
                chars[i] = " "
            i += 1
            continue
        if state == "text_block":
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            nxt2 = chars[i + 2] if i + 2 < len(chars) else ""
            if ch == '"' and nxt == '"' and nxt2 == '"':
                chars[i] = chars[i + 1] = chars[i + 2] = " "
                i += 3
                state = "normal"
            else:
                if ch != "\n":
                    chars[i] = " "
                i += 1
            continue
    return "".join(chars)


def _scan_java_methods(
    file_path: Path,
    rel_path: str,
    text: str,
    line_starts: Sequence[int],
    classes: Sequence[JavaClassInfo],
) -> List[JavaMethodInfo]:
    methods = []
    callable_kinds = _java_callable_kinds(text)
    idx = 0
    while idx < len(text):
        method_name, paren_idx = _scan_find_method_paren(text, idx, max_chars=None)
        if method_name is None or paren_idx is None:
            break
        paren_end = _scan_match_parens(text, paren_idx)
        if paren_end is None:
            idx = paren_idx + 1
            continue
        token, body_start = _scan_find_body_start(text, paren_end + 1, max_chars=None, disqualify_on_unmatched_paren=True)
        if token != "{" or body_start is None:
            idx = paren_end + 1
            continue
        body_end = _scan_match_braces(text, body_start)
        if body_end is None:
            idx = paren_end + 1
            continue
        name_index = text.rfind(method_name, 0, paren_idx)
        callable_kind = callable_kinds.get(name_index)
        if callable_kind is None:
            # Text such as ``ENUM_VALUE(args) { ... }`` and anonymous-class
            # construction also looks callable to the lightweight scanner.
            # It must not consume the enclosing body and hide real nested
            # method declarations that tree-sitter has already classified.
            idx = paren_end + 1
            continue
        begin_line = _idx_to_line(
            line_starts,
            name_index if name_index >= 0 else paren_idx,
        )
        # Annotations are part of a Java method declaration's stable source
        # span. Including them lets a reviewed annotation-line anchor select
        # an overload without a nearest-line fallback.
        source_lines = text.splitlines()
        preceding = begin_line - 2
        while preceding >= 0 and source_lines[preceding].strip().startswith("@"):
            begin_line = preceding + 1
            preceding -= 1
        end_line = _idx_to_line(line_starts, body_end)
        param_list = _normalize_param_list(text[paren_idx : paren_end + 1])
        signature = f"{method_name}{param_list}"
        body_text = text[body_start : body_end + 1]
        class_info = _find_enclosing_class(classes, begin_line)
        param_names, param_tokens, _ = _parse_parameter_items(signature)
        methods.append(
            JavaMethodInfo(
                file_path=file_path,
                rel_path=rel_path,
                class_name=class_info.class_name if class_info else file_path.stem,
                method_name=method_name,
                signature=signature,
                begin_line=begin_line,
                end_line=end_line,
                body_begin_line=_idx_to_line(line_starts, body_start),
                body_text=body_text,
                is_constructor=callable_kind == "constructor",
                parameter_names=param_names,
                parameter_tokens=param_tokens,
            )
        )
        idx = body_end + 1
    return methods


def _java_callable_kinds(text: str) -> Dict[int, str]:
    """Map callable declaration-name offsets to grammar-defined kinds.

    Constructor identity is a syntactic property: a constructor declaration
    has no return type. Comparing its name with the enclosing class would
    misclassify a legal method that has an explicit return type and happens to
    share the class name. Tree-sitter already distinguishes those declarations,
    including generic and enum constructors, so the source model preserves that
    distinction directly.
    """
    source = text.encode("utf-8")
    root = get_parser("java").parse(source).root_node
    byte_to_char: Dict[int, int] = {}
    byte_offset = 0
    for char_offset, char in enumerate(text):
        byte_to_char[byte_offset] = char_offset
        byte_offset += len(char.encode("utf-8"))
    byte_to_char[byte_offset] = len(text)

    kinds: Dict[int, str] = {}
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in {
            "compact_constructor_declaration",
            "constructor_declaration",
            "method_declaration",
        }:
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.start_byte in byte_to_char:
                kinds[byte_to_char[name_node.start_byte]] = (
                    "constructor"
                    if node.type
                    in {"compact_constructor_declaration", "constructor_declaration"}
                    else "method"
                )
        pending.extend(node.children)
    return kinds


def _extract_class_ranges(
    file_path: Path,
    rel_path: str,
    text: str,
    line_starts: Sequence[int],
) -> List[JavaClassInfo]:
    masked = mask_comments_and_strings(text)
    classes = []
    for match in CLASS_DECL_RE.finditer(masked):
        brace_idx = masked.find("{", match.end())
        if brace_idx < 0:
            continue
        close_idx = _scan_match_braces(text, brace_idx)
        if close_idx is None:
            continue
        parent = match.group(2)
        classes.append(
            JavaClassInfo(
                file_path=file_path,
                rel_path=rel_path,
                class_name=match.group(1),
                parent_name=parent.split(".")[-1] if parent else None,
                begin_line=_idx_to_line(line_starts, match.start()),
                end_line=_idx_to_line(line_starts, close_idx),
            )
        )
    return classes


def _find_enclosing_class(classes: Sequence[JavaClassInfo], line: int) -> Optional[JavaClassInfo]:
    candidates = [cls for cls in classes if cls.begin_line <= line <= cls.end_line]
    if not candidates:
        return None
    return min(candidates, key=lambda cls: (cls.end_line - cls.begin_line, cls.begin_line))


def _parse_parameter_items(signature: str) -> Tuple[List[str], List[str], int]:
    left = signature.find("(")
    right = signature.rfind(")")
    if left < 0 or right <= left:
        return [], [], 0
    inner = signature[left + 1 : right].strip()
    if not inner:
        return [], [], 0
    param_names = []
    param_tokens = []
    for raw in _split_top_level_commas(inner):
        part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
        part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
        part = re.sub(r"\s+", " ", part).strip()
        if not part:
            continue
        chunks = part.split(" ")
        raw_name = chunks[-1].strip().replace("...", "").replace("[]", "")
        type_norm = _normalize_type_name(" ".join(chunks[:-1]).strip())
        if raw_name:
            param_names.append(raw_name)
            param_tokens.append(f"{type_norm}:{_stem_name(raw_name)}")
    return param_names, param_tokens, len(param_names)


def _split_top_level_commas(text: str) -> List[str]:
    items = []
    current = []
    depth_angle = depth_paren = depth_bracket = 0
    for ch in text:
        if ch == "<":
            depth_angle += 1
        elif ch == ">" and depth_angle > 0:
            depth_angle -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]" and depth_bracket > 0:
            depth_bracket -= 1
        if ch == "," and depth_angle == 0 and depth_paren == 0 and depth_bracket == 0:
            token = "".join(current).strip()
            if token:
                items.append(token)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _normalize_type_name(type_text: str) -> str:
    text = re.sub(r"\b(?:public|private|protected|static|final|transient|volatile)\b", " ", type_text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(?:[a-z_][a-z0-9_]*\.)+([A-Za-z_$][A-Za-z0-9_$]*)", r"\1", text)
    return text.replace(" ?", "?").replace("< ", "<").replace(" >", ">") or "Object"


def _stem_name(name: str) -> str:
    lowered = name.lower()
    lowered = re.sub(r"\d+", "", lowered)
    return lowered or name.lower()


def _resolve_java_files(
    project_root: Path,
    *,
    include_tests: bool,
    target_files: Optional[Sequence[Path]],
) -> List[Path]:
    source_layout = None if include_tests else discover_java_source_layout(project_root)
    if target_files:
        resolved = []
        for path in target_files:
            candidate = path if path.is_absolute() else project_root / path
            if (
                candidate.exists()
                and candidate.suffix == ".java"
                and (
                    include_tests
                    or source_layout is None
                    or not source_layout.is_test_path(candidate)
                )
            ):
                resolved.append(candidate.resolve())
        return sorted(set(resolved))
    exclude = set(DEFAULT_EXCLUDE_PATHS)
    files = []
    for path in project_root.rglob("*.java"):
        if not path.is_file() or exclude & set(path.parts):
            continue
        rel_path = str(path.relative_to(project_root)).replace("\\", "/")
        if not include_tests and source_layout is not None and source_layout.is_test_path(rel_path):
            continue
        files.append(path)
    return sorted(files)


def _build_line_starts(text: str) -> List[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _idx_to_line(line_starts: Sequence[int], idx: int) -> int:
    return bisect_right(line_starts, idx)


def _is_preprocessor_continuation(text: str, newline_idx: int) -> bool:
    j = newline_idx - 1
    if j >= 0 and text[j] == "\r":
        j -= 1
    return j >= 0 and text[j] == "\\"


def _scan_find_method_paren(text: str, start_idx: int, max_chars: Optional[int] = 20000):
    end = len(text) if max_chars is None else min(len(text), start_idx + max_chars)
    s = text[start_idx:end]
    i = 0
    state = "normal"
    line_has_non_ws = False
    while i < len(s):
        ch = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if state == "normal":
            if ch == "\n":
                line_has_non_ws = False
                i += 1
                continue
            if not line_has_non_ws:
                if ch.isspace():
                    i += 1
                    continue
                if ch == "#":
                    state = "preprocessor"
                    i += 1
                    continue
            if not ch.isspace():
                line_has_non_ws = True
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == "(":
                j = i - 1
                while j >= 0 and s[j].isspace():
                    j -= 1
                end_id = j
                start_id = end_id
                while start_id >= 0 and (s[start_id].isalnum() or s[start_id] in ["_", "$"]):
                    start_id -= 1
                start_id += 1
                if start_id <= end_id:
                    name = s[start_id : end_id + 1]
                    if IDENT_RE.match(name) and name not in JAVA_CONTROL_KEYWORDS:
                        k = start_id - 1
                        while k >= 0 and s[k].isspace():
                            k -= 1
                        if k >= 0 and s[k] in {"@", "."}:
                            i += 1
                            continue
                        # Skip constructor calls / anonymous class instantiation: new Foo(
                        if k >= 0 and (s[k].isalnum() or s[k] == "_"):
                            kw_end = k
                            kw_start = kw_end
                            while kw_start >= 0 and (s[kw_start].isalnum() or s[kw_start] == "_"):
                                kw_start -= 1
                            kw_start += 1
                            if s[kw_start : kw_end + 1] == "new":
                                i += 1
                                continue
                        return name, start_idx + i
            i += 1
            continue
        state, i, line_has_non_ws = _advance_scan_state(s, i, state, line_has_non_ws)
    return None, None


def _scan_match_parens(text: str, open_paren_idx: int):
    return _scan_match_balanced(text, open_paren_idx, "(", ")")


def _scan_match_braces(text: str, open_brace_idx: int):
    return _scan_match_balanced(text, open_brace_idx, "{", "}")


def _scan_match_balanced(text: str, open_idx: int, open_ch: str, close_ch: str):
    i = open_idx
    depth = 0
    state = "normal"
    line_has_non_ws = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "normal":
            if ch == "\n":
                line_has_non_ws = False
                i += 1
                continue
            if not line_has_non_ws and ch == "#":
                state = "preprocessor"
                i += 1
                continue
            if not ch.isspace():
                line_has_non_ws = True
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i
            i += 1
            continue
        state, i, line_has_non_ws = _advance_scan_state(text, i, state, line_has_non_ws)
    return None


def _scan_find_body_start(text: str, after_idx: int, max_chars: Optional[int] = 20000, disqualify_on_unmatched_paren: bool = False):
    end = len(text) if max_chars is None else min(len(text), after_idx + max_chars)
    i = after_idx
    state = "normal"
    paren_depth = 0
    line_has_non_ws = False
    while i < end:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "normal":
            if ch == "\n":
                line_has_non_ws = False
                i += 1
                continue
            if not line_has_non_ws and ch == "#":
                state = "preprocessor"
                i += 1
                continue
            if not ch.isspace():
                line_has_non_ws = True
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                if disqualify_on_unmatched_paren and paren_depth == 0:
                    return None, None
                paren_depth = max(paren_depth - 1, 0)
            elif ch == "{":
                return ch, i
            elif ch == ";":
                return ch, i
            i += 1
            continue
        state, i, line_has_non_ws = _advance_scan_state(text, i, state, line_has_non_ws)
    return None, None


def _advance_scan_state(text: str, i: int, state: str, line_has_non_ws: bool):
    ch = text[i]
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if state == "preprocessor":
        if ch == "\n" and not _is_preprocessor_continuation(text, i):
            return "normal", i + 1, False
        return state, i + 1, line_has_non_ws
    if state == "line_comment":
        return ("normal", i + 1, False) if ch == "\n" else (state, i + 1, line_has_non_ws)
    if state == "block_comment":
        if ch == "*" and nxt == "/":
            return "normal", i + 2, line_has_non_ws
        return state, i + 1, (False if ch == "\n" else line_has_non_ws)
    if state in {"string", "char"}:
        quote = '"' if state == "string" else "'"
        if ch == "\\":
            return state, i + 2, line_has_non_ws
        if ch == quote:
            return "normal", i + 1, line_has_non_ws
        return state, i + 1, (False if ch == "\n" else line_has_non_ws)
    return "normal", i + 1, line_has_non_ws


def _normalize_param_list(param_list_with_parens: str) -> str:
    s = re.sub(r"\s+", " ", param_list_with_parens).strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\(\s+", "(", s)
    return re.sub(r"\s+\)", ")", s)


def _suspicious_name_reason(name: str, min_len: int, low_info_names: set[str], allow_too_short: bool = True) -> Optional[str]:
    lowered = name.lower()
    if lowered in low_info_names:
        return "low_info_name"
    if allow_too_short and len(name) <= min_len and lowered not in {"i", "j", "k", "x", "y", "z", "id", "ok"}:
        return "too_short"
    if re.fullmatch(r"[a-zA-Z]\d+", name):
        return "letter_digit"
    if re.fullmatch(r"([a-zA-Z])\1{2,}", name):
        return "repeated_char"
    return None


def _is_valid_java_identifier(name: str) -> bool:
    # ``var`` is a restricted type name, not a reserved identifier; it remains
    # legal as a method, parameter, field, or local-variable name.
    return bool(IDENT_RE.match(name)) and (name not in JAVA_KEYWORDS or name == "var")


def _should_exclude_mysterious_path(rel_path: str, profile: str, exclude_tests: bool, exclude_generated: bool) -> bool:
    if profile != "strict":
        return False
    normalized = "/" + rel_path.replace("\\", "/").lower().strip("/") + "/"
    if "/refactor_runs/" in normalized or "/refactors run/" in normalized:
        return True
    if exclude_tests and _is_test_like_path(rel_path):
        return True
    if exclude_generated and _is_generated_like_path(rel_path):
        return True
    return False


def _mysterious_evidence(kind: str, name: str, reason: str) -> str:
    return f"kind={kind}; name={name}; reason={reason}; len={len(name)}"


def _is_test_like_path(rel_path: str) -> bool:
    return standard_test_root(rel_path) is not None


def _is_generated_like_path(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/").lower().strip("/") + "/"
    return any(token in normalized for token in ("/generated/", "/build/generated/", "/target/generated-sources/"))


def _sort_findings(findings: Sequence[JavaSyntacticFinding]) -> List[JavaSyntacticFinding]:
    return sorted(findings, key=lambda item: (item.file, item.begin_line, item.class_name, item.method, item.rule_id, item.evidence))


def _empty_findings() -> Dict[str, List[JavaSyntacticFinding]]:
    return {
        "nested_complexity": [],
        "switch_statements": [],
        "mysterious_name": [],
    }
