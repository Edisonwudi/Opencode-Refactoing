#!/usr/bin/env python3
"""Audit Java oracle targets with the exact pre-c000 Target Guard gate.

The dataset supplies only smell category and target context.  This command
does not call a model, edit a project, write checkpoints, discover smells, or
run the full-project Java detector.  It verifies that the original pinned
source contains exactly one measurable finding at each supplied target.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
SCRIPTS = ROOT / "scripts"
sys.path[:0] = [str(RUNTIME), str(SCRIPTS)]

from run_smell_dataset import _dataset_target_context  # noqa: E402
from smell_core.checkpoints import capture_baseline_finding_snapshot  # noqa: E402
from smell_core.config import (  # noqa: E402
    bundled_refactor_config_path,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.project_revision import (  # noqa: E402
    DEFAULT_REVISIONS_PATH,
    assert_commit_present,
    load_revisions,
    resolve_revision,
    verify_checkout,
)


AUDIT_SCHEMA = "java-target-guard-baseline-audit/v1"
_WORKER_CONFIG: dict[str, Any] = {}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit caller-supplied Java smell targets without model calls or edits."
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=ROOT / "dataset" / "java" / "delivery_schema",
    )
    parser.add_argument("--projects-root", type=Path, default=Path("/opt/projects"))
    parser.add_argument(
        "--projects",
        required=True,
        help="Product project override YAML used by the ordinary runner.",
    )
    parser.add_argument(
        "--project-revisions",
        default=DEFAULT_REVISIONS_PATH,
        help="Authoritative pinned project commit/tree manifest.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runs/java-target-guard-baseline-audit"),
        help="Output stem; .json and .md are written atomically.",
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--smell", action="append", default=[])
    parser.add_argument("--sample-id", action="append", default=[])
    return parser.parse_args()


def _load_rows(
    dataset_root: Path,
    smells: set[str],
    sample_ids: set[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    paths = sorted(dataset_root.glob("*.csv"))
    if not paths:
        raise ValueError(f"no dataset CSV files found under {dataset_root}")
    required = {
        "sample_id",
        "language",
        "smell_type",
        "project_name",
        "location",
    }
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = required.difference(reader.fieldnames or ())
            if missing:
                raise ValueError(
                    f"{path} is missing required columns: {', '.join(sorted(missing))}"
                )
            for raw in reader:
                row = {str(key): str(value or "") for key, value in raw.items()}
                smell = row["smell_type"].strip()
                sample_id = row["sample_id"].strip()
                if smells and smell not in smells:
                    continue
                if sample_ids and sample_id not in sample_ids:
                    continue
                if row["language"].strip().lower() != "java":
                    raise ValueError(f"non-Java row in Java audit: {path}:{sample_id}")
                row["_dataset_file"] = path.name
                rows.append(row)
    identities = [(row["smell_type"], row["sample_id"]) for row in rows]
    if not rows:
        raise ValueError("no rows matched the requested filters")
    if len(set(identities)) != len(identities):
        duplicates = sorted(
            f"{smell}:{sample_id}"
            for (smell, sample_id), count in Counter(identities).items()
            if count > 1
        )
        raise ValueError("duplicate smell/sample identities: " + ", ".join(duplicates))
    return rows


def _git_clean_for_guard(project_root: Path) -> None:
    commands = (
        ["diff", "--quiet", "--"],
        ["diff", "--cached", "--quiet", "--"],
    )
    for args in commands:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(project_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"PROJECT_SOURCE_DIRTY: {project_root}")
    untracked = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(project_root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.java",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if untracked.returncode != 0:
        raise ValueError(f"PROJECT_SOURCE_STATUS_UNAVAILABLE: {project_root}")
    if untracked.stdout:
        raise ValueError(f"PROJECT_UNTRACKED_JAVA_SOURCE: {project_root}")


def _verify_projects(
    rows: list[dict[str, str]],
    projects_root: Path,
    revisions_path: str,
) -> dict[str, dict[str, str]]:
    revisions = load_revisions(revisions_path)
    audits: dict[str, dict[str, str]] = {}
    for project_name in sorted({row["project_name"] for row in rows}):
        project_root = (projects_root / project_name).resolve()
        if not project_root.is_dir():
            raise ValueError(f"PROJECT_ROOT_MISSING: {project_root}")
        revision = resolve_revision(project_name, revisions, revisions_path)
        assert_commit_present(project_root, revision.project_commit)
        audits[project_name] = verify_checkout(project_root, revision)
        _git_clean_for_guard(project_root)
    return audits


def _forbid_full_detector() -> None:
    from smell_core import checkpoint_adapters
    from smell_core.java import semantic_detector

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("FULL_PROJECT_DETECTOR_FORBIDDEN_IN_TARGET_GUARD_AUDIT")

    checkpoint_adapters.run_java_semantic_detector = forbidden
    semantic_detector.run_java_semantic_detector = forbidden


def _worker_init(projects_root: str, projects_path: str) -> None:
    _WORKER_CONFIG.clear()
    _WORKER_CONFIG.update(
        {
            "projects_root": Path(projects_root).resolve(),
            "refactor": load_refactor_config(bundled_refactor_config_path()),
            "projects": load_project_overrides(projects_path),
        }
    )
    _forbid_full_detector()


def _root_cause(message: str) -> str:
    known = (
        "FULL_PROJECT_DETECTOR_FORBIDDEN_IN_TARGET_GUARD_AUDIT",
        "BASELINE_FINDING_NOT_FOUND",
        "TARGET_AMBIGUOUS",
        "ANCESTOR_TYPE_AMBIGUOUS",
        "ANCESTOR_DECLARATION_AMBIGUOUS",
        "ANCESTOR_DECLARATION_NOT_FOUND",
        "TARGET_CONTEXT_INCOMPLETE",
        "TARGET_CLASS_NOT_FOUND",
        "GUARD_SCOPE_TOO_LARGE",
    )
    for code in known:
        if code in message:
            return code
    if ":" in message:
        tail = message.rsplit(":", 1)[-1].strip().split()[0]
        if tail.replace("_", "").isalnum() and tail.upper() == tail:
            return tail
    return "BASELINE_CAPTURE_FAILED"


def _audit_row(row: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    smell = row["smell_type"].strip()
    sample_id = row["sample_id"].strip()
    project_name = row["project_name"].strip()
    result: dict[str, Any] = {
        "identity": f"{smell}:{sample_id}",
        "dataset_file": row["_dataset_file"],
        "smell": smell,
        "sample_id": sample_id,
        "project_name": project_name,
        "location": row["location"].strip(),
        "target_context": _dataset_target_context(row),
    }
    try:
        project_root = (_WORKER_CONFIG["projects_root"] / project_name).resolve()
        config = resolve_run_config(
            refactor_config=_WORKER_CONFIG["refactor"],
            project_overrides=_WORKER_CONFIG["projects"],
            project_root=str(project_root),
            smell=smell,
            location=result["location"],
            cli_language="java",
            verification_mode=(
                row.get("verification_mode", "").strip() or "sample_optimized"
            ),
            sample_test_location=row.get("test_file", "").strip(),
            sample_test_command=row.get("test_command", "").strip(),
            target_context=result["target_context"],
        )
        metrics = capture_baseline_finding_snapshot(config, "")
        result.update(
            {
                "success": True,
                "status": "BASELINE_CAPTURED",
                "reason_code": "",
                "guard_rule_id": str(metrics.get("guard_rule_id") or ""),
                "target_match_count": int(metrics.get("target_match_count") or 0),
                "target_smell_present": metrics.get("target_smell_present") is True,
                "entity_identity": metrics.get("entity_identity") or {},
                "objectives": metrics.get("objectives") or {},
                "witness": metrics.get("witness") or {},
            }
        )
    except Exception as exc:
        message = str(exc)
        result.update(
            {
                "success": False,
                "status": "BASELINE_CAPTURE_FAILED",
                "reason_code": _root_cause(message),
                "error": message,
            }
        )
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return result


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def one_group(items: list[dict[str, Any]]) -> dict[str, Any]:
        elapsed = [float(item["elapsed_seconds"]) for item in items]
        status_counts = Counter(str(item["status"]) for item in items)
        reason_counts = Counter(
            str(item.get("reason_code") or "")
            for item in items
            if not item.get("success")
        )
        captured = status_counts["BASELINE_CAPTURED"]
        return {
            "rows": len(items),
            "baseline_captured": captured,
            "baseline_capture_rate": captured / len(items) if items else 0.0,
            "status_counts": dict(sorted(status_counts.items())),
            "failure_reasons": dict(sorted(reason_counts.items())),
            "elapsed_seconds": {
                "total": round(sum(elapsed), 6),
                "average": round(statistics.fmean(elapsed), 6) if elapsed else 0.0,
                "median": round(statistics.median(elapsed), 6) if elapsed else 0.0,
                "p90": round(_percentile(elapsed, 0.90), 6),
            },
        }

    by_smell: dict[str, Any] = {}
    for smell in sorted({str(row["smell"]) for row in rows}):
        by_smell[smell] = one_group(
            [row for row in rows if str(row["smell"]) == smell]
        )
    return {**one_group(rows), "by_smell": by_smell}


def _render_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Java Target Guard baseline audit",
        "",
        f"- schema: `{payload['schema_version']}`",
        f"- source implementation SHA256: `{payload['implementation_sha256']}`",
        f"- rows: {summary['rows']}",
        f"- baseline captured: {summary['baseline_captured']}/{summary['rows']}",
        "- model calls: 0",
        "- source edits/checkpoints: 0",
        "- full-project detector: forbidden",
        "",
        "| smell | rows | captured | rate | failed | avg s | median s | P90 s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for smell, item in summary["by_smell"].items():
        elapsed = item["elapsed_seconds"]
        lines.append(
            f"| {smell} | {item['rows']} | {item['baseline_captured']} | "
            f"{item['baseline_capture_rate']:.1%} | "
            f"{item['rows'] - item['baseline_captured']} | "
            f"{elapsed['average']:.3f} | {elapsed['median']:.3f} | {elapsed['p90']:.3f} |"
        )
    failures = [row for row in payload["rows"] if not row["success"]]
    if failures:
        lines.extend(
            [
                "",
                "## Baseline failures",
                "",
                "| smell | sample | project | reason | location |",
                "|---|---:|---|---|---|",
            ]
        )
        for row in failures:
            location = str(row["location"]).replace("|", "/")
            lines.append(
                f"| {row['smell']} | {row['sample_id']} | {row['project_name']} | "
                f"{row['reason_code']} | {location} |"
            )
    return "\n".join(lines) + "\n"


def _implementation_sha256() -> str:
    paths = (
        Path(__file__).resolve(),
        ROOT / "runtime" / "python" / "smell_core" / "checkpoints.py",
        ROOT / "runtime" / "python" / "smell_core" / "checkpoint_adapters.py",
        ROOT / "runtime" / "python" / "smell_core" / "java" / "target_guard.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = _parse_args()
    if args.jobs < 1 or args.jobs > 10:
        raise ValueError("--jobs must be between 1 and 10")
    smells = {str(value).strip() for value in args.smell if str(value).strip()}
    sample_ids = {
        str(value).strip() for value in args.sample_id if str(value).strip()
    }
    rows = _load_rows(args.dataset_root.resolve(), smells, sample_ids)
    project_audits = _verify_projects(
        rows,
        args.projects_root.resolve(),
        str(Path(args.project_revisions).resolve()),
    )
    results: list[dict[str, Any]] = []
    if args.jobs == 1:
        _worker_init(
            str(args.projects_root.resolve()),
            str(Path(args.projects).resolve()),
        )
        completed_results = enumerate(map(_audit_row, rows), start=1)
        for completed, result in completed_results:
            results.append(result)
            if completed % 25 == 0 or completed == len(rows):
                captured = sum(item.get("success") is True for item in results)
                print(
                    f"audited {completed}/{len(rows)} captured={captured} "
                    f"failed={completed - captured}",
                    flush=True,
                )
    else:
        with ProcessPoolExecutor(
            max_workers=args.jobs,
            initializer=_worker_init,
            initargs=(
                str(args.projects_root.resolve()),
                str(Path(args.projects).resolve()),
            ),
        ) as executor:
            pending = {executor.submit(_audit_row, row): row for row in rows}
            for completed, future in enumerate(as_completed(pending), start=1):
                result = future.result()
                results.append(result)
                if completed % 25 == 0 or completed == len(rows):
                    captured = sum(item.get("success") is True for item in results)
                    print(
                        f"audited {completed}/{len(rows)} captured={captured} "
                        f"failed={completed - captured}",
                        flush=True,
                    )
    results.sort(key=lambda item: (str(item["smell"]), int(item["sample_id"])))
    summary = _aggregate(results)
    payload = {
        "schema_version": AUDIT_SCHEMA,
        "implementation_sha256": _implementation_sha256(),
        "dataset_root": str(args.dataset_root.resolve()),
        "projects_root": str(args.projects_root.resolve()),
        "projects_config": str(Path(args.projects).resolve()),
        "project_revisions": str(Path(args.project_revisions).resolve()),
        "jobs": args.jobs,
        "contract": {
            "model_calls": 0,
            "source_edits": 0,
            "checkpoint_writes": 0,
            "full_project_detector": "forbidden",
            "input_authority": "caller_smell_and_target_context",
            "acceptance": "exactly_one_measurable_target_finding_present",
        },
        "project_revision_audits": project_audits,
        "summary": summary,
        "rows": results,
    }
    output_json = args.output.with_suffix(".json")
    output_md = args.output.with_suffix(".md")
    _write_atomic(
        output_json,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    _write_atomic(output_md, _render_markdown(payload))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["baseline_captured"] == summary["rows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
