"""Lightweight Java smell detector based on text/regex scanning.

Detects long_method, long_parameter_list, nested_complexity,
switch_statements, code_clone_type1, and mysterious_name without
requiring a full AST.  Suitable for fast pre-checks inside the guard
pipeline where tree-sitter is overkill.

For deeper semantic smells (feature_envy, data_clumps,
refused_bequest) see ``java_semantic_detector`` which uses
tree-sitter to build a full project model.
"""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .detector_utils import (
    normalize_group as _normalize_group,
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
    parse_group_from_evidence as _parse_group_from_evidence,
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
    r"\b(?:byte|short|int|long|float|double|boolean|char|String|var|[A-Z][A-Za-z0-9_$<>\[\],.?\s]*)\s+([a-zA-Z_$][A-Za-z0-9_$]*)\s*(?:=|;|,)"
)

DEFAULT_THRESHOLDS = {
    "long_method_ncss": 60,
    "long_parameter_list": 5,
    "cognitive_complexity": 20,
    "switch_density": 10.0,
    "switch_case_count": 8,
    "code_clone_min_tokens": 80,
    "mysterious_name_min_len": 2,
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
    body_text: str
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


@dataclass(frozen=True)
class JavaSyntacticDetectionResult:
    ok: bool
    findings: Dict[str, List[JavaSyntacticFinding]]
    error: str = ""


def run_java_syntactic_detector(
    project_root: Path,
    *,
    include_tests: bool = True,
    target_files: Optional[Sequence[Path]] = None,
    thresholds: Optional[Dict[str, object]] = None,
    include_code_clone: bool = True,
    include_mysterious_name: bool = True,
) -> JavaSyntacticDetectionResult:
    config = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    project_root = project_root.expanduser().resolve()
    try:
        java_files = _resolve_java_files(project_root, include_tests=include_tests, target_files=target_files)
        _, methods = load_project_model(project_root, java_files)
        findings = {
            "long_method": _detect_long_method(methods, int(config["long_method_ncss"])),
            "long_parameter_list": _detect_long_parameter_list(methods, int(config["long_parameter_list"])),
            "nested_complexity": _detect_nested_complexity(methods, int(config["cognitive_complexity"])),
            "switch_statements": _detect_switch_statements(
                methods,
                float(config["switch_density"]),
                int(config["switch_case_count"]),
            ),
            "code_clone_type1": _detect_code_clone(methods, int(config["code_clone_min_tokens"])) if include_code_clone else [],
            "mysterious_name": (
                _detect_mysterious_name(methods, int(config["mysterious_name_min_len"]), DEFAULT_LOW_INFO_NAMES)
                if include_mysterious_name
                else []
            ),
        }
        return JavaSyntacticDetectionResult(ok=True, findings={k: _sort_findings(v) for k, v in findings.items()})
    except Exception as exc:
        return JavaSyntacticDetectionResult(ok=False, findings=_empty_findings(), error=str(exc))


def find_matching_syntactic_finding(
    findings: Sequence[JavaSyntacticFinding],
    *,
    target_file: Path,
    project_root: Path,
    method: Optional[str],
    line: Optional[int],
    original_start_line: Optional[int] = None,
    original_param_count: Optional[int] = None,
    original_param_type_fingerprint: Optional[str] = None,
    evidence: str = "",
) -> Optional[JavaSyntacticFinding]:
    target_rel = _normalize_rel_path(target_file, project_root)
    target_method = _normalize_method(method)
    evidence_kind_name = parse_mysterious_evidence(evidence)
    evidence_group = _parse_group_from_evidence(evidence)
    has_strong_anchor = bool(target_method or evidence_group or evidence_kind_name != ("", ""))
    candidates: List[JavaSyntacticFinding] = []
    for finding in findings:
        if _normalize_path(finding.file) != target_rel:
            continue
        if target_method and _normalize_method(finding.method) != target_method:
            continue
        if not has_strong_anchor and line and finding.begin_line and abs(finding.begin_line - line) > 3:
            continue
        if evidence_group and _normalize_group(_parse_group_from_evidence(finding.evidence)) != _normalize_group(evidence_group):
            continue
        if evidence_kind_name != ("", ""):
            kind, name = parse_mysterious_evidence(finding.evidence)
            target_kind, target_name = evidence_kind_name
            if target_kind and kind and target_kind != kind:
                continue
            if target_name and name != target_name:
                continue
        candidates.append(finding)
    if original_param_type_fingerprint is not None:
        same_signature = [
            finding
            for finding in candidates
            if _finding_parameter_type_fingerprint(finding) == original_param_type_fingerprint
        ]
        if not same_signature:
            return None
        candidates = same_signature
    elif original_param_count is not None:
        same_arity = [
            finding
            for finding in candidates
            if _finding_parameter_count(finding) == original_param_count
        ]
        if not same_arity:
            return None
        candidates = same_arity
    if not candidates:
        return None
    if original_start_line is not None:
        return min(
            candidates,
            key=lambda item: (
                abs((item.begin_line or 0) - original_start_line),
                abs((item.begin_line or 0) - (line or original_start_line)),
            ),
        )
    if line:
        return min(candidates, key=lambda item: abs((item.begin_line or 0) - line))
    return candidates[0]


def _finding_parameter_count(finding: JavaSyntacticFinding) -> Optional[int]:
    evidence_match = re.search(r"param_count=(\d+)", str(finding.evidence or ""))
    if evidence_match:
        return int(evidence_match.group(1))
    score = finding.score
    if isinstance(score, (int, float)) and float(score).is_integer():
        return int(score)
    return None


def _finding_parameter_type_fingerprint(finding: JavaSyntacticFinding) -> Optional[str]:
    signature = str(finding.method or "")
    if "(" not in signature or ")" not in signature:
        return None
    inner = signature.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inner:
        return ""
    parts = _split_top_level_commas(inner)
    normalized: List[str] = []
    for raw in parts:
        part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
        part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
        part = re.sub(r"\s+", " ", part).strip()
        if not part:
            continue
        chunks = part.split(" ")
        type_text = " ".join(chunks[:-1]).strip()
        normalized.append(_normalize_type_name(type_text))
    return ",".join(normalized)


def find_matching_clone_pair(
    findings: Sequence[JavaSyntacticFinding],
    *,
    left_file: Path,
    right_file: Path,
    project_root: Path,
    left_method: Optional[str],
    right_method: Optional[str],
    left_line: Optional[int],
    right_line: Optional[int],
) -> Optional[Tuple[JavaSyntacticFinding, JavaSyntacticFinding]]:
    left = find_matching_syntactic_finding(
        findings,
        target_file=left_file,
        project_root=project_root,
        method=left_method,
        line=left_line,
    )
    right = find_matching_syntactic_finding(
        findings,
        target_file=right_file,
        project_root=project_root,
        method=right_method,
        line=right_line,
    )
    if left is None or right is None:
        return None
    if left.rule_id != right.rule_id:
        return None
    return left, right


def parse_mysterious_evidence(evidence: str) -> Tuple[str, str]:
    text = str(evidence or "")
    strict = re.search(r"kind=([^;]+);\s*name=([^;]+)", text)
    if strict:
        return strict.group(1).strip(), strict.group(2).strip()
    for key, kind in (("param", "param"), ("local", "local"), ("name", "method")):
        match = re.search(rf"(?:^|;\s*){key}=([^;,\s]+)", text)
        if match:
            return kind, match.group(1).strip()
    return "", ""


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


def _detect_long_method(methods: Sequence[JavaMethodInfo], threshold: int) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        ncss = count_non_comment_loc(method.body_text)
        if ncss <= threshold:
            continue
        rows.append(_finding("long_method", method, float(ncss), "custom:long_method_ncss", f"ncss={ncss}; threshold={threshold}"))
    return rows


def _detect_long_parameter_list(methods: Sequence[JavaMethodInfo], threshold: int) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        count = len(method.parameter_names)
        if count <= threshold:
            continue
        rows.append(_finding("long_parameter_list", method, float(count), "custom:long_parameter_list", f"param_count={count}; threshold={threshold}"))
    return rows


def _detect_nested_complexity(methods: Sequence[JavaMethodInfo], threshold: int) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        score = compute_cognitive_complexity(method.body_text)
        if score <= threshold:
            continue
        rows.append(_finding("nested_complexity", method, float(score), "custom:cognitive_complexity", f"complexity={score}; threshold={threshold}"))
    return rows


def _detect_switch_statements(
    methods: Sequence[JavaMethodInfo],
    density_threshold: float,
    case_threshold: int,
) -> List[JavaSyntacticFinding]:
    rows = []
    for method in methods:
        switch_count, case_count, density = compute_switch_metrics(method.body_text)
        if switch_count == 0:
            continue
        if density <= density_threshold and case_count <= case_threshold:
            continue
        score = max(float(case_count), float(density))
        rows.append(
            _finding(
                "switch_statements",
                method,
                score,
                "custom:switch_density_or_case_count",
                f"switch_count={switch_count}; case_count={case_count}; density={density:.2f}",
            )
        )
    return rows


def _detect_code_clone(methods: Sequence[JavaMethodInfo], min_tokens: int) -> List[JavaSyntacticFinding]:
    groups: Dict[str, List[Tuple[JavaMethodInfo, int]]] = defaultdict(list)
    for method in methods:
        tokens = tokenize_clone(method.body_text)
        if len(tokens) < min_tokens:
            continue
        digest = hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()
        groups[digest].append((method, len(tokens)))

    rows = []
    for digest, occurrences in groups.items():
        if len(occurrences) < 2:
            continue
        group_id = digest[:12]
        for method, token_count in occurrences:
            rows.append(
                _finding(
                    "code_clone_type1",
                    method,
                    float(token_count),
                    f"custom:code_clone:{group_id}",
                    f"group_size={len(occurrences)}; token_count={token_count}; min_tokens={min_tokens}",
                )
            )
    return rows


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
        if _is_valid_java_identifier(method.method_name):
            reason = _suspicious_name_reason(method.method_name, min_len, low_info, allow_too_short=True)
            if reason:
                evidence = _mysterious_evidence("method", method.method_name, reason) if strict_mode else f"name={method.method_name}; reason={reason}"
                rows.append(_finding("mysterious_name", method, 1.0, "custom:mysterious_method_name", evidence, end_line=method.begin_line))
        for pname in method.parameter_names:
            if not _is_valid_java_identifier(pname):
                continue
            reason = _suspicious_name_reason(pname, min_len, low_info, allow_too_short=not strict_mode)
            if reason:
                evidence = _mysterious_evidence("param", pname, reason) if strict_mode else f"param={pname}; reason={reason}"
                rows.append(_finding("mysterious_name", method, 1.0, "custom:mysterious_parameter_name", evidence, end_line=method.begin_line))
        masked_body = mask_comments_and_strings(method.body_text)
        for var in VAR_DECL_RE.findall(masked_body):
            if not _is_valid_java_identifier(var):
                continue
            reason = _suspicious_name_reason(var, min_len, low_info, allow_too_short=not strict_mode)
            if reason:
                evidence = _mysterious_evidence("local", var, reason) if strict_mode else f"local={var}; reason={reason}"
                rows.append(_finding("mysterious_name", method, 1.0, "custom:mysterious_local_name", evidence, end_line=method.begin_line))
    return rows


def _finding(
    smell_type: str,
    method: JavaMethodInfo,
    score: float,
    rule_id: str,
    evidence: str,
    *,
    end_line: Optional[int] = None,
) -> JavaSyntacticFinding:
    return JavaSyntacticFinding(
        smell_type=smell_type,
        file=method.rel_path,
        class_name=method.class_name,
        method=method.signature,
        begin_line=method.begin_line,
        end_line=method.end_line if end_line is None else end_line,
        score=score,
        rule_id=rule_id,
        evidence=evidence,
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


def compute_cognitive_complexity(block_text: str) -> int:
    masked = mask_comments_and_strings(block_text)
    # 'case' is excluded: switch itself already counts as a branch.
    # 'do' is excluded: the paired 'while' counts the do-while loop once.
    token_iter = re.finditer(r"\bif\b|\bfor\b|\bwhile\b|\bcatch\b|\bswitch\b|\{|\}", masked)
    depth = 0
    score = 0
    for token in token_iter:
        t = token.group(0)
        if t == "{":
            depth += 1
            continue
        if t == "}":
            depth = max(depth - 1, 0)
            continue
        score += 1 + max(depth - 1, 0)
    return score


def compute_switch_metrics(block_text: str) -> Tuple[int, int, float]:
    masked = mask_comments_and_strings(block_text)
    switch_count = len(re.findall(r"\bswitch\b", masked))
    case_count = len(re.findall(r"\bcase\b", masked))
    density = (case_count / switch_count) if switch_count else 0.0
    return switch_count, case_count, density


def tokenize_clone(block_text: str) -> List[str]:
    masked = mask_comments_and_strings(block_text)
    raw_tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*|\d+|==|!=|<=|>=|&&|\|\||::|[{}()\[\];,.+\-*/%<>?:=]", masked)
    normalized = []
    for token in raw_tokens:
        if re.fullmatch(r"\d+", token):
            normalized.append("NUM")
        elif re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token):
            normalized.append(token if token in JAVA_KEYWORDS else "ID")
        else:
            normalized.append(token)
    return normalized


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
        begin_line = _idx_to_line(line_starts, paren_idx)
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
                body_text=body_text,
                parameter_names=param_names,
                parameter_tokens=param_tokens,
            )
        )
        idx = body_end + 1
    return methods


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
    if target_files:
        resolved = []
        for path in target_files:
            candidate = path if path.is_absolute() else project_root / path
            if candidate.exists() and candidate.suffix == ".java":
                resolved.append(candidate.resolve())
        return sorted(set(resolved))
    exclude = set(DEFAULT_EXCLUDE_PATHS)
    files = []
    for path in project_root.rglob("*.java"):
        if not path.is_file() or exclude & set(path.parts):
            continue
        rel_path = str(path.relative_to(project_root)).replace("\\", "/")
        if not include_tests and _is_test_like_path(rel_path):
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
    return bool(IDENT_RE.match(name)) and name not in JAVA_KEYWORDS


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
    return bool(re.search(r"(?:^|/)(?:test|tests)(?:/|$)", rel_path.replace("\\", "/").lower()))


def _is_generated_like_path(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/").lower().strip("/") + "/"
    return any(token in normalized for token in ("/generated/", "/build/generated/", "/target/generated-sources/"))


def _sort_findings(findings: Sequence[JavaSyntacticFinding]) -> List[JavaSyntacticFinding]:
    return sorted(findings, key=lambda item: (item.file, item.begin_line, item.class_name, item.method, item.rule_id, item.evidence))


def _empty_findings() -> Dict[str, List[JavaSyntacticFinding]]:
    return {
        "long_method": [],
        "long_parameter_list": [],
        "nested_complexity": [],
        "switch_statements": [],
        "code_clone_type1": [],
        "mysterious_name": [],
    }
