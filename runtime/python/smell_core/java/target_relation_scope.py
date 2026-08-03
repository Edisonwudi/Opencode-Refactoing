"""Bounded exact relation scopes for target-only Java Guards.

This module resolves only source relations rooted at a caller-supplied target.
It does not enumerate Java sources, build a project class catalog, or run a
smell detector.  Candidate files come from an exact Git symbol query and are
accepted only after the ordinary scoped Java model proves one package/import
qualified declaration identity.

The first consumer is Refused Bequest: a rejecting override can inherit its
contract through an intermediate abstract class and one or more interfaces.
The Guard needs that small ancestor chain, but it does not need the rest of the
repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from ..analysis import method_basename
from ..location import LocationTarget, parse_location_descriptor
from . import semantic_detector as semantic
from .catalog_identity import stable_java_method_signature
from .source_layout import standard_test_root


DEFAULT_MAX_ANCESTOR_HOPS = 8
DEFAULT_MAX_RELATION_FILES = 24
DEFAULT_MAX_RELATION_BYTES = 4 * 1024 * 1024

_EXCLUDED_PATH_PARTS = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".smell-artifacts",
        "build",
        "dataset",
        "datasets",
        "dist",
        "node_modules",
        "out",
        "target",
    }
)
_TERMINAL_SOURCE_ANCESTORS = frozenset(
    {
        "java.lang.Object",
        "java.lang.Record",
        "java.lang.Enum",
        "java.lang.annotation.Annotation",
    }
)
_DECLARATION_PREFIX = rb"\b(?:class|interface)\s+"


@dataclass(frozen=True)
class JavaRelationEdge:
    """One exact source inheritance edge admitted to the Guard scope."""

    child: str
    parent: str
    child_file: str
    parent_file: str
    relation: str
    source_name: str
    depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "child": self.child,
            "parent": self.parent,
            "child_file": self.child_file,
            "parent_file": self.parent_file,
            "relation": self.relation,
            "source_name": self.source_name,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class RefusedBequestRelationScope:
    """Exact files and identities needed to evaluate one target hierarchy."""

    files: tuple[str, ...]
    target_file: str
    target_class: str
    ancestors: tuple[str, ...]
    edges: tuple[JavaRelationEdge, ...]
    reported_parent: str = ""
    resolved_reported_parent: str = ""
    source_bytes: int = 0

    def witness(self) -> dict[str, Any]:
        return {
            "target_file": self.target_file,
            "target_class": self.target_class,
            "scope_files": list(self.files),
            "ancestors": list(self.ancestors),
            "edges": [item.as_dict() for item in self.edges],
            "reported_parent": self.reported_parent,
            "resolved_reported_parent": self.resolved_reported_parent,
            "source_bytes": self.source_bytes,
        }


class TargetRelationScopeError(ValueError):
    """Fail-closed exact relation-resolution failure."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def violation(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            **self.details,
        }


@dataclass(frozen=True)
class _ParentReference:
    relation: str
    index: int
    source_name: str
    resolved_name: str


def resolve_refused_bequest_relation_scope(
    project_root: str | Path,
    target_files: Iterable[str | Path],
    location: LocationTarget | str,
    selector: Mapping[str, Any] | None = None,
    *,
    max_hops: int = DEFAULT_MAX_ANCESTOR_HOPS,
    max_files: int = DEFAULT_MAX_RELATION_FILES,
    max_bytes: int = DEFAULT_MAX_RELATION_BYTES,
) -> RefusedBequestRelationScope:
    """Resolve one target class's exact source ancestor chain.

    ``target_files`` must be the caller-frozen target scope, not every changed
    Java file.  Each parent simple name is queried with bounded ``git grep``;
    package/import resolution in the scoped semantic model then selects one
    declaration.  A missing or ambiguous declaration fails closed and never
    widens into a project scan.
    """

    root = _project_root(project_root)
    _validate_limits(max_hops=max_hops, max_files=max_files, max_bytes=max_bytes)
    parsed_location = _coerce_location(root, location)
    normalized_targets = set(_normalize_files(root, target_files))
    target_file = _relative_java_path(root, parsed_location.file_path)
    if target_file not in normalized_targets:
        raise TargetRelationScopeError(
            "TARGET_FILE_NOT_IN_SCOPE",
            "Refused Bequest target location is not present in target_files",
            target_file=target_file,
        )
    _enforce_scope_budget(
        root,
        normalized_targets,
        max_files=max_files,
        max_bytes=max_bytes,
    )

    selected = _selector_mapping(selector)
    initial_model = _build_model(root, normalized_targets)
    target = _select_target_class(
        initial_model,
        parsed_location,
        selected,
        target_file=target_file,
    )
    if not _direct_parent_references(target):
        raise TargetRelationScopeError(
            "ANCESTOR_RELATION_NOT_FOUND",
            "Refused Bequest target class has no explicit source parent",
            target_class=target.qualified_name,
        )

    scope_files = set(normalized_targets)
    edges: list[JavaRelationEdge] = []
    ancestors: set[str] = set()
    expanded: set[str] = set()
    queued: set[str] = {target.qualified_name}
    queue: list[tuple[str, int]] = [(target.qualified_name, 0)]

    while queue:
        child_name, child_depth = queue.pop(0)
        queued.discard(child_name)
        if child_name in expanded:
            continue
        model = _build_model(root, scope_files)
        child = _unique_class(model, child_name, scope_files)
        if child is None:
            raise TargetRelationScopeError(
                "RELATION_ENTITY_NOT_FOUND",
                "Previously resolved relation entity disappeared from the scoped model",
                class_name=child_name,
            )
        expanded.add(child_name)

        for reference in _direct_parent_references(child):
            next_depth = child_depth + 1
            if next_depth > max_hops:
                raise TargetRelationScopeError(
                    "RELATION_HOP_LIMIT_EXCEEDED",
                    "Refused Bequest ancestor chain exceeds the hop limit",
                    child=child.qualified_name,
                    source_parent=reference.source_name,
                    max_hops=max_hops,
                )
            parent = _resolve_parent_declaration(
                root,
                scope_files,
                child,
                reference,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            if parent is None:
                # Explicit Object/Record/Enum/Annotation ancestry cannot own a
                # user Refused Bequest method contract and is terminal.
                continue
            scope_files.add(parent.file)
            _enforce_scope_budget(
                root,
                scope_files,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            edge = JavaRelationEdge(
                child=child.qualified_name,
                parent=parent.qualified_name,
                child_file=child.file,
                parent_file=parent.file,
                relation=reference.relation,
                source_name=reference.source_name,
                depth=next_depth,
            )
            if edge not in edges:
                edges.append(edge)
            ancestors.add(parent.qualified_name)
            if parent.qualified_name not in expanded and parent.qualified_name not in queued:
                queue.append((parent.qualified_name, next_depth))
                queued.add(parent.qualified_name)

    reported_parent = _selector_value(selected, "parent")
    resolved_reported_parent = _resolve_reported_parent(
        reported_parent,
        ancestors,
    )
    total_bytes = _scope_bytes(root, scope_files)
    return RefusedBequestRelationScope(
        files=tuple(sorted(scope_files)),
        target_file=target_file,
        target_class=target.qualified_name,
        ancestors=tuple(sorted(ancestors)),
        edges=tuple(edges),
        reported_parent=reported_parent,
        resolved_reported_parent=resolved_reported_parent,
        source_bytes=total_bytes,
    )


def _resolve_parent_declaration(
    root: Path,
    scope_files: set[str],
    child: semantic.ClassRecord,
    reference: _ParentReference,
    *,
    max_files: int,
    max_bytes: int,
) -> semantic.ClassRecord | None:
    resolved = _clean_type_identity(reference.resolved_name)
    if resolved in _TERMINAL_SOURCE_ANCESTORS:
        return None

    current_model = _build_model(root, scope_files)
    if resolved and not _is_ambiguous_type(resolved):
        present = _class_candidates(current_model, resolved, scope_files)
        if len(present) == 1:
            return present[0]
        if len(present) > 1:
            raise TargetRelationScopeError(
                "ANCESTOR_DECLARATION_AMBIGUOUS",
                "Ancestor identity has multiple declarations in the explicit scope",
                parent=resolved,
                files=sorted({item.file for item in present}),
            )

    simple_name = _simple_type_name(reference.source_name or resolved)
    if not simple_name:
        raise TargetRelationScopeError(
            "ANCESTOR_IDENTITY_UNAVAILABLE",
            "Cannot derive the source parent type identity",
            child=child.qualified_name,
            source_parent=reference.source_name,
        )
    queried = _query_declaration_files(
        root,
        simple_name,
        source_name=reference.source_name,
        expected_qualified=(
            resolved if resolved and not _is_ambiguous_type(resolved) else ""
        ),
        max_files=max_files,
        max_bytes=max_bytes,
        existing_files=scope_files,
    )
    candidate_scope = set(scope_files).union(queried)
    _enforce_scope_budget(
        root,
        candidate_scope,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    candidate_model = _build_model(root, candidate_scope)
    candidate_child = _unique_class(
        candidate_model,
        child.qualified_name,
        candidate_scope,
    )
    if candidate_child is None:
        raise TargetRelationScopeError(
            "RELATION_ENTITY_NOT_FOUND",
            "Target child identity is not unique after adding ancestor candidates",
            child=child.qualified_name,
        )
    candidate_reference = _matching_parent_reference(candidate_child, reference)
    resolved = _clean_type_identity(candidate_reference.resolved_name)
    if resolved in _TERMINAL_SOURCE_ANCESTORS:
        return None
    if not resolved or _is_ambiguous_type(resolved):
        raise TargetRelationScopeError(
            "ANCESTOR_TYPE_AMBIGUOUS",
            "Package/import rules do not resolve the source parent uniquely",
            child=child.qualified_name,
            source_parent=reference.source_name,
            candidate_files=sorted(queried),
        )

    matches = _class_candidates(candidate_model, resolved, candidate_scope)
    if len(matches) == 0:
        raise TargetRelationScopeError(
            "ANCESTOR_DECLARATION_NOT_FOUND",
            "The resolved source parent declaration was not found by the bounded query",
            child=child.qualified_name,
            parent=resolved,
            source_parent=reference.source_name,
        )
    if len(matches) > 1:
        raise TargetRelationScopeError(
            "ANCESTOR_DECLARATION_AMBIGUOUS",
            "The resolved source parent has multiple declarations",
            child=child.qualified_name,
            parent=resolved,
            files=sorted({item.file for item in matches}),
        )
    return matches[0]


def _query_declaration_files(
    root: Path,
    simple_name: str,
    *,
    source_name: str,
    expected_qualified: str,
    max_files: int,
    max_bytes: int,
    existing_files: set[str],
) -> tuple[str, ...]:
    declaration_query = (
        r"(^|[^A-Za-z0-9_$])(class|interface)[[:space:]]+"
        + re.escape(simple_name)
        + r"([^A-Za-z0-9_$]|$)"
    )
    result = subprocess.run(
        [
            "git",
            "grep",
            "--untracked",
            "-l",
            "-z",
            "--extended-regexp",
            "-e",
            declaration_query,
            "--",
            "*.java",
        ],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise TargetRelationScopeError(
            "ANCESTOR_QUERY_FAILED",
            "Git could not execute the bounded ancestor symbol query",
            symbol=simple_name,
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    raw_candidates: tuple[str, ...] = tuple(
        sorted(
            {
                item.decode("utf-8", errors="surrogateescape")
                for item in result.stdout.split(b"\0")
                if item
            }
        )
    )
    expected_package = _queryable_expected_package(
        source_name=source_name,
        expected_qualified=expected_qualified,
        simple_name=simple_name,
    )
    if expected_package:
        package_query = (
            r"^[[:space:]]*package[[:space:]]+"
            + re.escape(expected_package)
            + r"[[:space:]]*;"
        )
        package_result = subprocess.run(
            [
                "git",
                "grep",
                "--untracked",
                "-l",
                "-z",
                "--extended-regexp",
                "-e",
                package_query,
                "--",
                "*.java",
            ],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if package_result.returncode not in (0, 1):
            raise TargetRelationScopeError(
                "ANCESTOR_QUERY_FAILED",
                "Git could not execute the ancestor package query",
                symbol=simple_name,
                package=expected_package,
                stderr=package_result.stderr.decode(
                    "utf-8", errors="replace"
                ).strip(),
            )
        package_files = {
            item.decode("utf-8", errors="surrogateescape")
            for item in package_result.stdout.split(b"\0")
            if item
        }
        raw_candidates = tuple(
            path for path in raw_candidates if path in package_files
        )
    normalized = tuple(
        path
        for path in raw_candidates
        if _is_bounded_production_java_path(path)
        and standard_test_root(path) is None
    )
    if len(set(existing_files).union(normalized)) > max_files:
        raise TargetRelationScopeError(
            "ANCESTOR_QUERY_TOO_BROAD",
            "Ancestor symbol query exceeds the bounded file limit",
            symbol=simple_name,
            candidate_count=len(normalized),
            max_files=max_files,
        )
    # Candidate bytes are budgeted before any Python source read or AST build.
    # Package/import disambiguation may later discard most candidates, but it
    # must not become an unbounded pre-analysis step itself.
    _enforce_scope_budget(
        root,
        set(existing_files).union(normalized),
        max_files=max_files,
        max_bytes=max_bytes,
    )
    declaration_pattern = re.compile(
        _DECLARATION_PREFIX
        + re.escape(simple_name.encode("utf-8"))
        + rb"(?![A-Za-z0-9_$])"
    )
    declarations: list[str] = []
    for relative in normalized:
        path = _safe_project_file(root, relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise TargetRelationScopeError(
                "ANCESTOR_SOURCE_READ_FAILED",
                f"Cannot read ancestor candidate: {exc}",
                path=relative,
            ) from exc
        if (
            declaration_pattern.search(payload)
            and _package_matches_expected(
                payload,
                expected_qualified=expected_qualified,
            )
        ):
            declarations.append(relative)
    selected = tuple(sorted(set(declarations)))
    _enforce_scope_budget(
        root,
        set(existing_files).union(selected),
        max_files=max_files,
        max_bytes=max_bytes,
    )
    return selected


def _queryable_expected_package(
    *,
    source_name: str,
    expected_qualified: str,
    simple_name: str,
) -> str:
    """Return a safe package prefilter for ordinary top-level type syntax.

    ``Outer.Parent`` may denote a nested type, so its FQCN prefix is not a
    package and must not be used as a Git package filter. Fully qualified
    source names conventionally begin with a lower-case package segment and
    are safe; an unqualified source name resolved by import/same-package is
    safe as well.
    """
    expected = _clean_type_identity(expected_qualified)
    source = _clean_type_identity(source_name)
    if not expected or "." not in expected:
        return ""
    if expected.rsplit(".", 1)[-1] != simple_name:
        return ""
    if "." in source and source.split(".", 1)[0][:1].isupper():
        return ""
    return expected.rsplit(".", 1)[0]


def _package_matches_expected(
    source: bytes,
    *,
    expected_qualified: str,
) -> bool:
    if not expected_qualified:
        return True
    match = re.search(
        rb"(?m)^\s*package\s+"
        rb"([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)"
        rb"\s*;",
        source,
    )
    package = (
        match.group(1).decode("utf-8", errors="replace") if match is not None else ""
    )
    if not package:
        return "." not in expected_qualified
    return expected_qualified.startswith(f"{package}.")


def _direct_parent_references(record: semantic.ClassRecord) -> tuple[_ParentReference, ...]:
    references: list[_ParentReference] = []
    if record.source_superclass_name:
        references.append(
            _ParentReference(
                relation="extends",
                index=0,
                source_name=record.source_superclass_name,
                resolved_name=record.superclass_name,
            )
        )
    for index, source_name in enumerate(record.source_interface_names):
        references.append(
            _ParentReference(
                relation="implements_or_extends_interface",
                index=index,
                source_name=source_name,
                resolved_name=(
                    record.interface_names[index]
                    if index < len(record.interface_names)
                    else ""
                ),
            )
        )
    return tuple(references)


def _matching_parent_reference(
    record: semantic.ClassRecord,
    expected: _ParentReference,
) -> _ParentReference:
    references = _direct_parent_references(record)
    matches = [
        item
        for item in references
        if item.relation == expected.relation and item.index == expected.index
    ]
    if len(matches) != 1:
        raise TargetRelationScopeError(
            "ANCESTOR_RELATION_CHANGED",
            "Source parent relation changed while resolving its declaration",
            child=record.qualified_name,
            relation=expected.relation,
            index=expected.index,
        )
    return matches[0]


def _select_target_class(
    model: semantic.ProjectModel,
    location: LocationTarget,
    selector: Mapping[str, Any],
    *,
    target_file: str,
) -> semantic.ClassRecord:
    candidates = [
        item
        for records in model.classes_by_simple.values()
        for item in records
        if item.file == target_file
    ]
    candidates = _deduplicate_classes(candidates)
    class_hint = (
        _selector_value(selector, "class")
        or _selector_value(selector, "target_class")
        or str(location.class_name or "")
    )
    if class_hint:
        exact = [item for item in candidates if item.qualified_name == class_hint]
        candidates = exact or [
            item
            for item in candidates
            if item.class_name == class_hint.rsplit(".", 1)[-1]
        ]

    method_hint = (
        _selector_value(selector, "method")
        or _selector_value(selector, "container_method")
        or str(location.method or "")
    )
    if method_hint:
        method_owners = {
            item.owner_qualified_name
            for item in model.methods
            if item.file == target_file and _same_method(item.method_signature, method_hint)
        }
        # During verify the frozen method may legitimately have been removed
        # or renamed.  A unique frozen class identity is still sufficient to
        # resolve its current ancestor relation and inspect possible
        # relocation findings.  At capture (where no class hint need exist),
        # the method remains the required disambiguator.
        if method_owners:
            candidates = [
                item for item in candidates if item.qualified_name in method_owners
            ]
        elif not class_hint:
            candidates = []

    if location.line:
        containing = [
            item
            for item in candidates
            if item.begin_line <= int(location.line) <= item.end_line
        ]
        if containing:
            smallest_span = min(item.end_line - item.begin_line for item in containing)
            candidates = [
                item
                for item in containing
                if item.end_line - item.begin_line == smallest_span
            ]

    if len(candidates) == 0:
        raise TargetRelationScopeError(
            "TARGET_CLASS_NOT_FOUND",
            "Cannot find the Refused Bequest target class in target_files",
            target_file=target_file,
            target_class=class_hint,
            target_method=method_hint,
        )
    if len(candidates) > 1:
        raise TargetRelationScopeError(
            "TARGET_AMBIGUOUS",
            "Refused Bequest target context matches multiple classes",
            target_file=target_file,
            candidates=sorted(item.qualified_name for item in candidates),
        )
    return candidates[0]


def _resolve_reported_parent(reported: str, ancestors: set[str]) -> str:
    if not reported:
        return ""
    normalized = _clean_type_identity(reported)
    exact = [item for item in ancestors if item == normalized]
    if len(exact) == 1:
        return exact[0]
    simple = _simple_type_name(normalized)
    matches = [item for item in ancestors if item.rsplit(".", 1)[-1] == simple]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise TargetRelationScopeError(
            "REPORTED_PARENT_NOT_IN_HIERARCHY",
            "selector.parent is not in the exact target ancestor chain",
            reported_parent=reported,
            ancestors=sorted(ancestors),
        )
    raise TargetRelationScopeError(
        "REPORTED_PARENT_AMBIGUOUS",
        "selector.parent matches multiple exact target ancestors",
        reported_parent=reported,
        matches=sorted(matches),
    )


def _class_candidates(
    model: semantic.ProjectModel,
    qualified_name: str,
    files: Iterable[str],
) -> list[semantic.ClassRecord]:
    allowed = set(files)
    simple = qualified_name.rsplit(".", 1)[-1]
    return _deduplicate_classes(
        item
        for item in model.classes_by_simple.get(simple, [])
        if item.file in allowed and item.qualified_name == qualified_name
    )


def _unique_class(
    model: semantic.ProjectModel,
    qualified_name: str,
    files: Iterable[str],
) -> semantic.ClassRecord | None:
    candidates = _class_candidates(model, qualified_name, files)
    return candidates[0] if len(candidates) == 1 else None


def _deduplicate_classes(
    values: Iterable[semantic.ClassRecord],
) -> list[semantic.ClassRecord]:
    output: dict[tuple[str, str, int], semantic.ClassRecord] = {}
    for item in values:
        output[(item.qualified_name, item.file, item.begin_line)] = item
    return [output[key] for key in sorted(output)]


def _same_method(actual: str, expected: str) -> bool:
    actual_text = stable_java_method_signature(actual)
    expected_text = stable_java_method_signature(expected)
    if not actual_text or not expected_text:
        return False
    if "(" in str(expected or "") and ")" in str(expected or ""):
        return actual_text == expected_text
    return str(method_basename(actual_text) or "") == str(
        method_basename(expected_text) or ""
    )


def _build_model(root: Path, files: Iterable[str]) -> semantic.ProjectModel:
    try:
        return semantic.build_scoped_project_model(root, tuple(sorted(set(files))))
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise TargetRelationScopeError(
            "RELATION_SCOPE_PARSE_FAILED",
            f"Cannot parse the exact Refused Bequest relation scope: {exc}",
        ) from exc


def _project_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise TargetRelationScopeError(
            "PROJECT_ROOT_UNREADABLE",
            "Refused Bequest relation scope requires a readable project root",
            project_root=str(root),
        )
    return root


def _coerce_location(root: Path, value: LocationTarget | str) -> LocationTarget:
    if isinstance(value, LocationTarget):
        _relative_java_path(root, value.file_path)
        return value
    try:
        return parse_location_descriptor(str(value), root)
    except (TypeError, ValueError) as exc:
        raise TargetRelationScopeError(
            "TARGET_LOCATION_INVALID",
            f"Cannot parse Refused Bequest target location: {exc}",
        ) from exc


def _normalize_files(root: Path, values: Iterable[str | Path]) -> tuple[str, ...]:
    normalized = tuple(sorted({_relative_java_path(root, value) for value in values}))
    if not normalized:
        raise TargetRelationScopeError(
            "TARGET_CONTEXT_INCOMPLETE",
            "Refused Bequest relation scope requires target_files",
        )
    return normalized


def _relative_java_path(root: Path, value: str | Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / PurePosixPath(str(value).replace("\\", "/"))
    try:
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise TargetRelationScopeError(
            "SCOPED_SOURCE_OUTSIDE_PROJECT",
            "Refused Bequest relation source is outside the project root",
            path=str(value),
        ) from exc
    if relative.suffix.casefold() != ".java" or not resolved.is_file():
        raise TargetRelationScopeError(
            "SCOPED_SOURCE_NOT_JAVA_FILE",
            "Refused Bequest relation source must be an existing Java file",
            path=relative.as_posix(),
        )
    if not _is_bounded_production_java_path(relative.as_posix()):
        raise TargetRelationScopeError(
            "SCOPED_SOURCE_NOT_PRODUCTION_JAVA",
            "Refused Bequest relation source is not a bounded production Java path",
            path=relative.as_posix(),
        )
    return relative.as_posix()


def _safe_project_file(root: Path, relative: str) -> Path:
    path = (root / PurePosixPath(relative)).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TargetRelationScopeError(
            "SCOPED_SOURCE_OUTSIDE_PROJECT",
            "Git relation query returned a path outside the project root",
            path=relative,
        ) from exc
    return path


def _is_bounded_production_java_path(value: str) -> bool:
    path = PurePosixPath(str(value).replace("\\", "/"))
    return (
        path.suffix.casefold() == ".java"
        and not any(part.casefold() in _EXCLUDED_PATH_PARTS for part in path.parts[:-1])
    )


def _validate_limits(*, max_hops: int, max_files: int, max_bytes: int) -> None:
    for name, value in (
        ("max_hops", max_hops),
        ("max_files", max_files),
        ("max_bytes", max_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TargetRelationScopeError(
                "RELATION_SCOPE_LIMIT_INVALID",
                "Refused Bequest relation limits must be positive integers",
                limit=name,
                value=value,
            )


def _enforce_scope_budget(
    root: Path,
    files: Iterable[str],
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    normalized = tuple(sorted(set(files)))
    if len(normalized) > max_files:
        raise TargetRelationScopeError(
            "RELATION_SCOPE_TOO_LARGE",
            "Refused Bequest relation scope exceeds the file limit",
            file_count=len(normalized),
            max_files=max_files,
        )
    total = _scope_bytes(root, normalized)
    if total > max_bytes:
        raise TargetRelationScopeError(
            "RELATION_SCOPE_TOO_LARGE",
            "Refused Bequest relation scope exceeds the byte limit",
            source_bytes=total,
            max_bytes=max_bytes,
        )


def _scope_bytes(root: Path, files: Iterable[str]) -> int:
    total = 0
    for relative in sorted(set(files)):
        path = _safe_project_file(root, relative)
        try:
            total += path.stat().st_size
        except OSError as exc:
            raise TargetRelationScopeError(
                "RELATION_SOURCE_STAT_FAILED",
                f"Cannot stat relation source: {exc}",
                path=relative,
            ) from exc
    return total


def _selector_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TargetRelationScopeError(
            "TARGET_SELECTOR_INVALID",
            "Refused Bequest target selector must be an object",
        )
    return {str(key): item for key, item in value.items()}


def _selector_value(selector: Mapping[str, Any], name: str) -> str:
    nested = selector.get("entity_identity")
    if isinstance(nested, Mapping) and nested.get(name):
        return str(nested[name]).strip()
    return str(selector.get(name) or "").strip()


def _simple_type_name(value: str) -> str:
    cleaned = _clean_type_identity(value)
    return cleaned.rsplit(".", 1)[-1] if cleaned else ""


def _clean_type_identity(value: str) -> str:
    text = str(value or "").strip().replace("$", ".")
    if not text:
        return ""
    output: list[str] = []
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
            continue
        if char == ">" and depth:
            depth -= 1
            continue
        if depth == 0 and not char.isspace():
            output.append(char)
    return "".join(output).strip()


def _is_ambiguous_type(value: str) -> bool:
    return _clean_type_identity(value).startswith(semantic.AMBIGUOUS_TYPE_PREFIX)


__all__ = [
    "DEFAULT_MAX_ANCESTOR_HOPS",
    "DEFAULT_MAX_RELATION_BYTES",
    "DEFAULT_MAX_RELATION_FILES",
    "JavaRelationEdge",
    "RefusedBequestRelationScope",
    "TargetRelationScopeError",
    "resolve_refused_bequest_relation_scope",
]
