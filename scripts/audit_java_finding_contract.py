#!/usr/bin/env python3
"""Audit all Java oracle rows against the product detector finding contract."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
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
    bundled_refactor_config_path,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
    select_project_override,
)
from smell_core.checkpoint_contract import (  # noqa: E402
    checkpoint_gate_result,
    evaluate_checkpoint_contract,
)
from smell_core.java.source_layout import (  # noqa: E402
    JavaSourceLayout,
    discover_java_source_layout,
)
from smell_core.resolution_plan import build_resolution_plan  # noqa: E402


def _rows(dataset_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(dataset_root.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(dict(item) for item in csv.DictReader(handle))
    return rows


def _render_md(
    summary: dict[str, Any],
    failures: list[dict[str, Any]],
    audit_context: dict[str, Any],
) -> str:
    projects_config = dict(audit_context["projects_config"])
    lines = [
        "# Java finding-contract baseline audit",
        "",
        f"- projects config: `{projects_config['path']}`",
        f"- projects config SHA256: `{projects_config['sha256']}`",
        f"- configured project entries: {projects_config['entry_count']}",
        f"- rows: {summary['rows']}",
        f"- baseline detector hit: {summary['baseline_hit']}/{summary['rows']}",
        f"- baseline metric unavailable: {summary['baseline_metric_unavailable']}",
        f"- target ambiguous: {summary['target_ambiguous']}",
        f"- original source guard PASS: {summary['original_guard_pass']}/{summary['rows']}",
        f"- evidence-free finding stable: {summary['evidence_free_same_finding']}/{summary['rows']}",
        f"- production-source provenance valid: {summary['production_source_provenance']}/{summary['rows']}",
        "- execution: every row is captured once with CSV evidence and recaptured with empty evidence; both calls use the target Guard.",
        "",
        "| smell | rows | hit | unavailable | ambiguous | evidence-free stable | production provenance | original PASS |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for smell, item in sorted(summary["by_smell"].items()):
        lines.append(
            f"| {smell} | {item['rows']} | {item['hit']} | {item['unavailable']} | "
            f"{item['ambiguous']} | {item['evidence_free_stable']} | "
            f"{item['production_source_provenance']} | {item['original_guard_pass']} |"
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


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _source_layout_manifest(source_layout: JavaSourceLayout) -> dict[str, Any]:
    contract = {
        "test_roots": list(source_layout.test_roots),
        "test_files": list(source_layout.test_files),
        "test_globs": list(source_layout.test_globs),
        "test_glob_excludes": list(source_layout.test_glob_excludes),
        "verification_files": list(source_layout.verification_files),
        "auxiliary_roots": list(source_layout.auxiliary_roots),
    }
    return {
        "sha256": hashlib.sha256(_canonical(contract).encode("utf-8")).hexdigest(),
        "test_root_count": len(source_layout.test_roots),
        "test_file_count": len(source_layout.test_files),
        "test_glob_count": len(source_layout.test_globs),
        "verification_file_count": len(source_layout.verification_files),
    }


_PRODUCTION_PROVENANCE_POLICY = "java-product-source-provenance-v1"
_JAVA_PRODUCT_DETECTORS = frozenset({
    "java_ast_ncss",
    "java_exact_clone_product_detector",
    "java_syntactic_detector",
    "python_semantic_detector",
})
_SOURCE_PATH_FIELDS = frozenset({
    "file",
    "left_file",
    "right_file",
    "source_file",
    "target_file",
    "caller_file",
    "callee_file",
})


def _snapshot_production_source_provenance(
    snapshot: dict[str, Any],
    *,
    smell: str,
    project_root: Path,
    source_layout: JavaSourceLayout,
    resolution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate explicit product/profile declarations and every contract path.

    This deliberately inspects values with source-path semantics.  It never
    guesses provenance from arbitrary key names, so harmless fields such as
    ``latest`` cannot be mistaken for test-derived detector state.
    """
    violations: list[str] = []
    detector = str(snapshot.get("detector") or "")
    profile = snapshot.get("detector_profile")
    if detector not in _JAVA_PRODUCT_DETECTORS:
        violations.append(f"UNDECLARED_JAVA_PRODUCT_DETECTOR:{detector or '<missing>'}")
    if not isinstance(profile, dict):
        violations.append("PRODUCT_DETECTOR_PROFILE_MISSING")
        profile = {}
    expected_profile_prefix = f"java-product/{smell}/"
    if not str(profile.get("id") or "").startswith(expected_profile_prefix):
        violations.append("PRODUCT_DETECTOR_PROFILE_ID_MISMATCH")
    if str(profile.get("language") or "") != "java":
        violations.append("PRODUCT_DETECTOR_LANGUAGE_MISMATCH")
    if str(profile.get("smell") or "") != smell:
        violations.append("PRODUCT_DETECTOR_SMELL_MISMATCH")
    if str(profile.get("source_layout") or "") != "static-build-descriptor-roles-v4":
        violations.append("PRODUCT_SOURCE_LAYOUT_PROFILE_MISMATCH")
    if str(profile.get("selector_input") or "") != "validated-target-context-only-v4":
        violations.append("PRODUCT_SELECTOR_PROFILE_MISMATCH")
    if str(profile.get("smell_evidence") or "") != "audit-only":
        violations.append("PRODUCT_EVIDENCE_PROFILE_MISMATCH")
    implementation = profile.get("implementation")
    if not isinstance(implementation, dict) or len(str(implementation.get("sha256") or "")) != 64:
        violations.append("PRODUCT_IMPLEMENTATION_PROFILE_MISSING")

    # Traverse the complete snapshot so a newly introduced closure/catalog
    # cannot silently escape this audit.  Only exact source-path field names
    # carry path semantics; unrelated keys such as ``latest`` and the
    # implementation profile's ``path`` remain ordinary metadata.
    sections: list[tuple[str, Any]] = [("snapshot", snapshot)]
    if isinstance(resolution_plan, dict):
        sections.append(("resolution_plan.worklist", resolution_plan.get("worklist")))

    checked_references = 0
    checked_paths: set[str] = set()
    identity_paths = list(
        _declared_source_paths(snapshot.get("finding_identity"), "finding_identity")
    )
    if not identity_paths:
        violations.append("FINDING_IDENTITY_SOURCE_PATH_MISSING")
    for section_name, value in sections:
        for trail, raw_path in _declared_source_paths(value, section_name):
            checked_references += 1
            normalized, error = _production_source_path(
                raw_path,
                project_root=project_root,
                source_layout=source_layout,
            )
            if error:
                violations.append(f"{error}:{trail}:{raw_path}")
            elif normalized:
                checked_paths.add(normalized)
    return {
        "ok": not violations,
        "policy": _PRODUCTION_PROVENANCE_POLICY,
        "detector": detector,
        "profile_id": str(profile.get("id") or ""),
        "checked_source_references": checked_references,
        "checked_unique_source_paths": len(checked_paths),
        "violations": violations,
    }


def _declared_source_paths(value: Any, trail: str):
    if isinstance(value, dict):
        for key, item in value.items():
            child_trail = f"{trail}.{key}"
            if key in _SOURCE_PATH_FIELDS and isinstance(item, str) and item.strip():
                yield child_trail, item.strip()
            elif isinstance(item, (dict, list)):
                yield from _declared_source_paths(item, child_trail)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _declared_source_paths(item, f"{trail}[{index}]")


def _production_source_path(
    raw_path: str,
    *,
    project_root: Path,
    source_layout: JavaSourceLayout,
) -> tuple[str, str]:
    root = project_root.expanduser().resolve()
    candidate = Path(raw_path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return "", "SOURCE_PATH_OUTSIDE_PROJECT"
    if resolved.suffix.casefold() != ".java" or not resolved.is_file():
        return "", "SOURCE_PATH_NOT_JAVA_SOURCE"
    if source_layout.is_test_path(relative):
        return "", "TEST_SOURCE_IN_PRODUCT_CONTRACT"
    return relative, ""


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
        required=True,
        help="Directory containing the pinned Java project checkouts named by the dataset.",
    )
    parser.add_argument(
        "--projects-config",
        type=Path,
        required=True,
        help="Required product project configuration supplying read-only build symbol roots.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runs" / "java-finding-contract-audit",
    )
    parser.add_argument(
        "--smell",
        action="append",
        default=[],
        help="Audit only the named smell; repeat for multiple smells.",
    )
    parser.add_argument(
        "--sample-id",
        action="append",
        default=[],
        help="Audit only the named sample id; repeat for multiple rows.",
    )
    args = parser.parse_args()

    projects_config_path = args.projects_config.expanduser().resolve()
    if not projects_config_path.is_file():
        parser.error(f"--projects-config is not a readable file: {projects_config_path}")
    refactor = load_refactor_config(bundled_refactor_config_path())
    projects = load_project_overrides(str(projects_config_path))
    records = _rows(args.dataset_root)
    selected_smells = {str(item).strip() for item in args.smell if str(item).strip()}
    if selected_smells:
        records = [
            row for row in records
            if str(row.get("smell_type") or row.get("smell") or "") in selected_smells
        ]
    selected_sample_ids = {
        str(item).strip() for item in args.sample_id if str(item).strip()
    }
    if selected_sample_ids:
        records = [
            row for row in records
            if str(row.get("sample_id") or "") in selected_sample_ids
        ]
    if not records:
        raise ValueError("no Java oracle rows matched the requested smell filters")
    configured_roots: dict[str, str] = {}
    source_layout_manifests: dict[str, dict[str, Any]] = {}
    for project_name in sorted({str(row.get("project_name") or "") for row in records}):
        project_root = (args.projects_root / project_name).resolve()
        override = select_project_override(projects, project_root)
        if override is None:
            parser.error(
                "--projects-config has no matching product entry for "
                f"{project_name}: {project_root}"
            )
        configured_roots[project_name] = str(override.root.resolve())
        source_layout_manifests[project_name] = _source_layout_manifest(
            discover_java_source_layout(project_root)
        )
    audit_context = {
        "dataset_root": str(args.dataset_root.expanduser().resolve()),
        "projects_root": str(args.projects_root.expanduser().resolve()),
        "selected_smells": sorted(selected_smells),
        "selected_sample_ids": sorted(selected_sample_ids),
        "projects_config": {
            "path": str(projects_config_path),
            "sha256": hashlib.sha256(projects_config_path.read_bytes()).hexdigest(),
            "entry_count": len(projects),
            "matched_roots": configured_roots,
        },
        "production_source_provenance": {
            "policy": _PRODUCTION_PROVENANCE_POLICY,
            "source_layout_profile": "static-build-descriptor-roles-v4",
            "path_fields": sorted(_SOURCE_PATH_FIELDS),
            "validated_snapshot": "recursive-exact-source-path-fields",
            "worklist": "resolution_plan.worklist",
            "project_layouts": source_layout_manifests,
        },
    }
    counts: dict[str, Counter[str]] = {}
    failures: list[dict[str, Any]] = []
    stable_total = 0
    hit_total = 0
    original_pass_total = 0
    production_provenance_total = 0

    for index, row in enumerate(records, start=1):
        smell = str(row.get("smell_type") or row.get("smell") or "")
        location = str(row.get("location") or "").strip()
        if smell in {
            "long_method",
            "long_parameter_list",
            "nested_complexity",
            "switch_statements",
        } and ":method=" not in location:
            raise ValueError(
                f"{smell} dataset location must contain an explicit method selector: "
                f"{location!r}"
            )
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
                location=location,
                cli_language="java",
                verification_mode=str(row.get("verification_mode") or "project_full"),
                sample_test_location=str(row.get("test_file") or ""),
                sample_test_command=str(row.get("test_command") or ""),
                target_context=_dataset_target_context(row),
            )
            # The first call deliberately supplies the CSV evidence. Product
            # adapters must ignore it; the second call proves the same finding
            # is emitted with an empty evidence string.
            snapshot = adapters.capture_metric_snapshot(
                config,
                str(row.get("evidence") or ""),
            )
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
            without_evidence = adapters.capture_metric_snapshot(config, "")
            stable = bool(
                without_evidence.get("ok")
                and without_evidence.get("finding_present") is True
                and without_evidence.get("detector") == snapshot.get("detector")
                and _canonical(without_evidence.get("detector_profile"))
                == _canonical(snapshot.get("detector_profile"))
                and _canonical(without_evidence.get("finding_identity"))
                == _canonical(snapshot.get("finding_identity"))
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
            source_layout = discover_java_source_layout(config.project_root)
            baseline_plan = build_resolution_plan(
                smell,
                finding_contract={"entity_identity": snapshot["finding_identity"]},
                baseline_metrics=snapshot,
                current_metrics=snapshot,
            )
            current_plan = build_resolution_plan(
                smell,
                finding_contract={"entity_identity": without_evidence["finding_identity"]},
                baseline_metrics=without_evidence,
                current_metrics=without_evidence,
            )
            baseline_provenance = _snapshot_production_source_provenance(
                snapshot,
                smell=smell,
                project_root=config.project_root,
                source_layout=source_layout,
                resolution_plan=baseline_plan,
            )
            current_provenance = _snapshot_production_source_provenance(
                without_evidence,
                smell=smell,
                project_root=config.project_root,
                source_layout=source_layout,
                resolution_plan=current_plan,
            )
            if baseline_provenance["ok"] and current_provenance["ok"]:
                production_provenance_total += 1
                counter["production_source_provenance"] += 1
            else:
                failures.append({
                    "smell": smell,
                    "sample_id": row.get("sample_id", ""),
                    "project": project_name,
                    "reason": "PRODUCTION_SOURCE_PROVENANCE_FAILED",
                    "candidate_count": candidates,
                    "details": {
                        "baseline": baseline_provenance,
                        "evidence_free": current_provenance,
                    },
                })
            unchanged_delta = evaluate_checkpoint_contract(
                snapshot,
                without_evidence,
                has_production_diff=False,
                smell=smell,
            ).to_dict()
            unchanged_gate = checkpoint_gate_result(
                smell,
                {
                    "required": True,
                    "checkpoint_id": "audit-c000",
                    "adapter": snapshot.get("adapter"),
                    "baseline_metrics": snapshot,
                    "current_metrics": without_evidence,
                    "delta": unchanged_delta,
                    "production_diff": [],
                },
            )
            if unchanged_gate is None:
                original_pass_total += 1
                counter["original_guard_pass"] += 1
                failures.append({
                    "smell": smell,
                    "sample_id": row.get("sample_id", ""),
                    "project": project_name,
                    "reason": "ORIGINAL_SOURCE_GUARD_PASS",
                    "candidate_count": candidates,
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
        "target_ambiguous": sum(values["ambiguous"] for values in counts.values()),
        "original_guard_pass": original_pass_total,
        "evidence_free_same_finding": stable_total,
        "production_source_provenance": production_provenance_total,
        "by_smell": {
            smell: {
                "rows": values["rows"],
                "hit": values["hit"],
                "unavailable": values["unavailable"],
                "ambiguous": values["ambiguous"],
                "evidence_free_stable": values["evidence_free_stable"],
                "original_guard_pass": values["original_guard_pass"],
                "production_source_provenance": values["production_source_provenance"],
            }
            for smell, values in counts.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps(
            {
                "audit_context": audit_context,
                "summary": summary,
                "failures": failures,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    args.output.with_suffix(".md").write_text(
        _render_md(summary, failures, audit_context),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    accepted = bool(
        hit_total == len(records)
        and stable_total == len(records)
        and original_pass_total == 0
        and production_provenance_total == len(records)
    )
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
