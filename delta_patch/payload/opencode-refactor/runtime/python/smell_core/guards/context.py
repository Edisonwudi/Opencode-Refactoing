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
    feature_envy_checkpoint_required: bool = False
    feature_envy_checkpoint_id: str = ""
    feature_envy_baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    feature_envy_current_metrics: Dict[str, Any] = field(default_factory=dict)
    feature_envy_metric_delta: Dict[str, Any] = field(default_factory=dict)
    feature_envy_has_production_diff: bool = False
    feature_envy_metric_progress: bool = False
