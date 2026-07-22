#!/usr/bin/env python3
"""Re-validate every feature_envy CSV row against the alias-folding detector.

For each of the 90 curated samples (c/cpp/python x 30) this runs
``analyze_feature_envy_target`` twice — with alias folding (the shipped
behavior) and without (the pre-folding baseline) — and asserts the folded
profile still hits the strict detector with a positive
``expected_receiver_access`` and the same ``begin_line``, mirroring
``build_envy_name_dataset.validate_envy``.  Alias folding can only inflate
baseline counts (original code may itself cache fields into locals); any
sample that stops hitting the thresholds is listed explicitly.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.feature_envy import (  # noqa: E402
    analyze_feature_envy_target,
    feature_envy_receiver_from_evidence,
)

DATASET_ROOT = Path(
    "/Users/a1-6/Code/Extension_develop/smell_datasets/"
    "final_non_java_dataset_20260511_command_validated_replacements_20260612"
)
LANGUAGES = ("c", "cpp", "python")


def _profile(row: dict, *, fold_aliases: bool) -> dict:
    root = Path(row["project_path"])
    return analyze_feature_envy_target(
        root,
        language=row["language"],
        target_file=root / row["file"],
        method=row["method"],
        line=int(row["begin_line"]),
        expected_receiver=feature_envy_receiver_from_evidence(row["evidence"]),
        fold_aliases=fold_aliases,
    )


def main() -> int:
    failures: list[str] = []
    examples: list[tuple[int, str, int, int]] = []  # (delta, label, raw, folded)
    checked = 0
    for language in LANGUAGES:
        csv_path = DATASET_ROOT / language / "feature_envy_30.csv"
        if not csv_path.is_file():
            failures.append(f"{language}: missing CSV {csv_path}")
            continue
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                checked += 1
                label = f"{language}#{row['sample_id']} {row['project_name']} {row['file']}:{row['begin_line']}"
                folded = _profile(row, fold_aliases=True)
                raw = _profile(row, fold_aliases=False)
                problems = []
                if not folded.get("ok"):
                    problems.append(f"detector error: {folded.get('error')}")
                else:
                    if not folded.get("strict_detector_hit"):
                        problems.append("strict_detector_hit lost")
                    if int(folded.get("expected_receiver_access") or 0) <= 0:
                        problems.append("expected_receiver_access<=0")
                    if folded.get("begin_line") != int(row["begin_line"]):
                        problems.append(f"begin_line moved to {folded.get('begin_line')}")
                if problems:
                    failures.append(f"{label}: {'; '.join(problems)}")
                    continue
                folded_access = int(folded["expected_receiver_access"])
                raw_access = int(raw.get("expected_receiver_access") or 0)
                examples.append((folded_access - raw_access, label, raw_access, folded_access))
    examples.sort(key=lambda item: (-item[0], item[1]))
    print(f"checked={checked} failures={len(failures)}")
    for failure in failures:
        print(f"[FAIL] {failure}")
    print("baseline expected_receiver_access (raw -> folded), top 3 deltas:")
    for delta, label, raw_access, folded_access in examples[:3]:
        print(f"  {label}: {raw_access} -> {folded_access} (+{delta})")
    if failures:
        return 1
    print("feature-envy-dataset-alias-fold-recheck PASS all 90 samples still hit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
