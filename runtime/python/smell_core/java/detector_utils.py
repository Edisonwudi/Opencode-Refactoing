"""Shared Java detector normalization utilities."""
from __future__ import annotations

import re

from ..detector_utils import (  # noqa: F401
    normalize_group,
    normalize_qualified_group,
    normalize_method,
    normalize_path,
    normalize_rel_path,
)


def erase_java_type(type_text: str, *, varargs_as_array: bool = True) -> str:
    """Return the Java declaration identity after generic type erasure."""
    text = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", str(type_text or ""))
    text = re.sub(
        r"\b(final|public|protected|private|static|volatile|transient)\b",
        "",
        text,
    )
    if varargs_as_array:
        text = text.replace("...", "[]")
    text = re.sub(r"<.*>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" []", "[]")


def normalize_erased_qualified_group(value: str) -> str:
    """Canonicalize a Java parameter group using erased declared types."""
    members: list[str] = []
    for raw in str(value or "").split("|"):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            members.append(item)
            continue
        type_name, name = item.rsplit(":", 1)
        members.append(f"{erase_java_type(type_name)}:{name}")
    return normalize_qualified_group("|".join(members))
