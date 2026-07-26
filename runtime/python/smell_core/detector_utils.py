"""Shared detector normalisation utilities."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


def normalize_rel_path(path: Path, project_root: Path) -> str:
    """Return a POSIX, lower-cased, dot-stripped relative path string."""
    try:
        return normalize_path(str(path.resolve().relative_to(project_root.resolve())))
    except ValueError:
        return normalize_path(str(path))


def normalize_path(value: str) -> str:
    """Normalise a file path for fuzzy comparison (POSIX, lower-case)."""
    return str(value or "").replace("\\", "/").lstrip("./").lower()


def normalize_method(value: Optional[str]) -> str:
    """Extract the bare method name from a possibly qualified signature."""
    text = str(value or "").strip()
    if not text:
        return ""
    before_params = text.split("(", 1)[0].strip()
    return re.split(r"[.#\s]+", before_params)[-1].lower()


def parse_evidence_value(evidence: str, name: str) -> str:
    """Extract one semicolon-delimited ``name=value`` field from evidence."""
    field = str(name or "").strip()
    if not field:
        return ""
    match = re.search(
        rf"(?:^|;\s*){re.escape(field)}=([^;]+)",
        str(evidence or ""),
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def parse_group_from_evidence(evidence: str) -> str:
    """Extract ``group=...`` from an evidence string, if present."""
    match = re.search(r"(?:^|;\s*)group=([^;]+)", evidence, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def normalize_group(value: str) -> str:
    """Canonicalise a ``type:stem|type:stem`` or ``name|name`` group key."""
    pieces: list[str] = []
    for item in _split_group_members(str(value or "")):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            type_name, name = item.rsplit(":", 1)
            pieces.append(f"{_normalize_group_type(type_name)}:{name.replace(' ', '').lower()}")
        else:
            pieces.append(item.replace(" ", "").lower())
    return "|".join(sorted(pieces))


def _split_group_members(value: str) -> list[str]:
    return [item for item in re.split(r"(?<!\s)\|(?!\s)", value) if item.strip()]


def _normalize_group_type(value: str) -> str:
    compact = str(value or "").replace(" ", "")
    if not compact:
        return ""
    suffix = ""
    while compact.endswith("[]"):
        suffix += "[]"
        compact = compact[:-2]
    compact = compact.replace("|", "or")
    return compact.rsplit(".", 1)[-1].lower() + suffix


def parse_parent_from_evidence(evidence: str) -> str:
    """Extract the primary parent from ``parent=`` or dataset ``parents=`` evidence."""
    match = re.search(r"(?:^|;\s*)parents?=([^;]+)", evidence, flags=re.IGNORECASE)
    if not match:
        return ""
    value = re.split(r"[|,]", match.group(1), maxsplit=1)[0]
    return value.strip().lower()
