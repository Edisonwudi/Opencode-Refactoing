from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .detector_utils import (
    normalize_group,
    normalize_qualified_group,
)
from .semantic_detector import (
    DEFAULT_THRESHOLDS,
    SemanticFinding,
    find_data_clump_group_occurrences,
)


def normalize_data_clump_group(group: str) -> str:
    return normalize_qualified_group(group)


def data_clump_finding_group(finding: SemanticFinding) -> str:
    return normalize_qualified_group(
        str(finding.attributes.get("group") or "")
    )


def matching_data_clump_groups(
    findings: Iterable[SemanticFinding],
    *,
    group: str,
) -> set[str]:
    """Resolve an exact group or one globally unique simple-type shorthand."""
    selector = normalize_qualified_group(group)
    if not selector:
        return set()
    candidate_groups = {
        data_clump_finding_group(item)
        for item in findings
        if data_clump_finding_group(item)
    }
    exact = {item for item in candidate_groups if item == selector}
    if exact:
        return exact
    selector_simple = normalize_group(selector)
    return {
        item for item in candidate_groups
        if normalize_group(item) == selector_simple
    }


def same_group_data_clump_findings(
    findings: Iterable[SemanticFinding],
    *,
    group: str,
) -> list[SemanticFinding]:
    target_group = normalize_data_clump_group(group)
    if not target_group:
        return []
    resolved = matching_data_clump_groups(findings, group=target_group)
    if len(resolved) != 1:
        return []
    selected = next(iter(resolved))
    return [
        finding
        for finding in findings
        if data_clump_finding_group(finding) == selected
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
    group: str,
    limit: int | None = None,
) -> dict[str, Any]:
    group = normalize_data_clump_group(group)
    if not group:
        return {
            "success": False,
            "group": "",
            "occurrence_count": 0,
            "occurrences": [],
            "error": "missing data-clump group selector",
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
        "group": data_clump_finding_group(finding),
        "evidence": finding.evidence,
    }
