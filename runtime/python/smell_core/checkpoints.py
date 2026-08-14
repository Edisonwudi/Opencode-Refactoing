"""Persistent, threshold-independent checkpoints for smell repair tasks."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .checkpoint_adapters import (
    CHECKPOINT_SMELLS,
    authorize_dead_code_target_absence,
    capture_metric_snapshot,
    detector_profile_for,
)
from .checkpoint_contract import CHECKPOINT_CONTRACT_VERSION, evaluate_checkpoint_contract
from .config import interpolate_command_text
from .java_test_evidence import java_sample_test_evidence_contract
from .resolution_plan import build_resolution_plan
from .test_change_contract import (
    TestChangeContractError,
    capture_test_change_contract,
    clean_transient_test_artifacts,
    evaluate_test_change_contract,
    is_standard_java_test_path,
)
from .java.source_layout import JavaSourceLayout, discover_java_source_layout
from .guard_scope import (
    GuardScopeError,
    GuardVerificationScope,
    build_guard_verification_scope,
    validate_guard_analysis_scope,
)


CHECKPOINT_SCHEMA_VERSION = 5
BASELINE_SEAL_VERSION = 1
BASELINE_SEAL_ALGORITHM = "sha256"
BASELINE_SEAL_FIELD = "baseline_seal"
VERIFICATION_CONTRACT_VERSION = 4


def _require_current_checkpoint_versions(payload: dict[str, Any]) -> None:
    """Reject checkpoints that predate either half of the frozen contract.

    Schema v5 describes the target-Guard manifest shape, while ``contract_version``
    versions the resolution/structural semantics stored inside it. Reusing a
    v4 manifest would silently preserve the retired Refused Bequest snapshot
    semantics instead of the target-Guard-only contract.
    """
    if int(payload.get("schema_version") or 0) != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: checkpoint schema v5 must be captured before editing"
        )
    if int(payload.get("contract_version") or 0) != CHECKPOINT_CONTRACT_VERSION:
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: checkpoint contract v5 must be captured before editing"
        )


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


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolved_path(value: Any) -> str:
    """Canonicalize one resolved execution path without consulting live config files."""
    if value is None or str(value).strip() == "":
        return ""
    return str(Path(str(value)).expanduser().resolve())


def _command_contract(command_config: Any, project_root: Path) -> dict[str, Any]:
    """Freeze the exact resolved command/script that the guard will execute."""
    configured_command = str(getattr(command_config, "command", None) or "")
    configured_script = str(getattr(command_config, "script", None) or "")
    return {
        "kind": (
            "script"
            if configured_script
            else "command"
            if configured_command
            else "missing"
        ),
        "configured_command": configured_command,
        "configured_script": configured_script,
        "rendered_command": (
            interpolate_command_text(configured_command, project_root)
            if configured_command
            else ""
        ),
        "rendered_script": (
            interpolate_command_text(configured_script, project_root)
            if configured_script
            else ""
        ),
    }


def capture_verification_contract(config: Any) -> dict[str, Any]:
    """Return the Java build/test execution identity resolved by config loading.

    The contract intentionally excludes detector prompts and model settings.  It
    contains only values that select or affect the build/test processes, so a
    caller cannot capture c000 with the project contract and later verify the
    same source with a focused, replaced, or no-op command.
    """
    project_root = Path(config.project_root).expanduser().resolve()
    verification_mode = str(getattr(config, "verification_mode", "") or "").strip()
    build_source = str(getattr(config, "build_source", "") or "").strip()
    test_source = str(getattr(config, "test_source", "") or "").strip()
    resolved_cwd = Path(config.cwd).expanduser().resolve()
    dataset_root = Path(config.dataset_root).expanduser().resolve()
    test_cwd = dataset_root if test_source == "dataset" else resolved_cwd
    defaults = getattr(config, "defaults", None)
    resolved_env = {
        str(key): str(value)
        for key, value in sorted(dict(getattr(config, "env", {}) or {}).items())
    }
    sample_test_command = str(
        getattr(config, "sample_test_command", "") or ""
    ).strip()
    sample_test_locations = [
        item.strip()
        for item in str(getattr(config, "sample_test_location", "") or "").split(";")
        if item.strip()
    ]
    return {
        "contract_version": VERIFICATION_CONTRACT_VERSION,
        "verification_mode": verification_mode,
        "build": {
            **_command_contract(getattr(config, "build", None), project_root),
            "source": build_source,
            "cwd": str(resolved_cwd),
        },
        "test": {
            **_command_contract(getattr(config, "test", None), project_root),
            "source": test_source,
            "cwd": str(test_cwd),
        },
        "sample_test": {
            **_command_contract(getattr(config, "sample_test", None), project_root),
            "source": "dataset",
            "cwd": str(dataset_root),
            "locations": sample_test_locations,
            "command_sha256": hashlib.sha256(
                sample_test_command.encode("utf-8")
            ).hexdigest(),
            "command_present": bool(sample_test_command),
            "evidence_adapter": java_sample_test_evidence_contract(config),
        },
        "execution": {
            "cwd": str(resolved_cwd),
            "build_root": _resolved_path(getattr(config, "build_root", None)),
            "dataset_root": str(dataset_root),
            "run_build": bool(getattr(defaults, "run_build", False)),
            "run_tests": bool(getattr(defaults, "run_tests", False)),
            "shell_timeout_seconds": int(
                getattr(defaults, "shell_timeout", 0) or 0
            ),
            "resolved_env_keys": sorted(resolved_env),
            "resolved_env_sha256": _canonical_hash(resolved_env),
        },
    }


def compute_c000_baseline_seal(manifest: Mapping[str, Any]) -> str:
    """Return the canonical digest for one complete c000 manifest.

    The seal field itself is excluded to avoid a recursive digest; every other
    manifest field, including detector output, finding identity, target hashes,
    revision identity, and the test-change contract, is covered.  The function
    is public so a controller can retain and compare the same canonical seal
    outside the model-writable checkout.
    """
    if not isinstance(manifest, Mapping):
        raise TypeError("c000 manifest must be a mapping")
    canonical_manifest = dict(manifest)
    canonical_manifest.pop(BASELINE_SEAL_FIELD, None)
    return _canonical_hash(canonical_manifest)


def validate_c000_baseline_seal(manifest: Mapping[str, Any]) -> str:
    """Validate and return a c000 manifest's canonical digest.

    Missing, malformed, or mismatched seals require recapture.  This validation
    is intentionally performed whenever c000 is loaded, not only when capture
    happens to encounter an existing file.
    """
    if not isinstance(manifest, Mapping):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 baseline manifest is not an object"
        )
    if str(manifest.get("checkpoint_id") or "") != "c000" or str(
        manifest.get("kind") or ""
    ) != "baseline":
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 baseline identity is invalid"
        )
    seal = manifest.get(BASELINE_SEAL_FIELD)
    if not isinstance(seal, Mapping):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 canonical baseline seal is missing"
        )
    if (
        int(seal.get("version") or 0) != BASELINE_SEAL_VERSION
        or str(seal.get("algorithm") or "") != BASELINE_SEAL_ALGORITHM
    ):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 canonical baseline seal format is unsupported"
        )
    actual = str(seal.get("digest") or "")
    expected = compute_c000_baseline_seal(manifest)
    if not actual or not hmac.compare_digest(actual, expected):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: CHECKPOINT_BASELINE_SEAL_MISMATCH: "
            "c000 manifest changed after capture"
        )
    return expected


def _selection_context(config: Any) -> dict[str, Any]:
    value = getattr(config, "target_context", None)
    if not isinstance(value, dict):
        return {}
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _target_records(config: Any, root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for target in config.locations:
        try:
            target_rel = target.file_path.resolve().relative_to(root).as_posix()
        except ValueError:
            target_rel = str(target.file_path)
        records.append({
            "file": target_rel,
            "method": target.method or "",
            "line": target.line,
            "source_hash": _source_hash(target.file_path),
        })
    return records


def _require_reusable_baseline_identity(
    existing: Mapping[str, Any],
    config: Any,
    root: Path,
) -> None:
    finding_contract = existing.get("guard_contract") or existing.get("finding_contract")
    if not isinstance(finding_contract, Mapping):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 has no frozen Guard contract"
        )
    frozen_context = finding_contract.get("selection_context")
    if not isinstance(frozen_context, Mapping):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: c000 has no frozen selection_context"
        )
    if _canonical_hash(dict(frozen_context)) != _canonical_hash(_selection_context(config)):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: selection_context changed since c000 capture"
        )

    current_commit = _git_text(root, ["rev-parse", "HEAD"])
    if str(existing.get("project_commit") or "") != current_commit:
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: HEAD commit changed since c000 capture"
        )
    current_tree = _git_text(root, ["rev-parse", "HEAD^{tree}"])
    if str(existing.get("tree_hash") or "") != current_tree:
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: HEAD tree changed since c000 capture"
        )

    frozen_targets = existing.get("targets")
    current_targets = _target_records(config, root)
    if not isinstance(frozen_targets, list) or len(frozen_targets) != len(current_targets):
        raise ValueError(
            "CHECKPOINT_RECAPTURE_REQUIRED: target set changed since c000 capture"
        )
    for index, (frozen, current) in enumerate(zip(frozen_targets, current_targets), start=1):
        if not isinstance(frozen, Mapping):
            raise ValueError(
                f"CHECKPOINT_RECAPTURE_REQUIRED: target {index} identity is missing from c000"
            )
        if str(frozen.get("file") or "") != str(current.get("file") or ""):
            raise ValueError(
                f"CHECKPOINT_RECAPTURE_REQUIRED: target {index} path changed since c000 capture"
            )
        if str(frozen.get("source_hash") or "") != str(current.get("source_hash") or ""):
            raise ValueError(
                f"CHECKPOINT_RECAPTURE_REQUIRED: target {index} source changed since c000 capture"
            )


def _finding_contract(smell: str, metrics: dict[str, Any], target_context: Any) -> dict[str, Any]:
    detector_id = str(metrics.get("detector") or "").strip()
    detector_profile = metrics.get("detector_profile")
    identity = metrics.get("finding_identity")
    if not detector_id:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: detector_id_missing")
    if not isinstance(detector_profile, dict) or not detector_profile:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: detector_profile_missing")
    if not isinstance(identity, dict) or not identity:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: finding_identity_missing")
    stable_identity = json.loads(json.dumps(identity, sort_keys=True, ensure_ascii=True))
    contract = {
        "detector_id": detector_id,
        "detector_profile": detector_profile,
        "detector_profile_hash": _canonical_hash(detector_profile),
        "finding_id": _canonical_hash({"smell": smell, "identity": stable_identity}),
        "entity_identity": stable_identity,
        "baseline_metrics": dict(metrics.get("objectives") or {}),
        "selection_context": dict(target_context) if isinstance(target_context, dict) else {},
    }
    finding_catalog = metrics.get("project_finding_catalog")
    if isinstance(finding_catalog, list):
        contract["baseline_finding_catalog"] = json.loads(
            json.dumps(finding_catalog, sort_keys=True, ensure_ascii=True)
        )
    occurrence_contract = metrics.get("occurrence_contract")
    if isinstance(occurrence_contract, list):
        contract["baseline_occurrence_contract"] = json.loads(
            json.dumps(occurrence_contract, sort_keys=True, ensure_ascii=True)
        )
    target_anchors = metrics.get("target_anchor_contract")
    if smell == "code_clone_type1" and isinstance(target_anchors, list):
        contract["baseline_target_anchors"] = json.loads(
            json.dumps(target_anchors, sort_keys=True, ensure_ascii=True)
        )
    return contract


def _guard_contract(smell: str, metrics: dict[str, Any], target_context: Any) -> dict[str, Any]:
    """Freeze one compact target predicate for Java Guard v5.

    Project-wide finding catalogs and occurrence catalogs are intentionally not
    accepted here.  The caller already supplied the target; c000 records only
    the predicate/profile, stable entity identity, baseline objectives, and a
    bounded witness needed to re-evaluate that same target.
    """
    rule_id = str(
        metrics.get("guard_rule_id") or metrics.get("detector") or ""
    ).strip()
    profile = metrics.get("guard_profile") or metrics.get("detector_profile")
    identity = metrics.get("entity_identity") or metrics.get("finding_identity")
    witness = metrics.get("witness")
    if not rule_id:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: guard_rule_id_missing")
    if not isinstance(profile, dict) or not profile:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: guard_profile_missing")
    if not isinstance(identity, dict) or not identity:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: entity_identity_missing")
    if not isinstance(witness, (dict, list)):
        witness = {}
    stable_identity = json.loads(json.dumps(identity, sort_keys=True, ensure_ascii=True))
    stable_witness = json.loads(json.dumps(witness, sort_keys=True, ensure_ascii=True))
    if len(json.dumps(stable_witness, ensure_ascii=True).encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: guard_witness_too_large")
    stable_profile = json.loads(json.dumps(profile, sort_keys=True, ensure_ascii=True))
    return {
        "guard_rule_id": rule_id,
        "guard_profile": stable_profile,
        "guard_profile_hash": _canonical_hash(stable_profile),
        "target_id": _canonical_hash({"smell": smell, "identity": stable_identity}),
        "entity_identity": stable_identity,
        "baseline_objectives": dict(metrics.get("objectives") or {}),
        "selection_context": dict(target_context) if isinstance(target_context, dict) else {},
        "witness": stable_witness,
    }


def _baseline_guard_scope(config: Any, root: Path) -> GuardVerificationScope:
    targets: set[str] = set()
    for location in config.locations:
        try:
            relative = location.file_path.resolve().relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError(
                "CHECKPOINT_BASELINE_CAPTURE_FAILED: target outside project root"
            ) from exc
        targets.add(relative)
    frozen = tuple(sorted(targets))
    try:
        validate_guard_analysis_scope(root, frozen)
    except GuardScopeError as exc:
        raise ValueError(f"{exc.status}: {exc.message}") from exc
    return GuardVerificationScope(
        changed_files=(),
        changed_production_files=(),
        target_files=frozen,
        analysis_files=frozen,
    )


def capture_baseline_finding_snapshot(
    config: Any,
    evidence: str = "",
) -> dict[str, Any]:
    """Run the exact finding gate used before c000 writes any artifacts.

    This is the read-only entry point for baseline audits.  Java callers get
    the same target-only Guard scope as :func:`capture_checkpoint_baseline`;
    the result is admitted only when one measurable target finding is present.
    It deliberately does not capture build/test policy or write a checkpoint.
    """
    smell = str(config.smell)
    if smell not in CHECKPOINT_SMELLS:
        raise ValueError(f"CHECKPOINT_NOT_SUPPORTED: {smell}")
    if not config.locations:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: no target location")
    is_java = str(config.language).strip().lower() == "java"
    if is_java:
        root = config.project_root.expanduser().resolve()
        config.guard_scope = _baseline_guard_scope(config, root)
    metrics = capture_metric_snapshot(config, evidence)
    if not metrics.get("ok") or not metrics.get("objectives"):
        raise ValueError(
            "CHECKPOINT_BASELINE_CAPTURE_FAILED: "
            f"{metrics.get('error', 'no measurable objectives')}"
        )
    candidate_count = int(
        metrics.get("target_match_count", metrics.get("candidate_count") or 0)
    )
    if candidate_count != 1:
        if candidate_count > 1:
            raise ValueError(
                f"TARGET_AMBIGUOUS: Guard matched {candidate_count} target entities"
            )
        raise ValueError("BASELINE_FINDING_NOT_FOUND")
    if metrics.get(
        "target_smell_present",
        metrics.get("finding_present"),
    ) is not True:
        raise ValueError("BASELINE_FINDING_NOT_FOUND")
    return metrics


def capture_checkpoint_baseline(
    config: Any,
    evidence: str = "",
    *,
    allow_test_changes: bool = False,
) -> dict[str, Any]:
    """Capture the immutable c000 metric snapshot for a migrated smell."""
    smell = str(config.smell)
    if smell not in CHECKPOINT_SMELLS:
        raise ValueError(f"CHECKPOINT_NOT_SUPPORTED: {smell}")
    if not config.locations:
        raise ValueError("CHECKPOINT_BASELINE_CAPTURE_FAILED: no target location")
    is_java = str(config.language).strip().lower() == "java"
    if (
        is_java
        and allow_test_changes
        and str(getattr(config, "verification_mode", "") or "") != "project_full"
    ):
        raise ValueError(
            "TEST_CHANGE_REQUIRES_PROJECT_FULL: allow_test_changes requires "
            "project_full verification"
        )
    root = config.project_root.expanduser().resolve()
    location = checkpoint_location(config)
    task_root = checkpoint_task_root(root, smell, location)
    baseline_path = task_root / "c000-baseline" / "manifest.json"
    if baseline_path.is_file():
        existing = _read_json(baseline_path)
        _require_current_checkpoint_versions(existing)
        validate_c000_baseline_seal(existing)
        if (
            str(existing.get("project_root")) != str(root)
            or str(existing.get("location")) != location
            or str(existing.get("smell")) != smell
        ):
            raise ValueError("CHECKPOINT_BASELINE_IDENTITY_MISMATCH")
        _require_reusable_baseline_identity(existing, config, root)
        existing_finding_contract = (
            existing.get("guard_contract") if is_java else existing.get("finding_contract")
        )
        expected_profile_hash = (
            str(
                existing_finding_contract.get("guard_profile_hash")
                or existing_finding_contract.get("detector_profile_hash")
                or ""
            )
            if isinstance(existing_finding_contract, dict)
            else ""
        )
        current_profile_hash = _canonical_hash(detector_profile_for(config))
        if not expected_profile_hash or current_profile_hash != expected_profile_hash:
            raise ValueError(
                "DETECTOR_PROFILE_MISMATCH: recapture c000 with the current target Guard"
            )
        existing_policy = existing.get("test_change_contract")
        if is_java:
            frozen_verification = existing.get("verification_contract")
            current_verification = capture_verification_contract(config)
            if not isinstance(frozen_verification, Mapping):
                raise ValueError(
                    "CHECKPOINT_RECAPTURE_REQUIRED: c000 has no frozen "
                    "verification_contract"
                )
            if _canonical_hash(dict(frozen_verification)) != _canonical_hash(
                current_verification
            ):
                raise ValueError(
                    "CHECKPOINT_VERIFICATION_CONTRACT_MISMATCH: resolved "
                    "build/test verification changed since c000 capture"
                )
            if not isinstance(existing_policy, dict):
                raise ValueError("CHECKPOINT_RECAPTURE_REQUIRED: c000 has no frozen test-change contract")
            if bool(existing_policy.get("allow_test_changes")) != bool(allow_test_changes):
                raise ValueError(
                    "CHECKPOINT_POLICY_MISMATCH: allow_test_changes is immutable after c000 capture"
                )
        return existing

    metrics = capture_baseline_finding_snapshot(config, evidence)
    finding_contract = (
        _guard_contract(smell, metrics, getattr(config, "target_context", {}))
        if is_java
        else _finding_contract(smell, metrics, getattr(config, "target_context", {}))
    )
    test_change_contract = (
        capture_test_change_contract(
            root,
            declared_test_files=str(getattr(config, "sample_test_location", "") or ""),
            allow_test_changes=allow_test_changes,
        )
        if str(config.language) == "java"
        else None
    )
    verification_contract = capture_verification_contract(config) if is_java else None
    targets = _target_records(config, root)
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
        "resolution_plan": build_resolution_plan(
            smell,
            finding_contract=finding_contract,
            baseline_metrics=metrics,
        ),
    }
    if is_java:
        manifest["guard_contract"] = finding_contract
    else:
        manifest["finding_contract"] = finding_contract
    if test_change_contract is not None:
        manifest["test_change_contract"] = test_change_contract
    if verification_contract is not None:
        manifest["verification_contract"] = verification_contract
    manifest[BASELINE_SEAL_FIELD] = {
        "version": BASELINE_SEAL_VERSION,
        "algorithm": BASELINE_SEAL_ALGORITHM,
        "digest": compute_c000_baseline_seal(manifest),
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
    if not path.is_file():
        return None
    payload = _read_json(path)
    _require_current_checkpoint_versions(payload)
    validate_c000_baseline_seal(payload)
    return payload


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


def _is_production_source(
    path: str,
    language: str,
    *,
    project_root: Path | None = None,
    configured_test_roots: tuple[str, ...] = (),
    source_layout: JavaSourceLayout | None = None,
) -> bool:
    relative = path.replace("\\", "/").lstrip("/")
    normalized = "/" + relative
    extensions = {
        "java": (".java",),
        "python": (".py",),
        "c": (".c", ".h"),
        # C++ projects routinely keep production code in .h headers (rocksdb);
        # without it a header-only repair can never produce a production diff.
        "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    }.get(language, ())
    if not normalized.endswith(extensions):
        return False
    if language == "java":
        layout = source_layout
        if layout is None and project_root is not None:
            layout = discover_java_source_layout(project_root)
            if configured_test_roots:
                layout = JavaSourceLayout(
                    project_root=layout.project_root,
                    test_roots=tuple(configured_test_roots),
                    test_files=layout.test_files,
                    test_globs=layout.test_globs,
                    test_glob_excludes=layout.test_glob_excludes,
                    verification_files=layout.verification_files,
                    auxiliary_roots=layout.auxiliary_roots,
                )
        if layout is not None and layout.is_test_path(relative):
            return False
        if project_root is None and is_standard_java_test_path(relative):
            return False
    lowered = normalized.lower()
    return not any(
        marker in lowered
        for marker in (
            "/src/test/", "/src/it/", "/tests/", "/test/",
            # Plain build/ trees (CMake probe sources, Gradle generated
            # sources) are outputs, never production sources.
            "/build/", "/build-refactoragent/", "/cmake-build-", "/.venv/", "/dist/",
        )
    )


def _diff_patch(
    root: Path,
    base_commit: str,
    paths: list[str] | None = None,
    *,
    fail_closed: bool = False,
) -> str | None:
    if paths is not None and not paths:
        return None if fail_closed else ""
    pathspec = list(paths or [])
    # Freeze the patch shape: repository/user diff.context, textconv and
    # external diff drivers must not redefine identity hunk boundaries.
    diff_options = [
        "--binary",
        "--unified=3",
        "--inter-hunk-context=0",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
    ]
    args = ["diff", *diff_options, base_commit, "--", *pathspec]
    tracked = _run_git(root, args) if base_commit else None
    if fail_closed and (tracked is None or tracked.returncode != 0):
        return None
    chunks = [tracked.stdout.rstrip("\n")] if tracked and tracked.returncode == 0 and tracked.stdout else []
    candidates = pathspec if paths is not None else _changed_paths(root, base_commit)
    for path in candidates:
        listed = _run_git(root, ["ls-files", "--", path])
        if fail_closed and listed.returncode != 0:
            return None
        if listed.returncode == 0 and listed.stdout.strip():
            continue
        result = _run_git(
            root,
            ["diff", "--no-index", *diff_options, "--", "/dev/null", path],
        )
        if fail_closed and (
            result.returncode not in {0, 1}
            or (not result.stdout and not (root / path).is_file())
        ):
            return None
        if result.stdout:
            chunks.append(result.stdout.rstrip("\n"))
    return "\n".join(chunks) + ("\n" if chunks else "")


def prepare_checkpoint(
    config: Any,
    evidence: str,
    *,
    expected_baseline_seal: str = "",
) -> dict[str, Any]:
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
    transient_test_artifact_cleanup: dict[str, Any] | None = None
    actual_baseline_seal = validate_c000_baseline_seal(baseline)
    if str(config.language) == "java":
        if not expected_baseline_seal:
            return {
                "required": False,
                "reason": "baseline_controller_seal_missing",
            }
        if not hmac.compare_digest(
            actual_baseline_seal,
            expected_baseline_seal,
        ):
            return {
                "required": False,
                "reason": "baseline_controller_seal_mismatch",
            }
        frozen_verification = baseline.get("verification_contract")
        current_verification = capture_verification_contract(config)
        if (
            not isinstance(frozen_verification, Mapping)
            or _canonical_hash(dict(frozen_verification))
            != _canonical_hash(current_verification)
        ):
            return {
                "required": False,
                "reason": "verification_contract_mismatch",
                "frozen_verification_contract_hash": (
                    _canonical_hash(dict(frozen_verification))
                    if isinstance(frozen_verification, Mapping)
                    else ""
                ),
                "current_verification_contract_hash": _canonical_hash(
                    current_verification
                ),
            }
        frozen_test_contract = baseline.get("test_change_contract")
        if not isinstance(frozen_test_contract, dict):
            return {
                "required": False,
                "reason": "checkpoint_recapture_required",
            }
        try:
            transient_test_artifact_cleanup = clean_transient_test_artifacts(
                root,
                frozen_test_contract,
            )
        except TestChangeContractError as exc:
            return {
                "required": False,
                "reason": exc.status.lower(),
                "error": exc.message,
                "details": dict(exc.details),
            }
    state_path = task_root / "task-state.json"
    state = _read_json(state_path)
    sequence = int(state.get("next_sequence") or 1)
    checkpoint_id = f"c{sequence:03d}"
    checkpoint_dir = task_root / f"{checkpoint_id}-verify"
    baseline_metrics = dict(baseline.get("metrics") or {})
    is_java = str(config.language).strip().lower() == "java"
    finding_contract = dict(
        (
            baseline.get("guard_contract")
            if is_java
            else baseline.get("finding_contract")
        )
        or {}
    )
    frozen_selection_context = finding_contract.get("selection_context")
    current_selection_context = _selection_context(config)
    if (
        not isinstance(frozen_selection_context, dict)
        or _canonical_hash(frozen_selection_context)
        != _canonical_hash(current_selection_context)
    ):
        return {
            "required": False,
            "reason": "checkpoint_selection_context_mismatch",
            ("guard_contract" if is_java else "finding_contract"): finding_contract,
            "frozen_selection_context": (
                dict(frozen_selection_context)
                if isinstance(frozen_selection_context, dict)
                else None
            ),
            "current_selection_context": current_selection_context,
        }
    if is_java:
        config.guard_contract = finding_contract
        config.finding_contract = {}
    else:
        config.finding_contract = finding_contract
    # Adapters consume the exact frozen selector after the equality check. This
    # prevents a mutable caller-owned dict from becoming a second live contract.
    config.target_context = json.loads(
        json.dumps(frozen_selection_context, sort_keys=True, ensure_ascii=True)
    )
    if is_java:
        try:
            config.guard_scope = build_guard_verification_scope(
                root,
                str(baseline.get("project_commit") or ""),
                [item.file_path for item in config.locations],
            )
        except Exception as exc:
            return {
                "required": False,
                "reason": str(getattr(exc, "status", "guard_scope_unavailable")).lower(),
                "error": str(exc),
                "guard_contract": finding_contract,
            }
        changed = list(config.guard_scope.changed_files)
        production_sources = list(config.guard_scope.changed_production_files)
    else:
        changed = _changed_paths(root, str(baseline.get("project_commit") or ""))
        java_source_layout = None
        production_sources = [
            path
            for path in changed
            if _is_production_source(
                path,
                str(config.language),
                project_root=root,
                source_layout=java_source_layout,
            )
        ]
    baseline_commit = str(baseline.get("project_commit") or "")
    patch = _diff_patch(root, baseline_commit)
    production_patch = _diff_patch(root, baseline_commit, production_sources)
    # Scope is frozen before evaluation.  Java Guard adapters therefore cannot
    # accidentally discover the repository while trying to establish diff
    # context.  Non-Java Data Clumps receives added hunks only from the same
    # caller-selected target files.  Clone consolidation may additionally
    # inspect the bounded production-only edit hunks so two identical target
    # declarations can move to one shared implementation without discovering
    # or scanning project source.
    nonjava_target_patch: str | None = None
    if not is_java and smell in {
        "data_clumps",
        "code_clone_type1",
        "feature_envy",
        "mysterious_name",
    }:
        target_patch_paths = (
            sorted({
                target.file_path.resolve().relative_to(root).as_posix()
                for target in config.locations
            })
            if smell in {"data_clumps", "feature_envy", "mysterious_name"}
            else sorted(production_sources)
        )
        nonjava_target_patch = _diff_patch(
            root,
            str(baseline.get("project_commit") or ""),
            target_patch_paths,
            fail_closed=True,
        )
    current = capture_metric_snapshot(
        config,
        evidence,
        changed_patch=nonjava_target_patch,
        compatibility_patch=(
            production_patch
            if not is_java and smell == "data_clumps"
            else None
        ),
    )
    if not is_java and smell == "dead_code":
        # Dead Code may cross from a present frozen declaration to an absent
        # declaration only with byte-exact old-line evidence from that one
        # caller-selected production file.  The adapter receives this bounded
        # context; it does not discover or search the project.
        target_production_patch = ""
        if len(config.locations) == 1:
            try:
                target_relative = (
                    config.locations[0]
                    .file_path.resolve()
                    .relative_to(root)
                    .as_posix()
                )
            except (OSError, ValueError):
                target_relative = ""
            if target_relative and target_relative in production_sources:
                target_production_patch = _diff_patch(
                    root,
                    baseline_commit,
                    [target_relative],
                )
        current = authorize_dead_code_target_absence(
            config,
            baseline_metrics,
            current,
            production_patch=target_production_patch,
            changed_production_source_files=production_sources,
        )
    expected_detector = str(
        finding_contract.get("guard_rule_id")
        or finding_contract.get("detector_id")
        or ""
    )
    current_detector = str(current.get("guard_rule_id") or current.get("detector") or "")
    expected_profile_hash = str(
        finding_contract.get("guard_profile_hash")
        or finding_contract.get("detector_profile_hash")
        or ""
    )
    current_profile_hash = _canonical_hash(
        current.get("guard_profile") or current.get("detector_profile") or {}
    )
    if (
        not finding_contract
        or current_detector != expected_detector
        or current_profile_hash != expected_profile_hash
    ):
        return {
            "required": False,
            "reason": "guard_profile_mismatch" if is_java else "detector_profile_mismatch",
            ("guard_contract" if is_java else "finding_contract"): finding_contract,
            "current_metrics": current,
        }
    has_production_diff = bool(production_sources)
    test_changes = None
    if str(config.language) == "java":
        frozen_test_contract = baseline.get("test_change_contract")
        if not isinstance(frozen_test_contract, dict):
            return {
                "required": False,
                "reason": "checkpoint_recapture_required",
                "guard_contract": finding_contract,
                "current_metrics": current,
            }
        test_changes = evaluate_test_change_contract(
            root,
            frozen_test_contract,
        ).to_dict()
        if transient_test_artifact_cleanup is not None:
            test_changes["transient_test_artifact_cleanup"] = (
                transient_test_artifact_cleanup
            )
    evaluation = evaluate_checkpoint_contract(
        baseline_metrics,
        current,
        has_production_diff=has_production_diff,
        smell=str(config.smell),
        changed_production_source_files=production_sources,
    )
    delta = {
        **evaluation.to_dict(),
        "has_production_diff": has_production_diff,
        "changed_production_source_files": production_sources,
        "target_missing": bool(current.get("target_missing")),
    }
    if test_changes is not None:
        delta["test_changes"] = test_changes
    resolution_plan = build_resolution_plan(
        smell,
        finding_contract=finding_contract,
        baseline_metrics=baseline_metrics,
        current_metrics=current,
        delta=delta,
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
        "verification_mode": str(getattr(config, "verification_mode", "") or ""),
        "location": location,
        "adapter": baseline.get("adapter", smell),
        "baseline_checkpoint": "c000",
        "baseline_project_commit": baseline_commit,
        "changed_files": changed,
        "changed_production_source_files": production_sources,
        "production_diff": has_production_diff,
        "production_diff_hash": hashlib.sha256(production_patch.encode("utf-8", errors="surrogateescape")).hexdigest(),
        "baseline_metrics": baseline_metrics,
        "current_metrics": current,
        "resolution_plan": resolution_plan,
        "delta": delta,
        "accepted": False,
        "best_checkpoint": False,
    }
    if is_java:
        manifest["guard_contract"] = finding_contract
    else:
        manifest["finding_contract"] = finding_contract
    if test_changes is not None:
        manifest["test_changes"] = test_changes
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
    resolution = str(verify_payload.get("resolution") or "").strip()
    build_test_success = (
        bool((verify_payload.get("build_test_guard") or {}).get("success"))
        if verify_payload.get("build_test_guard") is not None
        else None
    )
    contract = manifest.get("guard_contract") or manifest.get("finding_contract") or {}
    profile = contract.get("guard_profile") or contract.get("detector_profile") or {}
    java_product_contract = bool(
        manifest.get("guard_contract")
        or str(profile.get("language") or "") == "java"
    )
    current_metrics = manifest.get("current_metrics")
    target_absence_evidence = (
        current_metrics.get("target_absence_evidence")
        if isinstance(current_metrics, Mapping)
        else None
    )
    nonjava_dead_code_absence = bool(
        manifest.get("smell") == "dead_code"
        and not java_product_contract
        and isinstance(current_metrics, Mapping)
        and current_metrics.get("target_missing") is True
    )
    exact_dead_code_deletion = bool(
        nonjava_dead_code_absence
        and current_metrics.get("target_absence_allowed") is True
        and isinstance(target_absence_evidence, Mapping)
        and target_absence_evidence.get("contract")
        == "exact-target-declaration-deletion-v2"
        and target_absence_evidence.get("allowed") is True
    )
    accepted = bool(
        verify_payload.get("status") == "PASS"
        and resolution == "resolved"
        and verify_payload.get("success") is True
        and verify_payload.get("accepted") is True
        and (
            not nonjava_dead_code_absence
            or (
                exact_dead_code_deletion
                and manifest.get("verification_mode") == "project_full"
                and build_test_success is True
            )
        )
        and (not java_product_contract or build_test_success is True)
    )
    manifest["accepted"] = accepted
    manifest["progress"] = bool(verify_payload.get("progress"))
    manifest["resolution"] = resolution
    manifest["verify_status"] = verify_payload.get("status")
    manifest["build_test_success"] = build_test_success
    behavior_valid = manifest["build_test_success"] is True
    manifest["best_checkpoint"] = bool(accepted and behavior_valid)
    state_path = task_root / "task-state.json"
    state = _read_json(state_path)
    candidate_rank = _partial_checkpoint_rank(manifest, verify_payload)
    manifest["best_partial_eligible"] = candidate_rank is not None
    manifest["restorable"] = bool((accepted and behavior_valid) or candidate_rank is not None)
    current_is_best_partial = False
    existing = state.get("best_partial")
    if isinstance(existing, dict) and existing.get("build_test_success") is not True:
        # Old task-state files could point at a structurally better but
        # behavior-breaking checkpoint. Keep its manifest on disk for audit,
        # but never retain it as a recovery target.
        state.pop("best_partial", None)
    existing_best = str(state.get("best") or "")
    if existing_best:
        existing_best_path = task_root / f"{existing_best}-verify" / "manifest.json"
        existing_best_manifest = (
            _read_json(existing_best_path) if existing_best_path.is_file() else {}
        )
        if existing_best_manifest.get("build_test_success") is not True:
            # A local/unchecked PASS is still useful evidence, but it is not a
            # behavior-preserving recovery target.
            state.pop("best", None)
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
                "resolution": manifest.get("resolution"),
                "progress": manifest.get("progress"),
                "restorable": True,
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
    if accepted and behavior_valid:
        state["best"] = checkpoint_id
    _write_json(state_path, state)
    return manifest


def _partial_checkpoint_rank(
    manifest: dict[str, Any],
    verify_payload: dict[str, Any],
) -> tuple[int, float, int] | None:
    """Rank behavior-valid partial checkpoints without weakening acceptance."""
    if manifest.get("build_test_success") is not True:
        return None
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
    resolution = str(verify_payload.get("resolution") or "").strip()
    if resolution == "resolved":
        resolution_rank = 2
    elif resolution == "improved":
        resolution_rank = 1
    else:
        return None
    net_progress = round(sum(reductions), 6)
    improved_count = sum(value > 0 for value in reductions)
    return resolution_rank, net_progress, improved_count
