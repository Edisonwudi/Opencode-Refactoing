"""Generic Mysterious Name detection for non-Java languages.

Ports the Java heuristics (``java/syntactic_detector.py``) onto the
tree-sitter facilities in ``analysis.py``.  Findings keep the strict
``kind=...; name=...; reason=...; len=N`` evidence format so the shared
``parse_mysterious_evidence`` can read them unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analysis import (
    iter_function_signatures,
    iter_local_variable_names,
    parse_function_nodes,
)

DEFAULT_MYSTERIOUS_NAME_MIN_LEN = 2

DEFAULT_LOW_INFO_NAMES = frozenset({
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
})
TOO_SHORT_ALLOWLIST = frozenset({"i", "j", "k", "x", "y", "z", "id", "ok"})


@dataclass(frozen=True)
class MysteriousNameFinding:
    file_path: Path
    line: int
    kind: str
    name: str
    reason: str
    evidence: str
    func_start_line: int = 0
    func_end_line: int = 0


def suspicious_name_reason(
    name: str,
    *,
    min_len: int = DEFAULT_MYSTERIOUS_NAME_MIN_LEN,
    allow_too_short: bool = True,
    low_info_names: frozenset = DEFAULT_LOW_INFO_NAMES,
) -> Optional[str]:
    lowered = name.lower()
    if lowered in low_info_names:
        return "low_info_name"
    if allow_too_short and len(name) <= min_len and lowered not in TOO_SHORT_ALLOWLIST:
        return "too_short"
    if re.fullmatch(r"[a-zA-Z]\d+", name):
        return "letter_digit"
    if re.fullmatch(r"([a-zA-Z])\1{2,}", name):
        return "repeated_char"
    return None


def detect_mysterious_names(
    file_path: Path,
    *,
    language: str,
    min_len: int = DEFAULT_MYSTERIOUS_NAME_MIN_LEN,
) -> list[MysteriousNameFinding]:
    file_path = Path(file_path).expanduser().resolve()
    findings: list[MysteriousNameFinding] = []
    signatures = [
        signature
        for signature in iter_function_signatures(file_path.parent, language)
        if signature.file_path.resolve() == file_path
    ]
    for signature in signatures:
        reason = suspicious_name_reason(signature.name, min_len=min_len, allow_too_short=True)
        if reason:
            findings.append(
                _finding(
                    file_path,
                    signature.start_line,
                    "method",
                    signature.name,
                    reason,
                    func_start_line=signature.start_line,
                    func_end_line=signature.end_line,
                )
            )
        for fingerprint in signature.parameter_fingerprints:
            param_name = fingerprint.rsplit(":", 1)[-1].strip()
            if not param_name:
                continue
            reason = suspicious_name_reason(param_name, min_len=min_len, allow_too_short=True)
            if reason:
                findings.append(
                    _finding(
                        file_path,
                        signature.start_line,
                        "param",
                        param_name,
                        reason,
                        func_start_line=signature.start_line,
                        func_end_line=signature.end_line,
                    )
                )
    for function_node, source_bytes in parse_function_nodes(file_path, language):
        body_node = function_node.child_by_field_name("body")
        if body_node is None:
            continue
        func_start_line = function_node.start_point[0] + 1
        func_end_line = function_node.end_point[0] + 1
        for name, line in iter_local_variable_names(body_node, source_bytes, language):
            reason = suspicious_name_reason(name, min_len=min_len, allow_too_short=True)
            if reason:
                findings.append(
                    _finding(
                        file_path,
                        line,
                        "local",
                        name,
                        reason,
                        func_start_line=func_start_line,
                        func_end_line=func_end_line,
                    )
                )
    return findings


def find_matching_name_finding(
    findings: list[MysteriousNameFinding],
    *,
    kind: str,
    name: str,
    scope: Optional[tuple[int, int]] = None,
) -> Optional[MysteriousNameFinding]:
    for finding in findings:
        if finding.name != name or (kind and finding.kind != kind):
            continue
        if scope is not None and finding.func_start_line and finding.func_end_line:
            scope_start, scope_end = scope
            if finding.func_end_line < scope_start or finding.func_start_line > scope_end:
                continue
        return finding
    return None


def _finding(
    file_path: Path,
    line: int,
    kind: str,
    name: str,
    reason: str,
    *,
    func_start_line: int = 0,
    func_end_line: int = 0,
) -> MysteriousNameFinding:
    return MysteriousNameFinding(
        file_path=file_path,
        line=line,
        kind=kind,
        name=name,
        reason=reason,
        evidence=f"kind={kind}; name={name}; reason={reason}; len={len(name)}",
        func_start_line=func_start_line,
        func_end_line=func_end_line,
    )
