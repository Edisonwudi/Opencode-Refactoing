"""Persistent, threshold-independent checkpoints for smell repair tasks."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .java.semantic_detector import analyze_feature_envy_target


CHECKPOINT_SCHEMA_VERSION = 1


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
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


def expected_receiver_from_evidence(evidence: str) -> str:
    match = re.search(r"(?:^|;\s*)envied_type=([^;]+)", str(evidence or ""), flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


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


def _with_expected_receiver(profile: dict[str, Any], expected_receiver: str) -> dict[str, Any]:
    if expected_receiver or not profile.get("ok"):
        return profile
    dominant = str(profile.get("dominant_receiver_type") or "")
    profile["expected_receiver_type"] = dominant
    profile["expected_receiver_access"] = int(profile.get("dominant_receiver_access") or 0)
    profile["expected_receiver_ratio"] = float(profile.get("dominant_receiver_ratio") or 0.0)
    profile["matched_expected_types"] = {dominant: profile["expected_receiver_access"]} if dominant else {}
    profile["expected_receiver_source"] = "baseline_dominant_fallback"
    return profile


def capture_feature_envy_baseline(
    *,
    project_root: Path,
    target_file: Path,
    method: Optional[str],
    line: Optional[int],
    location: str,
    evidence: str,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    task_root = checkpoint_task_root(root, "feature_envy", location)
    baseline_path = task_root / "c000-baseline" / "manifest.json"
    if baseline_path.is_file():
        existing = _read_json(baseline_path)
        if str(existing.get("project_root")) != str(root) or str(existing.get("location")) != location:
            raise ValueError("FEATURE_ENVY_BASELINE_IDENTITY_MISMATCH")
        return existing

    expected_receiver = expected_receiver_from_evidence(evidence)
    profile = analyze_feature_envy_target(
        root,
        target_file=target_file,
        method=method,
        line=line,
        expected_receiver_type=expected_receiver,
    )
    profile = _with_expected_receiver(profile, expected_receiver)
    if not profile.get("ok"):
        raise ValueError(f"FEATURE_ENVY_BASELINE_CAPTURE_FAILED: {profile.get('error', 'unknown')}")
    try:
        target_rel = target_file.resolve().relative_to(root).as_posix()
    except ValueError:
        target_rel = str(target_file)
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": "c000",
        "kind": "baseline",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_root": str(root),
        "project_commit": _git_text(root, ["rev-parse", "HEAD"]),
        "tree_hash": _git_text(root, ["rev-parse", "HEAD^{tree}"]),
        "smell": "feature_envy",
        "location": location,
        "target_file": target_rel,
        "target_method": method or "",
        "target_line": line,
        "expected_receiver_type": profile.get("expected_receiver_type", expected_receiver),
        "target_source_hash": _source_hash(target_file),
        "metrics": profile,
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


def load_feature_envy_baseline(project_root: Path, location: str) -> Optional[dict[str, Any]]:
    path = checkpoint_task_root(project_root, "feature_envy", location) / "c000-baseline" / "manifest.json"
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


def _is_production_java(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    if not normalized.endswith(".java"):
        return False
    lowered = normalized.lower()
    return not any(marker in lowered for marker in ("/src/test/", "/src/it/", "/tests/", "/test/"))


def _diff_patch(root: Path, base_commit: str) -> str:
    tracked = _run_git(root, ["diff", "--binary", base_commit, "--"]) if base_commit else None
    chunks = [tracked.stdout.rstrip("\n")] if tracked and tracked.returncode == 0 and tracked.stdout else []
    for path in _changed_paths(root, base_commit):
        if _git_text(root, ["ls-files", "--", path]):
            continue
        result = _run_git(root, ["diff", "--no-index", "--binary", "--", "/dev/null", path])
        if result.stdout:
            chunks.append(result.stdout.rstrip("\n"))
    return "\n".join(chunks) + ("\n" if chunks else "")


def _required_reduction(before: int) -> int:
    """Require any strict reduction from a measurable immutable baseline."""
    return 1 if before > 0 else 0


def prepare_feature_envy_checkpoint(
    *,
    project_root: Path,
    target_file: Path,
    method: Optional[str],
    line: Optional[int],
    location: str,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    task_root = checkpoint_task_root(root, "feature_envy", location)
    baseline = load_feature_envy_baseline(root, location)
    if baseline is None:
        return {"required": False, "reason": "baseline_checkpoint_missing"}
    state_path = task_root / "task-state.json"
    state = _read_json(state_path)
    sequence = int(state.get("next_sequence") or 1)
    checkpoint_id = f"c{sequence:03d}"
    checkpoint_dir = task_root / f"{checkpoint_id}-verify"
    baseline_metrics = dict(baseline.get("metrics") or {})
    expected_receiver = str(baseline.get("expected_receiver_type") or "")
    current = analyze_feature_envy_target(
        root,
        target_file=target_file,
        method=method,
        line=line,
        expected_receiver_type=expected_receiver,
    )
    before = int(baseline_metrics.get("expected_receiver_access") or 0)
    after = int(current.get("expected_receiver_access") or 0) if current.get("ok") else 0
    reduction = before - after
    required_reduction = _required_reduction(before)
    changed = _changed_paths(root, str(baseline.get("project_commit") or ""))
    production_java = [path for path in changed if _is_production_java(path)]
    has_production_diff = bool(production_java)
    metric_available = before > 0
    target_missing = not bool(current.get("ok")) and current.get("error") == "target_method_not_found"
    metric_progress = bool(
        has_production_diff
        and metric_available
        and (target_missing or reduction >= required_reduction)
    )
    delta = {
        "expected_receiver_type": expected_receiver,
        "expected_receiver_access": {
            "before": before,
            "after": after if current.get("ok") else None,
            "absolute_reduction": reduction if current.get("ok") else before,
            "relative_reduction": round((reduction / before), 6) if before and current.get("ok") else None,
            "required_reduction": required_reduction,
        },
        "strict_detector_hit": {
            "before": bool(baseline_metrics.get("strict_detector_hit")),
            "after": bool(current.get("strict_detector_hit")) if current.get("ok") else None,
        },
        "has_production_diff": has_production_diff,
        "changed_production_java_files": production_java,
        "metric_available": metric_available,
        "metric_progress": metric_progress,
        "target_missing": target_missing,
    }
    patch = _diff_patch(root, str(baseline.get("project_commit") or ""))
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "parent": str(state.get("latest") or "c000"),
        "kind": "verify",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "required": True,
        "baseline_checkpoint": "c000",
        "changed_files": changed,
        "changed_production_java_files": production_java,
        "production_diff": has_production_diff,
        "production_diff_hash": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "baseline_metrics": baseline_metrics,
        "current_metrics": current,
        "delta": delta,
        "accepted": False,
        "best_checkpoint": False,
    }
    _write_json(checkpoint_dir / "manifest.json", manifest)
    _write_json(checkpoint_dir / "metrics.json", current)
    _write_json(checkpoint_dir / "delta.json", delta)
    (checkpoint_dir / "source.patch").write_text(patch, encoding="utf-8")
    state["latest"] = checkpoint_id
    state["next_sequence"] = sequence + 1
    _write_json(state_path, state)
    return manifest


def finalize_feature_envy_checkpoint(
    project_root: Path,
    location: str,
    checkpoint_id: str,
    verify_payload: dict[str, Any],
) -> None:
    task_root = checkpoint_task_root(project_root, "feature_envy", location)
    manifest_path = task_root / f"{checkpoint_id}-verify" / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = _read_json(manifest_path)
    accepted = bool(verify_payload.get("success"))
    manifest["accepted"] = accepted
    manifest["verify_status"] = verify_payload.get("status")
    manifest["build_test_success"] = bool((verify_payload.get("build_test_guard") or {}).get("success")) \
        if verify_payload.get("build_test_guard") is not None else None
    manifest["best_checkpoint"] = accepted
    _write_json(manifest_path, manifest)
    _write_json(manifest_path.parent / "verify.json", verify_payload)
    if accepted:
        state_path = task_root / "task-state.json"
        state = _read_json(state_path)
        state["best"] = checkpoint_id
        _write_json(state_path, state)
