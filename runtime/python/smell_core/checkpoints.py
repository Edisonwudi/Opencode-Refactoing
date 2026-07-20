"""Persistent, threshold-independent checkpoints for smell repair tasks."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .checkpoint_adapters import CHECKPOINT_SMELLS, capture_metric_snapshot
from .checkpoint_contract import CHECKPOINT_CONTRACT_VERSION, evaluate_checkpoint_contract


CHECKPOINT_SCHEMA_VERSION = 2


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    # Project sources are not guaranteed to be UTF-8 (e.g. POCO carries Latin-1
    # bytes).  surrogateescape keeps undecodable bytes as lone surrogates so the
    # diff text round-trips byte-exactly when written back with the same policy.
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _git_text(root: Path, args: list[str]) -> str:
    result = _run_git(root, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _task_key(smell: str, location: str) -> str:
    digest = hashlib.sha256(f"{smell}\0{location}".encode("utf-8")).hexdigest()[:16]
    return f"{smell}-{digest}"


def checkpoint_task_root(project_root: Path, smell: str, location: str) -> Path:
    raw = os.environ.get("SMELL_CHECKPOINT_ROOT", "").strip()
    base = Path(raw).expanduser().resolve() if raw else project_root / ".smell-artifacts" / "checkpoints"
    return base / _task_key(smell, location)


def checkpoint_location(config: Any) -> str:
    """Stable task identity including every target (not only clone target one)."""
    return " || ".join(str(item.raw) for item in config.locations)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"checkpoint payload is not an object: {path}")
    return payload


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def capture_checkpoint_baseline(config: Any, evidence: str) -> dict[str, Any]:
    """Capture the immutable c000 metric snapshot for a migrated smell."""
    smell = str(config.smell)
    if smell not in CHECKPOINT_SMELLS:
        raise ValueError(f"CHECKPOINT_NOT_SUPPORTED: {smell}")
    if not config.locations:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: no target location")
    root = config.project_root.expanduser().resolve()
    location = checkpoint_location(config)
    task_root = checkpoint_task_root(root, smell, location)
    baseline_path = task_root / "c000-baseline" / "manifest.json"
    if baseline_path.is_file():
        existing = _read_json(baseline_path)
        if (
            str(existing.get("project_root")) != str(root)
            or str(existing.get("location")) != location
            or str(existing.get("smell")) != smell
        ):
            raise ValueError("CHECKPOINT_BASELINE_IDENTITY_MISMATCH")
        return existing

    metrics = capture_metric_snapshot(config, evidence)
    if not metrics.get("ok") or not metrics.get("objectives"):
        raise ValueError(f"CHECKPOINT_BASELINE_CAPTURE_FAILED: {metrics.get('error', 'no measurable objectives')}")
    targets = []
    for target in config.locations:
        try:
            target_rel = target.file_path.resolve().relative_to(root).as_posix()
        except ValueError:
            target_rel = str(target.file_path)
        targets.append({
            "file": target_rel,
            "method": target.method or "",
            "line": target.line,
            "source_hash": _source_hash(target.file_path),
        })
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "contract_version": CHECKPOINT_CONTRACT_VERSION,
        "checkpoint_id": "c000",
        "kind": "baseline",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_root": str(root),
        "project_commit": _git_text(root, ["rev-parse", "HEAD"]),
        "tree_hash": _git_text(root, ["rev-parse", "HEAD^{tree}"]),
        "smell": smell,
        "location": location,
        "targets": targets,
        "adapter": metrics.get("adapter", smell),
        "metrics": metrics,
    }
    _write_json(baseline_path, manifest)
    _write_json(task_root / "task-state.json", {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "baseline": "c000",
        "latest": "c000",
        "best": "c000",
        "next_sequence": 1,
    })
    return manifest


def load_checkpoint_baseline(project_root: Path, smell: str, location: str) -> dict[str, Any] | None:
    path = checkpoint_task_root(project_root, smell, location) / "c000-baseline" / "manifest.json"
    return _read_json(path) if path.is_file() else None


def _changed_paths(root: Path, base_commit: str) -> list[str]:
    paths: set[str] = set()
    if base_commit:
        result = _run_git(root, ["diff", "--name-only", base_commit, "--"])
        if result.returncode == 0:
            paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    result = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if result.returncode == 0:
        paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(paths)


def _is_production_source(path: str, language: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    extensions = {
        "java": (".java",),
        "python": (".py",),
        "c": (".c", ".h"),
        "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    }.get(language, ())
    if not normalized.endswith(extensions):
        return False
    lowered = normalized.lower()
    return not any(
        marker in lowered
        for marker in (
            "/src/test/", "/src/it/", "/tests/", "/test/",
            "/build-refactoragent/", "/cmake-build-", "/.venv/", "/dist/",
        )
    )


def _diff_patch(root: Path, base_commit: str, paths: list[str] | None = None) -> str:
    if paths is not None and not paths:
        return ""
    pathspec = list(paths or [])
    args = ["diff", "--binary", base_commit, "--", *pathspec]
    tracked = _run_git(root, args) if base_commit else None
    chunks = [tracked.stdout.rstrip("\n")] if tracked and tracked.returncode == 0 and tracked.stdout else []
    candidates = pathspec if paths is not None else _changed_paths(root, base_commit)
    for path in candidates:
        if _git_text(root, ["ls-files", "--", path]):
            continue
        result = _run_git(root, ["diff", "--no-index", "--binary", "--", "/dev/null", path])
        if result.stdout:
            chunks.append(result.stdout.rstrip("\n"))
    return "\n".join(chunks) + ("\n" if chunks else "")


def prepare_checkpoint(config: Any, evidence: str) -> dict[str, Any]:
    """Create one verify checkpoint and evaluate the generic contract."""
    smell = str(config.smell)
    if smell not in CHECKPOINT_SMELLS or not config.locations:
        return {"required": False, "reason": "checkpoint_not_supported"}
    root = config.project_root.expanduser().resolve()
    location = checkpoint_location(config)
    task_root = checkpoint_task_root(root, smell, location)
    baseline = load_checkpoint_baseline(root, smell, location)
    if baseline is None:
        return {"required": False, "reason": "baseline_checkpoint_missing"}
    state_path = task_root / "task-state.json"
    state = _read_json(state_path)
    sequence = int(state.get("next_sequence") or 1)
    checkpoint_id = f"c{sequence:03d}"
    checkpoint_dir = task_root / f"{checkpoint_id}-verify"
    baseline_metrics = dict(baseline.get("metrics") or {})
    current = capture_metric_snapshot(config, evidence)
    changed = _changed_paths(root, str(baseline.get("project_commit") or ""))
    production_sources = [path for path in changed if _is_production_source(path, str(config.language))]
    has_production_diff = bool(production_sources)
    evaluation = evaluate_checkpoint_contract(
        baseline_metrics,
        current,
        has_production_diff=has_production_diff,
    )
    delta = {
        **evaluation.to_dict(),
        "has_production_diff": has_production_diff,
        "changed_production_source_files": production_sources,
        # Compatibility field retained for existing Java artifact consumers.
        "changed_production_java_files": production_sources if config.language == "java" else [],
        "target_missing": bool(current.get("target_missing")),
    }
    patch = _diff_patch(root, str(baseline.get("project_commit") or ""))
    production_patch = _diff_patch(
        root,
        str(baseline.get("project_commit") or ""),
        production_sources,
    )
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "contract_version": CHECKPOINT_CONTRACT_VERSION,
        "checkpoint_id": checkpoint_id,
        "parent": str(state.get("latest") or "c000"),
        "kind": "verify",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "required": True,
        "smell": smell,
        "location": location,
        "adapter": baseline.get("adapter", smell),
        "baseline_checkpoint": "c000",
        "changed_files": changed,
        "changed_production_source_files": production_sources,
        "changed_production_java_files": production_sources if config.language == "java" else [],
        "production_diff": has_production_diff,
        "production_diff_hash": hashlib.sha256(production_patch.encode("utf-8", errors="surrogateescape")).hexdigest(),
        "baseline_metrics": baseline_metrics,
        "current_metrics": current,
        "delta": delta,
        "accepted": False,
        "best_checkpoint": False,
    }
    best_partial = state.get("best_partial")
    if isinstance(best_partial, dict):
        manifest["best_partial"] = best_partial
    _write_json(checkpoint_dir / "manifest.json", manifest)
    _write_json(checkpoint_dir / "metrics.json", current)
    _write_json(checkpoint_dir / "delta.json", delta)
    # Patches must round-trip the exact bytes git emitted; surrogateescape is the
    # same policy _run_git used to decode them, so non-UTF-8 source bytes survive.
    (checkpoint_dir / "source.patch").write_text(patch, encoding="utf-8", errors="surrogateescape")
    (checkpoint_dir / "production.patch").write_text(production_patch, encoding="utf-8", errors="surrogateescape")
    state["latest"] = checkpoint_id
    state["next_sequence"] = sequence + 1
    _write_json(state_path, state)
    return manifest


def finalize_checkpoint(
    project_root: Path,
    smell: str,
    location: str,
    checkpoint_id: str,
    verify_payload: dict[str, Any],
) -> dict[str, Any] | None:
    task_root = checkpoint_task_root(project_root, smell, location)
    manifest_path = task_root / f"{checkpoint_id}-verify" / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = _read_json(manifest_path)
    accepted = bool(verify_payload.get("success"))
    manifest["accepted"] = accepted
    manifest["verify_status"] = verify_payload.get("status")
    manifest["build_test_success"] = bool((verify_payload.get("build_test_guard") or {}).get("success")) \
        if verify_payload.get("build_test_guard") is not None else None
    manifest["best_checkpoint"] = accepted
    state_path = task_root / "task-state.json"
    state = _read_json(state_path)
    candidate_rank = _partial_checkpoint_rank(manifest, verify_payload)
    current_is_best_partial = False
    if candidate_rank is not None:
        existing = state.get("best_partial")
        existing_rank = tuple(existing.get("rank") or ()) if isinstance(existing, dict) else ()
        if not existing_rank or candidate_rank > existing_rank:
            patch_path = manifest_path.parent / "production.patch"
            try:
                rendered_patch = patch_path.relative_to(project_root).as_posix()
            except ValueError:
                rendered_patch = str(patch_path)
            state["best_partial"] = {
                "checkpoint_id": checkpoint_id,
                "rank": list(candidate_rank),
                "objectives": dict((manifest.get("current_metrics") or {}).get("objectives") or {}),
                "smell_guard_success": bool((verify_payload.get("smell_guard") or {}).get("success")),
                "build_test_success": manifest.get("build_test_success"),
                "production_patch": rendered_patch,
            }
            current_is_best_partial = True
    best_partial = state.get("best_partial")
    if isinstance(best_partial, dict):
        manifest["best_partial"] = best_partial
        manifest["current_is_best_partial"] = current_is_best_partial or (
            best_partial.get("checkpoint_id") == checkpoint_id
        )
        best_rank = tuple(best_partial.get("rank") or ())
        manifest["regressed_from_best_partial"] = bool(
            best_partial.get("checkpoint_id") != checkpoint_id
            and (candidate_rank is None or (best_rank and candidate_rank < best_rank))
        )
    _write_json(manifest_path, manifest)
    stored_verify = dict(verify_payload)
    stored_verify["checkpoint"] = manifest
    _write_json(manifest_path.parent / "verify.json", stored_verify)
    if accepted:
        state["best"] = checkpoint_id
    _write_json(state_path, state)
    return manifest


def _partial_checkpoint_rank(
    manifest: dict[str, Any],
    verify_payload: dict[str, Any],
) -> tuple[int, float, int] | None:
    """Rank structurally useful checkpoints without weakening final acceptance."""
    delta = manifest.get("delta")
    if not isinstance(delta, dict) or delta.get("metric_progress") is not True:
        return None
    if delta.get("target_missing") is True or manifest.get("production_diff") is not True:
        return None
    objectives = delta.get("objectives")
    if not isinstance(objectives, dict):
        return None
    reductions: list[float] = []
    for values in objectives.values():
        if not isinstance(values, dict):
            continue
        reduction = values.get("relative_reduction")
        if isinstance(reduction, (int, float)) and not isinstance(reduction, bool):
            reductions.append(float(reduction))
    if not reductions:
        return None
    smell_complete = int(bool((verify_payload.get("smell_guard") or {}).get("success")))
    net_progress = round(sum(reductions), 6)
    improved_count = sum(value > 0 for value in reductions)
    return smell_complete, net_progress, improved_count
