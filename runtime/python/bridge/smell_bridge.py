#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
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
    finalize_checkpoint,
    prepare_checkpoint,
)
from smell_core.checkpoint_adapters import CHECKPOINT_SMELLS  # noqa: E402
from smell_core.checkpoint_contract import checkpoint_feedback_highlights  # noqa: E402
from smell_core.guards import GuardRunContext, god_class_relative_reduction, run_build_test_guard, run_smell_guards  # noqa: E402
from smell_core.data_clumps import detect_data_clump_occurrences as detect_generic_data_clump_occurrences  # noqa: E402
from smell_core.detector_utils import parse_structural_expectation  # noqa: E402
from smell_core.java.idea_refactor import (  # noqa: E402
    IdeaRefactorPreflightError,
    IdeaRefactorPreflightOptions,
    resolve_idea_refactor_cli,
    run_idea_refactor_preflight,
)
from smell_core.java.data_clumps import detect_data_clump_occurrences as detect_java_data_clump_occurrences  # noqa: E402
from smell_core.languages import get_language  # noqa: E402
from smell_core.loop_policy import REPAIRABLE_CATEGORY_GROUPS, parse_command_policy  # noqa: E402
from smell_core.planning import build_plan_context_payload, build_repair_context_payload  # noqa: E402
from smell_core.prompts.idea_router import build_idea_prompt_route  # noqa: E402
from smell_core.task_builder import build_task  # noqa: E402


def _config_path(value: Optional[str], env_name: str, bundled) -> str:
    raw = value or os.environ.get(env_name)
    return str(Path(raw).expanduser().resolve()) if raw else str(bundled())


def _projects_path(value: Optional[str]) -> str:
    return _config_path(value, "SMELL_PROJECTS", bundled_projects_config_path)


def _refactor_config_path(value: Optional[str]) -> str:
    return _config_path(value, "SMELL_CONFIG", bundled_refactor_config_path)


def _json_arg(value: Optional[str]) -> Dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return {str(key): str(val) for key, val in parsed.items()}


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


def _write_text_artifact(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    # surrogateescape keeps non-UTF-8 bytes (diff/build output) byte-exact on disk.
    path.write_text(content, encoding="utf-8", errors="surrogateescape")
    return str(path)


def _resolve(args: argparse.Namespace):
    refactor_config = load_refactor_config(_refactor_config_path(args.config))
    project_overrides = load_project_overrides(_projects_path(args.projects))
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
        or "local",
        sample_test_location=getattr(args, "sample_test_location", None)
        or os.environ.get("SMELL_SAMPLE_TEST_LOCATION", ""),
        sample_test_command=getattr(args, "sample_test_command", None)
        or os.environ.get("SMELL_SAMPLE_TEST_COMMAND", ""),
    )
    smell_evidence = getattr(args, "smell_evidence", None) or os.environ.get("SMELL_EVIDENCE", "")
    if smell_evidence:
        for guard in resolved.profile.guards:
            guard["evidence"] = smell_evidence
    for guard in resolved.profile.guards:
        guard.update(_json_arg(getattr(args, "guard_context_json", None)))
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


def _requires_strict_smell_resolution(smell: str, evidence: str) -> bool:
    """Return whether metric improvement is progress-only, never final acceptance."""
    return bool(
        smell == "code_clone_type1"
        or (
            smell == "refused_bequest"
            and parse_structural_expectation(evidence)
        )
    )


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
    if not _is_idea_backed(resolved.language):
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
    analyses: list[dict[str, Any]] = []
    for guard in resolved.profile.guards:
        if str(guard.get("type", "")).strip() != "data_clumps":
            continue
        evidence = str(guard.get("evidence") or "").strip()
        if not evidence:
            continue
        if resolved.language == "java":
            analysis = detect_java_data_clump_occurrences(resolved.project_root, evidence=evidence)
        else:
            analysis = detect_generic_data_clump_occurrences(
                resolved.project_root,
                language=resolved.language,
                evidence=evidence,
            )
        analyses.append(analysis)
        if not analysis.get("success"):
            guard["detected_group"] = analysis.get("group", "")
            guard["occurrence_detection_error"] = analysis.get("error", "")
            continue
        occurrences = analysis.get("occurrences") or []
        guard["detected_group"] = analysis.get("group", "")
        guard["detected_occurrence_count"] = analysis.get("occurrence_count", 0)
        guard["group_occurrences"] = json.dumps(occurrences, ensure_ascii=True)
        guard["listed_occurrence_count"] = str(len(occurrences))
        if not str(guard.get("reported_occurrence_count") or "").strip():
            guard["reported_occurrence_count"] = str(analysis.get("occurrence_count", 0))
    if not analyses:
        return None
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
            "cli": resolved.idea_refactor_cli if _is_idea_backed(resolved.language) else None,
            "root": str(resolved.idea_project_root) if _is_idea_backed(resolved.language) else "",
            "recommended_skill": "idea-refactor-cli" if _is_idea_backed(resolved.language) else "",
        },
    }
    if data_clumps_context is not None:
        context_payload["data_clumps"] = data_clumps_context
    full_payload = {
        "core_root": str(PROJECT_ROOT / "smell_core"),
        "config": resolved.to_dict(),
        "context": context_payload,
    }
    if getattr(args, "include_legacy_task_prompt", False):
        full_payload["legacy_task_prompt"] = build_task(
            config=resolved,
            attempt_number=max(1, args.attempt),
            total_attempts=max(1, args.total_attempts),
            failures=[],
        )
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
        if "legacy_task_prompt" in full_payload:
            payload["legacy_task_prompt"] = full_payload["legacy_task_prompt"]
        return payload
    raise ValueError(f"unsupported context mode {mode!r}; expected 'plan' or 'repair'")


def cmd_build_plan_context(args: argparse.Namespace) -> dict[str, Any]:
    context_args = argparse.Namespace(**vars(args))
    context_args.include_legacy_task_prompt = False
    context_args.mode = "plan"
    return cmd_build_context(context_args)


def cmd_run_smell_guard(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    results = run_smell_guards(resolved)
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "guard_results": results,
        "failure_count": len(failed),
        "retry_hint": resolved.profile.retry_hint_template if failed else "",
    }


def cmd_run_build_test_guard(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    result = run_build_test_guard(resolved)
    return {
        "success": bool(result.get("success")),
        "result": result,
    }


def cmd_capture_baseline(args: argparse.Namespace) -> dict[str, Any]:
    resolved = _resolve(args)
    if resolved.smell not in CHECKPOINT_SMELLS:
        return {"success": True, "status": "BASELINE_NOT_REQUIRED", "smell": resolved.smell}
    baseline = capture_checkpoint_baseline(
        resolved,
        getattr(args, "smell_evidence", "") or os.environ.get("SMELL_EVIDENCE", ""),
    )
    return {
        "success": True,
        "status": "BASELINE_CAPTURED",
        "smell": resolved.smell,
        "checkpoint_id": baseline.get("checkpoint_id"),
        "adapter": baseline.get("adapter"),
        "metrics": baseline.get("metrics"),
    }


def _checkpoint_context(resolved, evidence: str) -> tuple[Optional[GuardRunContext], Optional[dict[str, Any]]]:
    if resolved.smell not in CHECKPOINT_SMELLS or not resolved.locations:
        return None, None
    checkpoint = prepare_checkpoint(resolved, evidence)
    if not checkpoint.get("required"):
        return None, checkpoint
    delta = dict(checkpoint.get("delta") or {})
    changed = [resolved.project_root / item for item in checkpoint.get("changed_production_java_files") or []]
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
    strict_resolution_required = _requires_strict_smell_resolution(
        resolved.smell,
        evidence,
    )
    build_test_required = bool(
        os.environ.get("SMELL_REQUIRE_BUILD_TEST") == "1"
        or (
            resolved.smell == "refused_bequest"
            and strict_resolution_required
        )
    )
    if build_test_required and (
        args.skip_build_test
        or not args.run_build_test
        or resolved.verification_mode == "local"
    ):
        full_payload = {
            "success": False,
            "status": "BUILD_TEST_REQUIRED",
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
        artifacts = _write_verify_artifacts(artifact_dir, full_payload)
        failure_pack = _build_failure_pack(
            full_payload,
            artifacts,
            smell=resolved.smell,
            evidence=evidence,
        )
        full_payload["failure_pack"] = failure_pack
        artifacts["verify_full"] = _write_json_artifact(artifact_dir / "verify.full.json", full_payload)
        return {
            "success": False,
            "status": "BUILD_TEST_REQUIRED",
            "smell_guard": full_payload["smell_guard"],
            "build_test_guard": None,
            "snapshot": None,
            "artifacts": artifacts,
            "failure_pack": failure_pack,
        }
    guard_context, checkpoint = _checkpoint_context(resolved, evidence)
    smell_results = run_smell_guards(resolved, guard_context)
    failed_smell = [item for item in smell_results if not item.get("success")]
    # Contract improvement gate: a real production diff that reduces any valid
    # target metric vs baseline is an accepted improvement, even when the
    # strict detector still reports the smell. The detector verdict stays in
    # smell_guard for reporting; without this gate the loop burns the whole
    # sample deadline on samples where "detector fully silent" is unreachable.
    improvement_pass = bool(
        guard_context is not None
        and getattr(guard_context, "has_production_diff", False)
        and getattr(guard_context, "metric_progress", False)
    )
    # God-class (non-Java) additionally requires a meaningful reduction: its
    # ordinary guard only checks measurability, so a token extraction of a few
    # lines would otherwise pass both the guard and this gate.
    if improvement_pass and resolved.smell == "god_class" and resolved.language != "java":
        improvement_pass = god_class_relative_reduction(guard_context) >= _god_class_min_reduction(resolved)
    accepted_improvement_pass = improvement_pass and not strict_resolution_required
    build_test_result = None
    if (not failed_smell or improvement_pass) and args.run_build_test and resolved.verification_mode != "local":
        build_test_result = run_build_test_guard(resolved)
    snapshot = _snapshot_project(resolved.project_root) if args.snapshot else None
    success = (not failed_smell or accepted_improvement_pass) and (
        build_test_result is None or bool(build_test_result.get("success"))
    )
    resolution = ""
    if success:
        resolution = "resolved" if not failed_smell else "improved"
    elif improvement_pass and (
        build_test_result is None or bool(build_test_result.get("success"))
    ):
        resolution = "improved"
    continue_hint = ""
    if resolution == "improved":
        remaining = [str(item.get("message") or "") for item in failed_smell if item.get("message")]
        progress_disposition = (
            "Progress recorded but not accepted as final"
            if strict_resolution_required
            else "Progress accepted"
        )
        continue_hint = (
            f"{progress_disposition} (resolution=improved): the checkpoint confirms a real "
            "production diff with metric reduction vs baseline. The detector or structural "
            "guard still reports the smell, so keep refactoring toward resolution=resolved. "
            "Remaining detector signals: "
            + " | ".join(remaining[:3])
            + " Best partial progress is already saved; do not undo these metric "
            "gains. Make the next cohesive extraction or simplification, then call "
            "smell_verify again."
        )
    smell_guard = {
        "success": not failed_smell,
        "results": smell_results,
        "failure_count": len(failed_smell),
        "retry_hint": resolved.profile.retry_hint_template if failed_smell else "",
    }
    full_payload = {
        "success": success,
        "status": _verify_status(success, smell_guard, build_test_result, improvement_pass=improvement_pass),
        "resolution": resolution,
        "continue_hint": continue_hint,
        "smell_guard": smell_guard,
        "build_test_guard": build_test_result,
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
    artifacts = _write_verify_artifacts(artifact_dir, full_payload)
    failure_pack = None
    if not success:
        failure_pack = _build_failure_pack(
            full_payload,
            artifacts,
            smell=resolved.smell,
            evidence=evidence,
        )
        full_payload["failure_pack"] = failure_pack
        artifacts["verify_full"] = _write_json_artifact(artifact_dir / "verify.full.json", full_payload)
    payload = {
        "success": success,
        "status": full_payload["status"],
        "resolution": resolution,
        "continue_hint": continue_hint,
        "smell_guard": smell_guard,
        "build_test_guard": _summarize_build_test_guard(build_test_result),
        "snapshot": _summarize_snapshot(snapshot, artifacts),
        "artifacts": artifacts,
    }
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    if failure_pack is not None:
        payload["failure_pack"] = failure_pack
    return payload


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
        if build_test_result.get("verification_mode") == "sample_optimized":
            details = build_test_result.get("details") or {}
            test = details.get("test") or {}
            if test.get("status") == "missing":
                return "SAMPLE_TEST_SPEC_MISSING"
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
        "command",
        "script",
        "cwd",
        "source",
        "timeout_seconds",
        "summary",
        "failure_highlights",
        "summary_text",
        "tail",
    ):
        if key in result:
            summary[key] = result[key]
    if "timed_out" in result:
        summary["timed_out"] = result["timed_out"]
    if "error" in result:
        summary["error"] = result["error"]
    return summary


def _summarize_build_test_guard(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    details = result.get("details") or {}
    return {
        "type": result.get("type"),
        "success": bool(result.get("success")),
        "message": result.get("message"),
        "verification_mode": result.get("verification_mode", ""),
        "build_source": result.get("build_source", ""),
        "test_source": result.get("test_source", ""),
        "test_location": result.get("test_location", ""),
        "test_command_hash": result.get("test_command_hash", ""),
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
        "status": snapshot.get("status"),
        "diff_stat": snapshot.get("diff_stat"),
        "artifacts": {
            "snapshot": artifacts.get("snapshot", ""),
            "diff": artifacts.get("diff", ""),
            "diff_stat": artifacts.get("diff_stat", ""),
        },
    }


def _write_verify_artifacts(artifact_dir: Path, full_payload: dict[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    artifacts["verify_full"] = _write_json_artifact(artifact_dir / "verify.full.json", full_payload)

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
    verify_full = paths.get("verify_full")
    if verify_full:
        artifact_dir = Path(verify_full).parent
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
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def _failure_text_bundle(payload: Optional[dict[str, Any]], paths: dict[str, str]) -> str:
    chunks: list[str] = []
    if payload is not None:
        chunks.append(json.dumps(payload, ensure_ascii=True))
    for key in ("build_log", "test_log", "diff", "verify_full", "build_result", "test_result"):
        text = _read_artifact_text(paths, key)
        if text:
            chunks.append(f"\n--- {key} ---\n{text}")
    return "\n".join(chunks)


def _highlight_patterns(text: str, patterns: list[str], *, context: int = 2, limit: int = 12) -> list[str]:
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


def _capability_split_failure(payload: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the failed positive capability contract, when present."""
    if not isinstance(payload, dict):
        return None
    smell_guard = payload.get("smell_guard")
    if not isinstance(smell_guard, dict) or smell_guard.get("success") is not False:
        return None
    for result in smell_guard.get("results") or []:
        if not isinstance(result, dict) or result.get("success") is not False:
            continue
        details = result.get("details")
        if not isinstance(details, dict) or details.get("structural_expectation") != "capability_split":
            continue
        profile = details.get("capability_profile")
        if not isinstance(profile, dict) or profile.get("ok") is not True:
            continue
        if profile.get("capability_split_satisfied") is not True:
            return profile
    return None


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
    if status == "SAMPLE_TEST_SPEC_MISSING":
        return "SAMPLE_TEST_SPEC_MISSING", [
            "Sample-optimized verification requires SMELL_SAMPLE_TEST_COMMAND or --sample-test-command.",
        ]
    if status == "SAMPLE_TEST_FAILED":
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
    if isinstance(smell_guard, dict) and smell_guard.get("success") is False:
        capability_split_required = bool(
            smell == "refused_bequest"
            and parse_structural_expectation(evidence) == "capability_split"
        )
        if _capability_split_failure(payload) or capability_split_required:
            return "STRUCTURAL_ROUTE_MISMATCH", [
                "The required capability split is still incomplete. Do not implement or delegate the reported method as the final repair.",
                "Split the parent capability, migrate real implementers and production callers to narrow types, and remove the unsupported operation from the refusing type's inherited contract.",
            ]
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
    profile = _capability_split_failure(payload)
    max_highlights = 2 if profile else 1
    if profile:
        target = str(profile.get("target_class") or "?")
        method = str(profile.get("method") or "?")
        parent = str(profile.get("reported_parent") or "?")
        highlights.append(
            "CAPABILITY_SPLIT_REQUIRED "
            f"target={target} method={method} parent={parent}; "
            "implementing the method body is not accepted; split the parent capability "
            "and migrate implementers and callers."
        )
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
    # Continuation prompts only retain the first three highlights.  Put the
    # ordinary guard target first so the model sees the current score,
    # threshold, or remaining family before the compact checkpoint deltas.
    highlights = _smell_guard_failure_highlights(payload)
    highlights.extend(checkpoint_feedback_highlights(checkpoint))
    highlights.extend(_highlight_patterns(bundle, patterns))
    return {
        "failure_category": category,
        "failure_group": failure_group,
        "retryable": bool(failure_group),
        "verify_status": payload.get("status") if isinstance(payload, dict) else "",
        "artifact_paths": paths,
        "highlights": highlights,
        "recommendations": recommendations,
        "repair_contract": {
            "repair_agent_may_edit": True,
            "prefer_narrow_fix": True,
            "must_rerun_smell_verify": True,
            "do_not_weaken_tests": True,
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
    parser.add_argument("--guard-context-json", default="")
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
    baseline_parser.set_defaults(func=cmd_capture_baseline)

    context_parser = subparsers.add_parser("build-context")
    _add_common(context_parser)
    context_parser.add_argument("--attempt", type=int, default=1)
    context_parser.add_argument("--total-attempts", type=int, default=3)
    context_parser.add_argument("--no-idea-preflight", action="store_true")
    context_parser.add_argument("--no-idea-open", action="store_true")
    context_parser.add_argument("--idea-timeout", type=int, default=60)
    context_parser.add_argument("--idea-poll-interval", type=float, default=1.0)
    context_parser.add_argument("--include-legacy-task-prompt", action="store_true")
    context_parser.add_argument("--mode", choices=("plan", "repair"), default="repair")
    context_parser.set_defaults(
        func=cmd_build_context,
        ensure_idea_service=True,
        idea_open=True,
    )

    plan_context_parser = subparsers.add_parser("build-plan-context")
    _add_common(plan_context_parser)
    plan_context_parser.add_argument("--no-idea-preflight", action="store_true")
    plan_context_parser.add_argument("--no-idea-open", action="store_true")
    plan_context_parser.add_argument("--idea-timeout", type=int, default=60)
    plan_context_parser.add_argument("--idea-poll-interval", type=float, default=1.0)
    plan_context_parser.set_defaults(
        func=cmd_build_plan_context,
        ensure_idea_service=True,
        idea_open=True,
    )

    smell_guard_parser = subparsers.add_parser("run-smell-guard")
    _add_common(smell_guard_parser)
    smell_guard_parser.set_defaults(func=cmd_run_smell_guard)

    build_test_parser = subparsers.add_parser("run-build-test-guard")
    _add_common(build_test_parser)
    build_test_parser.set_defaults(func=cmd_run_build_test_guard)

    verify_parser = subparsers.add_parser("verify")
    _add_common(verify_parser)
    verify_parser.add_argument("--skip-build-test", action="store_true")
    verify_parser.add_argument("--no-snapshot", action="store_true")
    verify_parser.add_argument("--artifact-root")
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
    if getattr(args, "no_idea_preflight", False):
        args.ensure_idea_service = False
    if getattr(args, "no_idea_open", False):
        args.idea_open = False
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
