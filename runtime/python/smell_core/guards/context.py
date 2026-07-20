from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class GuardRunContext:
    changed_java_files: List[Path] = field(default_factory=list)
    feature_envy_baseline_findings: List[Dict[str, Any]] = field(default_factory=list)
    feature_envy_baseline_ok: bool = True
    feature_envy_baseline_error: str = ""
    checkpoint_required: bool = False
    checkpoint_smell: str = ""
    checkpoint_id: str = ""
    baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    current_metrics: Dict[str, Any] = field(default_factory=dict)
    metric_delta: Dict[str, Any] = field(default_factory=dict)
    has_production_diff: bool = False
    metric_progress: bool = False
    checkpoint: Dict[str, Any] = field(default_factory=dict)
