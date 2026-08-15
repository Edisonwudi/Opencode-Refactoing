#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smell_core.config import (  # noqa: E402
    bundled_projects_config_path,
    bundled_refactor_config_path,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.checkpoints import (  # noqa: E402
    capture_checkpoint_baseline,
    checkpoint_location,
    checkpoint_task_root,
    compute_c000_baseline_seal,
    finalize_checkpoint,
    prepare_checkpoint,
)
from smell_core.checkpoint_adapters import CHECKPOINT_SMELLS  # noqa: E402
from smell_core.checkpoint_contract import checkpoint_feedback_highlights  # noqa: E402
from smell_core.resolution_plan import resolution_plan_next_action  # noqa: E402
from smell_core.guards import (  # noqa: E402
    GuardRunContext,
    dead_code_checkpoint_absence_allowed,
    god_class_relative_reduction,
    run_build_test_guard,
    run_focused_preflight,
    run_smell_guards,
    validate_java_strict_verification_contract,
)
from smell_core.loop_policy import (  # noqa: E402
    REPAIRABLE_CATEGORY_GROUPS,
    resolve_command_payload,
)
from smell_core.target_context import parse_target_context_json  # noqa: E402


VERIFY_DECISION_SCHEMA = "smell.verify.decision/v1"
BASELINE_DECISION_SCHEMA = "smell.baseline.decision/v1"
GUARD_PROGRESS_SCHEMA = "smell.guard-progress/v1"
DECISION_MAX_BYTES = 64 * 1024
GUARD_EVIDENCE_MAX_BYTES = 2 * 1024 * 1024
DECISION_TEXT_LIMIT = 512
DECISION_HIGHLIGHT_LIMIT = 3


def _config_path(value: Optional[str], env_name: str, bundled) -> str:
    raw = value or os.environ.get(env_name)
    return str(Path(raw).expanduser().resolve()) if raw else str(bundled())


def _projects_path(value: Optional[str]) -> str:
    return _config_path(value, "SMELL_PROJECTS", bundled_projects_config_path)


def _refactor_config_path(value: Optional[str]) -> str:
    return _config_path(value, "SMELL_CONFIG", bundled_refactor_config_path)


def _target_context_arg(value: Optional[str]) -> Dict[str, Any]:
    return parse_target_context_json(value)


def _verify_artifact_dir(args: argparse.Namespace, project_root: Path) -> Path:
    raw_root = getattr(args, "artifact_root", None) or os.environ.get("SMELL_ARTIFACT_ROOT")
    root = Path(raw_root).expanduser() if raw_root else project_root / ".smell-artifacts"
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    artifact_dir = root.resolve() / f"verify-{timestamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _write_json_artifact(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return str(path)


def _write_guard_evidence_artifact(path: Path, payload: Any) -> str:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) >= GUARD_EVIDENCE_MAX_BYTES:
        raise ValueError(
            "GUARD_EVIDENCE_TOO_LARGE: "
            f"{len(encoded)} bytes exceeds {GUARD_EVIDENCE_MAX_BYTES}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return str(path)


def _write_text_artifact(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    # surrogateescape keeps non-UTF-8 bytes (diff/build output) byte-exact on disk.
    path.write_text(content, encoding="utf-8", errors="surrogateescape")
    return str(path)


def _bounded_text(value: Any, *, limit: int = DECISION_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    head = max(1, int((limit - 5) * 0.7))
    tail = max(1, limit - head - 5)
    return f"{text[:head]} ... {text[-tail:]}"


def _bounded_strings(
    values: Any,
    *,
    count: int = DECISION_HIGHLIGHT_LIMIT,
    limit: int = DECISION_TEXT_LIMIT,
) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    output: list[str] = []
    for value in values:
        rendered = _bounded_text(value, limit=limit)
        if rendered and rendered not in output:
            output.append(rendered)
        if len(output) >= count:
            break
    return output


def _compact_scalar_mapping(value: Any, *, count: int = 32) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, bool) or item is None or isinstance(item, (int, float)):
            output[str(key)] = item
        elif isinstance(item, str):
            output[str(key)] = _bounded_text(item, limit=256)
        else:
            continue
        if len(output) >= count:
            break
    return output


def _compact_objectives(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    allowed = {
        "before",
        "after",
        "baseline",
        "current",
        "delta",
        "reduction",
        "relative_reduction",
        "improved",
    }
    for name, item in value.items():
        if isinstance(item, bool) or isinstance(item, (int, float)):
            output[str(name)] = item
        elif isinstance(item, dict):
            compact = {
                str(key): nested
                for key, nested in item.items()
                if key in allowed and isinstance(nested, (bool, int, float))
            }
            if compact:
                output[str(name)] = compact
        if len(output) >= 64:
            break
    return output


def _compact_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output = _compact_scalar_mapping(value)
    objectives = _compact_objectives(value.get("objectives"))
    if objectives:
        output["objectives"] = objectives
    finding_identity = _compact_scalar_mapping(value.get("finding_identity"), count=20)
    if finding_identity:
        output["finding_identity"] = finding_identity
    entity_identity = _compact_scalar_mapping(value.get("entity_identity"), count=20)
    if entity_identity:
        output["entity_identity"] = entity_identity
    successor = value.get("successor")
    if isinstance(successor, dict):
        output["successor"] = _compact_scalar_mapping(successor, count=12)
    return output


def _compact_finding_contract(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output: dict[str, Any] = {}
    for key in (
        "guard_rule_id",
        "guard_profile_hash",
        "target_id",
        "detector_id",
        "detector_profile_hash",
        "finding_id",
    ):
        if value.get(key) not in (None, ""):
            output[key] = _bounded_text(value.get(key), limit=256)
    entity = _compact_scalar_mapping(value.get("entity_identity"), count=20)
    if entity:
        output["entity_identity"] = entity
    baseline = _compact_objectives(value.get("baseline_metrics"))
    if not baseline:
        baseline = _compact_objectives(value.get("baseline_objectives"))
    if baseline:
        output[
            "baseline_objectives"
            if "baseline_objectives" in value
            else "baseline_metrics"
        ] = baseline
    profile = value.get("guard_profile") or value.get("detector_profile")
    if isinstance(profile, dict):
        output[
            "guard_profile" if "guard_profile" in value else "detector_profile"
        ] = _compact_scalar_mapping(profile, count=16)
    return output or None


def _compact_resolution_plan(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output = _compact_scalar_mapping(value, count=32)
    for key in ("next_action", "route_family", "detector_blocker"):
        if value.get(key) not in (None, ""):
            output[key] = _bounded_text(value.get(key))
    deficits = _bounded_strings(value.get("objective_deficits"), count=8, limit=256)
    if deficits:
        output["objective_deficits"] = deficits
    forbidden = _bounded_strings(value.get("forbidden"), count=4, limit=256)
    if forbidden:
        output["forbidden"] = forbidden
    # Detailed Guard evidence remains in guard-evidence.json.  The decision
    # channel carries only its cardinality and next action.
    if isinstance(value.get("worklist"), list):
        output["worklist_count"] = len(value["worklist"])
    return output or None


def _compact_metric_budget(value: Any) -> list[dict[str, Any]]:
    """Keep only bounded scalar planning inputs for the first edit."""
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            continue
        metric = _bounded_text(raw_item.get("metric"), limit=96)
        unit = _bounded_text(raw_item.get("unit"), limit=48)
        boundary_key = (
            "passing_max"
            if raw_item.get("passing_max") is not None
            else "passing_exclusive_max"
        )
        if raw_item.get(boundary_key) is None:
            continue
        allowed_keys = (
            "metric",
            "current",
            boundary_key,
            "required_reduction",
            "unit",
        )
        compact: dict[str, Any] = {"metric": metric, "unit": unit}
        for key in ("current", boundary_key, "required_reduction"):
            raw_value = raw_item.get(key)
            if isinstance(raw_value, bool) or isinstance(raw_value, int):
                compact[key] = raw_value
            elif isinstance(raw_value, float) and math.isfinite(raw_value):
                compact[key] = raw_value
            elif isinstance(raw_value, str) and raw_value.strip():
                compact[key] = _bounded_text(raw_value, limit=96)
        if not metric or not unit or any(key not in compact for key in allowed_keys):
            continue
        output.append({key: compact[key] for key in allowed_keys})
        if len(output) >= 12:
            break
    return output


def _compact_baseline_resolution_plan(value: Any) -> Optional[dict[str, Any]]:
    """Expose immutable route constraints and numeric budget, never closure lists."""
    if not isinstance(value, dict):
        return None
    output: dict[str, Any] = {}
    for key in ("route_family", "detector_blocker"):
        if value.get(key) not in (None, ""):
            output[key] = _bounded_text(value.get(key), limit=128)
    forbidden = _bounded_strings(value.get("forbidden"), count=4, limit=256)
    if forbidden:
        output["forbidden"] = forbidden
    metric_budget = _compact_metric_budget(value.get("metric_budget"))
    if metric_budget:
        output["metric_budget"] = metric_budget
    return output or None


def _compact_contract_summary(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output = _compact_scalar_mapping(value, count=24)
    for key, item in value.items():
        if isinstance(item, list):
            output[f"{key}_count"] = len(item)
    return output or None


def _compact_test_changes(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output = _compact_scalar_mapping(value, count=24)
    for key in (
        "added",
        "changed",
        "deleted",
        "verification_config_added",
        "verification_config_changed",
        "verification_config_deleted",
        "test_strength_violations",
    ):
        if isinstance(value.get(key), list):
            output[f"{key}_count"] = len(value[key])
    cleanup = value.get("transient_test_artifact_cleanup")
    if isinstance(cleanup, dict):
        output["transient_test_artifact_cleanup"] = {
            "policy": _bounded_text(cleanup.get("policy"), limit=128),
            "removed_count": int(cleanup.get("removed_count") or 0),
            "removed_bytes": int(cleanup.get("removed_bytes") or 0),
        }
    return output or None


def _artifact_index(artifacts: dict[str, str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for name, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        entry: dict[str, Any] = {"path": raw_path}
        try:
            entry["bytes"] = path.stat().st_size
        except OSError:
            entry["bytes"] = None
        output[name] = entry
    return output


def _assert_decision_size(payload: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    size = len(rendered.encode("utf-8"))
    if size >= DECISION_MAX_BYTES:
        raise ValueError(
            f"DECISION_PAYLOAD_TOO_LARGE: {size} bytes exceeds {DECISION_MAX_BYTES}"
        )
    return payload


def _resolve(args: argparse.Namespace):
    refactor_config = load_refactor_config(_refactor_config_path(args.config))
    project_overrides = load_project_overrides(_projects_path(args.projects))
    smell_evidence = getattr(args, "smell_evidence", None) or os.environ.get("SMELL_EVIDENCE", "")
    explicit_target_context = _target_context_arg(
        getattr(args, "target_context_json", None)
        or os.environ.get("SMELL_TARGET_CONTEXT_JSON", "")
    )
    resolved = resolve_run_config(
        refactor_config=refactor_config,
        project_overrides=project_overrides,
        project_root=args.project_root,
        project_override_root=getattr(args, "project_override_root", None),
        smell=args.smell,
        location=args.location,
        cli_language=args.language or os.environ.get("SMELL_LANGUAGE", ""),
        verification_mode=getattr(args, "verification_mode", None)
        or os.environ.get("SMELL_VERIFICATION_MODE", "")
        or "",
        sample_test_location=getattr(args, "sample_test_location", None)
        or os.environ.get("SMELL_SAMPLE_TEST_LOCATION", ""),
        sample_test_command=getattr(args, "sample_test_command", None)
        or os.environ.get("SMELL_SAMPLE_TEST_COMMAND", ""),
        target_context=explicit_target_context,
    )
    if smell_evidence and resolved.language != "java":
        for guard in resolved.profile.guards:
            guard["evidence"] = smell_evidence
    return resolved


def _compact_delta(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output = _compact_scalar_mapping(value, count=24)
    objectives = _compact_objectives(value.get("objectives"))
    if objectives:
        output["objectives"] = objectives
    changed = value.get("changed_production_source_files")
    if isinstance(changed, list):
        output["changed_production_source_file_count"] = len(changed)
    return output or None


def _compact_best_partial(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output = _compact_scalar_mapping(value, count=20)
    objectives = _compact_objectives(value.get("objectives"))
    if objectives:
        output["objectives"] = objectives
    return output or None


def _compact_checkpoint(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    output: dict[str, Any] = {}
    for key in (
        "schema_version",
        "contract_version",
        "checkpoint_id",
        "parent",
        "kind",
        "required",
        "reason",
        "smell",
        "location",
        "adapter",
        "baseline_checkpoint",
        "production_diff",
        "accepted",
        "progress",
        "resolution",
        "verify_status",
        "build_test_success",
        "best_checkpoint",
        "best_partial_eligible",
        "restorable",
        "current_is_best_partial",
        "regressed_from_best_partial",
    ):
        item = value.get(key)
        if isinstance(item, str):
            output[key] = _bounded_text(item, limit=256)
        elif isinstance(item, (bool, int, float)) or item is None and key in value:
            output[key] = item
    delta = _compact_delta(value.get("delta"))
    if delta:
        output["delta"] = delta
    current = _compact_metrics(value.get("current_metrics"))
    if current:
        output["current_metrics"] = current
    guard_contract = _compact_finding_contract(value.get("guard_contract"))
    if guard_contract:
        output["guard_contract"] = guard_contract
    else:
        finding = _compact_finding_contract(value.get("finding_contract"))
        if finding:
            output["finding_contract"] = finding
    plan = _compact_resolution_plan(value.get("resolution_plan"))
    if plan:
        output["resolution_plan"] = plan
    best = _compact_best_partial(value.get("best_partial"))
    if best:
        output["best_partial"] = best
    changed = value.get("changed_production_source_files")
    if isinstance(changed, list):
        output["changed_production_source_file_count"] = len(changed)
    return output or None


def _compact_smell_guard(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"success": False, "failure_count": 1, "results": []}
    results: list[dict[str, Any]] = []
    raw_results = value.get("results") if isinstance(value.get("results"), list) else []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        compact: dict[str, Any] = {
            "type": _bounded_text(item.get("type"), limit=128),
            "success": bool(item.get("success")),
        }
        if item.get("message") not in (None, ""):
            compact["message"] = _bounded_text(item.get("message"))
        details = item.get("details")
        if isinstance(details, dict):
            selected: dict[str, Any] = {}
            for key in (
                "detector",
                "reason",
                "checkpoint_id",
                "has_production_diff",
                "metric_progress",
                "target_missing",
            ):
                nested = details.get(key)
                if isinstance(nested, str):
                    selected[key] = _bounded_text(nested, limit=256)
                elif isinstance(nested, (bool, int, float)):
                    selected[key] = nested
            metric_delta = _compact_delta(details.get("metric_delta"))
            if metric_delta:
                selected["metric_delta"] = metric_delta
            if selected:
                compact["details"] = selected
        results.append(compact)
        if len(results) >= 3:
            break
    return {
        "success": bool(value.get("success")),
        "failure_count": int(value.get("failure_count") or 0),
        "retry_hint": _bounded_text(value.get("retry_hint")),
        "results": results,
    }


def _failure_fingerprint(payload: dict[str, Any], failure_pack: Any) -> str:
    checkpoint = payload.get("checkpoint")
    delta = checkpoint.get("delta") if isinstance(checkpoint, dict) else None
    pack = failure_pack if isinstance(failure_pack, dict) else {}
    guard = _compact_smell_guard(payload.get("smell_guard"))
    source = {
        "status": payload.get("status") or "",
        "resolution": payload.get("resolution") or "",
        "failure_category": pack.get("failure_category") or "",
        "failure_group": pack.get("failure_group") or "",
        "next_action": _bounded_text(pack.get("next_action")),
        "guard_messages": [
            item.get("message") or ""
            for item in guard.get("results") or []
            if isinstance(item, dict)
        ],
        "delta": _compact_delta(delta),
    }
    rendered = json.dumps(source, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _compact_failure_pack(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    return {
        "failure_category": _bounded_text(value.get("failure_category"), limit=128),
        "failure_group": _bounded_text(value.get("failure_group"), limit=128),
        "retryable": bool(value.get("retryable")),
        "verify_status": _bounded_text(value.get("verify_status"), limit=128),
        "artifact_paths": {
            str(key): str(path)
            for key, path in (value.get("artifact_paths") or {}).items()
            if isinstance(path, str) and path
        }
        if isinstance(value.get("artifact_paths"), dict)
        else {},
        "highlights": _bounded_strings(value.get("highlights")),
        "next_action": _bounded_text(value.get("next_action")),
        "recommendations": _bounded_strings(value.get("recommendations")),
        "repair_contract": _compact_scalar_mapping(value.get("repair_contract"), count=12),
    }


def _baseline_decision_payload(
    baseline: dict[str, Any],
    *,
    resolved: Any,
    baseline_seal: str,
) -> dict[str, Any]:
    location = checkpoint_location(resolved)
    manifest = (
        checkpoint_task_root(resolved.project_root, resolved.smell, location)
        / "c000-baseline"
        / "manifest.json"
    )
    payload = {
        "schema_version": BASELINE_DECISION_SCHEMA,
        "success": True,
        "status": "BASELINE_CAPTURED",
        "smell": resolved.smell,
        "checkpoint_id": baseline.get("checkpoint_id"),
        "adapter": baseline.get("adapter"),
        "metrics": _compact_metrics(baseline.get("metrics")),
        "resolution_plan": _compact_baseline_resolution_plan(
            baseline.get("resolution_plan")
        ),
        "test_change_policy": _compact_contract_summary(baseline.get("test_change_contract")),
        "verification_policy": _compact_contract_summary(baseline.get("verification_contract")),
        "baseline_seal": baseline_seal,
        "artifacts": {"baseline_manifest": str(manifest)},
        "artifact_index": _artifact_index({"baseline_manifest": str(manifest)}),
    }
    if isinstance(baseline.get("guard_contract"), dict):
        payload["guard_contract"] = _compact_finding_contract(
            baseline.get("guard_contract")
        )
    else:
        payload["finding_contract"] = _compact_finding_contract(
            baseline.get("finding_contract")
        )
    return _assert_decision_size(payload)


def cmd_capture_baseline(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    allow_test_changes = bool(
        getattr(args, "allow_test_changes", False)
        or os.environ.get("SMELL_ALLOW_TEST_CHANGES") == "1"
    )
    if allow_test_changes and resolved.verification_mode != "project_full":
        raise ValueError(
            "TEST_CHANGE_REQUIRES_PROJECT_FULL: allow_test_changes requires "
            "project_full verification"
    )
    if resolved.smell not in CHECKPOINT_SMELLS:
        payload = {
            "success": True,
            "status": "BASELINE_NOT_REQUIRED",
            "smell": resolved.smell,
        }
        if getattr(args, "output_detail", "decision") == "decision":
            payload["schema_version"] = BASELINE_DECISION_SCHEMA
            return _assert_decision_size(payload)
        return payload
    strict_violations = validate_java_strict_verification_contract(resolved)
    if strict_violations:
        raise ValueError(
            "JAVA_VERIFICATION_CONTRACT_INVALID: "
            + ", ".join(str(item.get("code") or "") for item in strict_violations)
        )
    baseline = capture_checkpoint_baseline(
        resolved,
        getattr(args, "smell_evidence", "") or os.environ.get("SMELL_EVIDENCE", ""),
        allow_test_changes=allow_test_changes,
    )
    baseline_seal = compute_c000_baseline_seal(baseline)
    if getattr(args, "output_detail", "decision") == "decision":
        return _baseline_decision_payload(
            baseline,
            resolved=resolved,
            baseline_seal=baseline_seal,
        )
    payload = {
        "success": True,
        "status": "BASELINE_CAPTURED",
        "smell": resolved.smell,
        "checkpoint_id": baseline.get("checkpoint_id"),
        "adapter": baseline.get("adapter"),
        "metrics": baseline.get("metrics"),
        "resolution_plan": baseline.get("resolution_plan"),
        "test_change_contract": baseline.get("test_change_contract"),
        "verification_contract": baseline.get("verification_contract"),
        "baseline_seal": baseline_seal,
    }
    if isinstance(baseline.get("guard_contract"), dict):
        payload["guard_contract"] = baseline.get("guard_contract")
    else:
        payload["finding_contract"] = baseline.get("finding_contract")
    return payload


def cmd_guard_progress(args: argparse.Namespace) -> dict[str, Any]:
    """Evaluate the frozen source Guard without build/test or snapshots."""
    resolved = _resolve(args)
    language = str(resolved.language or "").strip().lower()
    location = str(getattr(args, "location", "") or "").strip().lower()
    java_source = bool(
        language == "java" or re.search(r"\.java(?::|\b)", location)
    )
    applicable = bool(
        (java_source or language in {"python", "c", "cpp"})
        and resolved.smell in CHECKPOINT_SMELLS
        and resolved.locations
    )
    if not applicable:
        return {
            "schema_version": GUARD_PROGRESS_SCHEMA,
            "success": True,
            "status": "GUARD_PROGRESS_NOT_APPLICABLE",
            "applicable": False,
            "checkpoint_required": False,
            "source_guard_passed": None,
            "ready_for_project_full": True,
            "project_full_executed": False,
            "metric_budget": [],
            "next_action": "",
        }

    evidence = (
        getattr(args, "smell_evidence", "")
        or os.environ.get("SMELL_EVIDENCE", "")
    )
    baseline_seal = str(
        getattr(args, "baseline_seal", "")
        or os.environ.get("SMELL_BASELINE_SEAL", "")
    ).strip()
    guard_context, checkpoint = _checkpoint_context(
        resolved,
        evidence,
        baseline_seal,
        persist=False,
    )
    checkpoint_payload = checkpoint if isinstance(checkpoint, dict) else {}
    guard_results = run_smell_guards(resolved, guard_context)
    failed_guard_count = sum(
        1
        for result in guard_results
        if not isinstance(result, dict) or result.get("success") is not True
    )
    source_guard_passed = bool(
        checkpoint_payload.get("required") is True
        and guard_results
        and failed_guard_count == 0
    )
    resolution_plan = (
        checkpoint_payload.get("resolution_plan")
        if isinstance(checkpoint_payload.get("resolution_plan"), dict)
        else {}
    )
    metric_budget = _compact_metric_budget(
        resolution_plan.get("metric_budget")
    )
    next_action = _guard_progress_next_action(
        metric_budget,
        source_guard_passed=source_guard_passed,
    )
    return {
        "schema_version": GUARD_PROGRESS_SCHEMA,
        "success": source_guard_passed,
        "status": (
            "GUARD_PROGRESS_PASSED"
            if source_guard_passed
            else "GUARD_PROGRESS_REQUIRED"
        ),
        "applicable": True,
        "checkpoint_required": True,
        "source_guard_passed": source_guard_passed,
        "ready_for_project_full": source_guard_passed,
        "project_full_executed": False,
        "guard_failure_count": failed_guard_count,
        "metric_budget": metric_budget,
        "next_action": _bounded_text(next_action),
    }


def cmd_focused_preflight_progress(args: argparse.Namespace) -> dict[str, Any]:
    """Run only the configured focused gate in an isolated worktree.

    This is editing feedback, never acceptance evidence. It deliberately
    reuses the project-full snapshot/worktree path so build output cannot
    mutate the candidate tree.
    """
    resolved = _resolve(args)
    command = resolved.focused_preflight
    if (
        resolved.language not in {"python", "c", "cpp"}
        or resolved.verification_mode != "project_full"
        or (not command.command and not command.script)
    ):
        return run_focused_preflight(resolved)

    evidence = (
        getattr(args, "smell_evidence", "")
        or os.environ.get("SMELL_EVIDENCE", "")
    )
    baseline_seal = str(
        getattr(args, "baseline_seal", "")
        or os.environ.get("SMELL_BASELINE_SEAL", "")
    ).strip()
    _, checkpoint = _checkpoint_context(
        resolved,
        evidence,
        baseline_seal,
        persist=False,
    )
    baseline_project_commit = (
        str(checkpoint.get("baseline_project_commit") or "")
        if isinstance(checkpoint, dict)
        else ""
    )
    declared_test_paths = [
        item.strip()
        for item in str(resolved.sample_test_location or "").split(";")
        if item.strip()
    ]
    snapshot = _snapshot_project(
        resolved.project_root,
        declared_test_paths=declared_test_paths,
        base_commit=baseline_project_commit or "HEAD",
    )
    change_audit = (
        snapshot.get("change_audit") if isinstance(snapshot, dict) else None
    )
    generated_audit = (
        change_audit.get("final_diff_generated_artifact_audit")
        if isinstance(change_audit, dict)
        else None
    )
    if (
        isinstance(generated_audit, dict)
        and generated_audit.get("status") == "FINAL_DIFF_GENERATED_ARTIFACTS"
    ):
        return {
            "schema_version": 1,
            "type": "focused_preflight",
            "success": False,
            "status": "FAILED",
            "acceptance": False,
            "project_full_executed": False,
            "cache_scope": "compiler_outputs_only",
            "test_result_reused": False,
            "pass_reused": False,
            "reason": "FINAL_DIFF_GENERATED_ARTIFACTS",
            "message": _bounded_text(generated_audit.get("message")),
            "generated_artifact_audit": {
                "status": "FINAL_DIFF_GENERATED_ARTIFACTS",
                "paths": _bounded_strings(
                    generated_audit.get("paths"), count=64, limit=512
                ),
            },
            "execution": None,
        }
    return _run_project_full_in_fresh_worktree(
        resolved,
        snapshot,
        focused_only=True,
    )


def _guard_progress_next_action(
    metric_budget: list[dict[str, Any]],
    *,
    source_guard_passed: bool,
) -> str:
    """Render editing guidance from scalar Guard budgets only."""
    if source_guard_passed:
        return ""
    if not metric_budget:
        return "restore frozen source Guard"
    routes: list[str] = []
    for item in metric_budget:
        boundary_key = (
            "passing_max"
            if "passing_max" in item
            else "passing_exclusive_max"
        )
        routes.append(
            f"metric={item['metric']}, current={item['current']}, "
            f"{boundary_key}={item[boundary_key]}, "
            f"required_reduction={item['required_reduction']}"
        )
    return (
        "make one narrow production edit that crosses at least one frozen "
        "scalar Guard route: " + "; ".join(routes)
    )


def _checkpoint_context(
    resolved,
    evidence: str,
    baseline_seal: str = "",
    *,
    persist: bool = True,
) -> tuple[Optional[GuardRunContext], Optional[dict[str, Any]]]:
    if resolved.smell not in CHECKPOINT_SMELLS or not resolved.locations:
        return None, None
    checkpoint = prepare_checkpoint(
        resolved,
        evidence,
        expected_baseline_seal=baseline_seal,
        persist=persist,
    )
    if not checkpoint.get("required"):
        # A migrated smell must never fall back to the ordinary threshold
        # detector when its immutable baseline is missing.  That detector can
        # legitimately consider the original source below its coarse
        # threshold, which would otherwise turn a setup failure into PASS.
        if checkpoint.get("reason"):
            return GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell=resolved.smell,
                checkpoint=checkpoint,
            ), checkpoint
        return None, checkpoint
    delta = dict(checkpoint.get("delta") or {})
    context = GuardRunContext(
        checkpoint_required=True,
        checkpoint_smell=resolved.smell,
        current_metrics=dict(checkpoint.get("current_metrics") or {}),
        metric_delta=delta,
        has_production_diff=bool(checkpoint.get("production_diff")),
        metric_progress=bool(delta.get("metric_progress")),
        checkpoint=checkpoint,
    )
    return context, checkpoint


def _god_class_min_improved_reduction(resolved) -> float:
    """Minimum relative metric reduction required only for IMPROVED."""
    for guard in resolved.profile.guards:
        if str(guard.get("type", "")).strip() == "god_class":
            try:
                return float(
                    guard.get("min_improved_relative_reduction", 0.05)
                )
            except (TypeError, ValueError):
                return 0.05
    return 0.05


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "guard_progress_only", False):
        return cmd_guard_progress(args)
    if getattr(args, "focused_preflight_only", False):
        return cmd_focused_preflight_progress(args)
    resolved = _resolve(args)
    evidence = getattr(args, "smell_evidence", "") or os.environ.get("SMELL_EVIDENCE", "")
    build_test_required = (
        resolved.language == "java"
        or (
            resolved.language in {"python", "c", "cpp"}
            and resolved.smell in {"dead_code", "data_clumps"}
        )
        or os.environ.get("SMELL_REQUIRE_BUILD_TEST") == "1"
    )
    if build_test_required and (
        args.skip_build_test
        or not args.run_build_test
        or resolved.verification_mode == "local"
    ):
        full_payload = {
            "success": False,
            "accepted": False,
            "progress": False,
            "status": "BUILD_TEST_REQUIRED",
            "resolution": "unresolved",
            "continue_hint": "",
            "smell_guard": {
                "success": True,
                "results": [],
                "failure_count": 0,
                "retry_hint": "",
            },
            "build_test_guard": None,
            "snapshot": None,
        }
        artifact_dir = _verify_artifact_dir(args, resolved.project_root)
        return _finalize_verify_artifacts_and_output(
            full_payload,
            artifact_dir=artifact_dir,
            smell=resolved.smell,
            evidence=evidence,
            output_detail=getattr(args, "output_detail", "decision"),
        )
    baseline_seal = str(
        getattr(args, "baseline_seal", "")
        or os.environ.get("SMELL_BASELINE_SEAL", "")
    ).strip()
    guard_context, checkpoint = _checkpoint_context(
        resolved,
        evidence,
        baseline_seal,
    )
    smell_results = run_smell_guards(resolved, guard_context)
    failed_smell = [item for item in smell_results if not item.get("success")]
    # Metric progress is a useful partial result, but only while the same
    # product-detector finding remains. A different structural guard failure
    # (for example, relocating a rejected capability) is not IMPROVED.
    improvement_pass = bool(
        guard_context is not None
        and getattr(guard_context, "has_production_diff", False)
        and getattr(guard_context, "metric_progress", False)
        and isinstance(getattr(guard_context, "current_metrics", None), dict)
        and guard_context.current_metrics.get("finding_present") is True
    )
    exact_dead_code_deletion = bool(
        resolved.language in {"python", "c", "cpp"}
        and resolved.smell == "dead_code"
        and dead_code_checkpoint_absence_allowed(guard_context)
    )
    # God-class PASS is owned entirely by the source-derived multi-metric
    # finding predicate. While that same finding remains, require a meaningful
    # reduction before granting another IMPROVED continuation.
    if improvement_pass and resolved.smell == "god_class" and resolved.language != "java":
        improvement_pass = (
            god_class_relative_reduction(guard_context)
            >= _god_class_min_improved_reduction(resolved)
        )
    build_test_result = None
    checkpoint_test_changes = (
        dict(checkpoint.get("test_changes") or {})
        if isinstance(checkpoint, dict)
        else {}
    )
    # Snapshot before executing build/test commands. This records the model's
    # complete deliverable (including build metadata and forbidden test edits)
    # without admitting tracked files dirtied by the verification process.
    declared_test_paths = [
        item.strip()
        for item in str(resolved.sample_test_location or "").split(";")
        if item.strip()
    ]
    baseline_project_commit = (
        str(checkpoint.get("baseline_project_commit") or "")
        if isinstance(checkpoint, dict)
        else ""
    )
    isolated_project_full = bool(
        resolved.verification_mode == "project_full"
        and resolved.language in {"python", "c", "cpp"}
    )
    project_full_snapshot_required = bool(
        args.run_build_test
        and isolated_project_full
    )
    snapshot = (
        _snapshot_project(
            resolved.project_root,
            declared_test_paths=declared_test_paths,
            base_commit=baseline_project_commit or "HEAD",
        )
        if args.snapshot or project_full_snapshot_required
        else None
    )
    change_audit = (
        snapshot.get("change_audit")
        if isinstance(snapshot, dict)
        else _project_change_audit(
            resolved.project_root,
            declared_test_paths=declared_test_paths,
            base_commit=baseline_project_commit or "HEAD",
        )
    )
    final_diff_generated_artifact_audit = (
        dict(change_audit.get("final_diff_generated_artifact_audit") or {})
        if isinstance(change_audit, dict)
        else {}
    )
    allow_test_changes = bool(
        checkpoint_test_changes.get("allow_test_changes") is True
        if checkpoint_test_changes
        else os.environ.get("SMELL_ALLOW_TEST_CHANGES") == "1"
    )
    worktree_test_changes = _worktree_test_change_audit(
        change_audit if isinstance(change_audit, dict) else {},
        allow_test_changes=allow_test_changes,
    )
    if checkpoint_test_changes:
        test_changes = checkpoint_test_changes
        # The Java c000 contract remains authoritative for semantic API
        # migration checks. The full worktree audit is an additional boundary
        # for conventional test paths that were not part of its source layout.
        test_changes["worktree_change_audit"] = worktree_test_changes
        if (
            test_changes.get("success") is not False
            and worktree_test_changes.get("success") is False
        ):
            test_changes = worktree_test_changes
    else:
        test_changes = worktree_test_changes
    validation_allowed = bool(
        (not failed_smell or improvement_pass)
        and args.run_build_test
        and resolved.verification_mode != "local"
    )
    if (
        validation_allowed
        and isolated_project_full
        and isinstance(change_audit, dict)
        and change_audit.get("success") is not True
    ):
        build_test_result = _final_verify_infra_failure_result(
            resolved,
            stage="capture_snapshot",
            message="The pre-build deliverable snapshot could not be captured.",
            base_commit=baseline_project_commit or "HEAD",
            snapshot_change_count=int(change_audit.get("change_count") or 0),
        )
    elif (
        final_diff_generated_artifact_audit.get("status")
        == "FINAL_DIFF_GENERATED_ARTIFACTS"
    ):
        build_test_result = _final_diff_generated_artifacts_result(
            resolved,
            final_diff_generated_artifact_audit,
        )
    elif test_changes and test_changes.get("success") is False:
        build_test_result = _test_source_modified_result(resolved, test_changes)
    elif validation_allowed:
        require_test_execution = _requires_fresh_test_execution(
            resolved,
            test_changes=test_changes,
            exact_dead_code_deletion=exact_dead_code_deletion,
        )
        if isolated_project_full:
            build_test_result = _run_project_full_in_fresh_worktree(
                resolved,
                snapshot,
                require_test_execution=require_test_execution,
            )
        else:
            build_test_result = run_build_test_guard(
                resolved,
                require_test_execution=require_test_execution,
            )
            if resolved.verification_mode == "project_full":
                build_test_result["project_full_executed"] = True
    behavior_valid = build_test_result is None or bool(build_test_result.get("success"))
    success = not failed_smell and (
        build_test_result is None or bool(build_test_result.get("success"))
    )
    verified_improvement = _verified_improvement(improvement_pass, behavior_valid)
    progress = bool(success or verified_improvement)
    resolution = (
        "resolved"
        if success
        else ("improved" if verified_improvement else "unresolved")
    )
    resolution_plan = (
        checkpoint.get("resolution_plan")
        if isinstance(checkpoint, dict)
        and isinstance(checkpoint.get("resolution_plan"), dict)
        else None
    )
    next_action = resolution_plan_next_action(resolution_plan)
    continue_hint = ""
    if resolution == "improved":
        continue_hint = (
            "Progress recorded but not accepted as final (resolution=improved). "
            "Preserve the behavior-valid metric gains. Required next action: "
            + (
                next_action
                or "complete the frozen target Guard finding closure"
            )
            + ". Then call smell_verify again."
        )
    smell_guard = {
        "success": not failed_smell,
        "results": smell_results,
        "failure_count": len(failed_smell),
        "retry_hint": resolved.profile.retry_hint_template if failed_smell else "",
    }
    full_payload = {
        "success": success,
        "accepted": success,
        "progress": progress,
        "status": _verify_status(
            success,
            smell_guard,
            build_test_result,
            improvement_pass=verified_improvement,
        ),
        "resolution": resolution,
        "continue_hint": continue_hint,
        "smell_guard": smell_guard,
        "build_test_guard": build_test_result,
        "project_full_executed": bool(
            isinstance(build_test_result, dict)
            and build_test_result.get("project_full_executed") is True
        ),
        "test_changes": test_changes or None,
        "snapshot": snapshot,
    }
    if checkpoint is not None:
        full_payload["checkpoint"] = checkpoint
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id:
            checkpoint["accepted"] = success
            checkpoint["verify_status"] = full_payload["status"]
            checkpoint["build_test_success"] = (
                bool(build_test_result.get("success")) if build_test_result is not None else None
            )
            finalized = finalize_checkpoint(
                resolved.project_root,
                resolved.smell,
                checkpoint_location(resolved),
                checkpoint_id,
                full_payload,
            )
            if finalized is not None:
                checkpoint.clear()
                checkpoint.update(finalized)
    artifact_dir = _verify_artifact_dir(args, resolved.project_root)
    return _finalize_verify_artifacts_and_output(
        full_payload,
        artifact_dir=artifact_dir,
        smell=resolved.smell,
        evidence=evidence,
        output_detail=getattr(args, "output_detail", "decision"),
    )


def _verified_improvement(metric_improvement: bool, behavior_valid: bool) -> bool:
    """An IMPROVED outcome requires both detector progress and valid behavior."""
    return bool(metric_improvement and behavior_valid)


def _requires_fresh_test_execution(
    resolved: Any,
    *,
    test_changes: dict[str, Any],
    exact_dead_code_deletion: bool,
) -> bool:
    """Define the test-evidence boundary independently of smell detection.

    ``project_full`` means a real project test suite ran; a version/help/file
    smoke cannot satisfy that mode. Other modes keep their existing narrow
    requirements for authorized test migration and exact dead-code deletion.
    """
    return bool(
        str(getattr(resolved, "verification_mode", "")) == "project_full"
        or test_changes.get("allow_test_changes") is True
        or exact_dead_code_deletion
    )


def _test_source_modified_result(resolved: Any, audit: dict[str, Any]) -> dict[str, Any]:
    changed_paths = [
        str(item.get("path") or "")
        for group in ("added", "changed", "deleted")
        for item in (audit.get(group) or [])
        if isinstance(item, dict) and item.get("path")
    ]
    reason = str(audit.get("reason") or audit.get("status") or "TEST_SOURCE_MODIFIED")
    if reason == "WORKTREE_CHANGE_AUDIT_FAILED":
        message = (
            "WORKTREE_CHANGE_AUDIT_FAILED: final Git status could not be audited; "
            "do not accept or repair the candidate until repository state is readable."
        )
    elif reason == "VERIFICATION_CONFIG_MODIFIED":
        config_paths = [
            str(item.get("path") or "")
            for group in (
                "verification_config_added",
                "verification_config_changed",
                "verification_config_deleted",
            )
            for item in (audit.get(group) or [])
            if isinstance(item, dict) and item.get("path")
        ]
        message = (
            "VERIFICATION_CONFIG_MODIFIED: build/test discovery inputs are controller-frozen; "
            "restore these changes before verification: " + ", ".join(config_paths)
        )
    elif reason == "TEST_SOURCE_MIGRATION_REJECTED":
        violations = [
            str(item.get("reason") or item.get("type") or "test_strength_weakened")
            + (f":{item.get('path')}" if item.get("path") else "")
            for item in audit.get("test_strength_violations") or []
            if isinstance(item, dict)
        ]
        message = (
            "TEST_SOURCE_MIGRATION_REJECTED: api_migration may update test API references "
            "but may not remove tests/assertions, add skip signals, or edit test resources; "
            "repair or restore: " + ", ".join(violations or changed_paths)
        )
    elif reason == "TEST_SOURCE_DELETED":
        message = (
            "TEST_SOURCE_DELETED: api_migration preserves every baseline test file; "
            "restore the deleted tests before verification: " + ", ".join(changed_paths)
        )
    else:
        message = (
            "TEST_SOURCE_MODIFIED: the controller froze tests as immutable at c000; "
            "restore these changes before verification: " + ", ".join(changed_paths)
        )
    return {
        "type": "build_test",
        "success": False,
        "message": message,
        "reason": reason,
        "verification_mode": resolved.verification_mode,
        "build_source": resolved.build_source,
        "test_source": resolved.test_source,
        "sample_test_source": "dataset",
        "test_location": resolved.sample_test_location,
        "test_changes": audit,
        "details": {
            "build": {"success": True, "status": "skipped_by_test_change_contract"},
            "test": {
                "success": False,
                "status": "test_source_modified",
                "failure_highlights": [message],
            },
            "sample_test": None,
        },
    }


def _generated_artifact_path_groups(
    audit: dict[str, Any],
) -> tuple[list[str], list[str]]:
    tracked: list[str] = []
    untracked: list[str] = []
    for item in list(audit.get("artifacts") or []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        target = untracked if item.get("status") == "??" else tracked
        target.append(path)
    return tracked, untracked


def _final_diff_generated_artifacts_result(
    resolved: Any,
    audit: dict[str, Any],
) -> dict[str, Any]:
    tracked_paths, untracked_paths = _generated_artifact_path_groups(audit)
    actions: list[str] = []
    if tracked_paths:
        actions.append(
            "restore tracked generated paths to the frozen baseline: "
            + ", ".join(tracked_paths)
        )
    if untracked_paths:
        actions.append(
            "remove untracked generated paths: " + ", ".join(untracked_paths)
        )
    message = "FINAL_DIFF_GENERATED_ARTIFACTS: " + "; ".join(actions)
    return {
        "type": "build_test",
        "success": False,
        "message": message,
        "reason": "FINAL_DIFF_GENERATED_ARTIFACTS",
        "verification_mode": resolved.verification_mode,
        "build_source": resolved.build_source,
        "test_source": resolved.test_source,
        "sample_test_source": "dataset",
        "test_location": resolved.sample_test_location,
        "final_diff_generated_artifact_audit": dict(audit),
        "details": {
            "build": {
                "success": False,
                "status": "skipped_by_final_diff_generated_artifact_contract",
                "failure_highlights": [message],
            },
            "test": {
                "success": False,
                "status": "skipped_by_final_diff_generated_artifact_contract",
                "failure_highlights": [message],
            },
            "sample_test": None,
        },
    }


def _final_verify_infra_failure_result(
    resolved: Any,
    *,
    stage: str,
    message: str,
    base_commit: str = "",
    snapshot_change_count: int = 0,
    cleanup_success: Optional[bool] = None,
) -> dict[str, Any]:
    rendered = f"FINAL_VERIFY_INFRA_FAILED ({stage}): {message}"
    isolation = {
        "contract_version": "project-full-fresh-worktree/v1",
        "mode": "detached_git_worktree",
        "success": False,
        "stage": stage,
        "base_commit": base_commit,
        "snapshot_change_count": int(snapshot_change_count),
        "cleanup_success": cleanup_success,
    }
    sample_command = str(getattr(resolved, "sample_test_command", "") or "").strip()
    return {
        "type": "build_test",
        "success": False,
        "message": rendered,
        "reason": "FINAL_VERIFY_INFRA_FAILED",
        "verification_mode": resolved.verification_mode,
        "build_source": resolved.build_source,
        "test_source": resolved.test_source,
        "sample_test_source": "dataset" if sample_command else "",
        "test_location": resolved.sample_test_location,
        "test_command_hash": (
            hashlib.sha256(sample_command.encode("utf-8")).hexdigest()
            if sample_command
            else ""
        ),
        "verification_isolation": isolation,
        "details": {
            "build": None,
            "test": None,
            "sample_test": None,
        },
    }


def _rebase_text_to_fresh_root(
    value: Optional[str],
    source_root: Path,
    fresh_root: Path,
) -> Optional[str]:
    if value is None:
        return None
    sources = sorted(
        {str(source_root), str(source_root.resolve())},
        key=len,
        reverse=True,
    )
    pattern = re.compile("|".join(re.escape(source) for source in sources))
    return pattern.sub(lambda _match: str(fresh_root), str(value))


def _rebase_path_to_fresh_root(
    value: Path,
    source_root: Path,
    fresh_root: Path,
) -> Path:
    candidate = Path(value).expanduser().resolve()
    try:
        relative = candidate.relative_to(source_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"verification path is outside the frozen project root: {candidate}"
        ) from exc
    return (fresh_root / relative).resolve()


def _rebase_build_test_config(
    resolved: Any,
    fresh_root: Path,
) -> Any:
    source_root = Path(resolved.project_root).expanduser()
    fresh_root = fresh_root.expanduser().resolve()
    command_tmp = fresh_root.parent / "tmp"
    tmux_tmp = fresh_root.parent / "tmux"
    command_tmp.mkdir()
    tmux_tmp.mkdir()

    def command_config(value: Any) -> Any:
        return replace(
            value,
            command=_rebase_text_to_fresh_root(
                getattr(value, "command", None),
                source_root,
                fresh_root,
            ),
            script=_rebase_text_to_fresh_root(
                getattr(value, "script", None),
                source_root,
                fresh_root,
            ),
        )

    rebased_env = {
        str(key): str(
            _rebase_text_to_fresh_root(
                str(value),
                source_root,
                fresh_root,
            )
        )
        for key, value in dict(resolved.env).items()
    }
    rebased_env.update({
        "TMPDIR": str(command_tmp),
        "TMUX_TMPDIR": str(tmux_tmp),
    })
    return replace(
        resolved,
        project_root=fresh_root,
        dataset_root=_rebase_path_to_fresh_root(
            resolved.dataset_root,
            source_root,
            fresh_root,
        ),
        idea_project_root=_rebase_path_to_fresh_root(
            resolved.idea_project_root,
            source_root,
            fresh_root,
        ),
        build_root=_rebase_path_to_fresh_root(
            resolved.build_root,
            source_root,
            fresh_root,
        ),
        cwd=_rebase_path_to_fresh_root(
            resolved.cwd,
            source_root,
            fresh_root,
        ),
        build=command_config(resolved.build),
        focused_preflight=command_config(resolved.focused_preflight),
        test=command_config(resolved.test),
        sample_test=command_config(resolved.sample_test),
        env=rebased_env,
        sample_test_location=str(
            _rebase_text_to_fresh_root(
                str(resolved.sample_test_location or ""),
                source_root,
                fresh_root,
            )
        ),
        sample_test_command=str(
            _rebase_text_to_fresh_root(
                str(resolved.sample_test_command or ""),
                source_root,
                fresh_root,
            )
        ),
    )


def _run_git_with_input(
    args: list[str],
    cwd: Path,
    content: str,
) -> dict[str, Any]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=content,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _git_failure_message(result: dict[str, Any], fallback: str) -> str:
    detail = str(result.get("stderr") or result.get("stdout") or "").strip()
    return _bounded_text(detail or fallback, limit=1024)


def _run_project_full_in_fresh_worktree(
    resolved: Any,
    snapshot: Optional[dict[str, Any]],
    *,
    require_test_execution: bool = False,
    focused_only: bool = False,
) -> dict[str, Any]:
    """Replay the frozen pre-build deliverable in one detached worktree."""
    if not isinstance(snapshot, dict):
        return _final_verify_infra_failure_result(
            resolved,
            stage="capture_snapshot",
            message="The pre-build deliverable snapshot is unavailable.",
        )
    change_audit = snapshot.get("change_audit")
    diff = snapshot.get("diff")
    base_commit = str(snapshot.get("base_commit") or "").strip()
    change_count = (
        int(change_audit.get("change_count") or 0)
        if isinstance(change_audit, dict)
        else 0
    )
    if not isinstance(change_audit, dict) or change_audit.get("success") is not True:
        return _final_verify_infra_failure_result(
            resolved,
            stage="capture_snapshot",
            message="The pre-build Git change audit failed.",
            base_commit=base_commit,
            snapshot_change_count=change_count,
        )
    if (
        not isinstance(diff, dict)
        or diff.get("returncode") != 0
        or not isinstance(diff.get("stdout"), str)
    ):
        return _final_verify_infra_failure_result(
            resolved,
            stage="capture_snapshot",
            message="The replayable pre-build Git patch is unavailable.",
            base_commit=base_commit,
            snapshot_change_count=change_count,
        )

    source_root = Path(resolved.project_root).expanduser().resolve()
    resolved_base = _run_git(
        ["rev-parse", "--verify", f"{base_commit}^{{commit}}"],
        source_root,
    )
    resolved_base_text = str(resolved_base.get("stdout") or "").strip()
    if resolved_base.get("returncode") != 0 or not resolved_base_text:
        return _final_verify_infra_failure_result(
            resolved,
            stage="resolve_base_commit",
            message=_git_failure_message(
                resolved_base,
                "The frozen base commit could not be resolved.",
            ),
            base_commit=base_commit,
            snapshot_change_count=change_count,
        )

    failure: Optional[dict[str, Any]] = None
    build_test_result: Optional[dict[str, Any]] = None
    focused_preflight_result: Optional[dict[str, Any]] = None
    cleanup_success: Optional[bool] = None
    try:
        with tempfile.TemporaryDirectory(prefix="smell-project-full-") as raw:
            fresh_root = (Path(raw) / "worktree").resolve()
            created = _run_git(
                [
                    "worktree",
                    "add",
                    "--detach",
                    str(fresh_root),
                    resolved_base_text,
                ],
                source_root,
            )
            worktree_created = created.get("returncode") == 0
            if not worktree_created:
                removed = _run_git(
                    ["worktree", "remove", "--force", str(fresh_root)],
                    source_root,
                )
                pruned = _run_git(["worktree", "prune"], source_root)
                cleanup_success = bool(
                    removed.get("returncode") == 0
                    or (
                        pruned.get("returncode") == 0
                        and not fresh_root.exists()
                    )
                )
                failure = _final_verify_infra_failure_result(
                    resolved,
                    stage="create_worktree",
                    message=_git_failure_message(
                        created,
                        "The detached verification worktree could not be created.",
                    ),
                    base_commit=resolved_base_text,
                    snapshot_change_count=change_count,
                    cleanup_success=cleanup_success,
                )
            else:
                try:
                    patch = str(diff.get("stdout") or "")
                    if patch:
                        applied = _run_git_with_input(
                            ["apply", "--binary", "--whitespace=nowarn", "-"],
                            fresh_root,
                            patch,
                        )
                        if applied.get("returncode") != 0:
                            failure = _final_verify_infra_failure_result(
                                resolved,
                                stage="apply_snapshot",
                                message=_git_failure_message(
                                    applied,
                                    "The pre-build deliverable patch could not be applied.",
                                ),
                                base_commit=resolved_base_text,
                                snapshot_change_count=change_count,
                            )
                    if failure is None:
                        try:
                            isolated = _rebase_build_test_config(
                                resolved,
                                fresh_root,
                            )
                        except (OSError, TypeError, ValueError) as exc:
                            failure = _final_verify_infra_failure_result(
                                resolved,
                                stage="resolve_verification_root",
                                message=_bounded_text(str(exc), limit=1024),
                                base_commit=resolved_base_text,
                                snapshot_change_count=change_count,
                            )
                        else:
                            try:
                                focused_preflight_result = run_focused_preflight(
                                    isolated
                                )
                            except Exception:
                                failure = _final_verify_infra_failure_result(
                                    resolved,
                                    stage="run_focused_preflight",
                                    message=(
                                        "The isolated focused preflight raised "
                                        "an exception."
                                    ),
                                    base_commit=resolved_base_text,
                                    snapshot_change_count=change_count,
                                )
                            else:
                                if focused_only:
                                    pass
                                elif focused_preflight_result.get("success") is False:
                                    execution = focused_preflight_result.get("execution")
                                    build_test_result = {
                                        "type": "build_test",
                                        "success": False,
                                        "reason": "FOCUSED_PREFLIGHT_FAILED",
                                        "message": focused_preflight_result.get("message"),
                                        "focused_preflight": focused_preflight_result,
                                        "project_full_executed": False,
                                        "details": {
                                            "build": execution,
                                            "test": None,
                                            "sample_test": None,
                                        },
                                    }
                                else:
                                    try:
                                        build_test_result = run_build_test_guard(
                                            isolated,
                                            require_test_execution=require_test_execution,
                                        )
                                        build_test_result["focused_preflight"] = (
                                            focused_preflight_result
                                        )
                                        build_test_result["project_full_executed"] = True
                                    except Exception:
                                        failure = _final_verify_infra_failure_result(
                                            resolved,
                                            stage="run_build_test_guard",
                                            message=(
                                                "The isolated build/test Guard raised "
                                                "an exception."
                                            ),
                                            base_commit=resolved_base_text,
                                            snapshot_change_count=change_count,
                                        )
                finally:
                    removed = _run_git(
                        ["worktree", "remove", "--force", str(fresh_root)],
                        source_root,
                    )
                    cleanup_success = removed.get("returncode") == 0
                    if not cleanup_success:
                        failure = _final_verify_infra_failure_result(
                            resolved,
                            stage="cleanup_worktree",
                            message=_git_failure_message(
                                removed,
                                "The detached verification worktree could not be removed.",
                            ),
                            base_commit=resolved_base_text,
                            snapshot_change_count=change_count,
                            cleanup_success=False,
                        )
    except OSError as exc:
        failure = _final_verify_infra_failure_result(
            resolved,
            stage="cleanup_worktree",
            message=_bounded_text(str(exc), limit=1024),
            base_commit=resolved_base_text,
            snapshot_change_count=change_count,
            cleanup_success=False,
        )
        cleanup_success = False

    if cleanup_success is False:
        _run_git(["worktree", "prune"], source_root)
    if failure is not None:
        isolation = failure.get("verification_isolation")
        if isinstance(isolation, dict) and isolation.get("cleanup_success") is None:
            isolation["cleanup_success"] = cleanup_success
        return failure
    if focused_only:
        if not isinstance(focused_preflight_result, dict):
            return _final_verify_infra_failure_result(
                resolved,
                stage="run_focused_preflight",
                message="The isolated focused preflight returned no result.",
                base_commit=resolved_base_text,
                snapshot_change_count=change_count,
                cleanup_success=cleanup_success,
            )
        focused_preflight_result["verification_isolation"] = {
            "contract_version": "focused-preflight-fresh-worktree/v1",
            "mode": "detached_git_worktree",
            "success": True,
            "stage": "completed",
            "base_commit": resolved_base_text,
            "snapshot_change_count": change_count,
            "cleanup_success": cleanup_success,
        }
        return focused_preflight_result
    if not isinstance(build_test_result, dict):
        return _final_verify_infra_failure_result(
            resolved,
            stage="run_build_test_guard",
            message="The isolated build/test Guard returned no result.",
            base_commit=resolved_base_text,
            snapshot_change_count=change_count,
            cleanup_success=cleanup_success,
        )

    sample_command = str(resolved.sample_test_command or "").strip()
    build_test_result.update({
        "focused_preflight": focused_preflight_result,
        "verification_mode": resolved.verification_mode,
        "build_source": resolved.build_source,
        "test_source": resolved.test_source,
        "sample_test_source": "dataset" if sample_command else "",
        "test_location": resolved.sample_test_location,
        "test_command_hash": (
            hashlib.sha256(sample_command.encode("utf-8")).hexdigest()
            if sample_command
            else ""
        ),
        "verification_isolation": {
            "contract_version": "project-full-fresh-worktree/v1",
            "mode": "detached_git_worktree",
            "success": True,
            "stage": "completed",
            "base_commit": resolved_base_text,
            "snapshot_change_count": change_count,
            "cleanup_success": cleanup_success,
        },
    })
    return build_test_result


def cmd_resolve_command(args: argparse.Namespace) -> dict[str, Any]:
    return resolve_command_payload(
        args.arguments,
        defaults={
            "project_root": os.environ.get("SMELL_PROJECT_ROOT"),
            "project_override_root": os.environ.get("SMELL_CANONICAL_PROJECT_ROOT"),
            "language": os.environ.get("SMELL_LANGUAGE"),
            "smell": os.environ.get("SMELL_SMELL"),
            "location": os.environ.get("SMELL_LOCATION"),
            "target_context_json": os.environ.get("SMELL_TARGET_CONTEXT_JSON"),
            "sample_test_location": os.environ.get("SMELL_SAMPLE_TEST_LOCATION"),
            "sample_test_command": os.environ.get("SMELL_SAMPLE_TEST_COMMAND"),
        },
    )


def _verify_status(
    success: bool,
    smell_guard: dict[str, Any],
    build_test_result: Optional[dict[str, Any]],
    improvement_pass: bool = False,
) -> str:
    if success:
        return "PASS"
    # Build/test regressions outrank the smell verdict when the improvement
    # gate passed (the smell improved; the edit broke something else).
    if build_test_result and build_test_result.get("success") is False:
        explicit_reason = str(build_test_result.get("reason") or "")
        if explicit_reason in {
            "FINAL_VERIFY_INFRA_FAILED",
            "FINAL_DIFF_GENERATED_ARTIFACTS",
            "TEST_SOURCE_MODIFIED",
            "TEST_SOURCE_MIGRATION_REJECTED",
            "TEST_SOURCE_DELETED",
            "VERIFICATION_CONFIG_MODIFIED",
            "WORKTREE_CHANGE_AUDIT_FAILED",
        }:
            return explicit_reason
        if build_test_result.get("verification_mode") == "sample_optimized":
            details = build_test_result.get("details") or {}
            test = details.get("test") or {}
            if test.get("status") == "missing":
                return "SAMPLE_TEST_SPEC_MISSING"
            if test.get("status") == "test_not_executed" and test.get("returncode") == 0:
                return "SAMPLE_TEST_EVIDENCE_MISSING"
            if test.get("success") is False:
                return "SAMPLE_TEST_FAILED"
        details = build_test_result.get("details") or {}
        build = details.get("build") or {}
        test = details.get("test") or {}
        sample_test = details.get("sample_test") or {}
        if build.get("success") is False:
            return "BUILD_FAILED"
        if test.get("success") is False:
            if test.get("status") == "test_not_executed":
                return "TEST_EVIDENCE_MISSING"
            return "TEST_FAILED"
        if sample_test.get("success") is False:
            if sample_test.get("status") == "test_not_executed":
                return "SAMPLE_TEST_EVIDENCE_MISSING"
            return "TEST_FAILED"
        return "BUILD_TEST_FAILED"
    if improvement_pass:
        return "IMPROVED"
    if smell_guard.get("success") is False:
        return "SMELL_GUARD_FAILED"
    return "VERIFY_FAILED"


def _summarize_command_result(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    summary: dict[str, Any] = {}
    for key in (
        "label",
        "success",
        "status",
        "returncode",
        "cwd",
        "source",
        "timeout_seconds",
    ):
        if key in result:
            summary[key] = result[key]
    for key in ("command", "script", "summary", "summary_text", "tail", "error"):
        if key in result:
            summary[key] = _bounded_text(result[key])
    if "failure_highlights" in result:
        summary["failure_highlights"] = _bounded_strings(result["failure_highlights"])
    if "timed_out" in result:
        summary["timed_out"] = result["timed_out"]
    return summary


def _summarize_build_test_guard(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    details = result.get("details") or {}
    final_artifact_audit = result.get("final_diff_generated_artifact_audit")
    isolation = result.get("verification_isolation")
    focused_preflight = result.get("focused_preflight")
    return {
        "type": result.get("type"),
        "success": bool(result.get("success")),
        "message": _bounded_text(result.get("message")),
        "reason": _bounded_text(result.get("reason", ""), limit=256),
        "verification_mode": result.get("verification_mode", ""),
        "build_source": result.get("build_source", ""),
        "test_source": result.get("test_source", ""),
        "sample_test_source": result.get("sample_test_source", ""),
        "test_location": _bounded_text(result.get("test_location", "")),
        "test_command_hash": result.get("test_command_hash", ""),
        "project_full_executed": result.get("project_full_executed") is True,
        "focused_preflight": {
            "schema_version": focused_preflight.get("schema_version"),
            "success": focused_preflight.get("success") is True,
            "status": _bounded_text(focused_preflight.get("status"), limit=128),
            "acceptance": False,
            "project_full_executed": False,
            "cache_scope": _bounded_text(
                focused_preflight.get("cache_scope"),
                limit=128,
            ),
            "execution": _summarize_command_result(
                focused_preflight.get("execution")
            ),
        }
        if isinstance(focused_preflight, dict)
        else None,
        "test_changes": _compact_test_changes(result.get("test_changes")),
        "final_diff_generated_artifact_audit": {
            "success": bool(final_artifact_audit.get("success")),
            "status": _bounded_text(final_artifact_audit.get("status"), limit=128),
            "paths": _bounded_strings(
                final_artifact_audit.get("paths"),
                count=64,
                limit=512,
            ),
        }
        if isinstance(final_artifact_audit, dict)
        else None,
        "verification_isolation": {
            "contract_version": _bounded_text(
                isolation.get("contract_version"),
                limit=128,
            ),
            "mode": _bounded_text(isolation.get("mode"), limit=128),
            "success": bool(isolation.get("success")),
            "stage": _bounded_text(isolation.get("stage"), limit=128),
            "base_commit": _bounded_text(
                isolation.get("base_commit"),
                limit=128,
            ),
            "snapshot_change_count": int(
                isolation.get("snapshot_change_count") or 0
            ),
            "cleanup_success": isolation.get("cleanup_success"),
        }
        if isinstance(isolation, dict)
        else None,
        "details": {
            "build": _summarize_command_result(details.get("build")),
            "test": _summarize_command_result(details.get("test")),
            "sample_test": _summarize_command_result(details.get("sample_test")),
        },
    }


def _summarize_snapshot(
    snapshot: Optional[dict[str, Any]],
    artifacts: dict[str, str],
) -> Optional[dict[str, Any]]:
    if snapshot is None:
        return None
    change_audit = snapshot.get("change_audit")
    audit_summary = None
    if isinstance(change_audit, dict):
        audit_summary = {
            "schema_version": change_audit.get("schema_version"),
            "success": bool(change_audit.get("success")),
            "change_count": int(change_audit.get("change_count") or 0),
            "category_counts": dict(change_audit.get("category_counts") or {}),
            "ignored_tracked_count": int(
                change_audit.get("ignored_tracked_count") or 0
            ),
            "ignored_untracked_count": int(
                change_audit.get("ignored_untracked_count") or 0
            ),
            "ignored_generated_count": int(
                change_audit.get("ignored_generated_count") or 0
            ),
        }
    return {
        "project_root": snapshot.get("project_root"),
        "scope": snapshot.get("scope"),
        "base_commit": snapshot.get("base_commit"),
        "change_audit": audit_summary,
        "status": _summarize_command_result(snapshot.get("status")),
        "diff_stat": _summarize_command_result(snapshot.get("diff_stat")),
        "artifacts": {
            "snapshot": artifacts.get("snapshot", ""),
            "diff": artifacts.get("diff", ""),
            "diff_stat": artifacts.get("diff_stat", ""),
        },
    }


def _legacy_verify_payload(
    full_payload: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    payload = {
        "success": bool(full_payload.get("success")),
        "accepted": bool(full_payload.get("accepted")),
        "progress": bool(full_payload.get("progress")),
        "status": full_payload.get("status"),
        "resolution": full_payload.get("resolution"),
        "continue_hint": full_payload.get("continue_hint", ""),
        "project_full_executed": full_payload.get("project_full_executed") is True,
        "smell_guard": full_payload.get("smell_guard"),
        "build_test_guard": _summarize_build_test_guard(full_payload.get("build_test_guard")),
        "test_changes": full_payload.get("test_changes"),
        "snapshot": _summarize_snapshot(full_payload.get("snapshot"), artifacts),
        "artifacts": artifacts,
    }
    if full_payload.get("checkpoint") is not None:
        payload["checkpoint"] = full_payload["checkpoint"]
    if full_payload.get("failure_pack") is not None:
        payload["failure_pack"] = full_payload["failure_pack"]
    return payload


def _verify_decision_payload(
    full_payload: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    failure_pack = _compact_failure_pack(full_payload.get("failure_pack"))
    payload: dict[str, Any] = {
        "schema_version": VERIFY_DECISION_SCHEMA,
        "success": bool(full_payload.get("success")),
        "accepted": bool(full_payload.get("accepted")),
        "progress": bool(full_payload.get("progress")),
        "status": _bounded_text(full_payload.get("status"), limit=128),
        "resolution": _bounded_text(full_payload.get("resolution"), limit=128),
        "continue_hint": _bounded_text(full_payload.get("continue_hint")),
        "project_full_executed": full_payload.get("project_full_executed") is True,
        "smell_guard": _compact_smell_guard(full_payload.get("smell_guard")),
        "build_test_guard": _summarize_build_test_guard(full_payload.get("build_test_guard")),
        "test_changes": _compact_test_changes(full_payload.get("test_changes")),
        "snapshot": _summarize_snapshot(full_payload.get("snapshot"), artifacts),
        "checkpoint": _compact_checkpoint(full_payload.get("checkpoint")),
        "failure_pack": failure_pack,
        "artifacts": artifacts,
        "artifact_index": _artifact_index(artifacts),
    }
    if failure_pack is not None:
        payload["failure_fingerprint"] = _failure_fingerprint(full_payload, failure_pack)
    return _assert_decision_size(payload)


def _finalize_verify_artifacts_and_output(
    full_payload: dict[str, Any],
    *,
    artifact_dir: Path,
    smell: str,
    evidence: str,
    output_detail: str,
) -> dict[str, Any]:
    artifacts = _write_verify_artifacts(artifact_dir, full_payload)
    if not bool(full_payload.get("success")):
        full_payload["failure_pack"] = _build_failure_pack(
            full_payload,
            artifacts,
            smell=smell,
            evidence=evidence,
        )
    # Build/test output and patches already live in dedicated artifacts.  Keep
    # the Guard result bounded instead of duplicating the complete process
    # payload (the old verify.full.json was a frequent OOM amplifier).
    guard_evidence_path = artifact_dir / "guard-evidence.json"
    artifacts["guard_evidence"] = _write_guard_evidence_artifact(
        guard_evidence_path,
        _guard_evidence_payload(full_payload, artifacts),
    )
    if output_detail == "audit":
        return _legacy_verify_payload(full_payload, artifacts)
    return _verify_decision_payload(full_payload, artifacts)


def _guard_evidence_payload(
    full_payload: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    checkpoint = full_payload.get("checkpoint")
    current = (
        checkpoint.get("current_metrics")
        if isinstance(checkpoint, dict)
        else None
    )
    witness = current.get("witness") if isinstance(current, dict) else None
    violations = (
        current.get("guard_violations") if isinstance(current, dict) else None
    )
    return {
        "schema_version": 1,
        "success": bool(full_payload.get("success")),
        "accepted": bool(full_payload.get("accepted")),
        "progress": bool(full_payload.get("progress")),
        "status": _bounded_text(full_payload.get("status"), limit=128),
        "resolution": _bounded_text(full_payload.get("resolution"), limit=128),
        "checkpoint": _compact_checkpoint(checkpoint),
        "witness": _bounded_evidence_mapping(witness),
        "guard_violations": _bounded_strings(violations, count=32, limit=512),
        "smell_guard": _compact_smell_guard(full_payload.get("smell_guard")),
        "test_changes": _compact_test_changes(full_payload.get("test_changes")),
        "artifacts": dict(artifacts),
    }


def _bounded_evidence_mapping(value: Any, *, limit: int = 256 * 1024) -> Any:
    if not isinstance(value, (dict, list)):
        return {}
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= limit:
        return value
    return {
        "truncated": True,
        "bytes": len(encoded),
        "summary": (
            _compact_scalar_mapping(value, count=64)
            if isinstance(value, dict)
            else {"item_count": len(value)}
        ),
    }


def _write_verify_artifacts(artifact_dir: Path, full_payload: dict[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    build_test_result = full_payload.get("build_test_guard")
    if isinstance(build_test_result, dict):
        artifacts["build_test_guard"] = _write_json_artifact(
            artifact_dir / "build-test-guard.full.json",
            build_test_result,
        )
        details = build_test_result.get("details") or {}
        build_result = details.get("build")
        test_result = details.get("test")
        sample_test_result = details.get("sample_test")
        if isinstance(build_result, dict):
            artifacts["build_result"] = _write_json_artifact(artifact_dir / "build.full.json", build_result)
            if isinstance(build_result.get("output"), str):
                artifacts["build_log"] = _write_text_artifact(artifact_dir / "build.log", build_result["output"])
        if isinstance(test_result, dict):
            artifacts["test_result"] = _write_json_artifact(artifact_dir / "test.full.json", test_result)
            if isinstance(test_result.get("output"), str):
                artifacts["test_log"] = _write_text_artifact(artifact_dir / "test.log", test_result["output"])
        if isinstance(sample_test_result, dict):
            artifacts["sample_test_result"] = _write_json_artifact(
                artifact_dir / "sample-test.full.json",
                sample_test_result,
            )
            if isinstance(sample_test_result.get("output"), str):
                artifacts["sample_test_log"] = _write_text_artifact(
                    artifact_dir / "sample-test.log",
                    sample_test_result["output"],
                )

    snapshot = full_payload.get("snapshot")
    if isinstance(snapshot, dict):
        artifacts["snapshot"] = _write_json_artifact(artifact_dir / "snapshot.full.json", snapshot)
        diff = snapshot.get("diff") or {}
        if isinstance(diff, dict) and isinstance(diff.get("stdout"), str):
            artifacts["diff"] = _write_text_artifact(artifact_dir / "diff.patch", diff["stdout"])
        diff_stat = snapshot.get("diff_stat") or {}
        if isinstance(diff_stat, dict) and isinstance(diff_stat.get("stdout"), str):
            artifacts["diff_stat"] = _write_text_artifact(artifact_dir / "diff.stat", diff_stat["stdout"])
    return artifacts


def _run_git(args: list[str], cwd: Path) -> dict[str, Any]:
    # surrogateescape mirrors smell_core.checkpoints: non-UTF-8 source bytes in
    # git diff output must survive a write-back round-trip byte-exactly.
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _git_untracked_files(root: Path, pathspecs: list[str] | None = None) -> list[str]:
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    result = _run_git(args, root)
    if result.get("returncode") != 0:
        return []
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return []
    return [
        path
        for path in stdout.split("\0")
        if path and not _is_verification_generated_path(root, path, tracked=False)
    ]


_CONTROLLER_OUTPUT_DIRECTORIES = frozenset({
    ".smell-test-reports",
    "build-refactoragent",
})
_CJSON_ROOT_BUILD_PRODUCTS = frozenset({
    "cJSON.o",
    "cJSON_Utils.o",
    "cJSON_test",
})
_CJSON_ROOT_LIBRARY_PRODUCT = re.compile(
    r"libcjson[^/]*\.(?:a|so(?:\.\d+)*)\Z"
)
_RRDTOOL_TRACKED_VERIFICATION_PRODUCTS = frozenset({
    "po/fr.po",
    "po/hu.po",
    "tests/graph2.output",
})


def _project_generated_output_root(root: Path, path: str) -> bool:
    """Recognize a configured, project-owned build root in the candidate tree."""
    normalized = path.replace("\\", "/").lstrip("/")
    return bool(
        normalized.startswith("build/")
        and (root / "src/google/protobuf").is_dir()
    )


_AUTOTOOLS_GENERATED_NAMES = frozenset({
    "aclocal.m4",
    "ar-lib",
    "compile",
    "config.cache",
    "config.guess",
    "config.h",
    "config.h.in",
    "config.h.in~",
    "config.log",
    "config.status",
    "config.sub",
    "configure",
    "depcomp",
    "install-sh",
    "libtool",
    "ltmain.sh",
    "missing",
    "mkinstalldirs",
    "test-driver",
    "ylwrap",
})
_AUTOTOOLS_INPUT_NAMES = frozenset({
    "configure.ac",
    "configure.in",
    "Makefile.am",
})
_LIBTOOL_GENERATED_MACROS = frozenset({
    "libtool.m4",
    "ltoptions.m4",
    "ltsugar.m4",
    "ltversion.m4",
    "lt~obsolete.m4",
})


def _has_autotools_input(root: Path, directory: Path) -> bool:
    """Return whether *directory* belongs to an Autotools source tree."""
    # Keep both paths lexical. macOS maps /var to /private/var; resolving only
    # the root would make an in-tree build-aux directory look unrelated.
    root = root.absolute()
    candidate = directory.absolute()
    while True:
        if any((candidate / name).is_file() for name in _AUTOTOOLS_INPUT_NAMES):
            return True
        if candidate == root or root not in candidate.parents:
            return False
        candidate = candidate.parent


def _looks_like_configure_makefile(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            header = handle.read(8192)
    except OSError:
        return False
    return bool(
        "Makefile.in generated by automake" in header
        or "Makefile.  Generated from Makefile.in by configure" in header
        or "Makefile generated by configure" in header
    )


def _generated_file_header(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.read(16384)
    except OSError:
        return ""


def _looks_like_cmake_generated_file(path: Path) -> bool:
    """Recognize an untracked CMake product from its own provenance marker."""
    header = _generated_file_header(path)
    return bool(
        header
        and any(
            marker in header
            for marker in (
                "# This is the CMakeCache file.",
                "# CMake generated Testfile for",
                "# Install script for directory:",
                "# CMAKE generated file: DO NOT EDIT!",
                "# Generated by \"Ninja\" Generator, CMake Version",
                "# This file is generated by cmake",
            )
        )
    )


def _looks_like_autotools_generated_file(path: Path) -> bool:
    """Recognize an Autotools product from a generator marker."""
    return _looks_like_autotools_generated_header(_generated_file_header(path))


def _looks_like_autotools_generated_header(header: str) -> bool:
    """Recognize an Autotools product from its bounded provenance header."""
    folded = header.casefold()
    return bool(
        header
        and any(
            marker in folded
            for marker in (
                "makefile.in generated by automake",
                "makefile.  generated from makefile.in by configure",
                "makefile generated by configure",
                "generated by gnu autoconf",
                "generated automatically by aclocal",
                "generated by autoheader",
                "generated by configure",
                "generated by automake",
                "automake helper",
                "generated by libtoolize",
            )
        )
    )


def _is_proven_untracked_output_directory(root: Path, pure: Path) -> bool:
    parts = pure.parts
    if any(part in _CONTROLLER_OUTPUT_DIRECTORIES for part in parts):
        return True
    for index, part in enumerate(parts):
        output_root = root.joinpath(*parts[:index])
        if fnmatch.fnmatch(part, "cmake-build-*"):
            output_root = output_root / part
            return any(
                _looks_like_cmake_generated_file(output_root / marker)
                for marker in ("CMakeCache.txt", "build.ninja")
            )
        if part in {"CMakeFiles", "Testing"}:
            return any(
                _looks_like_cmake_generated_file(output_root / marker)
                for marker in ("CMakeCache.txt", "build.ninja")
            )
        if part in {"autom4te.cache", ".deps", ".libs"}:
            return _has_autotools_input(root, output_root)
        if part == "__pycache__":
            return pure.suffix == ".pyc"
    return False


def _tracked_autotools_generated_artifact(
    root: Path,
    path: str,
    *,
    operation: str,
    base_commit: str,
) -> bool:
    """Identify a visible tracked Autotools product by its own provenance."""
    normalized = path.replace("\\", "/").strip("/")
    pure = Path(normalized)
    if (
        pure.name != "Makefile.in"
        and pure.name not in _AUTOTOOLS_GENERATED_NAMES
    ):
        return False
    current_generated = bool(
        operation != "deleted"
        and _is_verification_generated_path(root, normalized, tracked=False)
    )
    if operation == "added" or current_generated:
        return current_generated

    parent = root / pure.parent
    if pure.name == "Makefile.in":
        belongs_to_autotools = (parent / "Makefile.am").is_file()
    elif pure.name == "configure":
        belongs_to_autotools = any(
            (parent / item).is_file()
            for item in ("configure.ac", "configure.in")
        )
    else:
        belongs_to_autotools = _has_autotools_input(root, parent)
    if not belongs_to_autotools:
        return False
    blob = _run_git(["show", f"{base_commit}:{normalized}"], root)
    stdout = blob.get("stdout")
    return bool(
        blob.get("returncode") == 0
        and isinstance(stdout, str)
        and _looks_like_autotools_generated_header(stdout[:16384])
    )


def _is_verification_generated_path(
    root: Path,
    path: str,
    *,
    tracked: bool,
) -> bool:
    """Filter only controller-owned or provenance-marked untracked products.

    Every tracked modification remains in the change audit because this
    snapshot runs before the final controller build and cannot attribute such
    a change to a focused check. Untracked CMake and Autotools products require
    a local generator marker; source-owned build metadata stays deliverable.
    """
    if not tracked and path in {"opencode.json"}:
        return True
    normalized = path.replace("\\", "/").lstrip("/")
    pure = Path(normalized)
    parts = pure.parts
    # A tracked modification is candidate-owned and must remain auditable.
    # Snapshotting happens before the final controller build, so neither a
    # familiar filename nor a project name can prove that a tracked change was
    # produced by a focused check rather than by the candidate.
    if tracked:
        return False
    if not tracked and ".idea-refactoring" in parts:
        return True
    if _is_proven_untracked_output_directory(root, pure):
        return True
    cmake_generated_names = frozenset({
        "CMakeCache.txt",
        "CTestTestfile.cmake",
        "DartConfiguration.tcl",
        "cmake_install.cmake",
        "cmake_uninstall.cmake",
        "build.ninja",
        "rules.ninja",
    })
    candidate = root / pure
    if (
        pure.name in cmake_generated_names
        and _looks_like_cmake_generated_file(candidate)
    ):
        return True
    ignored_prefixes = (
        ".smell-artifacts/",
        ".idea/",
        ".opencode/",
    )
    if not tracked and any(normalized.startswith(prefix) for prefix in ignored_prefixes):
        return True

    parent = candidate.parent
    name = pure.name
    if name == "Makefile":
        return _looks_like_configure_makefile(candidate)
    if name == "Makefile.in":
        return (
            (parent / "Makefile.am").is_file()
            and _looks_like_autotools_generated_file(candidate)
        )
    if name == "configure":
        # A nested project-authored script called configure is not enough;
        # autoconf emits it beside configure.ac/configure.in.
        return any(
            (parent / item).is_file()
            for item in ("configure.ac", "configure.in")
        ) and _looks_like_autotools_generated_file(candidate)
    if name in _AUTOTOOLS_GENERATED_NAMES:
        return (
            _has_autotools_input(root, parent)
            and _looks_like_autotools_generated_file(candidate)
        )
    if fnmatch.fnmatch(name, "stamp-h*") and _has_autotools_input(root, parent):
        return True
    if name in _LIBTOOL_GENERATED_MACROS and pure.parent.name == "m4":
        return _has_autotools_input(root, parent)
    return False


_SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".java", ".kt", ".kts", ".groovy", ".scala", ".py", ".pyi",
    ".go", ".rs", ".js", ".jsx", ".ts", ".tsx", ".lua", ".rb",
    ".php", ".swift", ".m", ".mm", ".cs",
})
_TEST_DIRECTORY_NAMES = frozenset({
    "test", "tests", "testing", "unittest", "unittests",
    "unit-test", "unit-tests", "unit_test", "unit_tests", "integration-test",
    "integration-tests", "integration_test", "integration_tests",
    "functional-test", "functional-tests", "functional_test", "functional_tests",
})
_BUILD_METADATA_NAMES = frozenset({
    "makefile", "gnumakefile", "cmakelists.txt", "meson.build",
    "meson_options.txt", "configure", "configure.ac", "configure.in",
    "makefile.am", "makefile.in", "makefile.inc", "pom.xml", "build.xml", "build.gradle",
    "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "gradle.properties", "gradlew", "gradlew.bat", "mvnw", "mvnw.cmd",
    "build", "build.bazel", "workspace", "workspace.bazel", "module.bazel",
    "cmakecache.txt", "ctesttestfile.cmake", "dartconfiguration.tcl",
    "cmake_install.cmake", "cmake_uninstall.cmake",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum", "composer.json",
    "composer.lock", "gemfile", "gemfile.lock",
})
_BUILD_METADATA_SUFFIXES = (
    ".cmake", ".mk", ".mak", ".gradle", ".ninja", ".pc",
)


def _is_explicit_test_path(path: str) -> bool:
    """Classify only conventional, unambiguous test paths.

    The final audit is a safety boundary, not a test detector.  Exact directory
    components and conventional test-source basenames are sufficient for the
    known edit behavior without turning arbitrary occurrences of ``test`` into
    policy violations.
    """
    normalized = path.replace("\\", "/").strip("/")
    pure = Path(normalized)
    parts = tuple(part.casefold() for part in pure.parts[:-1])
    if any(part in _TEST_DIRECTORY_NAMES for part in parts):
        return True
    stem = pure.stem.casefold()
    suffix = pure.suffix.casefold()
    if suffix not in _SOURCE_SUFFIXES:
        return False
    return bool(
        stem.startswith(("test_", "test-"))
        or stem.endswith((
            "_test", "-test", "_tests", "-tests", "_unittest", "_spec", "-spec",
        ))
    )


def _is_build_metadata_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    pure = Path(normalized)
    name = pure.name.casefold()
    parts = tuple(part.casefold() for part in pure.parts[:-1])
    if name in _BUILD_METADATA_NAMES:
        return True
    if name in {item.casefold() for item in _AUTOTOOLS_GENERATED_NAMES}:
        return True
    if name in {item.casefold() for item in _LIBTOOL_GENERATED_MACROS}:
        return True
    if fnmatch.fnmatch(name, "stamp-h*"):
        return True
    if name.endswith(_BUILD_METADATA_SUFFIXES):
        return True
    if name.startswith("requirements") and name.endswith((".txt", ".in")):
        return True
    return any(part in {"cmake", "build-aux", "build_aux", "gradle"} for part in parts)


def _normalized_declared_test_paths(root: Path, values: list[str] | None) -> set[str]:
    normalized: set[str] = set()
    for raw in values or []:
        candidate = Path(str(raw).strip()).expanduser()
        if not str(candidate):
            continue
        try:
            relative = (
                candidate.resolve().relative_to(root.resolve())
                if candidate.is_absolute()
                else candidate
            )
        except (OSError, ValueError):
            continue
        rendered = relative.as_posix()
        if rendered and ".." not in relative.parts:
            normalized.add(rendered)
    return normalized


def _change_category(path: str, declared_test_paths: set[str]) -> str:
    if path in declared_test_paths or _is_explicit_test_path(path):
        return "test"
    if _is_build_metadata_path(path):
        return "build_metadata"
    if Path(path).suffix.casefold() in _SOURCE_SUFFIXES:
        return "production"
    return "other"


def _git_change_records(
    root: Path,
    *,
    declared_test_paths: list[str] | None = None,
    base_commit: str = "HEAD",
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Classify every path changed since the controller-frozen c000 commit."""
    result = _run_git(
        [
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
            "--",
        ],
        root,
    )
    stdout = result.get("stdout")
    if result.get("returncode") != 0 or not isinstance(stdout, str):
        return result, []
    records: list[dict[str, str]] = []
    ignored_tracked: list[str] = []
    ignored_untracked: list[str] = []
    declared = _normalized_declared_test_paths(root, declared_test_paths)
    fields = stdout.split("\0")
    for index in range(0, len(fields) - 1, 2):
        status = fields[index]
        path = fields[index + 1].replace("\\", "/")
        if not status or not path:
            continue
        if _is_verification_generated_path(root, path, tracked=True):
            ignored_tracked.append(path)
            continue
        if status.startswith("D"):
            operation = "deleted"
        elif status.startswith("A"):
            operation = "added"
        else:
            operation = "changed"
        records.append({
            "path": path,
            "operation": operation,
            "status": status,
            "category": _change_category(path, declared),
        })

    untracked = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
        root,
    )
    untracked_stdout = untracked.get("stdout")
    if untracked.get("returncode") != 0 or not isinstance(untracked_stdout, str):
        return {
            **result,
            "returncode": untracked.get("returncode"),
            "stderr": untracked.get("stderr"),
        }, []
    for raw_path in untracked_stdout.split("\0"):
        path = raw_path.replace("\\", "/")
        if not path:
            continue
        if _is_verification_generated_path(root, path, tracked=False):
            ignored_untracked.append(path)
            continue
        records.append({
            "path": path,
            "operation": "added",
            "status": "??",
            "category": _change_category(path, declared),
        })
    result["ignored_tracked_count"] = len(ignored_tracked)
    result["ignored_untracked_count"] = len(ignored_untracked)
    result["ignored_generated_count"] = len(ignored_tracked) + len(ignored_untracked)
    result["ignored_generated_paths"] = sorted(
        {*ignored_tracked, *ignored_untracked}
    )
    return result, sorted(records, key=lambda item: (item["path"], item["operation"]))


def _final_diff_generated_artifact_audit(
    change_audit: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Reject visible generated build/test products from the final delivery.

    This is deliberately not a suffix ignore.  The paths remain ordinary
    visible changes. Controller-owned root trees, provenance-marked tracked
    Autotools products, known RRDtool verification products, and the narrow
    cJSON root-level product family block final acceptance. New source, tests,
    documentation and authored build metadata keep their existing delivery.
    """
    artifacts: list[dict[str, str]] = []
    base_commit = str(change_audit.get("base_commit") or "HEAD")
    for raw_item in list(change_audit.get("changes") or []):
        if not isinstance(raw_item, dict):
            continue
        operation = str(raw_item.get("operation") or "")
        if operation not in {"added", "changed", "deleted"}:
            continue
        path = str(raw_item.get("path") or "").replace("\\", "/")
        if not path:
            continue
        controller_owned = any(
            path.startswith(f"{directory}/")
            for directory in _CONTROLLER_OUTPUT_DIRECTORIES
        )
        cjson_root_product = bool(
            operation in {"added", "changed"}
            and "/" not in path
            and (
                path in _CJSON_ROOT_BUILD_PRODUCTS
                or _CJSON_ROOT_LIBRARY_PRODUCT.fullmatch(path) is not None
            )
        )
        tracked_autotools_product = bool(
            project_root is not None
            and _tracked_autotools_generated_artifact(
                project_root,
                path,
                operation=operation,
                base_commit=base_commit,
            )
        )
        rrdtool_verification_product = bool(
            project_root is not None
            and project_root.name.casefold() == "rrdtool"
            and path in _RRDTOOL_TRACKED_VERIFICATION_PRODUCTS
        )
        project_build_product = bool(
            project_root is not None
            and _project_generated_output_root(project_root, path)
        )
        if not any((
            controller_owned,
            cjson_root_product,
            tracked_autotools_product,
            rrdtool_verification_product,
            project_build_product,
        )):
            continue
        artifacts.append({
            "path": path,
            "operation": operation,
            "status": str(raw_item.get("status") or ""),
        })
    artifacts.sort(key=lambda item: item["path"])
    audit_ok = change_audit.get("success") is True
    return {
        "contract_version": "final-diff-generated-artifacts/v1",
        "success": bool(audit_ok and not artifacts),
        "status": (
            "WORKTREE_CHANGE_AUDIT_FAILED"
            if not audit_ok
            else "FINAL_DIFF_GENERATED_ARTIFACTS"
            if artifacts
            else "FINAL_DIFF_GENERATED_ARTIFACTS_ABSENT"
        ),
        "reason": (
            "WORKTREE_CHANGE_AUDIT_FAILED"
            if not audit_ok
            else "FINAL_DIFF_GENERATED_ARTIFACTS"
            if artifacts
            else ""
        ),
        "paths": [item["path"] for item in artifacts],
        "artifacts": artifacts,
    }


def _project_change_audit(
    root: Path,
    *,
    declared_test_paths: list[str] | None = None,
    base_commit: str = "HEAD",
) -> dict[str, Any]:
    status, records = _git_change_records(
        root,
        declared_test_paths=declared_test_paths,
        base_commit=base_commit,
    )
    categories = {
        category: [dict(item) for item in records if item["category"] == category]
        for category in ("production", "test", "build_metadata", "other")
    }
    audit = {
        "schema_version": "smell.worktree-change-audit/v1",
        "base_commit": base_commit,
        "success": status.get("returncode") == 0,
        "status_returncode": status.get("returncode"),
        "change_count": len(records),
        "changes": records,
        "categories": categories,
        "category_counts": {key: len(value) for key, value in categories.items()},
        "ignored_tracked_count": int(status.get("ignored_tracked_count") or 0),
        "ignored_untracked_count": int(status.get("ignored_untracked_count") or 0),
        "ignored_generated_count": int(status.get("ignored_generated_count") or 0),
        "ignored_generated_paths": list(status.get("ignored_generated_paths") or []),
    }
    audit["final_diff_generated_artifact_audit"] = (
        _final_diff_generated_artifact_audit(audit, project_root=root)
    )
    return audit


def _worktree_test_change_audit(
    change_audit: dict[str, Any],
    *,
    allow_test_changes: bool,
) -> dict[str, Any]:
    categories = change_audit.get("categories") or {}
    test_records = categories.get("test") if isinstance(categories, dict) else []
    if not isinstance(test_records, list):
        test_records = []
    groups = {
        operation: [
            {"path": str(item.get("path") or "")}
            for item in test_records
            if isinstance(item, dict) and item.get("operation") == operation
        ]
        for operation in ("added", "changed", "deleted")
    }
    modified = any(groups.values())
    success = bool(change_audit.get("success")) and (
        allow_test_changes or not modified
    )
    status = (
        "WORKTREE_CHANGE_AUDIT_FAILED"
        if not change_audit.get("success")
        else "TEST_SOURCE_CHANGE_ALLOWED"
        if modified and allow_test_changes
        else "TEST_SOURCE_MODIFIED"
        if modified
        else "TEST_SOURCE_UNCHANGED"
    )
    return {
        "contract_version": "worktree-change-audit/v1",
        "success": success,
        "status": status,
        "reason": "" if success else status,
        "mode": "explicit_test_changes" if allow_test_changes else "immutable",
        "allow_test_changes": allow_test_changes,
        "modified": modified,
        "test_source_modified": modified,
        **groups,
        "change_count": sum(len(value) for value in groups.values()),
    }


def _git_status_snapshot(
    root: Path,
    *,
    base_commit: str = "HEAD",
    paths: list[str] | None = None,
) -> dict[str, Any]:
    if paths is not None and not paths:
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "ignored_untracked_count": 0,
        }
    pathspec = list(paths or [])
    result = _run_git(
        [
            "diff",
            "--name-status",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
            "--", *pathspec,
        ],
        root,
    )
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return result
    filtered_lines: list[str] = []
    filtered_lines.extend(stdout.splitlines())
    for path in _git_untracked_files(root, pathspec or None):
        filtered_lines.append(f"??\t{path}")
    result["stdout"] = ("\n".join(filtered_lines) + "\n") if filtered_lines else ""
    result["ignored_untracked_count"] = 0
    return result


def _diff_untracked_files(root: Path, paths: list[str], *, stat: bool = False) -> str:
    chunks: list[str] = []
    for path in paths:
        args = [
            "diff", "--no-index", "--no-ext-diff", "--no-textconv",
            "--inter-hunk-context=0", "--unified=3",
            "--src-prefix=a/", "--dst-prefix=b/",
            "--diff-algorithm=myers", "--no-indent-heuristic",
        ]
        if stat:
            args.append("--stat")
        else:
            args.append("--binary")
        args.extend(["--", "/dev/null", path])
        result = _run_git(args, root)
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        output = stdout if isinstance(stdout, str) and stdout else stderr
        if isinstance(output, str) and output:
            chunks.append(output.rstrip("\n"))
    return ("\n".join(chunks) + "\n") if chunks else ""


def _git_diff_with_untracked(
    root: Path,
    args: list[str],
    *,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    if paths is not None and not paths:
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "untracked_files": [],
        }
    pathspec = list(paths or [])
    result = _run_git([*args, "--", *pathspec], root)
    tracked_diff = result.get("stdout")
    if not isinstance(tracked_diff, str):
        tracked_diff = ""
    untracked_files = _git_untracked_files(root, pathspec or None)
    untracked_diff = _diff_untracked_files(root, untracked_files, stat="--stat" in args)
    result["stdout"] = tracked_diff + untracked_diff
    result["untracked_files"] = untracked_files
    return result


def _snapshot_project(
    root: Path,
    *,
    declared_test_paths: list[str] | None = None,
    base_commit: str = "HEAD",
) -> dict[str, Any]:
    """Capture the complete pre-verification deliverable patch and path audit."""
    change_audit = _project_change_audit(
        root,
        declared_test_paths=declared_test_paths,
        base_commit=base_commit,
    )
    deliverable_paths = [
        str(item.get("path") or "")
        for item in change_audit.get("changes") or []
        if isinstance(item, dict) and item.get("path")
    ]
    status = _git_status_snapshot(
        root,
        base_commit=base_commit,
        paths=deliverable_paths,
    )
    status.update({
        "ignored_tracked_count": int(
            change_audit.get("ignored_tracked_count") or 0
        ),
        "ignored_untracked_count": int(
            change_audit.get("ignored_untracked_count") or 0
        ),
        "ignored_generated_count": int(
            change_audit.get("ignored_generated_count") or 0
        ),
    })
    return {
        "project_root": str(root),
        "scope": "full_worktree_pre_verification",
        "base_commit": base_commit,
        "change_audit": change_audit,
        "status": status,
        # The c000 commit remains stable even if the candidate creates a local
        # commit. Build metadata therefore stays in the same replayable patch
        # as staged, unstaged and committed production changes.
        "diff_stat": _git_diff_with_untracked(
            root,
            [
                "diff", "--no-ext-diff", "--no-textconv",
                "--inter-hunk-context=0", "--unified=3",
                "--src-prefix=a/", "--dst-prefix=b/",
                "--diff-algorithm=myers", "--no-indent-heuristic",
                base_commit, "--stat",
            ],
            paths=deliverable_paths,
        ),
        "diff": _git_diff_with_untracked(
            root,
            [
                "diff", "--no-ext-diff", "--no-textconv",
                "--inter-hunk-context=0", "--unified=3", "--binary",
                "--src-prefix=a/", "--dst-prefix=b/",
                "--diff-algorithm=myers", "--no-indent-heuristic",
                base_commit,
            ],
            paths=deliverable_paths,
        ),
    }


def _artifact_paths_from_verify_payload(payload: Optional[dict[str, Any]], discovered: dict[str, str]) -> dict[str, str]:
    paths: dict[str, str] = dict(discovered)
    evidence_path = paths.get("guard_evidence") or paths.get("verify_full")
    if evidence_path:
        artifact_dir = Path(evidence_path).parent
        sibling_names = {
            "build_log": "build.log",
            "test_log": "test.log",
            "sample_test_log": "sample-test.log",
            "diff": "diff.patch",
            "diff_stat": "diff.stat",
            "build_result": "build.full.json",
            "test_result": "test.full.json",
            "sample_test_result": "sample-test.full.json",
        }
        for key, name in sibling_names.items():
            sibling = artifact_dir / name
            if sibling.is_file():
                paths.setdefault(key, str(sibling))
    if not isinstance(payload, dict):
        return paths
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        for key, value in artifacts.items():
            if isinstance(value, str) and value:
                paths[key] = value
    build_test = payload.get("build_test_guard")
    if isinstance(build_test, dict):
        nested = build_test.get("artifacts")
        if isinstance(nested, dict):
            for key, value in nested.items():
                if isinstance(value, str) and value:
                    paths.setdefault(key, value)
    return paths


def _read_artifact_text(paths: dict[str, str], key: str, *, max_chars: int = 20000) -> str:
    raw = paths.get(key)
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_file():
        return ""
    # Read only a bounded tail.  verify/full build artifacts can be hundreds of
    # megabytes, so read_text() here would defeat the compact decision channel.
    max_bytes = max(4096, max_chars * 4)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw_tail = handle.read(max_bytes)
    except OSError:
        return ""
    return raw_tail.decode("utf-8", errors="replace")[-max_chars:]


def _failure_payload_summary(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    smell_guard = _compact_smell_guard(payload.get("smell_guard"))
    return {
        "status": _bounded_text(payload.get("status"), limit=128),
        "resolution": _bounded_text(payload.get("resolution"), limit=128),
        "continue_hint": _bounded_text(payload.get("continue_hint")),
        "smell_guard": smell_guard,
        "build_test_guard": _summarize_build_test_guard(payload.get("build_test_guard")),
        "test_changes": _compact_test_changes(payload.get("test_changes")),
        "checkpoint": _compact_checkpoint(payload.get("checkpoint")),
    }


def _failure_text_bundle(payload: Optional[dict[str, Any]], paths: dict[str, str]) -> str:
    chunks: list[str] = []
    if payload is not None:
        chunks.append(json.dumps(_failure_payload_summary(payload), ensure_ascii=True))
    # Structured result artifacts duplicate the inline payload and may contain
    # complete Guard evidence.  Only diagnostic log/diff tails belong in
    # failure classification.
    for key in ("build_log", "test_log", "sample_test_log", "diff"):
        text = _read_artifact_text(paths, key)
        if text:
            chunks.append(f"\n--- {key} ---\n{text}")
    return "\n".join(chunks)


def _highlight_patterns(
    text: str,
    patterns: list[str],
    *,
    context: int = 2,
    limit: int = DECISION_HIGHLIGHT_LIMIT,
    max_chars: int = DECISION_TEXT_LIMIT,
) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    matched: list[str] = []
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for index, line in enumerate(lines):
        if not any(regex.search(line) for regex in regexes):
            continue
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        snippet = "\n".join(lines[start:end]).strip()
        snippet = _bounded_text(snippet, limit=max_chars)
        if snippet and snippet not in matched:
            matched.append(snippet)
        if len(matched) >= limit:
            break
    return matched


def _looks_like_dependency_resolution_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "dependencyresolutionexception",
            "could not resolve dependencies",
            "could not find artifact",
            "failed to collect dependencies",
            "non-resolvable parent pom",
        )
    )


def _timed_out_build_test_step(payload: dict[str, Any]) -> str:
    build_test = payload.get("build_test_guard")
    details = build_test.get("details") if isinstance(build_test, dict) else None
    if not isinstance(details, dict):
        return ""
    for label in ("build", "test", "sample_test"):
        result = details.get(label)
        if isinstance(result, dict) and result.get("status") == "timeout":
            return label
    return ""


def _test_not_executed_failure(
    result: dict[str, Any],
    *,
    sample_level: bool,
) -> tuple[str, list[str]]:
    failure_text = " ".join(
        str(item) for item in (result.get("failure_highlights") or [])
    )
    if (
        sample_level
        and "Pinned sample test location does not identify a test class" in failure_text
    ):
        return "SAMPLE_TEST_EVIDENCE_INVALID", [
            "The configured test command passed, but the pinned test-file evidence is invalid.",
            "This dataset/configuration defect cannot be repaired by editing production code.",
        ]
    category = "SAMPLE_TEST_EVIDENCE_MISSING" if sample_level else "TEST_EVIDENCE_MISSING"
    return category, [
        "The configured test stage did not execute a verifiable test suite.",
        "Treat this as a verification configuration/evidence problem; do not repair production code or weaken tests.",
    ]


def _classify_failure_pack(
    payload: Optional[dict[str, Any]],
    text: str,
    *,
    smell: str = "",
    evidence: str = "",
) -> tuple[str, list[str]]:
    if payload is None:
        return "UNKNOWN_VERIFY_FAILURE", ["No verify artifact or inline verify payload was available."]
    status = str(payload.get("status") or "").strip()
    if status == "BUILD_TEST_REQUIRED":
        return "BUILD_TEST_REQUIRED", [
            "Build/test verification is required for this batch run; rerun smell_verify without skipBuildTest.",
        ]
    if status == "VERIFICATION_CONFIG_MODIFIED":
        return "VERIFICATION_CONFIG_MODIFIED", [
            "Restore the controller-frozen build/test discovery configuration; production refactoring must not weaken verification."
        ]
    if status == "WORKTREE_CHANGE_AUDIT_FAILED":
        return "WORKTREE_CHANGE_AUDIT_FAILED", [
            "Final Git status/diff audit failed; resolve the repository-state error before evaluating or repairing the candidate."
        ]
    if status == "FINAL_VERIFY_INFRA_FAILED":
        return "FINAL_VERIFY_INFRA_FAILED", [
            "The controller could not create, populate, resolve or clean the isolated project-full verification worktree.",
            "Do not edit production or tests for this infrastructure failure; retry only after the verification environment is healthy.",
        ]
    if status == "FINAL_DIFF_GENERATED_ARTIFACTS":
        build_test = payload.get("build_test_guard") or {}
        audit = (
            build_test.get("final_diff_generated_artifact_audit")
            if isinstance(build_test, dict)
            else None
        )
        tracked_paths, untracked_paths = _generated_artifact_path_groups(
            audit if isinstance(audit, dict) else {}
        )
        recommendations: list[str] = []
        if tracked_paths:
            recommendations.append(
                "Restore tracked generated paths to the frozen baseline: "
                + ", ".join(tracked_paths)
                + "."
            )
        if untracked_paths:
            recommendations.append(
                "Remove untracked generated paths: "
                + ", ".join(untracked_paths)
                + "."
            )
        recommendations.append(
            "Keep authored source, tests, documentation and build metadata in the final diff."
        )
        return "FINAL_DIFF_GENERATED_ARTIFACTS", recommendations
    if status == "TEST_SOURCE_MODIFIED":
        return "TEST_SOURCE_MODIFIED", [
            "Restore test-tree changes, or start a new command with explicit controller authorization to edit tests."
        ]
    if status == "TEST_SOURCE_MIGRATION_REJECTED":
        return "TEST_BEHAVIOR_REGRESSION", [
            "Keep the controller-authorized test API migration, but restore every removed test/assertion and remove newly added disabled/ignored/assumption-skip signals."
        ]
    if status == "TEST_SOURCE_DELETED":
        return "TEST_BEHAVIOR_REGRESSION", [
            "Restore every baseline test file; api_migration permits API edits, not deletion of behavior checks."
        ]
    timed_out_step = _timed_out_build_test_step(payload)
    if timed_out_step:
        return "TIMEOUT_OR_MODAL_SUSPECTED", [
            f"The configured {timed_out_step} command exceeded its controller timeout.",
            "Treat this as an execution/infrastructure failure; do not automatically rewrite production or test code.",
        ]
    if status == "SAMPLE_TEST_SPEC_MISSING":
        return "SAMPLE_TEST_SPEC_MISSING", [
            "Sample-optimized verification requires SMELL_SAMPLE_TEST_COMMAND or --sample-test-command.",
        ]
    if status == "SAMPLE_TEST_EVIDENCE_MISSING":
        smell_guard = payload.get("smell_guard") or {}
        if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
            return "SMELL_GUARD_FAILED", [
                "Smell guard did not pass; continue the refactoring rather than repairing tests."
            ]
        return "SAMPLE_TEST_EVIDENCE_MISSING", [
            "The sample test command exited successfully, but no fresh structured test report was retained.",
            "Treat this as a verification configuration problem; do not repair production code or weaken tests.",
        ]
    if status == "TEST_EVIDENCE_MISSING":
        smell_guard = payload.get("smell_guard") or {}
        if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
            return "SMELL_GUARD_FAILED", [
                "Smell guard did not pass; continue the refactoring while the controller fixes project test evidence."
            ]
        return "TEST_EVIDENCE_MISSING", [
            "The project test stage did not retain evidence that a real test suite executed.",
            "Treat this as a verification configuration/evidence problem; do not repair production code.",
        ]
    if status == "SAMPLE_TEST_FAILED":
        test_changes = payload.get("test_changes") or {}
        if isinstance(test_changes, dict) and test_changes.get("status") in {
            "TEST_SOURCE_MODIFIED",
            "TEST_SOURCE_MIGRATION_REJECTED",
            "TEST_SOURCE_DELETED",
            "VERIFICATION_CONFIG_MODIFIED",
            "WORKTREE_CHANGE_AUDIT_FAILED",
        }:
            return "SAMPLE_TEST_FAILED", [
                "TEST_SOURCE_MODIFIED: restore the test-tree changes frozen as immutable at c000."
            ]
        smell_guard = payload.get("smell_guard") or {}
        if not (isinstance(smell_guard, dict) and smell_guard.get("success") is False):
            build_test = payload.get("build_test_guard") or {}
            details = build_test.get("details") if isinstance(build_test, dict) else {}
            test = details.get("test") if isinstance(details, dict) else {}
            if (
                isinstance(details, dict)
                and isinstance(details.get("sample_test"), dict)
                and details["sample_test"].get("success") is False
            ):
                test = details["sample_test"]
            test_status = str(test.get("status") or "") if isinstance(test, dict) else ""
            if (
                test_status == "test_not_executed"
            ):
                return _test_not_executed_failure(test, sample_level=True)
            return "SAMPLE_TEST_FAILED", [
                "The sample-level test command failed; fix the regression or report the blocker explicitly.",
            ]
    smell_guard = payload.get("smell_guard") or {}
    test_changes = payload.get("test_changes") or {}
    if isinstance(test_changes, dict) and test_changes.get("status") in {
        "TEST_SOURCE_MODIFIED",
        "TEST_SOURCE_MIGRATION_REJECTED",
        "TEST_SOURCE_DELETED",
        "VERIFICATION_CONFIG_MODIFIED",
        "WORKTREE_CHANGE_AUDIT_FAILED",
    }:
        policy_status = str(test_changes.get("status") or "")
        if policy_status in {"TEST_SOURCE_MIGRATION_REJECTED", "TEST_SOURCE_DELETED"}:
            return "TEST_BEHAVIOR_REGRESSION", [
                "Repair the controller-authorized API migration without deleting or weakening the frozen test behavior."
            ]
        return "TEST_BEHAVIOR_REGRESSION", [
            "TEST_SOURCE_MODIFIED: restore the test-tree changes frozen as immutable at c000."
        ]
    build_test = payload.get("build_test_guard") or {}
    if isinstance(build_test, dict):
        details = build_test.get("details") or {}
        build = details.get("build") or {}
        test = details.get("test") or {}
        sample_test = details.get("sample_test") or {}
        if isinstance(build, dict) and build.get("success") is False:
            if _looks_like_dependency_resolution_failure(text):
                return "BUILD_DEPENDENCY_RESOLUTION", [
                    "Build stopped while resolving Maven/Gradle dependencies or generated classifier artifacts.",
                    "Treat this as a project verification configuration issue unless logs also show source compilation errors.",
                ]
            return "BUILD_COMPILE_ERROR", ["Inspect the build log and fix the build failure before retrying verification."]
        if isinstance(test, dict) and test.get("success") is False:
            if str(test.get("status") or "") == "test_not_executed":
                if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
                    return "SMELL_GUARD_FAILED", [
                        "Smell guard did not pass; continue the refactoring while the controller fixes project test evidence."
                    ]
                return _test_not_executed_failure(test, sample_level=False)
            return "TEST_BEHAVIOR_REGRESSION", [
                "The structured project test stage failed; inspect its assertions and repair the behavior regression.",
            ]
        if isinstance(sample_test, dict) and sample_test.get("success") is False:
            if str(sample_test.get("status") or "") == "test_not_executed":
                if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
                    return "SMELL_GUARD_FAILED", [
                        "Smell guard did not pass; continue the refactoring while the controller fixes sample test evidence."
                    ]
                return _test_not_executed_failure(sample_test, sample_level=True)
            return "SAMPLE_TEST_FAILED", [
                "The structured sample test stage failed; repair the regression without weakening the test.",
            ]
    # A concrete build or test failure is the immediate repair target even if
    # the smell objective is also incomplete. Only route back to the smell
    # guard when verification did not report a more fundamental failure.
    if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
        return "SMELL_GUARD_FAILED", ["Smell guard did not pass; continue the refactoring rather than repairing tests."]
    lowered = text.lower()
    if _looks_like_dependency_resolution_failure(text):
        return "BUILD_DEPENDENCY_RESOLUTION", [
            "Build stopped while resolving Maven/Gradle dependencies or generated classifier artifacts.",
            "Treat this as a project verification configuration issue unless logs also show source compilation errors.",
        ]
    if "nosuchmethodexception" in lowered and ("getdeclaredmethod" in lowered or "getmethod" in lowered):
        return "TEST_REFLECTION_ENTRY_STALE", [
            "Verification points to a stale reflection entrypoint. Inspect reflected method names and parameter type arrays.",
            "If production behavior is correct, update the test or fixture signature without weakening assertions.",
        ]
    if any(marker in lowered for marker in ("compilation failure", "cannot find symbol", "package does not exist", "class, interface, enum, or record expected")):
        return "BUILD_COMPILE_ERROR", ["Fix the compile error in production/test source before retrying verification."]
    if any(marker in lowered for marker in ("stale_draft", "selectionkind", "selection kind", "needs_more_info", "operationcandidates")):
        return "IDEA_SELECTION_OR_DRAFT_FAILED", ["Re-locate from fresh file contents or choose a smaller valid operation selection."]
    # Top-level structured outcomes outrank incidental words in logs (for
    # example a test function whose name contains ``timeout``). Free-text
    # timeout matching below is only a diagnostic fallback when no build/test
    # stage reported an authoritative outcome.
    if status == "BUILD_FAILED":
        return "BUILD_COMPILE_ERROR", ["Inspect the build log and fix the build failure before retrying verification."]
    if status == "TEST_FAILED":
        return "TEST_BEHAVIOR_REGRESSION", ["Treat the structured test failure as a behavior regression."]
    if status == "SAMPLE_TEST_FAILED":
        return "SAMPLE_TEST_FAILED", ["The sample-level test command failed; inspect its structured result."]
    if any(marker in lowered for marker in ("timeout", "modal", "dialog", "frontmost_window", "timed_out")):
        return "TIMEOUT_OR_MODAL_SUSPECTED", ["Inspect timeout diagnostics and IDEA window artifacts before retrying the same operation."]
    if status == "TEST_FAILED" or " failed" in lowered or "assertionerror" in lowered:
        return "TEST_BEHAVIOR_REGRESSION", ["Treat this as a behavior regression unless logs clearly show a stale test entrypoint."]
    return "UNKNOWN_VERIFY_FAILURE", ["Read the linked artifacts before choosing a repair route."]


def _smell_guard_failure_highlights(payload: Optional[dict[str, Any]], *, limit: int = 190) -> list[str]:
    """Keep the authoritative guard target visible in the continuation prompt."""
    if not isinstance(payload, dict):
        return []
    smell_guard = payload.get("smell_guard")
    if not isinstance(smell_guard, dict) or smell_guard.get("success") is not False:
        return []
    highlights: list[str] = []
    max_highlights = 1
    for result in smell_guard.get("results") or []:
        if not isinstance(result, dict) or result.get("success") is not False:
            continue
        message = " ".join(str(result.get("message") or "").split())
        if not message:
            continue
        prefix = "GUARD_TARGET "
        available = max(1, limit - len(prefix))
        if len(message) > available:
            head = max(1, int(available * 0.65))
            tail = max(1, available - head - 5)
            message = f"{message[:head]} ... {message[-tail:]}"
        highlights.append(prefix + message)
        if len(highlights) >= max_highlights:
            break
    return [
        item if len(item) <= limit else item[: max(1, limit - 3)].rstrip() + "..."
        for item in highlights
    ]


def _build_failure_pack(
    payload: Optional[dict[str, Any]],
    artifact_paths: dict[str, str],
    *,
    smell: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    paths = _artifact_paths_from_verify_payload(payload, artifact_paths)
    bundle = _failure_text_bundle(payload, paths)
    category, recommendations = _classify_failure_pack(
        payload,
        bundle,
        smell=smell,
        evidence=evidence,
    )
    failure_group = REPAIRABLE_CATEGORY_GROUPS.get(category, "")
    repairable = bool(failure_group)
    patterns = [
        "DependencyResolutionException",
        "Could not resolve dependencies",
        "Could not find artifact",
        "Failed to collect dependencies",
        "Non-resolvable parent POM",
        "NoSuchMethodException",
        "getDeclaredMethod",
        "cannot find symbol",
        "Compilation failure",
        "FAILED",
        "Segmentation fault",
        "core dumped",
        "fatal error: Killed",
        "ninja: build stopped",
        "AssertionError",
        "STALE_DRAFT",
        "selectionKind",
        "needs_more_info",
        "timeout",
        "modal",
    ]
    checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else None
    test_changes = payload.get("test_changes") if isinstance(payload, dict) else None
    tests_may_change = bool(
        isinstance(test_changes, dict)
        and (
            test_changes.get("mode") == "api_migration"
            or test_changes.get("allow_test_changes") is True
        )
    )
    highlights = _smell_guard_failure_highlights(payload)
    highlights.extend(checkpoint_feedback_highlights(checkpoint))
    highlights.extend(_highlight_patterns(bundle, patterns))
    highlights = _bounded_strings(
        highlights,
        count=DECISION_HIGHLIGHT_LIMIT,
        limit=DECISION_TEXT_LIMIT,
    )
    resolution_plan = (
        checkpoint.get("resolution_plan")
        if isinstance(checkpoint, dict)
        and isinstance(checkpoint.get("resolution_plan"), dict)
        else None
    )
    next_action = resolution_plan_next_action(resolution_plan)
    return {
        "failure_category": category,
        "failure_group": failure_group,
        "retryable": repairable,
        "verify_status": payload.get("status") if isinstance(payload, dict) else "",
        "artifact_paths": paths,
        "highlights": highlights,
        "next_action": _bounded_text(next_action),
        "recommendations": _bounded_strings(recommendations),
        "repair_contract": {
            "repair_agent_may_edit": repairable,
            "prefer_narrow_fix": repairable,
            "must_rerun_smell_verify": repairable,
            "tests_may_change": tests_may_change,
        },
    }


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--project-override-root")
    parser.add_argument("--smell", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--language", default="")
    parser.add_argument("--config")
    parser.add_argument("--projects")
    parser.add_argument("--smell-evidence", default="")
    parser.add_argument("--target-context-json", default="")
    parser.add_argument("--baseline-seal", default="")
    parser.add_argument(
        "--verification-mode",
        choices=("local", "auto", "sample_optimized", "project_full"),
        default="",
    )
    parser.add_argument("--sample-test-location", default="")
    parser.add_argument("--sample-test-command", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smell_bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_command_parser = subparsers.add_parser("resolve-command")
    resolve_command_parser.add_argument("--arguments", required=True)
    resolve_command_parser.set_defaults(func=cmd_resolve_command)

    baseline_parser = subparsers.add_parser("capture-baseline")
    _add_common(baseline_parser)
    baseline_parser.add_argument("--allow-test-changes", action="store_true")
    baseline_parser.add_argument(
        "--output-detail",
        choices=("decision", "audit"),
        default="decision",
    )
    baseline_parser.set_defaults(func=cmd_capture_baseline)

    verify_parser = subparsers.add_parser("verify")
    _add_common(verify_parser)
    verify_parser.add_argument("--guard-progress-only", action="store_true")
    verify_parser.add_argument("--focused-preflight-only", action="store_true")
    verify_parser.add_argument("--skip-build-test", action="store_true")
    verify_parser.add_argument("--no-snapshot", action="store_true")
    verify_parser.add_argument("--artifact-root")
    verify_parser.add_argument(
        "--output-detail",
        choices=("decision", "audit"),
        default="decision",
    )
    verify_parser.set_defaults(
        func=cmd_verify,
        run_build_test=True,
        snapshot=True,
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "skip_build_test", False):
        args.run_build_test = False
    if getattr(args, "no_snapshot", False):
        args.snapshot = False
    try:
        payload = args.func(args)
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=True), file=sys.stdout)
        return 1
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
