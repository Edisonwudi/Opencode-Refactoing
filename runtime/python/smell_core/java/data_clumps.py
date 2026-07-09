from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .detector_utils import normalize_group, parse_group_from_evidence
from .semantic_detector import (
    DEFAULT_THRESHOLDS,
    SemanticFinding,
    find_data_clump_group_occurrences,
)


def data_clump_group_from_evidence(evidence: str) -> str:
    return normalize_group(parse_group_from_evidence(evidence))


def same_group_data_clump_findings(
    findings: Iterable[SemanticFinding],
    *,
    evidence: str,
) -> list[SemanticFinding]:
    target_group = data_clump_group_from_evidence(evidence)
    if not target_group:
        return []
    return [
        finding
        for finding in findings
        if normalize_group(parse_group_from_evidence(finding.evidence)) == target_group
    ]


def data_clump_occurrence_payloads(
    findings: Iterable[SemanticFinding],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    ordered = sorted(findings, key=lambda item: (item.file, item.begin_line, item.method))
    if limit is not None:
        ordered = ordered[:limit]
    return [_data_clump_occurrence_payload(item) for item in ordered]


def detect_data_clump_occurrences(
    project_root: Path,
    *,
    evidence: str,
    limit: int | None = None,
) -> dict[str, Any]:
    group = data_clump_group_from_evidence(evidence)
    if not group:
        return {
            "success": False,
            "group": "",
            "occurrence_count": 0,
            "occurrences": [],
            "error": "missing group=... evidence",
        }
    try:
        matches = find_data_clump_group_occurrences(project_root, group=group)
    except Exception as exc:
        return {
            "success": False,
            "group": group,
            "occurrence_count": 0,
            "occurrences": [],
            "error": f"data clump group detection failed: {exc}",
        }
    return {
        "success": True,
        "group": group,
        "occurrence_count": len(matches),
        "occurrences": data_clump_occurrence_payloads(matches, limit=limit),
    }


def data_clump_occurrence_threshold() -> int:
    return int(DEFAULT_THRESHOLDS["data_clumps_occurrences"])


def _data_clump_occurrence_payload(finding: SemanticFinding) -> dict[str, Any]:
    return {
        "file": finding.file,
        "location": f"{finding.file}:line={finding.begin_line}",
        "class": finding.class_name,
        "method": finding.method,
        "begin_line": finding.begin_line,
        "end_line": finding.end_line,
        "score": finding.score,
        "rule_id": finding.rule_id,
        "evidence": finding.evidence,
    }
