#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
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
    god_class_relative_reduction,
    run_build_test_guard,
    run_smell_guards,
    validate_java_strict_verification_contract,
)
from smell_core.java.idea_refactor import (  # noqa: E402
    IdeaRefactorPreflightError,
    IdeaRefactorPreflightOptions,
    resolve_idea_refactor_cli,
    run_idea_refactor_preflight,
)
from smell_core.languages import get_language  # noqa: E402
from smell_core.loop_policy import REPAIRABLE_CATEGORY_GROUPS, parse_command_policy  # noqa: E402
from smell_core.planning import build_plan_context_payload, build_repair_context_payload  # noqa: E402
from smell_core.prompts.idea_router import build_idea_prompt_route  # noqa: E402
from smell_core.target_context import parse_target_context_json  # noqa: E402


VERIFY_DECISION_SCHEMA = "smell.verify.decision/v1"
BASELINE_DECISION_SCHEMA = "smell.baseline.decision/v1"
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
    if _is_idea_backed(resolved.language):
        resolved.idea_refactor_cli = resolve_idea_refactor_cli(
            resolved,
            getattr(args, "idea_refactor_cli", None)
            or os.environ.get("SMELL_IDEA_REFACTOR_CLI")
            or os.environ.get("IDEA_REFACTOR_CLI"),
        )
    return resolved


def _is_idea_backed(language: str) -> bool:
    support = get_language(language)
    return bool(support and support.idea_backed)


def _location_payload(resolved) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in resolved.locations:
        idea_project_path = None
        try:
            idea_project_path = str(item.file_path.relative_to(resolved.idea_project_root))
        except ValueError:
            pass
        payload.append(
            {
                "raw": item.raw,
                "project_path": str(item.project_path),
                "idea_project_path": idea_project_path,
                "file_path": str(item.file_path),
                "display_path": item.display_path,
                "line": item.line,
                "method": item.method,
                "class_name": item.class_name,
                "start_line": item.start_line,
                "signature_text": item.signature_text,
                "parameter_count": item.parameter_count,
            }
        )
    return payload


def _profile_payload(resolved) -> dict[str, Any]:
    return {
        "instruction": resolved.profile.instruction,
        "constraints": list(resolved.profile.constraints),
        "verification": list(resolved.profile.verification),
        "guards": list(resolved.profile.guards),
        "retry_hint_template": resolved.profile.retry_hint_template,
    }


def _route_payload(resolved) -> dict[str, Any]:
    if not _is_idea_backed(resolved.language) or not resolved.idea_refactor_ready:
        return {
            "smell": resolved.smell,
            "route_ids": [],
            "preferred_operations": [],
            "guide": "",
            "examples": [],
        }
    route = build_idea_prompt_route(str(resolved.idea_refactor_cli or "idea-refactor"), resolved.idea_project_root, resolved)
    return {
        "smell": route.smell,
        "route_ids": list(route.route_ids),
        "preferred_operations": list(route.preferred_operations),
        "guide": route.guide,
        "examples": list(route.examples),
    }


def _run_idea_preflight(resolved, args: argparse.Namespace) -> dict[str, Any]:
    if not _is_idea_backed(resolved.language):
        return {
            "requested": False,
            "ready": False,
            "status": "unsupported_language",
            "message": f"IDEA refactoring is not configured for language '{resolved.language}'.",
        }
    if not getattr(args, "ensure_idea_service", False):
        return {
            "requested": False,
            "ready": False,
            "status": "skipped",
            "message": "IDEA service preflight was disabled.",
        }
    if os.environ.get("SMELL_IDEA_PREPARED") == "1":
        resolved.idea_refactor_ready = True
        return {
            "requested": False,
            "ready": True,
            "status": "externally_prepared",
            "message": "IDEA refactoring service was prepared by the batch runner.",
        }
    try:
        payload = run_idea_refactor_preflight(
            resolved,
            IdeaRefactorPreflightOptions(
                required=True,
                open=bool(getattr(args, "idea_open", False)),
                timeout=max(1, int(getattr(args, "idea_timeout", 60))),
                poll_interval=max(0.1, float(getattr(args, "idea_poll_interval", 1.0))),
                cli_path=getattr(args, "idea_refactor_cli", None) or resolved.idea_refactor_cli,
            ),
        )
    except IdeaRefactorPreflightError as exc:
        return {
            "requested": True,
            "ready": False,
            "status": "failed",
            "code": exc.code,
            "message": str(exc),
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }
    resolved.idea_refactor_ready = True
    return {
        "requested": True,
        "ready": True,
        "status": "ok",
        "message": "IDEA refactoring service is ready.",
        "details": payload,
    }


def _augment_data_clumps_context(resolved) -> Optional[dict[str, Any]]:
    if resolved.smell != "data_clumps":
        return None
    snapshot = capture_metric_snapshot(resolved, "")
    analyses = [{
        "success": bool(snapshot.get("ok")),
        "group": snapshot.get("group", ""),
        "occurrence_count": int(dict(snapshot.get("objectives") or {}).get("occurrence_count", 0)),
        "occurrences": list(snapshot.get("occurrences") or []),
        "error": snapshot.get("error", ""),
        "candidate_count": snapshot.get("candidate_count", 0),
    }]
    analysis = analyses[0]
    for guard in resolved.profile.guards:
        if str(guard.get("type", "")).strip() != "data_clumps":
            continue
        occurrences = analysis["occurrences"]
        guard["detected_group"] = analysis["group"]
        guard["detected_occurrence_count"] = analysis["occurrence_count"]
        guard["group_occurrences"] = json.dumps(occurrences, ensure_ascii=True)
        guard["listed_occurrence_count"] = str(len(occurrences))
    return {
        "groups": analyses,
    }


def cmd_build_context(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    data_clumps_context = _augment_data_clumps_context(resolved)
    idea_preflight = _run_idea_preflight(resolved, args)
    route_payload = _route_payload(resolved)
    context_payload = {
        "project_root": str(resolved.project_root),
        "roots": {
            "dataset": str(resolved.dataset_root),
            "idea": str(resolved.idea_project_root),
            "build": str(resolved.build_root),
        },
        "cwd": str(resolved.cwd),
        "language": resolved.language,
        "smell": resolved.smell,
        "locations": _location_payload(resolved),
        "profile": _profile_payload(resolved),
        "build": resolved.build.to_dict(),
        "test": resolved.test.to_dict(),
        "idea_refactor_cli": resolved.idea_refactor_cli,
        "idea_refactor_ready": bool(resolved.idea_refactor_ready),
        "idea_preflight": idea_preflight,
        "idea": {
            "ready": bool(resolved.idea_refactor_ready),
            "cli": (
                resolved.idea_refactor_cli
                if _is_idea_backed(resolved.language) and resolved.idea_refactor_ready
                else None
            ),
            "root": (
                str(resolved.idea_project_root)
                if _is_idea_backed(resolved.language) and resolved.idea_refactor_ready
                else ""
            ),
            "recommended_skill": (
                "idea-refactor-cli"
                if _is_idea_backed(resolved.language) and resolved.idea_refactor_ready
                else ""
            ),
        },
    }
    if data_clumps_context is not None:
        context_payload["data_clumps"] = data_clumps_context
    full_payload = {
        "core_root": str(PROJECT_ROOT / "smell_core"),
        "config": resolved.to_dict(),
        "context": context_payload,
    }
    mode = str(getattr(args, "mode", "repair") or "repair").strip().lower()
    if mode == "plan":
        plan_context = build_plan_context_payload(
            resolved=resolved,
            context_payload=context_payload,
            route_payload=route_payload,
        )
        plan_context["mode"] = "plan"
        return plan_context
    if mode == "repair":
        payload = build_repair_context_payload(
            context_payload=context_payload,
            route_payload=route_payload,
        )
        payload["core_root"] = full_payload["core_root"]
        payload["config"] = full_payload["config"]
        return payload
    raise ValueError(f"unsupported context mode {mode!r}; expected 'plan' or 'repair'")


def cmd_build_plan_context(args: argparse.Namespace) -> dict[str, Any]:
    context_args = argparse.Namespace(**vars(args))
    context_args.mode = "plan"
    return cmd_build_context(context_args)


def cmd_run_build_test_guard(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    result = run_build_test_guard(resolved)
    return {
        "success": bool(result.get("success")),
        "result": result,
    }


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
        "resolution_plan": _compact_resolution_plan(baseline.get("resolution_plan")),
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


def _checkpoint_context(
    resolved,
    evidence: str,
    baseline_seal: str = "",
) -> tuple[Optional[GuardRunContext], Optional[dict[str, Any]]]:
    if resolved.smell not in CHECKPOINT_SMELLS or not resolved.locations:
        return None, None
    checkpoint = prepare_checkpoint(
        resolved,
        evidence,
        expected_baseline_seal=baseline_seal,
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
    changed = [
        resolved.project_root / item
        for item in checkpoint.get("changed_production_source_files") or []
    ]
    context = GuardRunContext(
        changed_java_files=changed,
        checkpoint_required=True,
        checkpoint_smell=resolved.smell,
        checkpoint_id=str(checkpoint.get("checkpoint_id") or ""),
        baseline_metrics=dict(checkpoint.get("baseline_metrics") or {}),
        current_metrics=dict(checkpoint.get("current_metrics") or {}),
        metric_delta=delta,
        has_production_diff=bool(checkpoint.get("production_diff")),
        metric_progress=bool(delta.get("metric_progress")),
        checkpoint=checkpoint,
    )
    return context, checkpoint


def _god_class_min_reduction(resolved) -> float:
    """Minimum relative class_loc reduction required of a non-Java god-class repair."""
    for guard in resolved.profile.guards:
        if str(guard.get("type", "")).strip() == "god_class":
            try:
                return float(guard.get("min_relative_reduction", 0.05))
            except (TypeError, ValueError):
                return 0.05
    return 0.05


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    evidence = getattr(args, "smell_evidence", "") or os.environ.get("SMELL_EVIDENCE", "")
    build_test_required = (
        resolved.language == "java"
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
    # God-class (non-Java) additionally requires a meaningful reduction: its
    # ordinary guard only checks measurability, so a token extraction of a few
    # lines would otherwise pass both the guard and this gate.
    if improvement_pass and resolved.smell == "god_class" and resolved.language != "java":
        improvement_pass = god_class_relative_reduction(guard_context) >= _god_class_min_reduction(resolved)
    build_test_result = None
    test_changes = (
        dict(checkpoint.get("test_changes") or {})
        if isinstance(checkpoint, dict)
        else {}
    )
    if test_changes and test_changes.get("success") is False:
        build_test_result = _test_source_modified_result(resolved, test_changes)
    elif (not failed_smell or improvement_pass) and args.run_build_test and resolved.verification_mode != "local":
        build_test_result = run_build_test_guard(
            resolved,
            require_test_execution=bool(
                test_changes.get("allow_test_changes") is True
            ),
        )
    snapshot = _snapshot_project(resolved.project_root) if args.snapshot else None
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


def _test_source_modified_result(resolved: Any, audit: dict[str, Any]) -> dict[str, Any]:
    changed_paths = [
        str(item.get("path") or "")
        for group in ("added", "changed", "deleted")
        for item in (audit.get(group) or [])
        if isinstance(item, dict) and item.get("path")
    ]
    reason = str(audit.get("reason") or audit.get("status") or "TEST_SOURCE_MODIFIED")
    if reason == "VERIFICATION_CONFIG_MODIFIED":
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
        "test_location": resolved.sample_test_location,
        "test_changes": audit,
        "details": {
            "build": {"success": True, "status": "skipped_by_test_change_contract"},
            "test": {
                "success": False,
                "status": "test_source_modified",
                "failure_highlights": [message],
            },
        },
    }


def cmd_resolve_command(args: argparse.Namespace) -> dict[str, Any]:
    return parse_command_policy(args.arguments).to_dict()


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
            "TEST_SOURCE_MODIFIED",
            "TEST_SOURCE_MIGRATION_REJECTED",
            "TEST_SOURCE_DELETED",
            "VERIFICATION_CONFIG_MODIFIED",
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
        if build.get("success") is False:
            return "BUILD_FAILED"
        if test.get("success") is False:
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
    return {
        "type": result.get("type"),
        "success": bool(result.get("success")),
        "message": _bounded_text(result.get("message")),
        "reason": _bounded_text(result.get("reason", ""), limit=256),
        "verification_mode": result.get("verification_mode", ""),
        "build_source": result.get("build_source", ""),
        "test_source": result.get("test_source", ""),
        "test_location": _bounded_text(result.get("test_location", "")),
        "test_command_hash": result.get("test_command_hash", ""),
        "test_changes": _compact_test_changes(result.get("test_changes")),
        "details": {
            "build": _summarize_command_result(details.get("build")),
            "test": _summarize_command_result(details.get("test")),
        },
    }


def _summarize_snapshot(
    snapshot: Optional[dict[str, Any]],
    artifacts: dict[str, str],
) -> Optional[dict[str, Any]]:
    if snapshot is None:
        return None
    return {
        "project_root": snapshot.get("project_root"),
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
        if isinstance(build_result, dict):
            artifacts["build_result"] = _write_json_artifact(artifact_dir / "build.full.json", build_result)
            if isinstance(build_result.get("output"), str):
                artifacts["build_log"] = _write_text_artifact(artifact_dir / "build.log", build_result["output"])
        if isinstance(test_result, dict):
            artifacts["test_result"] = _write_json_artifact(artifact_dir / "test.full.json", test_result)
            if isinstance(test_result.get("output"), str):
                artifacts["test_log"] = _write_text_artifact(artifact_dir / "test.log", test_result["output"])

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
    args = ["ls-files", "--others", "--exclude-standard"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    result = _run_git(args, root)
    if result.get("returncode") != 0:
        return []
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return []
    return [line for line in stdout.splitlines() if line and not _is_ignored_untracked_path(line)]


def _is_ignored_untracked_path(path: str) -> bool:
    if path in {"opencode.json"}:
        return True
    parts = Path(path).parts
    if ".idea-refactoring" in parts:
        return True
    ignored_parts = {
        "build-refactoragent",
        "build",
        "CMakeFiles",
        "Testing",
        "__pycache__",
    }
    if any(part in ignored_parts or fnmatch.fnmatch(part, "cmake-build-*") for part in parts):
        return True
    ignored_names = (
        "*.o",
        "*.obj",
        "*.a",
        "*.dylib",
        "*.so",
        "*.dll",
        "*.exe",
        "*.ninja",
        "*.pc",
        "CMakeCache.txt",
        "CTestTestfile.cmake",
        "DartConfiguration.tcl",
        "cmake_install.cmake",
        "cmake_uninstall.cmake",
    )
    if any(fnmatch.fnmatch(Path(path).name, pattern) for pattern in ignored_names):
        return True
    ignored_prefixes = (
        ".smell-artifacts/",
        ".idea/",
        ".opencode/",
    )
    return any(path.startswith(prefix) for prefix in ignored_prefixes)


def _git_status_snapshot(root: Path) -> dict[str, Any]:
    result = _run_git(["status", "--short", "--untracked-files=all"], root)
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return result
    filtered_lines: list[str] = []
    ignored_lines: list[str] = []
    for line in stdout.splitlines():
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if status == "??" and _is_ignored_untracked_path(path):
            ignored_lines.append(line)
            continue
        filtered_lines.append(line)
    result["stdout"] = ("\n".join(filtered_lines) + "\n") if filtered_lines else ""
    result["ignored_untracked_count"] = len(ignored_lines)
    return result


def _diff_untracked_files(root: Path, paths: list[str], *, stat: bool = False) -> str:
    chunks: list[str] = []
    for path in paths:
        args = ["diff", "--no-index"]
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


def _git_diff_with_untracked(root: Path, args: list[str], pathspecs: list[str]) -> dict[str, Any]:
    result = _run_git(args, root)
    tracked_diff = result.get("stdout")
    if not isinstance(tracked_diff, str):
        tracked_diff = ""
    untracked_files = _git_untracked_files(root, pathspecs)
    untracked_diff = _diff_untracked_files(root, untracked_files, stat="--stat" in args)
    result["stdout"] = tracked_diff + untracked_diff
    result["untracked_files"] = untracked_files
    return result


def _snapshot_project(root: Path) -> dict[str, Any]:
    return {
        "project_root": str(root),
        "status": _git_status_snapshot(root),
        "diff_stat": _git_diff_with_untracked(root, ["diff", "--stat"], []),
        "diff": _git_diff_with_untracked(root, ["diff", "--binary"], []),
    }


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.project_root).expanduser().resolve()
    return _snapshot_project(root)


def _artifact_paths_from_verify_payload(payload: Optional[dict[str, Any]], discovered: dict[str, str]) -> dict[str, str]:
    paths: dict[str, str] = dict(discovered)
    evidence_path = paths.get("guard_evidence") or paths.get("verify_full")
    if evidence_path:
        artifact_dir = Path(evidence_path).parent
        sibling_names = {
            "build_log": "build.log",
            "test_log": "test.log",
            "diff": "diff.patch",
            "diff_stat": "diff.stat",
            "build_result": "build.full.json",
            "test_result": "test.full.json",
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
    for key in ("build_log", "test_log", "diff"):
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
    if status == "SAMPLE_TEST_FAILED":
        test_changes = payload.get("test_changes") or {}
        if isinstance(test_changes, dict) and test_changes.get("status") in {
            "TEST_SOURCE_MODIFIED",
            "TEST_SOURCE_MIGRATION_REJECTED",
            "TEST_SOURCE_DELETED",
            "VERIFICATION_CONFIG_MODIFIED",
        }:
            return "SAMPLE_TEST_FAILED", [
                "TEST_SOURCE_MODIFIED: restore the test-tree changes frozen as immutable at c000."
            ]
        smell_guard = payload.get("smell_guard") or {}
        if not (isinstance(smell_guard, dict) and smell_guard.get("success") is False):
            build_test = payload.get("build_test_guard") or {}
            details = build_test.get("details") if isinstance(build_test, dict) else {}
            test = details.get("test") if isinstance(details, dict) else {}
            test_status = str(test.get("status") or "") if isinstance(test, dict) else ""
            failure_text = " ".join(
                str(item)
                for item in (test.get("failure_highlights") or [])
            ) if isinstance(test, dict) else ""
            if (
                test_status == "test_not_executed"
                and "Pinned sample test location does not identify a test class" in failure_text
            ):
                return "SAMPLE_TEST_EVIDENCE_INVALID", [
                    "The configured test command passed, but the pinned test-file evidence is invalid.",
                    "This dataset/configuration defect cannot be repaired by editing production code.",
                ]
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
    }:
        policy_status = str(test_changes.get("status") or "")
        if policy_status in {"TEST_SOURCE_MIGRATION_REJECTED", "TEST_SOURCE_DELETED"}:
            return "TEST_BEHAVIOR_REGRESSION", [
                "Repair the controller-authorized API migration without deleting or weakening the frozen test behavior."
            ]
        return "TEST_BEHAVIOR_REGRESSION", [
            "TEST_SOURCE_MODIFIED: restore the test-tree changes frozen as immutable at c000."
        ]
    if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
        return "SMELL_GUARD_FAILED", ["Smell guard did not pass; continue the refactoring rather than repairing tests."]
    build_test = payload.get("build_test_guard") or {}
    if isinstance(build_test, dict):
        details = build_test.get("details") or {}
        build = details.get("build") or {}
        test = details.get("test") or {}
        if isinstance(build, dict) and build.get("success") is False:
            if _looks_like_dependency_resolution_failure(text):
                return "BUILD_DEPENDENCY_RESOLUTION", [
                    "Build stopped while resolving Maven/Gradle dependencies or generated classifier artifacts.",
                    "Treat this as a project verification configuration issue unless logs also show source compilation errors.",
                ]
            return "BUILD_COMPILE_ERROR", ["Inspect the build log and fix the build failure before retrying verification."]
        if isinstance(test, dict) and test.get("success") is False:
            status = "TEST_FAILED"
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
        "retryable": bool(failure_group),
        "verify_status": payload.get("status") if isinstance(payload, dict) else "",
        "artifact_paths": paths,
        "highlights": highlights,
        "next_action": _bounded_text(next_action),
        "recommendations": _bounded_strings(recommendations),
        "repair_contract": {
            "repair_agent_may_edit": True,
            "prefer_narrow_fix": True,
            "must_rerun_smell_verify": True,
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
    parser.add_argument("--idea-refactor-cli")
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

    context_parser = subparsers.add_parser("build-context")
    _add_common(context_parser)
    context_parser.add_argument("--attempt", type=int, default=1)
    context_parser.add_argument("--total-attempts", type=int, default=3)
    context_parser.add_argument("--mode", choices=("plan", "repair"), default="repair")
    context_parser.set_defaults(
        func=cmd_build_context,
        ensure_idea_service=False,
        idea_open=False,
    )

    plan_context_parser = subparsers.add_parser("build-plan-context")
    _add_common(plan_context_parser)
    plan_context_parser.set_defaults(
        func=cmd_build_plan_context,
        ensure_idea_service=False,
        idea_open=False,
    )

    build_test_parser = subparsers.add_parser("run-build-test-guard")
    _add_common(build_test_parser)
    build_test_parser.set_defaults(func=cmd_run_build_test_guard)

    verify_parser = subparsers.add_parser("verify")
    _add_common(verify_parser)
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

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--project-root", required=True)
    snapshot_parser.set_defaults(func=cmd_snapshot)

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
