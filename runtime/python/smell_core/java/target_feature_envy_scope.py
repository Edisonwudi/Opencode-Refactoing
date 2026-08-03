"""Conditional exact source scope for a target Feature Envy Guard.

Feature Envy is evaluated at one caller-supplied method.  The ordinary case
therefore needs only the file containing that method.  A source relation is
added only when the target predicate actually needs it: an inherited owner
field follows the exact ancestor chain, while an ambiguously imported type of
an owner field used as a member receiver follows one exact type-declaration
query.  Parameters, locals, unrelated imports, and unused fields never widen
the scope.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import target_relation_scope as relation_scope
from . import semantic_detector as semantic


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
class FeatureEnvyReceiverType:
    """One target-field receiver type admitted by an exact declaration query."""

    field: str
    source_type: str
    resolved_type: str
    declaration_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source_type": self.source_type,
            "resolved_type": self.resolved_type,
            "declaration_files": list(self.declaration_files),
        }


@dataclass(frozen=True)
class FeatureEnvyTargetScope:
    """The complete and bounded Java source scope for one target method."""

    files: tuple[str, ...]
    target_file: str
    target_class: str
    ancestors: tuple[str, ...] = ()
    edges: tuple[FeatureEnvyScopeEdge, ...] = ()
    receiver_types: tuple[FeatureEnvyReceiverType, ...] = ()
    source_bytes: int = 0

    @property
    def expanded_for_inheritance(self) -> bool:
        return bool(self.edges)

    @property
    def expanded_for_receivers(self) -> bool:
        return bool(self.receiver_types)

    def witness(self) -> dict[str, Any]:
        return {
            "target_file": self.target_file,
            "target_class": self.target_class,
            "scope_files": list(self.files),
            "ancestors": list(self.ancestors),
            "edges": [item.as_dict() for item in self.edges],
            "expanded_for_inheritance": self.expanded_for_inheritance,
            "receiver_types": [item.as_dict() for item in self.receiver_types],
            "expanded_for_receivers": self.expanded_for_receivers,
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
    file named by ``location``.  It is never replaced by project-wide Java
    discovery.  Exact declaration queries admit only source types already
    referenced by the selected target predicate.
    """

    frozen_target_files = tuple(target_files)
    selected = _target_selector(selector)
    target_only = _target_only_scope(
        project_root,
        frozen_target_files,
        location,
        target_class="",
        max_files=max_files,
        max_bytes=max_bytes,
    )
    receiver_only = _expand_receiver_type_scope(
        project_root,
        target_only,
        location,
        selected,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    if _target_profile_present(
        project_root,
        receiver_only,
        location,
        selected,
    ):
        return receiver_only

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
            return receiver_only
        raise FeatureEnvyScopeError(
            exc.code,
            _feature_envy_message(exc.message),
            **exc.details,
        ) from exc

    inherited = FeatureEnvyTargetScope(
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
    return _expand_receiver_type_scope(
        project_root,
        inherited,
        location,
        selected,
        max_files=max_files,
        max_bytes=max_bytes,
    )


def _expand_receiver_type_scope(
    project_root: str | Path,
    scope: FeatureEnvyTargetScope,
    location: relation_scope.LocationTarget | str,
    selector: Mapping[str, Any],
    *,
    max_files: int,
    max_bytes: int,
) -> FeatureEnvyTargetScope:
    """Add declarations only for ambiguous owner-field receivers in target."""
    try:
        root = relation_scope._project_root(project_root)
        parsed_location = relation_scope._coerce_location(root, location)
        scope_files = set(scope.files)
        model = relation_scope._build_model(root, scope_files)
        target = relation_scope._select_target_class(
            model,
            parsed_location,
            selector,
            target_file=scope.target_file,
        )
        method = _select_target_method(
            model,
            target,
            parsed_location,
            selector,
        )
        if method is None:
            return replace(
                scope,
                target_class=target.qualified_name,
            )

        owner = semantic._feature_envy_owner_view(model, target)
        shadowed = set(method.parameters).union(method.local_variables)
        aliases = semantic._stable_owner_field_aliases(method, owner)
        receiver_fields = {
            field_name
            for expression in semantic._member_access_receiver_expressions(method.body)
            if (
                field_name := semantic._owner_field_for_receiver_expression(
                    expression,
                    owner,
                    shadowed_names=shadowed,
                    aliases=aliases,
                )
            )
        }
        unresolved_fields = {
            field_name: owner.fields.get(field_name, "")
            for field_name in receiver_fields
            if semantic._model_type_is_ambiguous(
                model,
                owner.fields.get(field_name, ""),
            )
        }
        if not unresolved_fields:
            return replace(
                scope,
                target_class=target.qualified_name,
            )

        queried_by_type: dict[str, tuple[str, ...]] = {}
        field_source_types: dict[str, str] = {}
        for field_name, unresolved_type in sorted(unresolved_fields.items()):
            simple_name = _ambiguous_simple_type(unresolved_type)
            if not simple_name:
                continue
            field_source_types[field_name] = simple_name
            if simple_name in queried_by_type:
                continue
            declaration = _field_declaring_class(model, target, field_name)
            file_model = _file_model(model, declaration.file if declaration else "")
            packages = _candidate_type_packages(file_model, simple_name)
            candidates: set[str] = set()
            for package in packages:
                expected = f"{package}.{simple_name}" if package else simple_name
                candidates.update(
                    relation_scope.query_type_declaration_files(
                        root,
                        simple_name,
                        source_name=simple_name,
                        expected_qualified=expected,
                        max_files=max_files,
                        max_bytes=max_bytes,
                        existing_files=scope_files.union(candidates),
                    )
                )
            queried_by_type[simple_name] = tuple(sorted(candidates))
            scope_files.update(candidates)

        relation_scope._enforce_scope_budget(
            root,
            scope_files,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        final_model = relation_scope._build_model(root, scope_files)
        final_target = relation_scope._select_target_class(
            final_model,
            parsed_location,
            selector,
            target_file=scope.target_file,
        )
        final_owner = semantic._feature_envy_owner_view(final_model, final_target)
        receiver_types = tuple(
            FeatureEnvyReceiverType(
                field=field_name,
                source_type=source_type,
                resolved_type=final_owner.fields.get(field_name, ""),
                declaration_files=queried_by_type.get(source_type, ()),
            )
            for field_name, source_type in sorted(field_source_types.items())
            if queried_by_type.get(source_type)
        )
        return replace(
            scope,
            files=tuple(sorted(scope_files)),
            target_class=final_target.qualified_name,
            receiver_types=receiver_types,
            source_bytes=relation_scope._scope_bytes(root, scope_files),
        )
    except FeatureEnvyScopeError:
        raise
    except relation_scope.TargetRelationScopeError as exc:
        raise FeatureEnvyScopeError(
            exc.code,
            _feature_envy_message(exc.message),
            **exc.details,
        ) from exc


def _target_profile_present(
    project_root: str | Path,
    scope: FeatureEnvyTargetScope,
    location: relation_scope.LocationTarget | str,
    selector: Mapping[str, Any],
) -> bool:
    try:
        root = relation_scope._project_root(project_root)
        parsed_location = relation_scope._coerce_location(root, location)
        model = relation_scope._build_model(root, scope.files)
        target = relation_scope._select_target_class(
            model,
            parsed_location,
            selector,
            target_file=scope.target_file,
        )
        method = _select_target_method(model, target, parsed_location, selector)
        return bool(
            method is not None
            and semantic._designite_feature_envy_profile(
                model,
                method,
            ).envy_access_diff > 1
        )
    except relation_scope.TargetRelationScopeError:
        return False


def _select_target_method(
    model: semantic.ProjectModel,
    target: semantic.ClassRecord,
    location: relation_scope.LocationTarget,
    selector: Mapping[str, Any],
) -> semantic.MethodRecord | None:
    method_hint = (
        relation_scope._selector_value(selector, "method")
        or relation_scope._selector_value(selector, "container_method")
        or str(location.method or "")
    )
    candidates = [
        item
        for item in model.methods
        if item.owner_qualified_name == target.qualified_name
    ]
    if method_hint:
        method_matches = [
            item
            for item in candidates
            if relation_scope._same_method(item.method_signature, method_hint)
        ]
        if method_matches or location.line is None:
            candidates = method_matches
    if location.line is not None:
        containing = [
            item
            for item in candidates
            if item.begin_line <= int(location.line) <= item.end_line
        ]
        if containing:
            candidates = containing
    if len(candidates) != 1:
        return None
    return candidates[0]


def _ambiguous_simple_type(value: str) -> str:
    erased = semantic._erase_type(value)
    if not erased.startswith(semantic.AMBIGUOUS_TYPE_PREFIX):
        return ""
    return erased[len(semantic.AMBIGUOUS_TYPE_PREFIX) :].rsplit(".", 1)[-1]


def _field_declaring_class(
    model: semantic.ProjectModel,
    target: semantic.ClassRecord,
    field_name: str,
) -> semantic.ClassRecord | None:
    pending = [target]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current.qualified_name in seen:
            continue
        seen.add(current.qualified_name)
        if field_name in current.fields:
            return current
        for parent_name in [current.superclass_name, *current.interface_names]:
            parent = semantic._class_record_for_type(model, parent_name)
            if parent is not None and parent.qualified_name not in seen:
                pending.append(parent)
    return None


def _file_model(
    model: semantic.ProjectModel,
    relative: str,
) -> semantic.JavaFileModel | None:
    return next((item for item in model.files if item.rel_path == relative), None)


def _candidate_type_packages(
    file_model: semantic.JavaFileModel | None,
    simple_name: str,
) -> tuple[str, ...]:
    if file_model is None:
        return ("",)
    imported = str(file_model.imports.get(simple_name) or "")
    if imported and "." in imported:
        return (imported.rsplit(".", 1)[0],)
    return tuple(
        sorted(
            {
                file_model.package,
                *file_model.wildcard_imports,
            }
        )
    ) or ("",)


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
    "FeatureEnvyReceiverType",
    "FeatureEnvyTargetScope",
    "resolve_feature_envy_scope",
]
