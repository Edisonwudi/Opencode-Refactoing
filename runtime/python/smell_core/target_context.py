"""Validated selector-only context for product smell findings.

Target context may narrow detector candidates, but it must never contain a
detector verdict, a threshold, or an expected refactoring outcome.
"""
from __future__ import annotations

import json
from typing import Any, Mapping


TARGET_CONTEXT_KEYS = frozenset({
    "symbol_kind",
    "symbol_name",
    "container_method",
    "receiver_type",
    "group",
    "parent",
    "target_class",
    "target_parameter_count",
})

FORBIDDEN_TARGET_CONTEXT_KEYS = frozenset({
    "score",
    "threshold",
    "metric",
    "metrics",
    "finding_present",
    "structural_expectation",
    "refactor_path",
})


def validate_target_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a normalized selector context or reject unsupported fields."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("target context must be a JSON object")
    normalized = {str(key): item for key, item in value.items()}
    keys = set(normalized)
    forbidden = sorted(keys.intersection(FORBIDDEN_TARGET_CONTEXT_KEYS))
    if forbidden:
        raise ValueError(
            "target context cannot contain detector verdict fields: "
            + ", ".join(forbidden)
        )
    unknown = sorted(keys.difference(TARGET_CONTEXT_KEYS))
    if unknown:
        raise ValueError("unsupported target context fields: " + ", ".join(unknown))
    result: dict[str, Any] = {}
    for key, item in normalized.items():
        if key == "target_parameter_count":
            if isinstance(item, bool) or not str(item).isdigit():
                raise ValueError("target_parameter_count must be a non-negative integer")
            result[key] = int(item)
            continue
        if not isinstance(item, str):
            raise ValueError(f"target context field '{key}' must be a string")
        cleaned = item.strip()
        if cleaned:
            result[key] = cleaned
    return result


def parse_target_context_json(value: str | None) -> dict[str, Any]:
    """Parse the only serialized target-context runtime input."""
    if not str(value or "").strip():
        return {}
    parsed = json.loads(str(value))
    return validate_target_context(parsed)
