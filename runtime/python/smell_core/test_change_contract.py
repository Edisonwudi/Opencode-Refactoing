"""Controller-owned contract for Java verification inputs.

The smell detector deliberately knows nothing about tests.  This module only
freezes both the test tree and the project files that control build/test
discovery or execution at ``c000``. It reports both filesystem deltas at final
verification. Whether test-source edits are allowed is captured in that
immutable baseline; callers cannot change the policy while evaluating a
candidate. Verification configuration is always immutable.

The default ``immutable`` policy is fail-closed: any added, changed, or deleted
file under a standard Java test source set (or any explicitly declared
``test_file``) yields ``TEST_SOURCE_MODIFIED``. The legacy controller input
``allow_test_changes=True`` is normalized once, at c000 capture, to the explicit
``api_migration`` mode. That mode permits source-level test API migration only:
baseline test files and verification configuration stay immutable, non-source
test inputs cannot change, test-method/assertion counts cannot decrease, and no
new disabled/ignored or assumption-skip signal may be introduced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .java.source_layout import (
    JavaSourceLayout,
    JavaSourceLayoutError,
    discover_java_source_layout,
    discover_java_verification_files,
    is_java_verification_config_path as _layout_is_verification_config_path,
    standard_test_root,
)


# v7 adds one pre-verification cleanup lane for newly created, Git-untracked
# runtime artifacts under frozen test roots. The files are removed before the
# authoritative build/tests run, so they can neither cause a false test-source
# rejection nor influence the final behavior result. Older c000 manifests must
# be recaptured because this policy is part of the frozen verification input.
TEST_CHANGE_CONTRACT_VERSION = 7
TEST_SEMANTIC_AUDIT_VERSION = 1
TRANSIENT_TEST_ARTIFACT_POLICY = "c000-new-untracked-test-runtime-artifacts/v1"

_TEST_CHANGE_MODES = frozenset({"immutable", "api_migration"})
_TEST_SOURCE_SUFFIXES = frozenset({".java", ".groovy", ".kt", ".kts", ".scala"})
_TRANSIENT_TEST_ARTIFACT_SUFFIXES = frozenset(
    {".db", ".journal", ".lock", ".log", ".pid", ".shm", ".tmp", ".wal"}
)
_MAX_TRANSIENT_TEST_ARTIFACTS = 512
_TEST_ANNOTATION = re.compile(
    r"@\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*"
    r"(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b"
)
_JUNIT3_TEST_METHOD = re.compile(
    r"\b(?:public\s+)?(?:final\s+)?void\s+test[A-Za-z0-9_$]*\s*\("
)
_ASSERTION_CALL = re.compile(
    r"(?<![\w$])(?:assert[A-Z][A-Za-z0-9_$]*|assertThat|fail|expectThrows|"
    r"verify|verifyNoInteractions|verifyNoMoreInteractions)\s*\("
)
_ASSERT_STATEMENT = re.compile(r"\bassert\s+(?![A-Za-z_$][\w$]*\s*\()[^;]+;")
_DISABLED_SIGNAL = re.compile(
    r"@\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*"
    r"(?:Disabled|Ignore|IgnoreRest|PendingFeature|Quarantined)\b"
)
_ASSUMPTION_SKIP_SIGNAL = re.compile(
    r"(?<![\w$])(?:assumeTrue|assumeFalse|assumeThat|assumeNoException|"
    r"assumingThat|assume)\s*\(|\b(?:throw\s+new\s+)?SkipException\s*\("
)

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".mvn",
        "build",
        "dist",
        "node_modules",
        "out",
        "target",
    }
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


class TestChangeContractError(ValueError):
    """Invalid or unreadable test-change contract input."""

    def __init__(self, status: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@dataclass(frozen=True)
class TestChangeEvaluation:
    """Result of comparing the live test tree with its frozen baseline."""

    success: bool
    status: str
    mode: str
    allow_test_changes: bool
    modified: bool
    test_source_modified: bool
    baseline_tree_sha256: str
    current_tree_sha256: str
    standard_test_roots: tuple[str, ...]
    added: tuple[dict[str, str], ...]
    changed: tuple[dict[str, str], ...]
    deleted: tuple[dict[str, str], ...]
    verification_config_modified: bool
    baseline_verification_config_tree_sha256: str
    current_verification_config_tree_sha256: str
    verification_config_added: tuple[dict[str, str], ...]
    verification_config_changed: tuple[dict[str, str], ...]
    verification_config_deleted: tuple[dict[str, str], ...]
    baseline_test_strength: dict[str, Any]
    current_test_strength: dict[str, Any]
    test_strength_violations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": TEST_CHANGE_CONTRACT_VERSION,
            "success": self.success,
            "status": self.status,
            "reason": "" if self.success else self.status,
            "mode": self.mode,
            "allow_test_changes": self.allow_test_changes,
            "modified": self.modified,
            "test_source_modified": self.test_source_modified,
            "baseline_tree_sha256": self.baseline_tree_sha256,
            "current_tree_sha256": self.current_tree_sha256,
            "standard_test_roots": list(self.standard_test_roots),
            # Do not truncate these lists: this object is the authoritative
            # audit record when test changes were explicitly enabled.
            "added": [dict(item) for item in self.added],
            "changed": [dict(item) for item in self.changed],
            "deleted": [dict(item) for item in self.deleted],
            "change_count": len(self.added) + len(self.changed) + len(self.deleted),
            "verification_config_modified": self.verification_config_modified,
            "baseline_verification_config_tree_sha256": (
                self.baseline_verification_config_tree_sha256
            ),
            "current_verification_config_tree_sha256": (
                self.current_verification_config_tree_sha256
            ),
            # These lists are also authoritative and intentionally untruncated.
            "verification_config_added": [
                dict(item) for item in self.verification_config_added
            ],
            "verification_config_changed": [
                dict(item) for item in self.verification_config_changed
            ],
            "verification_config_deleted": [
                dict(item) for item in self.verification_config_deleted
            ],
            "verification_config_change_count": (
                len(self.verification_config_added)
                + len(self.verification_config_changed)
                + len(self.verification_config_deleted)
            ),
            "baseline_test_strength": dict(self.baseline_test_strength),
            "current_test_strength": dict(self.current_test_strength),
            "test_strength_violations": [
                dict(item) for item in self.test_strength_violations
            ],
        }


def capture_test_change_contract(
    project_root: str | Path,
    *,
    declared_test_files: str | Iterable[str] | None = None,
    allow_test_changes: bool = False,
) -> dict[str, Any]:
    """Freeze the Java test tree and controller policy for checkpoint ``c000``.

    ``declared_test_files`` accepts the delivery schema's semicolon-separated
    form or an iterable.  A declared path is always tracked, even when it lives
    outside a conventional test source set.
    """
    if not isinstance(allow_test_changes, bool):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "allow_test_changes must be a boolean captured by the controller",
            allow_test_changes=allow_test_changes,
        )
    root = _project_root(project_root)
    declared = _normalize_declared_test_files(declared_test_files)
    layout = _source_layout(root)
    snapshot = _snapshot_test_tree(
        root,
        declared,
        require_declared=True,
        layout=layout,
    )
    verification_config = _snapshot_verification_config(root)
    mode = "api_migration" if allow_test_changes else "immutable"
    semantic_audit = _snapshot_test_semantics(root, snapshot["files"])
    return {
        "contract_version": TEST_CHANGE_CONTRACT_VERSION,
        "mode": mode,
        "allow_test_changes": allow_test_changes,
        "declared_test_files": declared,
        "standard_test_roots": snapshot["standard_test_roots"],
        "configured_test_files": list(layout.test_files),
        "configured_test_globs": list(layout.test_globs),
        "configured_test_glob_excludes": list(layout.test_glob_excludes),
        "files": snapshot["files"],
        "tree_sha256": snapshot["tree_sha256"],
        "semantic_audit": semantic_audit,
        "verification_config_files": verification_config["files"],
        "verification_config_tree_sha256": verification_config["tree_sha256"],
    }


def evaluate_test_change_contract(
    project_root: str | Path,
    baseline_contract: Mapping[str, Any],
) -> TestChangeEvaluation:
    """Audit the live tree using only the policy frozen in ``baseline_contract``."""
    root = _project_root(project_root)
    baseline = _validated_baseline(baseline_contract)
    current = _snapshot_test_tree(
        root,
        baseline["declared_test_files"],
        require_declared=False,
        frozen_test_roots=baseline["standard_test_roots"],
        frozen_test_files=baseline["configured_test_files"],
        frozen_test_globs=baseline["configured_test_globs"],
        frozen_test_glob_excludes=baseline["configured_test_glob_excludes"],
    )
    before: dict[str, str] = baseline["files"]
    after: dict[str, str] = current["files"]
    added, changed, deleted = _manifest_delta(before, after)
    test_source_modified = bool(added or changed or deleted)
    current_semantic_audit = _snapshot_test_semantics(
        root,
        current["files"],
        reusable_audit=baseline["semantic_audit"],
        reusable_manifest=baseline["files"],
    )
    current_verification_config = _snapshot_verification_config(root)
    verification_config_added, verification_config_changed, verification_config_deleted = (
        _manifest_delta(
            baseline["verification_config_files"],
            current_verification_config["files"],
        )
    )
    verification_config_modified = bool(
        verification_config_added
        or verification_config_changed
        or verification_config_deleted
    )
    modified = test_source_modified or verification_config_modified
    mode = baseline["mode"]
    allowed = mode == "api_migration"
    baseline_test_deleted = bool(deleted)
    strength_violations = (
        _test_strength_violations(
            baseline["semantic_audit"],
            current_semantic_audit,
            added=added,
            changed=changed,
            deleted=deleted,
        )
        if allowed and test_source_modified
        else ()
    )
    success = (
        not verification_config_modified
        and not baseline_test_deleted
        and not strength_violations
        and (allowed or not test_source_modified)
    )
    if verification_config_modified:
        status = "VERIFICATION_CONFIG_MODIFIED"
    elif not test_source_modified:
        status = "TEST_SOURCE_UNCHANGED"
    elif baseline_test_deleted and allowed:
        status = "TEST_SOURCE_DELETED"
    elif allowed and strength_violations:
        status = "TEST_SOURCE_MIGRATION_REJECTED"
    elif allowed:
        status = "TEST_SOURCE_API_MIGRATION_ALLOWED"
    else:
        status = "TEST_SOURCE_MODIFIED"
    return TestChangeEvaluation(
        success=success,
        status=status,
        mode=mode,
        allow_test_changes=allowed,
        modified=modified,
        test_source_modified=test_source_modified,
        baseline_tree_sha256=baseline["tree_sha256"],
        current_tree_sha256=current["tree_sha256"],
        standard_test_roots=tuple(current["standard_test_roots"]),
        added=added,
        changed=changed,
        deleted=deleted,
        verification_config_modified=verification_config_modified,
        baseline_verification_config_tree_sha256=baseline[
            "verification_config_tree_sha256"
        ],
        current_verification_config_tree_sha256=current_verification_config[
            "tree_sha256"
        ],
        verification_config_added=verification_config_added,
        verification_config_changed=verification_config_changed,
        verification_config_deleted=verification_config_deleted,
        baseline_test_strength=_semantic_audit_summary(baseline["semantic_audit"]),
        current_test_strength=_semantic_audit_summary(current_semantic_audit),
        test_strength_violations=strength_violations,
    )


def clean_transient_test_artifacts(
    project_root: str | Path,
    baseline_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove newly generated test-runtime files before final verification.

    This is intentionally not a project-name or filename allowlist. A file is
    removable only when all of these source-derived conditions hold:

    * it is under the same frozen Java test-input scope used by c000;
    * it did not exist in the frozen test manifest;
    * Git proves that it is untracked or ignored, never tracked;
    * it is a regular, non-symlink file with a conventional runtime-artifact
      suffix.

    The final build and both test stages run after removal. Consequently an
    authored fixture cannot use this lane to make a candidate pass: it is gone
    before behavior verification. Tracked resources and ordinary added test
    resources remain visible to the strict test-change audit.
    """
    root = _project_root(project_root)
    baseline = _validated_baseline(baseline_contract)
    current = _snapshot_test_tree(
        root,
        baseline["declared_test_files"],
        require_declared=False,
        frozen_test_roots=baseline["standard_test_roots"],
        frozen_test_files=baseline["configured_test_files"],
        frozen_test_globs=baseline["configured_test_globs"],
        frozen_test_glob_excludes=baseline["configured_test_glob_excludes"],
    )
    added_paths = sorted(set(current["files"]) - set(baseline["files"]))
    candidates: list[dict[str, Any]] = []
    for relative in added_paths:
        if relative.endswith("#target"):
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        if Path(relative).suffix.casefold() not in _TRANSIENT_TEST_ARTIFACT_SUFFIXES:
            continue
        disposition = _git_untracked_disposition(root, relative)
        if not disposition:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise TestChangeContractError(
                "TRANSIENT_TEST_ARTIFACT_CLEANUP_FAILED",
                f"cannot inspect generated test artifact {relative}: {exc}",
                path=relative,
            ) from exc
        candidates.append(
            {
                "path": relative,
                "sha256": current["files"][relative],
                "bytes": size,
                "git_disposition": disposition,
            }
        )
    if len(candidates) > _MAX_TRANSIENT_TEST_ARTIFACTS:
        raise TestChangeContractError(
            "TRANSIENT_TEST_ARTIFACT_LIMIT_EXCEEDED",
            "too many generated test artifacts to clean safely",
            count=len(candidates),
            limit=_MAX_TRANSIENT_TEST_ARTIFACTS,
        )
    for item in candidates:
        path = root / str(item["path"])
        try:
            path.unlink()
        except OSError as exc:
            raise TestChangeContractError(
                "TRANSIENT_TEST_ARTIFACT_CLEANUP_FAILED",
                f"cannot remove generated test artifact {item['path']}: {exc}",
                path=item["path"],
            ) from exc
    return {
        "policy": TRANSIENT_TEST_ARTIFACT_POLICY,
        "removed_count": len(candidates),
        "removed_bytes": sum(int(item["bytes"]) for item in candidates),
        "removed": candidates,
    }


def is_standard_java_test_path(path: str | Path) -> bool:
    """Return whether a relative path belongs to a conventional Java test root."""
    return standard_test_root(path) is not None


def discover_java_test_source_roots(project_root: str | Path) -> tuple[str, ...]:
    """Discover configured Java test roots without executing the build.

    Conventional ``src/test``-style roots remain covered by
    :func:`is_standard_java_test_path`.  This function closes the important
    product gap for custom Maven, Gradle, Ant, and Bazel test source sets.  It
    reads only the same build descriptors that the c000 verification contract
    freezes, so neither dataset evidence nor test contents can define a root.
    Unresolved build expressions are ignored instead of being guessed.
    """
    return _source_layout(_project_root(project_root)).test_roots


def is_java_test_source_path(
    path: str | Path,
    *,
    project_root: str | Path,
    configured_test_roots: Iterable[str] | None = None,
) -> bool:
    """Return whether ``path`` belongs to a conventional or configured test root."""
    root = _project_root(project_root)
    layout = _source_layout(root)
    if configured_test_roots is not None:
        layout = JavaSourceLayout(
            project_root=root,
            test_roots=tuple(sorted(set(configured_test_roots))),
            test_files=layout.test_files,
            test_globs=layout.test_globs,
            test_glob_excludes=layout.test_glob_excludes,
            verification_files=layout.verification_files,
            auxiliary_roots=layout.auxiliary_roots,
        )
    return layout.is_test_path(path)


def is_java_verification_config_path(path: str | Path) -> bool:
    """Return whether a project-relative path controls Java verification.

    The rules describe build-system conventions, never dataset rows. They cover
    multi-module descriptors as well as the configuration roots used by Maven,
    Gradle, Ant/NetBeans, and Bazel.
    """
    return _layout_is_verification_config_path(path)


def _source_layout(project_root: Path) -> JavaSourceLayout:
    try:
        return discover_java_source_layout(project_root)
    except JavaSourceLayoutError as exc:
        raise TestChangeContractError(exc.status, exc.message, **exc.details) from exc


def _project_root(project_root: str | Path) -> Path:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise TestChangeContractError(
            "TEST_TREE_UNREADABLE",
            f"project root is not a readable directory: {root}",
            project_root=str(root),
        )
    return root


def _git_untracked_disposition(root: Path, relative: str) -> str:
    """Return ``untracked``/``ignored`` only when Git proves that state."""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tracked.returncode == 0:
        return ""
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ignored.returncode == 0:
        return "ignored"
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            relative,
        ],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    expected = b"?? " + os.fsencode(relative) + b"\0"
    return "untracked" if status.returncode == 0 and status.stdout == expected else ""


def _normalize_declared_test_files(
    declared: str | Iterable[str] | None,
) -> list[str]:
    if declared is None:
        values: list[str] = []
    elif isinstance(declared, str):
        values = [declared]
    else:
        try:
            values = [str(item) for item in declared]
        except TypeError as exc:
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                "declared_test_files must be a string or iterable of strings",
            ) from exc

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw_path in value.split(";"):
            candidate = raw_path.strip().replace("\\", "/")
            while candidate.startswith("./"):
                candidate = candidate[2:]
            if not candidate:
                continue
            path = Path(candidate)
            if (
                path.is_absolute()
                or _WINDOWS_ABSOLUTE.match(candidate)
                or ".." in path.parts
            ):
                raise TestChangeContractError(
                    "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                    f"declared test file must stay inside the project: {raw_path}",
                    test_file=raw_path,
                )
            canonical = path.as_posix()
            if canonical not in seen:
                seen.add(canonical)
                normalized.append(canonical)
    return sorted(normalized)


def _snapshot_test_tree(
    root: Path,
    declared_test_files: Iterable[str],
    *,
    require_declared: bool,
    layout: JavaSourceLayout | None = None,
    frozen_test_roots: Iterable[str] = (),
    frozen_test_files: Iterable[str] = (),
    frozen_test_globs: Iterable[str] = (),
    frozen_test_glob_excludes: Iterable[str] = (),
) -> dict[str, Any]:
    files: dict[str, str] = {}
    roots: set[str] = set()
    live_layout = layout or _source_layout(root)
    effective_layout = JavaSourceLayout(
        project_root=root,
        test_roots=tuple(sorted({*live_layout.test_roots, *(str(item) for item in frozen_test_roots)})),
        test_files=tuple(sorted({*live_layout.test_files, *(str(item) for item in frozen_test_files)})),
        test_globs=tuple(sorted({*live_layout.test_globs, *(str(item) for item in frozen_test_globs)})),
        test_glob_excludes=tuple(sorted({
            *live_layout.test_glob_excludes,
            *(str(item) for item in frozen_test_glob_excludes),
        })),
        verification_files=live_layout.verification_files,
        auxiliary_roots=live_layout.auxiliary_roots,
    )
    try:
        _walk_test_inputs(root, root, "", effective_layout, files, roots, set())
    except OSError as exc:
        raise TestChangeContractError(
            "TEST_TREE_UNREADABLE",
            f"cannot read Java test tree under {root}: {exc}",
            project_root=str(root),
        ) from exc

    for relative in declared_test_files:
        path = root / relative
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                f"declared test file escapes the project: {relative}",
                test_file=relative,
            ) from exc
        if not path.is_file():
            if require_declared:
                raise TestChangeContractError(
                    "TEST_FILE_MISSING",
                    f"declared test file is missing at baseline capture: {relative}",
                    test_file=relative,
                )
            continue
        files[relative] = _sha256_path(path)

    ordered_files = {path: files[path] for path in sorted(files)}
    return {
        "standard_test_roots": sorted(roots),
        "files": ordered_files,
        "tree_sha256": _tree_sha256(ordered_files),
    }


def _snapshot_test_semantics(
    root: Path,
    file_manifest: Mapping[str, str],
    *,
    reusable_audit: Mapping[str, Any] | None = None,
    reusable_manifest: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a language-level test-strength snapshot without running tests.

    This deliberately recognizes framework concepts rather than project- or
    smell-specific symbols. The project-full test gate remains authoritative
    for behavior; this audit only prevents an authorized API migration from
    weakening the verification inputs before that gate runs.
    """
    files: dict[str, dict[str, Any]] = {}
    reusable_files = (
        reusable_audit.get("files", {})
        if isinstance(reusable_audit, Mapping)
        else {}
    )
    for relative in sorted(file_manifest):
        if relative.endswith("#target") or not _is_test_source_identity(relative):
            continue
        if _can_reuse_test_source_audit(
            relative,
            current_manifest=file_manifest,
            reusable_manifest=reusable_manifest,
            reusable_files=reusable_files,
        ):
            files[relative] = dict(reusable_files[relative])
            continue
        path = root / relative
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise TestChangeContractError(
                "TEST_SOURCE_AUDIT_FAILED",
                f"cannot audit test source {relative}: {exc}",
                path=relative,
            ) from exc
        files[relative] = {
            "manifest_sha256": file_manifest[relative],
            **_test_source_strength(source),
        }
    totals = _test_strength_totals(files)
    payload = {
        "audit_version": TEST_SEMANTIC_AUDIT_VERSION,
        "files": files,
        "totals": totals,
    }
    payload["audit_sha256"] = _semantic_audit_sha256(payload)
    return payload


def _can_reuse_test_source_audit(
    relative: str,
    *,
    current_manifest: Mapping[str, str],
    reusable_manifest: Mapping[str, str] | None,
    reusable_files: Mapping[str, Any],
) -> bool:
    """Return whether a c000 source audit is valid for the live manifest.

    A source symlink has two frozen identities: the link itself and its
    ``#target`` content. Both must match before reusing semantic metrics.
    """
    if not isinstance(reusable_manifest, Mapping):
        return False
    frozen_metrics = reusable_files.get(relative)
    if not isinstance(frozen_metrics, Mapping):
        return False
    if frozen_metrics.get("manifest_sha256") != current_manifest.get(relative):
        return False
    for identity in (relative, f"{relative}#target"):
        if reusable_manifest.get(identity) != current_manifest.get(identity):
            return False
    return True


def _is_test_source_identity(relative: str) -> bool:
    return _test_source_path_for_identity(relative) is not None


def _test_source_path_for_identity(relative: str) -> str | None:
    source_path = relative.removesuffix("#target")
    if Path(source_path).suffix.casefold() not in _TEST_SOURCE_SUFFIXES:
        return None
    return source_path


def _test_source_strength(source: str) -> dict[str, int]:
    code = _mask_comments_and_literals(source)
    return {
        "test_methods": len(_TEST_ANNOTATION.findall(code))
        + len(_JUNIT3_TEST_METHOD.findall(code)),
        "assertions": len(_ASSERTION_CALL.findall(code))
        + len(_ASSERT_STATEMENT.findall(code)),
        "disabled_or_ignored": len(_DISABLED_SIGNAL.findall(code)),
        "assumption_skips": len(_ASSUMPTION_SKIP_SIGNAL.findall(code)),
    }


def _mask_comments_and_literals(source: str) -> str:
    """Mask comments and literals while preserving code positions/newlines."""
    masked: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                masked.extend((" ", " "))
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                masked.extend((" ", " "))
                index += 2
                state = "block_comment"
                continue
            if source.startswith('"""', index) or source.startswith("'''", index):
                quote = source[index : index + 3]
                masked.extend((" ", " ", " "))
                state = "literal"
                index += 3
                continue
            if char in {'"', "'"}:
                masked.append(" ")
                quote = char
                state = "literal"
                index += 1
                continue
            masked.append(char)
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                masked.append("\n")
                state = "code"
            else:
                masked.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                masked.extend((" ", " "))
                index += 2
                state = "code"
            else:
                masked.append("\n" if char == "\n" else " ")
                index += 1
            continue
        # string or character literal
        if len(quote) == 3 and source.startswith(quote, index):
            masked.extend((" ", " ", " "))
            index += 3
            state = "code"
            continue
        if char == "\\" and next_char:
            masked.extend((" ", "\n" if next_char == "\n" else " "))
            index += 2
            continue
        if char == quote:
            masked.append(" ")
            index += 1
            state = "code"
            continue
        masked.append("\n" if char == "\n" else " ")
        index += 1
    return "".join(masked)


def _test_strength_totals(files: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    keys = ("test_methods", "assertions", "disabled_or_ignored", "assumption_skips")
    return {
        "source_files": len(files),
        **{
            key: sum(int(metrics.get(key, 0)) for metrics in files.values())
            for key in keys
        },
    }


def _semantic_audit_sha256(payload: Mapping[str, Any]) -> str:
    canonical = {
        "audit_version": payload.get("audit_version"),
        "files": payload.get("files"),
        "totals": payload.get("totals"),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _semantic_audit_summary(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "audit_version": audit["audit_version"],
        **dict(audit["totals"]),
        "audit_sha256": audit["audit_sha256"],
    }


def _test_strength_violations(
    baseline_audit: Mapping[str, Any],
    current_audit: Mapping[str, Any],
    *,
    added: Iterable[Mapping[str, str]],
    changed: Iterable[Mapping[str, str]],
    deleted: Iterable[Mapping[str, str]],
) -> tuple[dict[str, Any], ...]:
    """Reject changes outside the narrow, strength-preserving API lane."""
    violations: list[dict[str, Any]] = []
    delta = [*added, *changed, *deleted]
    for item in delta:
        path = str(item.get("path") or "")
        if _test_source_path_for_identity(path) is None:
            violations.append(
                {
                    "reason": "NON_SOURCE_TEST_INPUT_MODIFIED",
                    "path": path,
                }
            )

    before_files = baseline_audit["files"]
    after_files = current_audit["files"]
    source_delta_paths = sorted(
        {
            source_path
            for item in [*added, *changed]
            if (
                source_path := _test_source_path_for_identity(
                    str(item.get("path") or "")
                )
            )
        }
    )
    for path in source_delta_paths:
        before = before_files.get(path, {})
        after = after_files.get(path)
        if not isinstance(after, Mapping):
            violations.append(
                {
                    "reason": "TEST_SOURCE_AUDIT_MISSING",
                    "path": path,
                }
            )
            continue
        for metric, reason in (
            ("test_methods", "TEST_METHOD_COUNT_DECREASED"),
            ("assertions", "ASSERTION_COUNT_DECREASED"),
        ):
            baseline_value = int(before.get(metric, 0))
            current_value = int(after.get(metric, 0))
            if current_value < baseline_value:
                violations.append(
                    {
                        "reason": reason,
                        "path": path,
                        "baseline": baseline_value,
                        "current": current_value,
                    }
                )
        for metric, reason in (
            ("disabled_or_ignored", "DISABLED_OR_IGNORED_ADDED"),
            ("assumption_skips", "ASSUMPTION_SKIP_ADDED"),
        ):
            baseline_value = int(before.get(metric, 0))
            current_value = int(after.get(metric, 0))
            if current_value > baseline_value:
                violations.append(
                    {
                        "reason": reason,
                        "path": path,
                        "baseline": baseline_value,
                        "current": current_value,
                    }
                )
    return tuple(violations)


def _walk_test_inputs(
    project_root: Path,
    physical: Path,
    logical_prefix: str,
    layout: JavaSourceLayout,
    files: dict[str, str],
    roots: set[str],
    active_directories: set[Path],
) -> None:
    """Walk test inputs while preserving symlink names in the manifest."""
    resolved_directory = physical.resolve(strict=True)
    try:
        resolved_directory.relative_to(project_root)
    except ValueError as exc:
        raise TestChangeContractError(
            "TEST_TREE_UNREADABLE",
            f"test source directory link escapes the project: {logical_prefix}",
            path=logical_prefix,
            resolved_path=str(resolved_directory),
        ) from exc
    if resolved_directory in active_directories:
        return
    active_directories.add(resolved_directory)
    try:
        for child in sorted(physical.iterdir(), key=lambda item: item.name):
            if child.name.casefold() in _IGNORED_DIRECTORY_NAMES:
                continue
            relative = f"{logical_prefix}/{child.name}".strip("/")
            selected = layout.is_test_path(relative)
            descendant = layout.contains_test_descendant(relative)
            if child.is_symlink():
                try:
                    target = child.resolve(strict=True)
                    target.relative_to(project_root)
                except (OSError, ValueError) as exc:
                    if selected or descendant:
                        raise TestChangeContractError(
                            "TEST_TREE_UNREADABLE",
                            f"test source link escapes the project or is broken: {relative}",
                            path=relative,
                        ) from exc
                    continue
                if selected or descendant:
                    files[relative] = _sha256_path(child)
                    test_root = standard_test_root(relative) or _matching_configured_test_root(
                        relative, layout.test_roots
                    )
                    if test_root:
                        roots.add(test_root)
                    if target.is_dir():
                        _walk_test_inputs(
                            project_root,
                            target,
                            relative,
                            layout,
                            files,
                            roots,
                            active_directories,
                        )
                    elif selected:
                        # The link digest records its identity; the target digest
                        # makes an internal target edit visible as well.
                        files[f"{relative}#target"] = _sha256_regular_file(target)
                continue
            if child.is_dir():
                _walk_test_inputs(
                    project_root,
                    child,
                    relative,
                    layout,
                    files,
                    roots,
                    active_directories,
                )
            elif child.is_file() and selected:
                test_root = standard_test_root(relative) or _matching_configured_test_root(
                    relative, layout.test_roots
                )
                if test_root:
                    roots.add(test_root)
                files[relative] = _sha256_path(child)
    finally:
        active_directories.remove(resolved_directory)


def _snapshot_verification_config(root: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    try:
        for relative in discover_java_verification_files(root):
            path = root / relative
            files[relative] = _sha256_path(path)
    except (OSError, JavaSourceLayoutError) as exc:
        message = exc.message if isinstance(exc, JavaSourceLayoutError) else str(exc)
        raise TestChangeContractError(
            "VERIFICATION_CONFIG_UNREADABLE",
            f"cannot read Java verification configuration under {root}: {message}",
            project_root=str(root),
        ) from exc
    ordered_files = {path: files[path] for path in sorted(files)}
    return {
        "files": ordered_files,
        "tree_sha256": _tree_sha256(ordered_files),
    }


def _manifest_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> tuple[
    tuple[dict[str, str], ...],
    tuple[dict[str, str], ...],
    tuple[dict[str, str], ...],
]:
    added = tuple(
        {
            "path": path,
            "before_sha256": "",
            "after_sha256": after[path],
        }
        for path in sorted(set(after) - set(before))
    )
    changed = tuple(
        {
            "path": path,
            "before_sha256": before[path],
            "after_sha256": after[path],
        }
        for path in sorted(set(before).intersection(after))
        if before[path] != after[path]
    )
    deleted = tuple(
        {
            "path": path,
            "before_sha256": before[path],
            "after_sha256": "",
        }
        for path in sorted(set(before) - set(after))
    )
    return added, changed, deleted


def _matching_configured_test_root(
    relative: str,
    configured_roots: Iterable[str],
) -> str | None:
    normalized = str(relative).replace("\\", "/").strip("/")
    matches = [
        str(root).replace("\\", "/").strip("/")
        for root in configured_roots
        if normalized == str(root).replace("\\", "/").strip("/")
        or normalized.startswith(str(root).replace("\\", "/").strip("/") + "/")
    ]
    return max(matches, key=len) if matches else None


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return digest.hexdigest()
    return _sha256_regular_file(path)


def _sha256_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TestChangeContractError(
            "TEST_TREE_UNREADABLE",
            f"cannot hash test file {path}: {exc}",
            path=str(path),
        ) from exc
    return digest.hexdigest()


def _tree_sha256(files: Mapping[str, str]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_hash_manifest(
    value: Any,
    *,
    label: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            f"baseline {label} manifest must be an object",
        )
    validated: dict[str, str] = {}
    for raw_path, raw_hash in value.items():
        if not isinstance(raw_path, str) or not isinstance(raw_hash, str):
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                f"baseline {label} manifest must map paths to SHA256 strings",
            )
        normalized_path = _normalize_declared_test_files([raw_path])
        if normalized_path != [raw_path] or not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                f"invalid baseline {label} manifest entry: {raw_path}",
                path=raw_path,
            )
        validated[raw_path] = raw_hash
    return validated


def _validated_semantic_audit(
    value: Any,
    *,
    file_manifest: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit must be an object",
        )
    if value.get("audit_version") != TEST_SEMANTIC_AUDIT_VERSION:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit version is invalid",
        )
    raw_files = value.get("files")
    raw_totals = value.get("totals")
    audit_sha256 = value.get("audit_sha256")
    if not isinstance(raw_files, Mapping) or not isinstance(raw_totals, Mapping):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit files and totals must be objects",
        )
    expected_paths = {
        path
        for path in file_manifest
        if not path.endswith("#target") and _is_test_source_identity(path)
    }
    if set(raw_files) != expected_paths:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit does not cover the frozen source manifest",
        )
    validated_files: dict[str, dict[str, Any]] = {}
    metric_keys = (
        "test_methods",
        "assertions",
        "disabled_or_ignored",
        "assumption_skips",
    )
    for path in sorted(expected_paths):
        metrics = raw_files[path]
        if not isinstance(metrics, Mapping):
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                f"baseline semantic test audit entry is invalid: {path}",
                path=path,
            )
        if metrics.get("manifest_sha256") != file_manifest[path]:
            raise TestChangeContractError(
                "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                f"baseline semantic audit digest does not match test manifest: {path}",
                path=path,
            )
        validated_metrics: dict[str, Any] = {
            "manifest_sha256": file_manifest[path]
        }
        for key in metric_keys:
            raw_metric = metrics.get(key)
            if isinstance(raw_metric, bool) or not isinstance(raw_metric, int) or raw_metric < 0:
                raise TestChangeContractError(
                    "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
                    f"baseline semantic audit metric is invalid: {path}:{key}",
                    path=path,
                    metric=key,
                )
            validated_metrics[key] = raw_metric
        validated_files[path] = validated_metrics
    computed_totals = _test_strength_totals(validated_files)
    if dict(raw_totals) != computed_totals:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit totals are inconsistent",
        )
    validated = {
        "audit_version": TEST_SEMANTIC_AUDIT_VERSION,
        "files": validated_files,
        "totals": computed_totals,
    }
    computed_digest = _semantic_audit_sha256(validated)
    if not isinstance(audit_sha256, str) or audit_sha256 != computed_digest:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline semantic test audit digest is inconsistent",
        )
    validated["audit_sha256"] = computed_digest
    return validated


def _validated_baseline(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline test-change contract must be an object",
        )
    version = contract.get("contract_version")
    if version != TEST_CHANGE_CONTRACT_VERSION:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_VERSION_MISMATCH",
            "test-change contract version changed; recapture checkpoint c000",
            expected=TEST_CHANGE_CONTRACT_VERSION,
            actual=version,
        )
    mode = contract.get("mode")
    allowed = contract.get("allow_test_changes")
    declared = contract.get("declared_test_files")
    files = contract.get("files")
    tree_sha256 = contract.get("tree_sha256")
    verification_config_files = contract.get("verification_config_files")
    verification_config_tree_sha256 = contract.get(
        "verification_config_tree_sha256"
    )
    standard_test_roots = contract.get("standard_test_roots")
    configured_test_files = contract.get("configured_test_files")
    configured_test_globs = contract.get("configured_test_globs")
    configured_test_glob_excludes = contract.get("configured_test_glob_excludes")
    semantic_audit = contract.get("semantic_audit")
    if (
        mode not in _TEST_CHANGE_MODES
        or not isinstance(allowed, bool)
        or allowed != (mode == "api_migration")
        or not isinstance(declared, list)
        or not isinstance(files, Mapping)
        or not isinstance(standard_test_roots, list)
        or not isinstance(configured_test_files, list)
        or not isinstance(configured_test_globs, list)
        or not isinstance(configured_test_glob_excludes, list)
    ):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline test-change contract has invalid policy, declarations, or files",
        )
    normalized_declared = _normalize_declared_test_files(declared)
    if normalized_declared != declared:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline declared test files are not canonical",
        )
    normalized_test_roots = _normalize_declared_test_files(standard_test_roots)
    if normalized_test_roots != standard_test_roots:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline test source roots are not canonical",
        )
    normalized_test_files = _normalize_declared_test_files(configured_test_files)
    normalized_test_globs = _normalize_declared_test_files(configured_test_globs)
    normalized_test_glob_excludes = _normalize_declared_test_files(
        configured_test_glob_excludes
    )
    if (
        normalized_test_files != configured_test_files
        or normalized_test_globs != configured_test_globs
        or normalized_test_glob_excludes != configured_test_glob_excludes
    ):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline configured Java source layout is not canonical",
        )
    validated_files = _validated_hash_manifest(files, label="test file")
    computed_tree = _tree_sha256(validated_files)
    if not isinstance(tree_sha256, str) or tree_sha256 != computed_tree:
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline test tree digest does not match its file manifest",
        )
    validated_semantic_audit = _validated_semantic_audit(
        semantic_audit,
        file_manifest=validated_files,
    )
    validated_verification_config = _validated_hash_manifest(
        verification_config_files,
        label="verification config",
    )
    # Applied Gradle scripts and imported Ant files need not have conventional
    # names. Their authority comes from the controller-owned c000 seal; this
    # local digest check validates internal consistency, not authenticity.
    computed_verification_config_tree = _tree_sha256(
        validated_verification_config
    )
    if (
        not isinstance(verification_config_tree_sha256, str)
        or verification_config_tree_sha256 != computed_verification_config_tree
    ):
        raise TestChangeContractError(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            "baseline verification config digest does not match its file manifest",
        )
    return {
        "mode": mode,
        "allow_test_changes": allowed,
        "declared_test_files": normalized_declared,
        "files": validated_files,
        "tree_sha256": tree_sha256,
        "semantic_audit": validated_semantic_audit,
        "standard_test_roots": normalized_test_roots,
        "configured_test_files": normalized_test_files,
        "configured_test_globs": normalized_test_globs,
        "configured_test_glob_excludes": normalized_test_glob_excludes,
        "verification_config_files": validated_verification_config,
        "verification_config_tree_sha256": verification_config_tree_sha256,
    }
