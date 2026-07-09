from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analysis import FunctionSignature, iter_function_signatures
from .detector_utils import normalize_group, parse_group_from_evidence

DEFAULT_DATA_CLUMPS_OCCURRENCES = 3


@dataclass(frozen=True)
class DataClumpOccurrence:
    file: str
    method: str
    begin_line: int
    end_line: int
    signature_text: str
    evidence: str


def data_clump_group_from_evidence(evidence: str) -> str:
    return normalize_group(parse_group_from_evidence(evidence))


def data_clump_occurrence_threshold() -> int:
    return DEFAULT_DATA_CLUMPS_OCCURRENCES


def detect_data_clump_occurrences(
    project_root: Path,
    *,
    language: str,
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
        signatures = iter_function_signatures(project_root, language)
    except Exception as exc:
        return {
            "success": False,
            "group": group,
            "occurrence_count": 0,
            "occurrences": [],
            "error": f"data clump group detection failed: {exc}",
        }
    occurrences = [
        _occurrence_payload(project_root, signature, group)
        for signature in signatures
        if _signature_contains_group(signature, group)
    ]
    occurrences.sort(key=lambda item: (item["file"], item["begin_line"], item["method"]))
    return {
        "success": True,
        "group": group,
        "occurrence_count": len(occurrences),
        "occurrences": occurrences[:limit] if limit is not None else occurrences,
    }


def _signature_contains_group(signature: FunctionSignature, group: str) -> bool:
    target = set(group.split("|"))
    params: set[str] = set()
    for item in signature.parameter_fingerprints:
        if not item.strip():
            continue
        normalized = normalize_group(item)
        params.add(normalized)
        if normalized.startswith(":"):
            params.add(normalized[1:])
    return bool(target) and target.issubset(params)


def _occurrence_payload(project_root: Path, signature: FunctionSignature, group: str) -> dict[str, Any]:
    rel = signature.file_path.resolve().relative_to(project_root.resolve()).as_posix()
    evidence = f"group={group}; parameters={'|'.join(signature.parameter_fingerprints)}"
    return {
        "file": rel,
        "location": f"{rel}:line={signature.start_line}",
        "class": "",
        "method": signature.name,
        "begin_line": signature.start_line,
        "end_line": signature.end_line,
        "score": len(group.split("|")),
        "rule_id": f"generic:data_clumps:{_short_hash(group)}",
        "evidence": evidence,
        "signature_text": signature.signature_text,
    }


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
