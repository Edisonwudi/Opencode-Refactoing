"""Bounded file scope for Guard capture/verification.

The Guard is given target files by its caller.  It does not discover targets
and this module deliberately does not enumerate source files.  The base parse
scope contains only the frozen targets.  Production Java paths changed since
the baseline are retained as metadata so an individual smell Guard may select
an exact, bounded relation or changed-method scope.

Renames contribute both their baseline and current paths to that metadata.
They do not automatically widen the parse scope.
"""

from __future__ import annotations

import subprocess
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .java.source_layout import JavaSourceLayout, standard_test_root


_NON_PRODUCTION_COMPONENTS = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        "build",
        "dist",
        "node_modules",
        "out",
        "target",
    }
)
MAX_GUARD_ANALYSIS_FILES = 32
MAX_GUARD_ANALYSIS_BYTES = 8 * 1024 * 1024
_HUNK_HEADER = re.compile(
    rb"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)


class GuardScopeError(ValueError):
    """Fail-closed error while constructing or reading a Guard scope."""

    def __init__(self, status: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@dataclass(frozen=True)
class GuardVerificationScope:
    """Immutable set of files that one Guard verification may analyze."""

    changed_files: tuple[str, ...]
    changed_production_files: tuple[str, ...]
    target_files: tuple[str, ...]
    analysis_files: tuple[str, ...]
    baseline_commit: str = ""
    # Current-worktree line spans touched by the refactoring diff.  These are
    # used only by predicates such as Feature Envy that must inspect every
    # changed method without treating every method in a changed file as new.
    changed_line_ranges: tuple[tuple[str, int, int], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "changed_files",
            "changed_production_files",
            "target_files",
            "analysis_files",
        ):
            value = getattr(self, name)
            if value != tuple(sorted(set(value))):
                raise ValueError(f"{name} must be sorted and unique")
        if not set(self.changed_production_files).issubset(self.changed_files):
            raise ValueError(
                "changed_production_files must be a subset of changed_files"
            )
        expected = self.target_files
        if self.analysis_files != expected:
            raise ValueError(
                "analysis_files is the target-only base; smell resolvers add exact dependencies"
            )
        if self.changed_line_ranges != tuple(sorted(set(self.changed_line_ranges))):
            raise ValueError("changed_line_ranges must be sorted and unique")
        for path, start, end in self.changed_line_ranges:
            if path not in self.changed_production_files or start < 1 or end < start:
                raise ValueError("changed_line_ranges must reference valid production spans")


@dataclass(frozen=True)
class _GitContext:
    project_root: Path
    repository_root: Path
    project_prefix: str


def build_guard_verification_scope(
    project_root: str | Path,
    baseline_commit: str,
    target_files: Iterable[str | Path],
    *,
    source_layout: JavaSourceLayout | None = None,
) -> GuardVerificationScope:
    """Build a target-bounded, rename-aware Java verification scope.

    ``changed_files`` contains both sides of a detected rename.  Deleted files
    therefore remain available through :func:`read_baseline_bytes`, while the
    destination is available through :func:`read_current_bytes`.

    No source directory is scanned here.  Git provides the exact changed-path
    set and the caller provides the target set.  ``source_layout`` is the
    shared static build-descriptor contract used to exclude test sources.
    """

    context = _git_context(project_root)
    resolved_commit = _resolve_commit(context, baseline_commit)
    layout = source_layout
    if layout is not None and layout.project_root.resolve() != context.project_root:
        raise GuardScopeError(
            "SOURCE_LAYOUT_ROOT_MISMATCH",
            "Java source layout belongs to a different project root",
            expected_root=str(context.project_root),
            actual_root=str(layout.project_root),
        )

    normalized_targets = tuple(
        sorted(
            {
                _normalize_relative_path(context.project_root, item)
                for item in target_files
            }
        )
    )
    if not normalized_targets:
        raise GuardScopeError(
            "TARGET_CONTEXT_INCOMPLETE",
            "Guard verification requires at least one target file",
        )
    invalid_targets = tuple(
        path for path in normalized_targets if not _is_production_java(path, layout)
    )
    if invalid_targets:
        raise GuardScopeError(
            "TARGET_NOT_PRODUCTION_JAVA",
            "Guard target files must be production Java sources",
            paths=invalid_targets,
        )

    changed_files = _changed_paths(context, resolved_commit)
    changed_production_files = tuple(
        path for path in changed_files if _is_production_java(path, layout)
    )
    analysis_files = normalized_targets
    validate_guard_analysis_scope(context.project_root, analysis_files)
    return GuardVerificationScope(
        changed_files=changed_files,
        changed_production_files=changed_production_files,
        target_files=normalized_targets,
        analysis_files=analysis_files,
        baseline_commit=resolved_commit,
    )


def build_changed_line_ranges(
    project_root: str | Path,
    baseline_commit: str,
    production_files: Iterable[str | Path],
) -> tuple[tuple[str, int, int], ...]:
    """Resolve diff spans only for files selected by one smell Guard."""
    context = _git_context(project_root)
    resolved_commit = _resolve_commit(context, baseline_commit)
    normalized = tuple(
        sorted(
            {
                _normalize_relative_path(context.project_root, item)
                for item in production_files
            }
        )
    )
    invalid = tuple(path for path in normalized if not _is_production_java(path, None))
    if invalid:
        raise GuardScopeError(
            "SCOPED_SOURCE_NOT_PRODUCTION_JAVA",
            "Changed-line scope accepts production Java files only",
            paths=invalid,
        )
    validate_guard_analysis_scope(context.project_root, normalized)
    return _changed_line_ranges(context, resolved_commit, normalized)


def validate_guard_analysis_scope(
    project_root: str | Path,
    analysis_files: Iterable[str | Path],
) -> None:
    """Enforce the common parse budget for every target Guard scope."""
    root = Path(project_root).expanduser().resolve()
    normalized_files = tuple(
        sorted(
            {
                _normalize_relative_path(root, item)
                for item in analysis_files
            }
        )
    )
    if len(normalized_files) > MAX_GUARD_ANALYSIS_FILES:
        raise GuardScopeError(
            "GUARD_SCOPE_TOO_LARGE",
            "Guard analysis scope exceeds the bounded file budget",
            file_count=len(normalized_files),
            max_files=MAX_GUARD_ANALYSIS_FILES,
        )
    total_bytes = 0
    for relative in normalized_files:
        path = root / relative
        if not path.is_file():
            continue
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            raise GuardScopeError(
                "CURRENT_READ_FAILED",
                f"Cannot stat Guard source: {exc}",
                path=relative,
            ) from exc
        if total_bytes > MAX_GUARD_ANALYSIS_BYTES:
            raise GuardScopeError(
                "GUARD_SCOPE_TOO_LARGE",
                "Guard analysis scope exceeds the bounded byte budget",
                source_bytes=total_bytes,
                max_bytes=MAX_GUARD_ANALYSIS_BYTES,
            )


def _changed_line_ranges(
    context: _GitContext,
    resolved_commit: str,
    production_files: tuple[str, ...],
) -> tuple[tuple[str, int, int], ...]:
    """Return current-line diff spans for explicit changed Java paths only."""
    ranges: set[tuple[str, int, int]] = set()
    for relative in production_files:
        current = context.project_root / relative
        if not current.is_file():
            continue
        repository_path = _repository_relative_path(context, relative)
        if not _baseline_path_exists(context, resolved_commit, repository_path):
            try:
                payload = current.read_bytes()
            except OSError as exc:
                raise GuardScopeError(
                    "CURRENT_READ_FAILED",
                    f"Cannot read changed Guard source: {exc}",
                    path=relative,
                ) from exc
            line_count = max(1, payload.count(b"\n") + (not payload.endswith(b"\n")))
            ranges.add((relative, 1, int(line_count)))
            continue

        result = _run_git_bytes(
            context.project_root,
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=0",
                resolved_commit,
                "--",
                relative,
            ),
        )
        if result.returncode != 0:
            raise GuardScopeError(
                "CHANGED_LINES_UNAVAILABLE",
                "Git could not produce changed-line spans for Guard scope",
                path=relative,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        for raw_line in result.stdout.splitlines():
            match = _HUNK_HEADER.match(raw_line)
            if match is None:
                continue
            start = max(1, int(match.group("start")))
            count_group = match.group("count")
            count = int(count_group) if count_group is not None else 1
            # A deletion-only hunk has no current lines.  Its current anchor is
            # still enough to identify an enclosing method whose body shrank.
            end = start if count == 0 else start + count - 1
            ranges.add((relative, start, end))
    return tuple(sorted(ranges))


def read_baseline_bytes(
    project_root: str | Path,
    baseline_commit: str,
    relative_path: str | Path,
) -> bytes | None:
    """Read one exact path from ``baseline_commit`` without checking out files.

    ``None`` means the path did not exist at the valid baseline commit.  Git is
    invoked for this one blob only; no tree or source catalog is materialized.
    """

    context = _git_context(project_root)
    resolved_commit = _resolve_commit(context, baseline_commit)
    relative = _normalize_relative_path(context.project_root, relative_path)
    repository_path = _repository_relative_path(context, relative)
    result = _run_git_bytes(
        context.repository_root,
        ("show", f"{resolved_commit}:{repository_path}"),
    )
    if result.returncode == 0:
        return result.stdout
    if _baseline_path_exists(context, resolved_commit, repository_path):
        raise GuardScopeError(
            "BASELINE_READ_FAILED",
            "Git could not read a baseline path that exists",
            path=relative,
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    return None


def read_current_bytes(
    project_root: str | Path,
    relative_path: str | Path,
) -> bytes | None:
    """Read one exact worktree path without following links outside the root."""

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise GuardScopeError(
            "PROJECT_ROOT_UNREADABLE",
            "Guard project root is not a directory",
            project_root=str(root),
        )
    relative = _normalize_relative_path(root, relative_path)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    if candidate.is_symlink():
        raise GuardScopeError(
            "CURRENT_PATH_SYMLINK_UNSUPPORTED",
            "Guard current-path reads do not follow symbolic links",
            path=relative,
        )
    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise GuardScopeError(
            "PATH_OUTSIDE_PROJECT",
            "Guard path escapes the project root",
            path=relative,
        ) from exc
    if not candidate.exists():
        return None
    if not candidate.is_file():
        raise GuardScopeError(
            "CURRENT_PATH_NOT_FILE",
            "Guard current path is not a regular file",
            path=relative,
        )
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise GuardScopeError(
            "CURRENT_READ_FAILED",
            f"Cannot read Guard current path: {exc}",
            path=relative,
        ) from exc


def _git_context(project_root: str | Path) -> _GitContext:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise GuardScopeError(
            "PROJECT_ROOT_UNREADABLE",
            "Guard project root is not a directory",
            project_root=str(root),
        )
    result = _run_git_bytes(root, ("rev-parse", "--show-toplevel"))
    if result.returncode != 0:
        raise GuardScopeError(
            "PROJECT_NOT_GIT_WORKTREE",
            "Guard project root is not inside a Git worktree",
            project_root=str(root),
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    repository_root = Path(
        result.stdout.decode("utf-8", errors="surrogateescape").strip()
    ).resolve()
    try:
        prefix = root.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise GuardScopeError(
            "PROJECT_ROOT_OUTSIDE_REPOSITORY",
            "Git reported a repository outside the Guard project root",
            project_root=str(root),
            repository_root=str(repository_root),
        ) from exc
    return _GitContext(
        project_root=root,
        repository_root=repository_root,
        project_prefix="" if prefix == "." else prefix,
    )


def _resolve_commit(context: _GitContext, baseline_commit: str) -> str:
    commit = str(baseline_commit).strip()
    if not commit or "\x00" in commit or "\n" in commit or "\r" in commit:
        raise GuardScopeError(
            "BASELINE_COMMIT_INVALID",
            "Guard baseline commit is empty or malformed",
        )
    result = _run_git_bytes(
        context.project_root,
        (
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{commit}^{{commit}}",
        ),
    )
    if result.returncode != 0:
        raise GuardScopeError(
            "BASELINE_COMMIT_INVALID",
            "Guard baseline commit cannot be resolved",
            baseline_commit=commit,
        )
    resolved = result.stdout.decode("ascii", errors="strict").strip()
    if not resolved:
        raise GuardScopeError(
            "BASELINE_COMMIT_INVALID",
            "Guard baseline commit resolved to an empty object name",
            baseline_commit=commit,
        )
    return resolved


def _changed_paths(
    context: _GitContext,
    resolved_commit: str,
) -> tuple[str, ...]:
    result = _run_git_bytes(
        context.project_root,
        (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--relative",
            "--name-status",
            "-z",
            "--find-renames",
            resolved_commit,
            "--",
            ".",
        ),
    )
    if result.returncode != 0:
        raise GuardScopeError(
            "CHANGED_PATHS_UNAVAILABLE",
            "Git could not compare the baseline commit with the worktree",
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    paths = _parse_name_status(context.project_root, result.stdout)

    untracked = _run_git_bytes(
        context.project_root,
        ("ls-files", "--others", "--exclude-standard", "-z", "--", "."),
    )
    if untracked.returncode != 0:
        raise GuardScopeError(
            "CHANGED_PATHS_UNAVAILABLE",
            "Git could not enumerate untracked worktree paths",
            stderr=untracked.stderr.decode("utf-8", errors="replace").strip(),
        )
    for raw in untracked.stdout.split(b"\x00"):
        if raw:
            paths.add(
                _normalize_relative_path(
                    context.project_root,
                    raw.decode("utf-8", errors="surrogateescape"),
                )
            )
    return tuple(sorted(paths))


def _parse_name_status(project_root: Path, payload: bytes) -> set[str]:
    fields = payload.split(b"\x00")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        raw_status = fields[index]
        index += 1
        if not raw_status:
            continue
        try:
            status = raw_status.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GuardScopeError(
                "CHANGED_PATHS_PARSE_FAILED",
                "Git emitted a non-ASCII change status",
            ) from exc
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise GuardScopeError(
                "CHANGED_PATHS_PARSE_FAILED",
                "Git emitted a truncated rename-aware path record",
                status=status,
            )
        for raw_path in fields[index : index + path_count]:
            if not raw_path:
                raise GuardScopeError(
                    "CHANGED_PATHS_PARSE_FAILED",
                    "Git emitted an empty changed path",
                    status=status,
                )
            paths.add(
                _normalize_relative_path(
                    project_root,
                    raw_path.decode("utf-8", errors="surrogateescape"),
                )
            )
        index += path_count
    return paths


def _normalize_relative_path(project_root: Path, path: str | Path) -> str:
    raw = str(path).replace("\\", "/")
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            raw = candidate.resolve(strict=False).relative_to(project_root).as_posix()
        except (OSError, ValueError) as exc:
            raise GuardScopeError(
                "PATH_OUTSIDE_PROJECT",
                "Guard path is outside the project root",
                path=str(path),
            ) from exc
    normalized = PurePosixPath(raw)
    if (
        not normalized.parts
        or normalized.as_posix() in {"", "."}
        or normalized.is_absolute()
        or ".." in normalized.parts
        or "\x00" in raw
    ):
        raise GuardScopeError(
            "INVALID_RELATIVE_PATH",
            "Guard paths must be non-empty project-relative paths",
            path=str(path),
        )
    return normalized.as_posix()


def _is_production_java(
    relative_path: str,
    layout: JavaSourceLayout | None,
) -> bool:
    path = PurePosixPath(relative_path)
    if path.suffix.casefold() != ".java":
        return False
    lowered = tuple(part.casefold() for part in path.parts)
    if any(
        part in _NON_PRODUCTION_COMPONENTS or part.startswith("bazel-")
        for part in lowered[:-1]
    ):
        return False
    return not (
        layout.is_test_path(relative_path)
        if layout is not None
        else standard_test_root(relative_path) is not None
    )


def _repository_relative_path(context: _GitContext, relative_path: str) -> str:
    if not context.project_prefix:
        return relative_path
    return f"{context.project_prefix}/{relative_path}"


def _baseline_path_exists(
    context: _GitContext,
    resolved_commit: str,
    repository_path: str,
) -> bool:
    result = _run_git_bytes(
        context.repository_root,
        ("cat-file", "-e", f"{resolved_commit}:{repository_path}"),
    )
    return result.returncode == 0


def _run_git_bytes(
    root: Path,
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise GuardScopeError(
            "GIT_UNAVAILABLE",
            f"Cannot execute Git for Guard scope: {exc}",
        ) from exc


__all__ = [
    "GuardScopeError",
    "GuardVerificationScope",
    "MAX_GUARD_ANALYSIS_BYTES",
    "MAX_GUARD_ANALYSIS_FILES",
    "build_guard_verification_scope",
    "build_changed_line_ranges",
    "read_baseline_bytes",
    "read_current_bytes",
    "validate_guard_analysis_scope",
]
