from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class GuardRunContext:
    checkpoint_required: bool = False
    checkpoint_smell: str = ""
    current_metrics: Dict[str, Any] = field(default_factory=dict)
    metric_delta: Dict[str, Any] = field(default_factory=dict)
    has_production_diff: bool = False
    metric_progress: bool = False
    checkpoint: Dict[str, Any] = field(default_factory=dict)
