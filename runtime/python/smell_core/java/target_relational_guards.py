"""Target-only relational Java guards for LPL and Data Clumps.

These functions evaluate a caller-supplied target and an explicit source
scope.  They never discover Java files, enumerate parameter combinations, or
invoke a project smell detector.  Selector scores and thresholds are ignored;
the rule boundaries in this module are the product contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from ..guard_scope import GuardScopeError, read_current_bytes
from ..location import LocationTarget, parse_location_descriptor
from .detector_utils import (
    erase_java_type,
    normalize_erased_qualified_group as normalize_qualified_group,
    normalize_group,
)


LONG_PARAMETER_LIST_THRESHOLD = 6
DATA_CLUMPS_OCCURRENCE_THRESHOLD = 3
DATA_CLUMPS_CLASS_THRESHOLD = 3
DATA_CLUMPS_METHOD_NAME_THRESHOLD = 2

_CALLABLE_NODE_TYPES = frozenset(
    {"constructor_declaration", "method_declaration"}
)
_OWNER_NODE_TYPES = frozenset(
    {
        "annotation_type_declaration",
        "class_declaration",
        "enum_declaration",
        "interface_declaration",
        "record_declaration",
    }
)
_PARAMETER_NODE_TYPES = frozenset({"formal_parameter", "spread_parameter"})
_PRIMITIVES = frozenset(
    {"boolean", "byte", "char", "double", "float", "int", "long", "short"}
)
_JAVA_LANG_TYPES = frozenset(
    {
        "Boolean",
        "Byte",
        "Character",
        "CharSequence",
        "Class",
        "Comparable",
        "Double",
        "Enum",
        "Exception",
        "Float",
        "Integer",
        "Iterable",
        "Long",
        "Number",
        "Object",
        "RuntimeException",
        "Short",
        "String",
        "Throwable",
        "Void",
    }
)
_TYPE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_$])"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
)


@dataclass(frozen=True)
class _JavaCallable:
    file: str
    owner: str
    method: str
    signature: str
    parameter_types: tuple[str, ...]
    parameter_names: tuple[str, ...]
    begin_line: int
    end_line: int

    @property
    def descriptors(self) -> tuple[str, ...]:
        return tuple(
            f"{type_name}:{name.casefold()}"
            for type_name, name in zip(self.parameter_types, self.parameter_names)
        )

    @property
    def identity_key(self) -> str:
        return (
            f"{self.owner}#{self.method}"
            f"({','.join(self.parameter_types)})"
        )

    def witness(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "class": self.owner,
            "method": self.method,
            "signature": self.signature,
            "parameter_types": list(self.parameter_types),
            "parameter_count": len(self.parameter_types),
            "begin_line": self.begin_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class _ParsedScope:
    files: tuple[str, ...]
    callables: tuple[_JavaCallable, ...]


class TargetRelationalGuardError(ValueError):
    """Fail-closed input or parse failure for a target relational Guard."""

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


def evaluate_long_parameter_list_guard(
    project_root: str | Path,
    location: LocationTarget | str,
    selector: Mapping[str, Any] | None,
    *,
    analysis_files: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Evaluate the frozen long-signature target in an explicit file scope.

    At capture, the location selects one declaration and the returned
    ``entity_identity`` freezes its owner, name, and parameter types.  At
    verification, if that exact signature is absent, exactly one new short,
    strongly typed successor must be present in ``analysis_files``.  Existing
    baseline short overloads are not accepted as successors.
    """

    try:
        root = _project_root(project_root)
        target = _coerce_location(root, location)
        explicit_analysis = _normalize_paths(root, analysis_files or ())
        target_file = _relative_path(root, target.file_path)
        scope_files = tuple(sorted({target_file, *explicit_analysis}))
        parsed = _parse_scope(root, scope_files)
        selected = _selector_mapping(selector)
        identity = _selector_identity(selected)

        method_hint = _method_name(
            identity.get("method")
            or identity.get("container_method")
            or selected.get("container_method")
            or target.method
            or ""
        )
        owner_hint = str(
            identity.get("class")
            or identity.get("owner")
            or identity.get("target_class")
            or selected.get("target_class")
            or target.class_name
            or ""
        ).strip()
        frozen_types = _type_tuple(identity.get("parameter_types"))
        signature_hint_types = (
            frozen_types
            or _signature_hint_types(str(target.method or ""))
        )

        if frozen_types:
            matches = [
                item
                for item in parsed.callables
                if _same_method(item.method, method_hint)
                and _same_frozen_owner(item.owner, owner_hint)
                and _same_types(item.parameter_types, frozen_types)
            ]
        else:
            matches = [
                item
                for item in parsed.callables
                if item.file == target_file
                and (not method_hint or _same_method(item.method, method_hint))
                and (not owner_hint or _same_owner(item.owner, owner_hint))
                and (
                    not signature_hint_types
                    or _same_types(item.parameter_types, signature_hint_types)
                )
            ]
            target_count_hint = selected.get("target_parameter_count")
            if (
                not isinstance(target_count_hint, bool)
                and str(target_count_hint or "").isdigit()
            ):
                matches = [
                    item
                    for item in matches
                    if len(item.parameter_types) == int(target_count_hint)
                ]
            if target.line is not None:
                matches = [
                    item
                    for item in matches
                    if item.begin_line <= int(target.line) <= item.end_line
                ]

        matches.sort(key=_callable_sort_key)
        violations: list[dict[str, Any]] = []
        witness: dict[str, Any] = {"scope_files": list(parsed.files)}
        entity_identity = dict(identity)

        if len(matches) > 1:
            violations.append(
                _violation(
                    "TARGET_AMBIGUOUS",
                    "Long Parameter List target matched multiple declarations",
                    target_match_count=len(matches),
                )
            )
            return _result(
                ok=True,
                target_match_count=len(matches),
                target_smell_present=any(
                    len(item.parameter_types) >= LONG_PARAMETER_LIST_THRESHOLD
                    for item in matches
                ),
                target_missing=False,
                objectives={},
                entity_identity=entity_identity,
                witness=witness,
                guard_violations=violations,
            )

        if len(matches) == 1:
            match = matches[0]
            parameter_count = len(match.parameter_types)
            short_overloads = sorted(
                item.identity_key
                for item in parsed.callables
                if _same_frozen_owner(item.owner, match.owner)
                and _same_method(item.method, match.method)
                and len(item.parameter_types) < LONG_PARAMETER_LIST_THRESHOLD
                and item.identity_key != match.identity_key
            )
            if not entity_identity:
                entity_identity = {
                    "file": match.file,
                    "class": match.owner,
                    "method": match.method,
                    "signature": match.signature,
                    "parameter_types": list(match.parameter_types),
                    "baseline_short_overloads": short_overloads,
                }
            witness["target"] = match.witness()
            return _result(
                ok=True,
                target_match_count=1,
                target_smell_present=(
                    parameter_count >= LONG_PARAMETER_LIST_THRESHOLD
                ),
                target_missing=False,
                objectives={"parameter_count": parameter_count},
                entity_identity=entity_identity,
                witness=witness,
                guard_violations=violations,
            )

        if not frozen_types:
            violations.append(
                _violation(
                    "TARGET_NOT_FOUND",
                    "Long Parameter List target declaration was not found",
                )
            )
            return _result(
                ok=True,
                target_match_count=0,
                target_smell_present=False,
                target_missing=True,
                objectives={},
                entity_identity=entity_identity,
                witness=witness,
                guard_violations=violations,
            )

        baseline_short_overloads = {
            str(value)
            for value in identity.get("baseline_short_overloads", [])
            if str(value)
        }
        successor_candidates = [
            item
            for item in parsed.callables
            if item.file in explicit_analysis
            and _same_frozen_owner(item.owner, owner_hint)
            and _same_method(item.method, method_hint)
            and len(item.parameter_types) < LONG_PARAMETER_LIST_THRESHOLD
            and item.identity_key not in baseline_short_overloads
        ]
        successor_candidates.sort(key=_callable_sort_key)
        strong_successors = [
            item for item in successor_candidates if _is_strongly_typed(item)
        ]
        witness["successor_candidate_count"] = len(successor_candidates)
        witness["strong_successor_candidate_count"] = len(strong_successors)

        successor: _JavaCallable | None = None
        if len(successor_candidates) == 0:
            violations.append(
                _violation(
                    "LPL_SUCCESSOR_NOT_FOUND",
                    "Frozen long signature disappeared without a short successor in the changed scope",
                )
            )
        elif len(successor_candidates) > 1:
            violations.append(
                _violation(
                    "LPL_SUCCESSOR_AMBIGUOUS",
                    "Frozen long signature has multiple new short successors in the changed scope",
                    successor_candidate_count=len(successor_candidates),
                )
            )
        elif len(strong_successors) != 1:
            violations.append(
                _violation(
                    "LPL_STRONG_SUCCESSOR_REQUIRED",
                    "The unique short successor does not use explicit strong parameter types",
                )
            )
        else:
            successor = strong_successors[0]
            witness["successor"] = successor.witness()

        return _result(
            ok=True,
            target_match_count=0,
            target_smell_present=False,
            target_missing=True,
            objectives={
                "parameter_count": (
                    len(successor.parameter_types) if successor is not None else 0
                )
            },
            entity_identity=entity_identity,
            witness=witness,
            guard_violations=violations,
        )
    except (
        GuardScopeError,
        TargetRelationalGuardError,
        OSError,
        RuntimeError,
    ) as exc:
        return _error_result(exc)


def evaluate_data_clumps_guard(
    project_root: str | Path,
    location: LocationTarget | str,
    selector: Mapping[str, Any] | None,
    *,
    analysis_files: Iterable[str | Path] | None = None,
    source_files: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Measure exactly one selector-supplied parameter group.

    The target file first resolves the group identity. Candidate files are
    then parsed one at a time, so the active AST scope stays bounded even when
    a common parameter group occurs in many source files.
    """

    try:
        root = _project_root(project_root)
        target = _coerce_location(root, location)
        selected = _selector_mapping(selector)
        identity = _selector_identity(selected)
        raw_group = str(identity.get("group") or selected.get("group") or "")
        group = normalize_qualified_group(raw_group)
        members = tuple(item for item in group.split("|") if item)
        if len(members) < 3 or len(set(members)) != len(members):
            raise TargetRelationalGuardError(
                "DATA_CLUMPS_GROUP_INVALID",
                "Data Clumps selector.group must contain at least three distinct normalized members",
                group=group,
            )

        target_file = _relative_path(root, target.file_path)
        explicit_analysis = _normalize_paths(root, analysis_files or ())
        explicit_sources = _normalize_paths(root, source_files or ())
        scope_files = tuple(
            sorted({target_file, *explicit_analysis, *explicit_sources})
        )
        target_scope = _parse_scope(root, (target_file,))
        target_group_matches = [
            (item, matched)
            for item in target_scope.callables
            if _matches_data_clump_anchor(
                item,
                target=target,
                target_file=target_file,
                identity=identity,
            )
            for matched in [_match_exact_group(item, group)]
            if matched is not None
        ]
        anchor_group = group
        if not _has_anchor_identity(identity) and len(target_group_matches) == 1:
            anchor_group = target_group_matches[0][1][0]

        occurrence_count = 0
        owner_names: set[str] = set()
        method_names: set[str] = set()
        occurrence_files: set[str] = set()
        anchor_candidates: list[_JavaCallable] = []
        occurrence_preview: list[dict[str, Any]] = []
        parsed_files: list[str] = []
        preview_limit = 32
        for relative in scope_files:
            parsed = (
                target_scope
                if relative == target_file
                else _parse_scope(root, (relative,))
            )
            parsed_files.extend(parsed.files)
            for item in parsed.callables:
                matched = _match_anchored_group(item, anchor_group)
                if matched is None:
                    continue
                resolved_group, match_mode = matched
                occurrence_count += 1
                owner_names.add(item.owner)
                method_names.add(item.method)
                occurrence_files.add(item.file)
                if _matches_data_clump_anchor(
                    item,
                    target=target,
                    target_file=target_file,
                    identity=identity,
                ):
                    anchor_candidates.append(item)
                if len(occurrence_preview) < preview_limit:
                    occurrence_preview.append(
                        {
                            **item.witness(),
                            "group": resolved_group,
                            "match_mode": match_mode,
                        }
                    )

        violations: list[dict[str, Any]] = []
        class_count = len(owner_names)
        method_name_count = len(method_names)
        target_smell_present = (
            len(anchor_candidates) == 1
            and occurrence_count >= DATA_CLUMPS_OCCURRENCE_THRESHOLD
            and class_count >= DATA_CLUMPS_CLASS_THRESHOLD
            and method_name_count >= DATA_CLUMPS_METHOD_NAME_THRESHOLD
        )

        entity_identity = dict(identity)
        if not _has_anchor_identity(entity_identity) and len(anchor_candidates) == 1:
            anchor = anchor_candidates[0]
            entity_identity = {
                "file": anchor.file,
                "class": anchor.owner,
                "method": anchor.method,
                "parameter_types": list(anchor.parameter_types),
                "group": anchor_group,
            }
        if len(anchor_candidates) > 1:
            violations.append(
                _violation(
                    "TARGET_AMBIGUOUS",
                    "Data Clumps target matched multiple scoped occurrences",
                    target_match_count=len(anchor_candidates),
                )
            )
            target_smell_present = False

        scope_preview_limit = 64
        class_preview = sorted(owner_names)[:scope_preview_limit]
        method_preview = sorted(method_names)[:scope_preview_limit]
        occurrence_file_preview = sorted(occurrence_files)[:scope_preview_limit]
        witness = {
            "group": anchor_group,
            "requested_group": group,
            "scope_file_count": len(parsed_files),
            "scope_files": parsed_files[:scope_preview_limit],
            "scope_files_truncated": len(parsed_files) > scope_preview_limit,
            "occurrence_file_count": len(occurrence_files),
            "occurrence_files": occurrence_file_preview,
            "occurrence_files_truncated": (
                len(occurrence_files) > scope_preview_limit
            ),
            "occurrences": occurrence_preview,
            "occurrence_preview_truncated": occurrence_count > preview_limit,
            "class_preview": class_preview,
            "classes_truncated": len(owner_names) > scope_preview_limit,
            "method_name_preview": method_preview,
            "method_names_truncated": len(method_names) > scope_preview_limit,
            "scan_mode": "target_anchor_then_stream",
        }
        return _result(
            ok=True,
            target_match_count=len(anchor_candidates),
            target_smell_present=target_smell_present,
            target_missing=len(anchor_candidates) == 0,
            objectives={
                "occurrence_count": occurrence_count,
                "class_count": class_count,
                "method_name_count": method_name_count,
            },
            entity_identity=entity_identity,
            witness=witness,
            guard_violations=violations,
        )
    except (
        GuardScopeError,
        TargetRelationalGuardError,
        OSError,
        RuntimeError,
    ) as exc:
        return _error_result(exc)


def _project_root(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise TargetRelationalGuardError(
            "PROJECT_ROOT_UNREADABLE",
            "Target relational Guard project root is not a directory",
            project_root=str(root),
        )
    return root


def _coerce_location(
    project_root: Path,
    location: LocationTarget | str,
) -> LocationTarget:
    if isinstance(location, LocationTarget):
        _relative_path(project_root, location.file_path)
        return location
    try:
        return parse_location_descriptor(str(location), project_root)
    except (TypeError, ValueError) as exc:
        raise TargetRelationalGuardError(
            "TARGET_LOCATION_INVALID",
            f"Cannot parse target location: {exc}",
        ) from exc


def _normalize_paths(
    project_root: Path,
    values: Iterable[str | Path],
) -> tuple[str, ...]:
    return tuple(sorted({_relative_path(project_root, value) for value in values}))


def _relative_path(project_root: Path, value: str | Path) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / PurePosixPath(str(value).replace("\\", "/"))
    try:
        relative = candidate.resolve(strict=False).relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise TargetRelationalGuardError(
            "SCOPED_SOURCE_OUTSIDE_PROJECT",
            "Explicit Guard source is outside the project root",
            path=str(value),
        ) from exc
    if relative.suffix.casefold() != ".java":
        raise TargetRelationalGuardError(
            "SCOPED_SOURCE_NOT_JAVA",
            "Explicit Guard source must be a Java file",
            path=relative.as_posix(),
        )
    return relative.as_posix()


def _parse_scope(project_root: Path, files: Sequence[str]) -> _ParsedScope:
    parser = get_parser("java")
    callables: list[_JavaCallable] = []
    existing_files: list[str] = []
    for relative in files:
        source = read_current_bytes(project_root, relative)
        if source is None:
            continue
        existing_files.append(relative)
        root = parser.parse(source).root_node
        if root.has_error:
            raise TargetRelationalGuardError(
                "JAVA_PARSE_FAILED",
                "Explicit Guard source contains Java parse errors",
                path=relative,
            )
        package, imports, wildcard_imports = _source_namespace(root, source)
        local_types = _local_type_identities(root, source, package)
        for node in _walk(root):
            if node.type not in _CALLABLE_NODE_TYPES:
                continue
            parsed = _callable_from_node(
                relative,
                node,
                source,
                package=package,
                imports=imports,
                wildcard_imports=wildcard_imports,
                local_types=local_types,
            )
            if parsed is not None:
                callables.append(parsed)
    callables.sort(key=_callable_sort_key)
    return _ParsedScope(
        files=tuple(existing_files),
        callables=tuple(callables),
    )


def _source_namespace(
    root: Node,
    source: bytes,
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    package = ""
    imports: dict[str, str] = {}
    wildcard_imports: list[str] = []
    for child in root.named_children:
        text = _node_text(source, child).strip()
        if child.type == "package_declaration":
            match = re.search(r"\bpackage\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)", text)
            package = match.group(1) if match else ""
        elif child.type == "import_declaration" and " static " not in f" {text} ":
            match = re.search(r"\bimport\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$*][\w$*]*)*)", text)
            if match is None:
                continue
            imported = match.group(1)
            if imported.endswith(".*"):
                wildcard_imports.append(imported[:-2])
            else:
                imports[imported.rsplit(".", 1)[-1]] = imported
    return package, imports, tuple(sorted(set(wildcard_imports)))


def _local_type_identities(
    root: Node,
    source: bytes,
    package: str,
) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for node in _walk(root):
        if node.type not in _OWNER_NODE_TYPES:
            continue
        names: list[str] = []
        current: Node | None = node
        while current is not None:
            if current.type in _OWNER_NODE_TYPES:
                name_node = current.child_by_field_name("name")
                name = _node_text(source, name_node).strip() if name_node else ""
                if name:
                    names.append(name)
            current = current.parent
        names.reverse()
        if not names:
            continue
        local = ".".join(names)
        qualified = f"{package}.{local}" if package else local
        for key in {names[-1], local}:
            candidates.setdefault(key, set()).add(qualified)
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if len(values) == 1
    }


def _callable_from_node(
    relative: str,
    node: Node,
    source: bytes,
    *,
    package: str,
    imports: Mapping[str, str],
    wildcard_imports: Sequence[str],
    local_types: Mapping[str, str],
) -> _JavaCallable | None:
    name_node = node.child_by_field_name("name")
    parameters = node.child_by_field_name("parameters")
    if name_node is None or parameters is None:
        return None
    method = _node_text(source, name_node).strip()
    if not method:
        return None
    type_parameters = _type_parameter_erasures(
        node,
        source,
        package=package,
        imports=imports,
        wildcard_imports=wildcard_imports,
        local_types=local_types,
    )
    parameter_types: list[str] = []
    parameter_names: list[str] = []
    for parameter in parameters.named_children:
        if parameter.type not in _PARAMETER_NODE_TYPES:
            continue
        type_node = _parameter_type_node(parameter)
        parameter_name = _parameter_name_node(parameter)
        if type_node is None or parameter_name is None:
            continue
        type_name = _canonical_type(
            _node_text(source, type_node),
            package=package,
            imports=imports,
            wildcard_imports=wildcard_imports,
            local_types=local_types,
            type_parameters=type_parameters,
        )
        if parameter.type == "spread_parameter":
            type_name += "..."
        name = _node_text(source, parameter_name).strip()
        if type_name and name:
            parameter_types.append(type_name)
            parameter_names.append(name)
    owner = _owner_identity(node, source, package)
    signature = (
        f"{method}("
        + ", ".join(
            f"{type_name} {name}"
            for type_name, name in zip(parameter_types, parameter_names)
        )
        + ")"
    )
    return _JavaCallable(
        file=relative,
        owner=owner,
        method=method,
        signature=signature,
        parameter_types=tuple(parameter_types),
        parameter_names=tuple(parameter_names),
        begin_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
    )


def _parameter_type_node(parameter: Node) -> Node | None:
    direct = parameter.child_by_field_name("type")
    if direct is not None:
        return direct
    for child in parameter.named_children:
        if child.type in {
            "array_type",
            "boolean_type",
            "floating_point_type",
            "generic_type",
            "integral_type",
            "scoped_type_identifier",
            "type_identifier",
        }:
            return child
    return None


def _parameter_name_node(parameter: Node) -> Node | None:
    direct = parameter.child_by_field_name("name")
    if direct is not None:
        return direct
    declarator = parameter.child_by_field_name("declarator")
    if declarator is None:
        declarator = next(
            (
                child
                for child in parameter.named_children
                if child.type == "variable_declarator"
            ),
            None,
        )
    return (
        declarator.child_by_field_name("name")
        if declarator is not None
        else None
    )


def _canonical_type(
    raw: str,
    *,
    package: str = "",
    imports: Mapping[str, str] | None = None,
    wildcard_imports: Sequence[str] = (),
    local_types: Mapping[str, str] | None = None,
    type_parameters: Mapping[str, str] | None = None,
) -> str:
    text = re.sub(
        r"\s+",
        "",
        erase_java_type(str(raw or ""), varargs_as_array=False),
    )
    imports = imports or {}
    local_types = local_types or {}
    type_parameters = type_parameters or {}

    def resolve(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in _PRIMITIVES or token in {"extends", "super", "void"}:
            return token
        if token in type_parameters:
            return str(type_parameters[token])
        if token in imports:
            return str(imports[token])
        if token in local_types:
            return str(local_types[token])
        if token in _JAVA_LANG_TYPES:
            return f"java.lang.{token}"
        if "." in token:
            first, remainder = token.split(".", 1)
            if first in imports:
                return f"{imports[first]}.{remainder}"
            return token
        if len(token) == 1 and token.isupper():
            return token
        if wildcard_imports and token[:1].isupper():
            if len(wildcard_imports) == 1:
                return f"{wildcard_imports[0]}.{token}"
            # Without a project catalog there is no sound way to choose one
            # of several on-demand imports. Keep the simple type unresolved;
            # the target-anchored matcher will only compare compatible types.
            return token
        if package and token[:1].isupper():
            return f"{package}.{token}"
        return token

    return _TYPE_TOKEN.sub(resolve, text)


def _type_parameter_erasures(
    node: Node,
    source: bytes,
    *,
    package: str,
    imports: Mapping[str, str],
    wildcard_imports: Sequence[str],
    local_types: Mapping[str, str],
) -> dict[str, str]:
    """Resolve class and method type variables to their Java erasures."""
    owners: list[Node] = []
    current = node.parent
    while current is not None:
        if current.type in _OWNER_NODE_TYPES:
            owners.append(current)
        current = current.parent
    declarations = [*reversed(owners), node]
    resolved: dict[str, str] = {}
    for declaration in declarations:
        parameters = next(
            (
                child
                for child in declaration.named_children
                if child.type == "type_parameters"
            ),
            None,
        )
        if parameters is None:
            continue
        for parameter in parameters.named_children:
            if parameter.type != "type_parameter":
                continue
            name_node = next(
                (
                    child
                    for child in parameter.named_children
                    if child.type == "type_identifier"
                ),
                None,
            )
            if name_node is None:
                continue
            name = _node_text(source, name_node).strip()
            if not name:
                continue
            bound_text = "java.lang.Object"
            bound = next(
                (
                    child
                    for child in parameter.named_children
                    if child.type == "type_bound"
                ),
                None,
            )
            if bound is not None:
                first_bound = next(
                    (
                        child
                        for child in bound.named_children
                        if child.type
                        in {
                            "array_type",
                            "generic_type",
                            "scoped_type_identifier",
                            "type_identifier",
                        }
                    ),
                    None,
                )
                if first_bound is not None:
                    bound_text = _node_text(source, first_bound)
            resolved[name] = _canonical_type(
                bound_text,
                package=package,
                imports=imports,
                wildcard_imports=wildcard_imports,
                local_types=local_types,
                type_parameters=resolved,
            )
    return resolved


def _owner_identity(node: Node, source: bytes, package: str) -> str:
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in _OWNER_NODE_TYPES:
            name_node = current.child_by_field_name("name")
            name = _node_text(source, name_node).strip() if name_node else ""
            if name:
                names.append(name)
        current = current.parent
    names.reverse()
    local = ".".join(names)
    return f"{package}.{local}" if package and local else local or package


def _selector_identity(selector: Mapping[str, Any]) -> dict[str, Any]:
    nested = selector.get("entity_identity")
    if isinstance(nested, Mapping):
        return {str(key): value for key, value in nested.items()}
    identity_keys = {
        "baseline_short_overloads",
        "class",
        "file",
        "group",
        "method",
        "owner",
        "parameter_types",
        "signature",
        "target_class",
    }
    return {
        str(key): value
        for key, value in selector.items()
        if str(key) in identity_keys
    }


def _selector_mapping(
    selector: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if selector is None:
        return {}
    if not isinstance(selector, Mapping):
        raise TargetRelationalGuardError(
            "TARGET_SELECTOR_INVALID",
            "Target relational Guard selector must be an object",
        )
    return {str(key): value for key, value in selector.items()}


def _signature_hint_types(signature: str) -> tuple[str, ...]:
    if "(" not in signature or ")" not in signature:
        return ()
    inside = signature.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inside:
        return ()
    types: list[str] = []
    for raw in _split_top_level_commas(inside):
        part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
        part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
        chunks = re.sub(r"\s+", " ", part).strip().split(" ")
        type_text = part if len(chunks) == 1 else " ".join(chunks[:-1])
        types.append(_canonical_type(type_text))
    return tuple(types)


def _split_top_level_commas(value: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    angle = paren = bracket = 0
    for char in value:
        if char == "<":
            angle += 1
        elif char == ">" and angle:
            angle -= 1
        elif char == "(":
            paren += 1
        elif char == ")" and paren:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket:
            bracket -= 1
        if char == "," and angle == paren == bracket == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        result.append(tail)
    return result


def _type_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_canonical_type(str(item)) for item in value)


def _method_name(value: Any) -> str:
    text = str(value or "").strip()
    before = text.split("(", 1)[0].strip()
    return re.split(r"[.#\s]+", before)[-1] if before else ""


def _same_method(actual: str, expected: str) -> bool:
    return not expected or actual == expected


def _same_owner(actual: str, expected: str) -> bool:
    if not expected:
        return True
    return actual == expected or actual.rsplit(".", 1)[-1] == expected.rsplit(".", 1)[-1]


def _same_frozen_owner(actual: str, expected: str) -> bool:
    return bool(
        actual
        and expected
        and actual.replace("$", ".") == expected.replace("$", ".")
    )


def _same_types(actual: Sequence[str], expected: Sequence[str]) -> bool:
    if len(actual) != len(expected):
        return False
    for actual_type, expected_type in zip(actual, expected):
        if actual_type == expected_type:
            continue
        if _simple_type(actual_type) != _simple_type(expected_type):
            return False
    return True


def _simple_type(type_name: str) -> str:
    return re.sub(
        r"(?:[a-z_][a-z0-9_$]*\.)+([A-Za-z_$][A-Za-z0-9_$]*)",
        r"\1",
        str(type_name),
        flags=re.IGNORECASE,
    )


def _is_strongly_typed(item: _JavaCallable) -> bool:
    if not item.parameter_types:
        return False
    for type_name in item.parameter_types:
        compact = type_name.replace(" ", "")
        erased = re.sub(r"<.*>", "", compact).removesuffix("...")
        lowered = erased.casefold()
        if lowered in {"?", "java.lang.object", "object", "var"}:
            return False
        if (
            lowered.rsplit(".", 1)[-1]
            in {"collection", "iterable", "list", "map", "set"}
            and "<" not in compact
        ):
            return False
    return True


def _match_exact_group(
    item: _JavaCallable,
    group: str,
) -> tuple[str, str] | None:
    qualified_members = tuple(
        normalize_qualified_group(descriptor) for descriptor in item.descriptors
    )
    simple_members = tuple(normalize_group(descriptor) for descriptor in item.descriptors)
    requested_qualified = tuple(group.split("|"))
    requested_simple = tuple(normalize_group(group).split("|"))

    selected: list[str] = []
    unused = set(range(len(qualified_members)))
    match_mode = "qualified"
    for member in requested_qualified:
        index = next(
            (
                candidate
                for candidate in sorted(unused)
                if qualified_members[candidate] == member
            ),
            None,
        )
        if index is None:
            selected = []
            break
        unused.remove(index)
        selected.append(item.descriptors[index])
    if not selected:
        match_mode = "simple"
        unused = set(range(len(simple_members)))
        for member in requested_simple:
            index = next(
                (
                    candidate
                    for candidate in sorted(unused)
                    if simple_members[candidate] == member
                ),
                None,
            )
            if index is None:
                return None
            unused.remove(index)
            selected.append(item.descriptors[index])
    return normalize_qualified_group("|".join(selected)), match_mode


def _match_anchored_group(
    item: _JavaCallable,
    group: str,
) -> tuple[str, str] | None:
    """Match a callable against the target-resolved group identity.

    Fully qualified conflicts are rejected. A simple type is accepted only
    when its normalized descriptor agrees and at least one side is genuinely
    unresolved (for example, multiple wildcard imports). This prevents an
    unrelated same-simple-name type from invalidating or joining the target
    clump while keeping the Guard independent of a project type catalog.
    """
    exact = _match_exact_qualified_group(item, group)
    if exact is not None:
        return exact, "qualified"

    requested = tuple(group.split("|"))
    actual = tuple(item.descriptors)
    unused = set(range(len(actual)))
    selected: list[str] = []
    for member in requested:
        index = next(
            (
                candidate
                for candidate in sorted(unused)
                if _compatible_group_member(actual[candidate], member)
            ),
            None,
        )
        if index is None:
            return None
        unused.remove(index)
        selected.append(actual[index])
    return normalize_qualified_group("|".join(selected)), "compatible"


def _match_exact_qualified_group(
    item: _JavaCallable,
    group: str,
) -> str | None:
    qualified_members = tuple(
        normalize_qualified_group(descriptor) for descriptor in item.descriptors
    )
    unused = set(range(len(qualified_members)))
    selected: list[str] = []
    for member in group.split("|"):
        index = next(
            (
                candidate
                for candidate in sorted(unused)
                if qualified_members[candidate] == member
            ),
            None,
        )
        if index is None:
            return None
        unused.remove(index)
        selected.append(item.descriptors[index])
    return normalize_qualified_group("|".join(selected))


def _compatible_group_member(actual: str, expected: str) -> bool:
    if normalize_group(actual) != normalize_group(expected):
        return False
    actual_type = _descriptor_type(actual)
    expected_type = _descriptor_type(expected)
    if actual_type == expected_type:
        return True
    return not (
        _is_fully_qualified_type(actual_type)
        and _is_fully_qualified_type(expected_type)
    )


def _descriptor_type(descriptor: str) -> str:
    return str(descriptor).rsplit(":", 1)[0].strip()


def _is_fully_qualified_type(type_name: str) -> bool:
    erased = re.sub(r"<.*>", "", str(type_name)).removesuffix("...")
    erased = erased.removesuffix("[]")
    first = erased.split(".", 1)[0]
    return "." in erased and bool(first) and first[:1].islower()


def _matches_data_clump_anchor(
    item: _JavaCallable,
    *,
    target: LocationTarget,
    target_file: str,
    identity: Mapping[str, Any],
) -> bool:
    if _has_anchor_identity(identity):
        method = _method_name(identity.get("method") or "")
        owner = str(identity.get("class") or identity.get("owner") or "")
        types = _type_tuple(identity.get("parameter_types"))
        frozen_file = _frozen_file(identity.get("file")) or target_file
        owner_matches = not owner or (
            _same_frozen_owner(item.owner, owner)
            if identity.get("file")
            else _same_owner(item.owner, owner)
        )
        return (
            frozen_file == target_file
            and item.file == frozen_file
            and _same_method(item.method, method)
            and owner_matches
            and (not types or _same_types(item.parameter_types, types))
        )
    if item.file != target_file:
        return False
    if target.method and not _same_method(item.method, _method_name(target.method)):
        return False
    if target.class_name and not _same_owner(item.owner, target.class_name):
        return False
    if target.line is not None and not (
        item.begin_line <= int(target.line) <= item.end_line
    ):
        return False
    hint_types = _signature_hint_types(str(target.method or ""))
    return not hint_types or _same_types(item.parameter_types, hint_types)


def _has_anchor_identity(identity: Mapping[str, Any]) -> bool:
    return any(
        identity.get(key)
        for key in ("class", "file", "method", "owner", "parameter_types")
    )


def _frozen_file(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    return PurePosixPath(text).as_posix()


def _callable_sort_key(item: _JavaCallable) -> tuple[Any, ...]:
    return (
        item.file,
        item.begin_line,
        item.owner,
        item.method,
        item.parameter_types,
    )


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def _violation(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _result(
    *,
    ok: bool,
    target_match_count: int,
    target_smell_present: bool,
    target_missing: bool,
    objectives: Mapping[str, Any],
    entity_identity: Mapping[str, Any],
    witness: Mapping[str, Any],
    guard_violations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "target_match_count": int(target_match_count),
        "target_smell_present": bool(target_smell_present),
        "target_missing": bool(target_missing),
        "objectives": dict(objectives),
        "entity_identity": dict(entity_identity),
        "witness": dict(witness),
        "guard_violations": [dict(item) for item in guard_violations],
    }


def _error_result(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TargetRelationalGuardError):
        violation = exc.violation()
    elif isinstance(exc, GuardScopeError):
        violation = {
            "code": exc.status,
            "message": exc.message,
            **exc.details,
        }
    else:
        violation = {
            "code": "TARGET_GUARD_UNAVAILABLE",
            "message": str(exc),
        }
    return _result(
        ok=False,
        target_match_count=0,
        target_smell_present=False,
        target_missing=True,
        objectives={},
        entity_identity={},
        witness={},
        guard_violations=[violation],
    )


__all__ = [
    "DATA_CLUMPS_CLASS_THRESHOLD",
    "DATA_CLUMPS_METHOD_NAME_THRESHOLD",
    "DATA_CLUMPS_OCCURRENCE_THRESHOLD",
    "LONG_PARAMETER_LIST_THRESHOLD",
    "TargetRelationalGuardError",
    "evaluate_data_clumps_guard",
    "evaluate_long_parameter_list_guard",
]
