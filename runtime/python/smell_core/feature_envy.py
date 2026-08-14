"""Generic Feature Envy analysis for non-Java languages.

Tree-sitter based counterpart of the Java ``analyze_feature_envy_target``:
member accesses are grouped by the root receiver identifier instead of a
resolved type (there is no type resolution for python/c/cpp), so
``foreign_by_type`` maps receiver *names* to access counts.

Access counting applies simple-alias folding (see
``analysis.iter_effective_member_accesses``): caching a receiver field chain
into a local (``newbuffer = p->buffer``) does not reduce the measured count —
later reads of the alias are folded back onto the original receiver, so only
genuinely moving behavior lowers the metrics.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .analysis import (
    count_meaningful_lines,
    extract_effective_member_accesses,
    extract_member_accesses,
    extract_simple_aliases,
    extract_snippet,
    split_top_level_params,
)
from .location import LocationTarget

# Aligned with the Java detector thresholds (java/semantic_detector.py).
FEATURE_ENVY_MIN_LOC = 5
FEATURE_ENVY_MIN_FOREIGN_ACCESS = 4
FEATURE_ENVY_FOREIGN_RATIO = 0.6

_LOCAL_RECEIVERS = {
    "python": {"self", "cls"},
    "cpp": {"this"},
}
_TYPE_QUALIFIERS = {"const", "volatile", "static", "struct", "class", "enum", "unsigned", "signed"}


def feature_envy_receiver_from_evidence(evidence: str) -> str:
    """Expected envied receiver: ``envied_receiver=<name>`` wins over ``envied_type=<type>``."""
    text = str(evidence or "")
    for key in ("envied_receiver", "envied_type"):
        match = re.search(rf"(?:^|;\s*){key}=([^;]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def analyze_feature_envy_target(
    project_root: Path,
    *,
    language: str,
    target_file: Path,
    method: Optional[str] = None,
    line: Optional[int] = None,
    expected_receiver: str = "",
    exact_receiver_selector: bool = False,
    fold_aliases: bool = True,
) -> dict[str, Any]:
    """Threshold-independent Feature Envy metrics for one non-Java function.

    With ``fold_aliases`` (the default) member accesses are counted after
    simple-alias folding; ``fold_aliases=False`` exposes the raw counts for
    diagnostics and baseline comparisons.
    """
    target_file = Path(target_file).expanduser().resolve()
    root = Path(project_root).expanduser().resolve()
    try:
        project_path = target_file.relative_to(root)
    except ValueError:
        project_path = target_file
    target = LocationTarget(
        raw=str(target_file),
        project_path=project_path,
        file_path=target_file,
        line=line,
        method=method,
    )
    snippet = extract_snippet(target, language)
    if snippet is None:
        return {
            "ok": False,
            "error": "target_method_not_found",
            "file": str(target_file),
            "method": str(method or ""),
            "line": line,
        }
    raw_accesses = extract_member_accesses(target, language) or []
    if fold_aliases:
        accesses = extract_effective_member_accesses(target, language) or []
    else:
        accesses = raw_accesses
    method_loc = count_meaningful_lines(snippet.body_text, language)
    local_names = _LOCAL_RECEIVERS.get(language, set())
    foreign_by_receiver: dict[str, int] = {}
    local_access = 0
    for access in accesses:
        if not access.receiver:
            continue  # unresolved root expression; counted in the total only
        if access.receiver in local_names:
            local_access += 1
        else:
            foreign_by_receiver[access.receiver] = foreign_by_receiver.get(access.receiver, 0) + 1
    total = len(accesses)
    dominant_receiver = ""
    dominant_count = 0
    for receiver, count in sorted(foreign_by_receiver.items()):
        if count > dominant_count:
            dominant_receiver, dominant_count = receiver, count
    ratio = dominant_count / total if total else 0.0
    if exact_receiver_selector:
        # Product target_context freezes a receiver *root name*.  Do not let a
        # missing root silently turn into a same-spelled type or alias match.
        # Type/evidence resolution remains available only to offline dataset
        # migration callers that have not yet materialized the selector.
        expected_access = foreign_by_receiver.get(str(expected_receiver).strip(), 0)
        expected_resolved = ""
    else:
        expected_access, expected_resolved = _expected_receiver_access(
            foreign_by_receiver,
            snippet.signature_text,
            language,
            expected_receiver,
            aliases=(extract_simple_aliases(target, language) or {}) if fold_aliases else {},
        )
    expected_ratio = expected_access / total if total else 0.0
    expected_strict_hit = (
        method_loc >= FEATURE_ENVY_MIN_LOC
        and expected_access >= FEATURE_ENVY_MIN_FOREIGN_ACCESS
        and expected_ratio >= FEATURE_ENVY_FOREIGN_RATIO
    )
    strict_hit = (
        method_loc >= FEATURE_ENVY_MIN_LOC
        and dominant_count >= FEATURE_ENVY_MIN_FOREIGN_ACCESS
        and ratio >= FEATURE_ENVY_FOREIGN_RATIO
    )
    return {
        "ok": True,
        "detector": "tree_sitter_generic",
        "language": language,
        "file": str(target_file),
        "method": str(method or ""),
        "begin_line": snippet.start_line,
        "end_line": snippet.end_line,
        "method_loc": method_loc,
        "total_member_access": total,
        "local_access": local_access,
        "alias_folded_access": (len(accesses) - len(raw_accesses)) if fold_aliases else 0,
        "foreign_by_type": dict(sorted(foreign_by_receiver.items())),
        "dominant_receiver_type": dominant_receiver,
        "dominant_receiver_access": dominant_count,
        "dominant_receiver_ratio": round(ratio, 6),
        "expected_receiver_type": expected_receiver,
        "expected_receiver_name": expected_receiver if exact_receiver_selector else "",
        "expected_receiver_access": expected_access,
        "expected_receiver_ratio": round(expected_ratio, 6),
        "expected_receiver_strict_hit": expected_strict_hit,
        "expected_receiver_resolved": expected_resolved,
        "strict_detector_hit": strict_hit,
    }


def _expected_receiver_access(
    foreign_by_receiver: dict[str, int],
    signature_text: str,
    language: str,
    expected_receiver: str,
    *,
    aliases: Optional[dict[str, str]] = None,
) -> tuple[int, str]:
    expected = str(expected_receiver or "").strip()
    if not expected:
        return 0, ""
    matching = {expected} if expected in foreign_by_receiver else set()
    # The evidence receiver may itself be a local alias of a parameter field
    # (``struct win32op *win32op = base->evbase;``): folding attributes its
    # accesses to the root receiver, so follow the alias map for the lookup.
    resolved = _resolve_alias_name(expected, aliases or {})
    if resolved and resolved in foreign_by_receiver:
        matching.add(resolved)
    # ``envied_type=`` fallback: match the receiver whose declared parameter
    # type (c/cpp declaration text, python annotation) names the envied type.
    expected_simple = _simple_type_name(expected)
    if expected_simple:
        for name, type_text in _parameter_type_map(signature_text, language).items():
            if name in foreign_by_receiver and _simple_type_name(type_text) == expected_simple:
                matching.add(name)
    access = sum(foreign_by_receiver.get(name, 0) for name in matching)
    return access, resolved if resolved != expected else ""


def _resolve_alias_name(name: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    current = name
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _parameter_type_map(signature_text: str, language: str) -> dict[str, str]:
    if "(" not in signature_text or ")" not in signature_text:
        return {}
    inner = signature_text.split("(", 1)[1].rsplit(")", 1)[0].strip()
    mapping: dict[str, str] = {}
    for raw in split_top_level_params(inner):
        part = raw.strip()
        if not part or part in {"void", "*", "/"}:
            continue
        if language == "python":
            part = re.sub(r"=.*", "", part).strip()
            if ":" in part:
                name_text, type_text = part.split(":", 1)
            else:
                name_text, type_text = part, ""
            name = re.sub(r"^[*]+", "", name_text).strip()
            if name:
                mapping[name] = type_text.strip()
            continue
        match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", part)
        if not match:
            continue
        type_text = re.sub(r"\s*[*&]+\s*$", "", part[: match.start()]).strip()
        mapping[match.group(1)] = type_text
    return mapping


def _simple_type_name(type_text: str) -> str:
    text = re.sub(r"[<({].*", "", str(type_text or "")).strip()
    tokens = [token for token in re.split(r"\s+", text) if token and token not in _TYPE_QUALIFIERS]
    if not tokens:
        return ""
    return tokens[-1].rstrip("*&").rsplit("::", 1)[-1].strip()
