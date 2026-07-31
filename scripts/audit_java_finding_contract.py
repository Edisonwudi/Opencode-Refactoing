#!/usr/bin/env python3
"""Audit all Java oracle rows against the product detector finding contract."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
BRIDGE_DIR = RUNTIME / "bridge"
SCRIPTS_DIR = ROOT / "scripts"
sys.path[:0] = [str(RUNTIME), str(BRIDGE_DIR), str(SCRIPTS_DIR)]

import smell_core.checkpoint_adapters as adapters  # noqa: E402
from run_smell_dataset import _dataset_target_context  # noqa: E402
from smell_core.config import (  # noqa: E402
    bundled_projects_config_path,
    bundled_refactor_config_path,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.java import semantic_detector  # noqa: E402


@lru_cache(maxsize=None)
def _semantic(project_root: str, include_tests: bool):
    return _ORIGINAL_SEMANTIC(Path(project_root), include_tests=include_tests)


@lru_cache(maxsize=None)
def _model(project_root: str):
    return semantic_detector._build_project_model(Path(project_root), include_tests=False)


def _cached_semantic(project_root: Path, *, include_tests: bool = True, **kwargs: Any):
    del kwargs
    return _semantic(str(project_root.expanduser().resolve()), include_tests)


def _cached_feature_envy(project_root: Path, **kwargs: Any):
    return _ORIGINAL_FEATURE_ENVY(
        project_root,
        **kwargs,
        project_model=_model(str(project_root.expanduser().resolve())),
    )


_ORIGINAL_SEMANTIC = adapters.run_java_semantic_detector
_ORIGINAL_FEATURE_ENVY = adapters.analyze_feature_envy_target


def _rows(dataset_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(dataset_root.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(dict(item) for item in csv.DictReader(handle))
    return rows


def _render_md(summary: dict[str, Any], failures: list[dict[str, Any]]) -> str:
    lines = [
        "# Java finding-contract baseline audit",
        "",
        f"- rows: {summary['rows']}",
        f"- baseline detector hit: {summary['baseline_hit']}/{summary['rows']}",
        f"- baseline metric unavailable: {summary['baseline_metric_unavailable']}",
        f"- original source guard PASS: {summary['original_guard_pass']}/{summary['rows']}",
        f"- evidence-free finding stable: {summary['evidence_free_same_finding']}/{summary['rows']}",
        "",
        "| smell | rows | hit | unavailable | ambiguous | evidence-free stable |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for smell, item in sorted(summary["by_smell"].items()):
        lines.append(
            f"| {smell} | {item['rows']} | {item['hit']} | {item['unavailable']} | "
            f"{item['ambiguous']} | {item['evidence_free_stable']} |"
        )
    if failures:
        lines.extend([
            "",
            "## Non-admitted rows",
            "",
            "| smell | sample | project | reason | candidates |",
            "|---|---:|---|---|---:|",
        ])
        for item in failures:
            lines.append(
                f"| {item['smell']} | {item['sample_id']} | {item['project']} | "
                f"{str(item['reason']).replace('|', '/')} | {item['candidate_count']} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=ROOT / "dataset" / "java" / "delivery_schema",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=Path("/Users/a1-6/Code/Project/Java_Project"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "java-finding-contract-audit-current",
    )
    args = parser.parse_args()

    adapters.run_java_semantic_detector = _cached_semantic
    adapters.analyze_feature_envy_target = _cached_feature_envy
    refactor = load_refactor_config(bundled_refactor_config_path())
    projects = load_project_overrides(bundled_projects_config_path())
    records = _rows(args.dataset_root)
    counts: dict[str, Counter[str]] = {}
    failures: list[dict[str, Any]] = []
    stable_total = 0
    hit_total = 0

    for index, row in enumerate(records, start=1):
        smell = str(row.get("smell_type") or row.get("smell") or "")
        project_name = str(row.get("project_name") or "")
        project_root = (args.projects_root / project_name).resolve()
        counter = counts.setdefault(smell, Counter())
        counter["rows"] += 1
        try:
            config = resolve_run_config(
                refactor_config=refactor,
                project_overrides=projects,
                project_root=str(project_root),
                smell=smell,
                location=str(row.get("location") or ""),
                cli_language="java",
                target_context=_dataset_target_context(row),
            )
            snapshot = adapters.capture_metric_snapshot(config, "")
        except Exception as exc:
            snapshot = {"ok": False, "candidate_count": 0, "error": str(exc)}
            config = None
        candidates = int(snapshot.get("candidate_count") or 0)
        admitted = bool(
            snapshot.get("ok")
            and snapshot.get("finding_present") is True
            and candidates == 1
            and isinstance(snapshot.get("finding_identity"), dict)
            and snapshot.get("finding_identity")
        )
        if admitted:
            hit_total += 1
            counter["hit"] += 1
            assert config is not None
            config.target_context = {}
            config.finding_contract = {
                "entity_identity": dict(snapshot["finding_identity"]),
            }
            without_evidence = adapters.capture_metric_snapshot(config, "")
            stable = bool(
                without_evidence.get("ok")
                and without_evidence.get("finding_present") is True
                and without_evidence.get("detector") == snapshot.get("detector")
            )
            if stable:
                stable_total += 1
                counter["evidence_free_stable"] += 1
            else:
                failures.append({
                    "smell": smell,
                    "sample_id": row.get("sample_id", ""),
                    "project": project_name,
                    "reason": "EVIDENCE_FREE_FINDING_MISMATCH",
                    "candidate_count": int(without_evidence.get("candidate_count") or 0),
                })
        else:
            reason = str(snapshot.get("error") or "")
            if candidates > 1:
                reason = "TARGET_AMBIGUOUS"
                counter["ambiguous"] += 1
            else:
                reason = reason or "BASELINE_FINDING_NOT_FOUND"
                counter["unavailable"] += 1
            failures.append({
                "smell": smell,
                "sample_id": row.get("sample_id", ""),
                "project": project_name,
                "reason": reason,
                "candidate_count": candidates,
            })
        if index % 50 == 0:
            print(f"audited {index}/{len(records)}", flush=True)

    summary = {
        "rows": len(records),
        "baseline_hit": hit_total,
        "baseline_metric_unavailable": len(records) - hit_total,
        "original_guard_pass": 0,
        "evidence_free_same_finding": stable_total,
        "by_smell": {
            smell: {
                "rows": values["rows"],
                "hit": values["hit"],
                "unavailable": values["unavailable"],
                "ambiguous": values["ambiguous"],
                "evidence_free_stable": values["evidence_free_stable"],
            }
            for smell, values in counts.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps({"summary": summary, "failures": failures}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.output.with_suffix(".md").write_text(_render_md(summary, failures), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if hit_total == len(records) and stable_total == len(records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
