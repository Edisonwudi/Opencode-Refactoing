#!/usr/bin/env python3
"""Capture every canonical non-Java c000 target Guard baseline.

This audit performs no source edit, model call, build, or test.  It verifies
that every row in the final per-smell CSVs can resolve its explicit target and
produce a current checkpoint baseline on the pinned project checkout.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime/python/bridge/smell_bridge.py"
DEFAULT_DATASET_ROOT = ROOT / "dataset/nonjava"
LANGUAGES = ("python", "c", "cpp")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_rows(dataset_root: Path, language: str) -> list[dict[str, Any]]:
    language_root = dataset_root / language
    selected: list[dict[str, Any]] = []
    for dataset_path in sorted(language_root.glob("*.csv")):
        with dataset_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            selected.append(
                {
                    **row,
                    "dataset_path": str(dataset_path.relative_to(ROOT)),
                }
            )
    if not selected:
        raise SystemExit(f"no dataset rows found for {language}")
    return selected


def _error_status(payload: dict[str, Any], returncode: int) -> str:
    if returncode == 0 and payload.get("success") is True:
        return str(payload.get("status") or "BASELINE_CAPTURED")
    detail = str(payload.get("error") or payload.get("status") or "")
    known = (
        "TARGET_NOT_LOCATED",
        "TARGET_AMBIGUOUS",
        "DETECTOR_PROFILE_MISMATCH",
        "CHECKPOINT_RECAPTURE_REQUIRED",
        "CHECKPOINT_BASELINE_IDENTITY_MISMATCH",
        "CHECKPOINT_BASELINE_CAPTURE_FAILED",
    )
    for status in known:
        if status in detail:
            return status
    return "BASELINE_CAPTURE_FAILED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", required=True, choices=LANGUAGES)
    parser.add_argument("--dataset-root", default=str(DEFAULT_DATASET_ROOT))
    parser.add_argument("--config", default=str(ROOT / "runtime/python/smell_core/defaults/refactor.yaml"))
    parser.add_argument("--projects", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).resolve()
    config = Path(args.config).resolve()
    projects = Path(args.projects).resolve()
    output_root = Path(args.output_root).resolve()
    if not dataset_root.is_dir() or not config.is_file() or not projects.is_file():
        raise SystemExit("dataset root, config, or projects file is missing")
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "rows").mkdir()

    rows = _load_rows(dataset_root, args.language)
    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for ordinal, row in enumerate(rows, 1):
        sample_id = str(row["sample_id"])
        smell = str(row["smell_type"])
        project = str(row["project_name"])
        row_id = f"{ordinal:03d}-{smell}-sid{sample_id}-{project}"
        checkpoint_root = output_root / "rows" / row_id / "checkpoints"
        command = [
            sys.executable,
            str(BRIDGE),
            "capture-baseline",
            "--output-detail",
            "audit",
            "--project-root",
            str(row["project_path"]),
            "--config",
            str(config),
            "--projects",
            str(projects),
            "--smell",
            smell,
            "--location",
            str(row["location"]),
            "--language",
            args.language,
            "--verification-mode",
            "project_full",
        ]
        evidence = str(row.get("evidence") or "")
        if evidence:
            command.extend(["--smell-evidence", evidence])
        context = str(row.get("target_context_json") or "").strip()
        if context:
            parsed_context = json.loads(context)
            command.extend(
                [
                    "--target-context-json",
                    json.dumps(parsed_context, sort_keys=True, separators=(",", ":")),
                ]
            )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMELL_ALLOW_TEST_CHANGES": "0",
                "SMELL_CHECKPOINT_ROOT": str(checkpoint_root),
            }
        )
        try:
            process = subprocess.run(
                command,
                cwd=str(ROOT),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout,
                check=False,
            )
            try:
                payload = json.loads(process.stdout)
            except json.JSONDecodeError:
                payload = {
                    "success": False,
                    "error": "BASELINE_OUTPUT_PARSE_FAILED",
                    "stdout": process.stdout,
                }
            returncode = process.returncode
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            payload = {"success": False, "error": "BASELINE_CAPTURE_TIMEOUT"}
            returncode = 124
            stderr = str(exc)
        status = _error_status(payload, returncode)
        counts[status] += 1
        record = {
            "row_id": row_id,
            "ordinal": ordinal,
            "language": args.language,
            "smell": smell,
            "sample_id": sample_id,
            "project_name": project,
            "project_path": str(row["project_path"]),
            "location": str(row["location"]),
            "dataset_path": str(row["dataset_path"]),
            "returncode": returncode,
            "success": payload.get("success") is True,
            "status": status,
            "error": str(payload.get("error") or ""),
            "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
            "baseline_seal": str(payload.get("baseline_seal") or ""),
            "stderr": stderr,
        }
        records.append(record)
        _write_json(output_root / "rows" / row_id / "result.json", record)

    summary = {
        "schema": "nonjava-target-guard-baseline-audit/v1",
        "created_at": _utc_now(),
        "language": args.language,
        "model_invoked": False,
        "build_test_invoked": False,
        "source_discovery": "explicit dataset locations only",
        "dataset_root": str(dataset_root),
        "counts": {
            "total": len(records),
            "captured": counts.get("BASELINE_CAPTURED", 0),
            "failed": len(records) - counts.get("BASELINE_CAPTURED", 0),
            "status_counts": dict(sorted(counts.items())),
        },
        "records": records,
    }
    _write_json(output_root / "summary.json", summary)
    print(json.dumps(summary["counts"], sort_keys=True))
    return 0 if summary["counts"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
