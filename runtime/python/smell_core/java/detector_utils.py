"""Compatibility re-export for shared detector normalisation utilities."""
from __future__ import annotations

from ..detector_utils import (  # noqa: F401
    normalize_group,
    normalize_method,
    normalize_path,
    normalize_rel_path,
    parse_group_from_evidence,
    parse_parent_from_evidence,
    parse_structural_expectation,
)
