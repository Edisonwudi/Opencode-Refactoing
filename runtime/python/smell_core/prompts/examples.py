"""Few-shot refactoring example retrieval for IDEA-backed tasks."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..config import ResolvedRunConfig

_EXAMPLES_CACHE: Optional[Dict[str, List[Dict[str, Any]]]] = None
_EXAMPLES_PATH = Path(__file__).resolve().parent.parent.parent / "refactor_paths_samples" / "refactor_paths.yaml"


def load_refactor_examples() -> Dict[str, List[Dict[str, Any]]]:
    """Load and cache the refactor-path example database."""
    global _EXAMPLES_CACHE
    if _EXAMPLES_CACHE is not None:
        return _EXAMPLES_CACHE
    if not _EXAMPLES_PATH.exists():
        _EXAMPLES_CACHE = {}
        return _EXAMPLES_CACHE
    with open(_EXAMPLES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _EXAMPLES_CACHE = data if isinstance(data, dict) else {}
    return _EXAMPLES_CACHE


def retrieve_refactor_examples(config: ResolvedRunConfig) -> List[Dict[str, Any]]:
    """Return all verified local examples for the current Java smell."""
    if config.language != "java":
        return []
    examples_by_smell = load_refactor_examples()
    candidates = examples_by_smell.get(config.smell, [])
    return [entry for entry in candidates if isinstance(entry, dict)]


def retrieve_refactor_example(config: ResolvedRunConfig) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper returning the first local example for the smell."""
    examples = retrieve_refactor_examples(config)
    return examples[0] if examples else None
