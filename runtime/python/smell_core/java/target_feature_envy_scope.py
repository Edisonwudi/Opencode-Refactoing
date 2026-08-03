"""Conditional exact source scope for a target Feature Envy Guard.

Feature Envy is evaluated at one caller-supplied method.  The ordinary case
therefore needs only the file containing that method.  A source ancestor is
added only when the target class explicitly ``extends`` or ``implements`` it;
the shared bounded hierarchy resolver then follows that relation recursively.

In particular, this module does not add the source file for a field, parameter,
local variable, explicit import, or same-package receiver type.  Receiver type
names are detector inputs, not reasons to widen a Guard scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import target_relation_scope as relation_scope


DEFAULT_MAX_ANCESTOR_HOPS = 8
DEFAULT_MAX_SCOPE_FILES = 32
DEFAULT_MAX_SCOPE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FeatureEnvyScopeEdge:
    """One exact inheritance edge admitted to a Feature Envy scope."""

    child: str
    parent: str
    child_file: str
    parent_file: str
    relation: str
    source_name: str
    depth: int

    @classmethod
    def from_relation_edge(
        cls,
        edge: relation_scope.JavaRelationEdge,
    ) -> "FeatureEnvyScopeEdge":
        return cls(
            child=edge.child,
            parent=edge.parent,
            child_file=edge.child_file,
            parent_file=edge.parent_file,
            relation=edge.relation,
            source_name=edge.source_name,
            depth=edge.depth,
        )

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
class FeatureEnvyTargetScope:
    """The complete and bounded Java source scope for one target method."""

    files: tuple[str, ...]
    target_file: str
    target_class: str
    ancestors: tuple[str, ...] = ()
    edges: tuple[FeatureEnvyScopeEdge, ...] = ()
    source_bytes: int = 0

    @property
    def expanded_for_inheritance(self) -> bool:
        return bool(self.edges)

    def witness(self) -> dict[str, Any]:
        return {
            "target_file": self.target_file,
            "target_class": self.target_class,
            "scope_files": list(self.files),
            "ancestors": list(self.ancestors),
            "edges": [item.as_dict() for item in self.edges],
            "expanded_for_inheritance": self.expanded_for_inheritance,
            "source_bytes": self.source_bytes,
        }


class FeatureEnvyScopeError(ValueError):
    """Fail-closed Feature Envy scope-resolution failure."""

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


def resolve_feature_envy_scope(
    project_root: str | Path,
    target_files: Iterable[str | Path],
    location: relation_scope.LocationTarget | str,
    selector: Mapping[str, Any] | None = None,
    *,
    max_hops: int = DEFAULT_MAX_ANCESTOR_HOPS,
    max_files: int = DEFAULT_MAX_SCOPE_FILES,
    max_bytes: int = DEFAULT_MAX_SCOPE_BYTES,
) -> FeatureEnvyTargetScope:
    """Resolve only source files required by one Feature Envy target.

    ``target_files`` is the caller-frozen target scope, normally the single
    file named by ``location``.  It is not a list of receiver files and it is
    never replaced by project-wide Java discovery.  If the selected class has
    no explicit source parent, the exact target scope is returned unchanged.

    If inheritance is present, the existing bounded relation resolver performs
    exact declaration queries and package/import disambiguation.  Multiple
    wildcard imports that expose more than one declaration fail closed.
    """

    frozen_target_files = tuple(target_files)
    selected = _target_selector(selector)
    try:
        resolved = relation_scope.resolve_refused_bequest_relation_scope(
            project_root,
            frozen_target_files,
            location,
            selected,
            max_hops=max_hops,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except relation_scope.TargetRelationScopeError as exc:
        if exc.code == "ANCESTOR_RELATION_NOT_FOUND":
            return _target_only_scope(
                project_root,
                frozen_target_files,
                location,
                target_class=str(exc.details.get("target_class") or ""),
                max_files=max_files,
                max_bytes=max_bytes,
            )
        raise FeatureEnvyScopeError(
            exc.code,
            _feature_envy_message(exc.message),
            **exc.details,
        ) from exc

    return FeatureEnvyTargetScope(
        files=resolved.files,
        target_file=resolved.target_file,
        target_class=resolved.target_class,
        ancestors=resolved.ancestors,
        edges=tuple(
            FeatureEnvyScopeEdge.from_relation_edge(edge)
            for edge in resolved.edges
        ),
        source_bytes=resolved.source_bytes,
    )


def _target_only_scope(
    project_root: str | Path,
    target_files: Iterable[str | Path],
    location: relation_scope.LocationTarget | str,
    *,
    target_class: str,
    max_files: int,
    max_bytes: int,
) -> FeatureEnvyTargetScope:
    try:
        root = relation_scope._project_root(project_root)
        parsed_location = relation_scope._coerce_location(root, location)
        files = relation_scope._normalize_files(root, target_files)
        target_file = relation_scope._relative_java_path(
            root,
            parsed_location.file_path,
        )
        if target_file not in files:
            raise FeatureEnvyScopeError(
                "TARGET_FILE_NOT_IN_SCOPE",
                "Feature Envy target location is not present in target_files",
                target_file=target_file,
            )
        relation_scope._enforce_scope_budget(
            root,
            files,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        source_bytes = relation_scope._scope_bytes(root, files)
    except FeatureEnvyScopeError:
        raise
    except relation_scope.TargetRelationScopeError as exc:
        raise FeatureEnvyScopeError(
            exc.code,
            _feature_envy_message(exc.message),
            **exc.details,
        ) from exc

    return FeatureEnvyTargetScope(
        files=files,
        target_file=target_file,
        target_class=target_class,
        source_bytes=source_bytes,
    )


def _target_selector(
    selector: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep only target identity; receiver/evidence cannot widen the scope."""
    if selector is None:
        return {}
    if not isinstance(selector, Mapping):
        raise FeatureEnvyScopeError(
            "TARGET_SELECTOR_INVALID",
            "Feature Envy target selector must be an object",
        )
    allowed = {"class", "container_method", "method", "target_class"}
    result = {
        str(key): value
        for key, value in selector.items()
        if str(key) in allowed
    }
    nested = selector.get("entity_identity")
    if isinstance(nested, Mapping):
        identity = {
            str(key): value
            for key, value in nested.items()
            if str(key) in allowed
        }
        if identity:
            result["entity_identity"] = identity
    return result


def _feature_envy_message(message: str) -> str:
    return str(message).replace("Refused Bequest", "Feature Envy")


__all__ = [
    "DEFAULT_MAX_ANCESTOR_HOPS",
    "DEFAULT_MAX_SCOPE_BYTES",
    "DEFAULT_MAX_SCOPE_FILES",
    "FeatureEnvyScopeEdge",
    "FeatureEnvyScopeError",
    "FeatureEnvyTargetScope",
    "resolve_feature_envy_scope",
]
