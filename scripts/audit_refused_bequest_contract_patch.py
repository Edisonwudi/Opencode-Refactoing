#!/usr/bin/env python3
"""Replay one saved patch against the Refused Bequest compatibility contract."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402
from smell_core.detector_utils import (  # noqa: E402
    parse_parent_from_evidence,
    parse_target_parameter_count,
)
from smell_core.java.semantic_detector import build_refused_bequest_impact_map  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402


def run_git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def snapshot(
    project: Path,
    sample: dict,
    *,
    target_class_name: str = "",
) -> dict:
    target = parse_location_descriptor(str(sample["location"]), project)
    impact = build_refused_bequest_impact_map(
        project,
        target_file=target.file_path,
        method=target.method,
        line=target.line,
        reported_parent=parse_parent_from_evidence(str(sample["evidence"])),
        target_parameter_count=parse_target_parameter_count(str(sample["evidence"])),
        target_class_name=target_class_name,
    )
    if not impact.get("ok"):
        raise RuntimeError(f"contract snapshot unavailable: {impact}")
    return impact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--sample-json", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    sample = json.loads(args.sample_json.read_text(encoding="utf-8"))

    dirty = run_git(project, "status", "--porcelain")
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise RuntimeError("audit project must be a clean disposable checkout")
    baseline = snapshot(project, sample)
    target_class = str((baseline.get("target") or {}).get("class") or "")
    applied = run_git(project, "apply", "--check", str(args.patch.resolve()))
    if applied.returncode != 0:
        raise RuntimeError(f"saved patch does not apply: {applied.stderr.strip()}")
    applied = run_git(project, "apply", str(args.patch.resolve()))
    if applied.returncode != 0:
        raise RuntimeError(f"saved patch apply failed: {applied.stderr.strip()}")
    try:
        current = snapshot(project, sample, target_class_name=target_class)
        evaluation = evaluate_checkpoint_contract(
            {
                "ok": True,
                "objectives": {"rejection_signals": 1.0},
                "contract_snapshot": baseline["target_contract"],
            },
            {
                "ok": True,
                "objectives": {"rejection_signals": 0.0},
                "contract_snapshot": current["target_contract"],
            },
            has_production_diff=True,
            smell="refused_bequest",
        )
        print(
            json.dumps(
                {
                    "sample_id": sample.get("sample_id"),
                    "project_name": sample.get("project_name"),
                    "target_class": target_class,
                    "contract_preserved": evaluation.semantic_contract_preserved,
                    "reason": evaluation.reason,
                    "semantic_contract": evaluation.semantic_contract_delta,
                },
                sort_keys=True,
            )
        )
    finally:
        reverted = run_git(project, "apply", "-R", str(args.patch.resolve()))
        if reverted.returncode != 0:
            raise RuntimeError(f"saved patch revert failed: {reverted.stderr.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
