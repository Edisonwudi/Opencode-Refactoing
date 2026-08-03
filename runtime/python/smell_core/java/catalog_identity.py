"""Stable identities for Java structural finding catalogs.

Structural checkpoint catalogs are compared across two independent detector
sessions.  Resolver output is not an entity identity: an unchanged declaration
may be rendered as ``Widget`` in one session and ``org.example.Widget`` (or an
ambiguous-type marker) in another as the available classpath inventory changes.

This module keeps the comparison source-oriented and diff-scoped.  It exposes
one identity contract for exact-clone method keys and Refused Bequest findings,
plus helpers that retain only additions inside the production-diff impact cone.
The direct cone is the set of changed production files; callers may add methods
or classes reached through a frozen call/hierarchy graph.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence


CATALOG_IDENTITY_SCHEMA = "java-source-entity-v2"
_AMBIGUOUS_TYPE_PREFIX = "__ambiguous_java_type__"
_JAVA_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_QUALIFIED_TYPE = re.compile(rf"(?:{_JAVA_IDENTIFIER}\.)+({_JAVA_IDENTIFIER})")
_TRAILING_PARAMETER_NAME = re.compile(
    rf"\b({_JAVA_IDENTIFIER})\s*((?:\[\s*\]\s*)*)$"
)
_ANNOTATION = re.compile(
    rf"@{_JAVA_IDENTIFIER}(?:\.{_JAVA_IDENTIFIER})*(?:\s*\([^()]*\))?\s*"
)


def normalize_catalog_path(value: Any) -> str:
    """Return the repository-relative path spelling used by detector catalogs."""
    text = str(value or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def stable_java_method_signature(
    value: Any,
    *,
    preserve_source_qualification: bool = False,
) -> str:
    """Canonicalize a detector-rendered Java method signature.

    Parameter names are deliberately absent. Resolver-added qualification is
    removed by default, but callers holding a lexical source signature can set
    ``preserve_source_qualification``. That distinction is important because
    ``foo(org.a.Widget)`` and ``foo(org.b.Widget)`` are legal overloads in one
    owner, while ``Widget`` versus resolver-rendered ``org.a.Widget`` is not a
    source edit.
    """
    text = str(value or "").strip()
    if "(" not in text or ")" not in text:
        return text.rsplit(".", 1)[-1]
    name, tail = text.split("(", 1)
    parameters = tail.rsplit(")", 1)[0]
    method_name = name.strip().rsplit(".", 1)[-1]
    canonical = [
        _stable_parameter_type(
            item,
            preserve_source_qualification=preserve_source_qualification,
        )
        for item in split_top_level_java_types(parameters)
        if item.strip()
    ]
    return f"{method_name}({','.join(canonical)})"


def stable_method_record_signature(method: Any) -> str:
    """Build a method signature from lexical source, never resolved types."""
    body = getattr(method, "body", None)
    declaration = getattr(body, "parent", None)
    parameters = (
        declaration.child_by_field_name("parameters")
        if declaration is not None
        else None
    )
    lexical_types: list[str] = []
    if parameters is not None:
        lexical_types = [
            _lexical_parameter_type(parameter)
            for parameter in parameters.named_children
            if getattr(parameter, "type", "")
            in {"formal_parameter", "spread_parameter", "receiver_parameter"}
        ]
    # A compact record constructor has no parameter node of its own; Java
    # defines its parameters to be the record header components. The semantic
    # model follows that rule, so its source-stable identity must read the same
    # lexical component list instead of treating the constructor as zero-arity.
    if (
        not lexical_types
        and getattr(declaration, "type", "") == "compact_constructor_declaration"
    ):
        owner = getattr(declaration, "parent", None)
        while owner is not None and getattr(owner, "type", "") != "record_declaration":
            owner = getattr(owner, "parent", None)
        record_parameters = (
            owner.child_by_field_name("parameters")
            if owner is not None
            else None
        )
        if record_parameters is not None:
            lexical_types = [
                _lexical_parameter_type(parameter)
                for parameter in record_parameters.named_children
                if getattr(parameter, "type", "")
                in {"formal_parameter", "spread_parameter", "receiver_parameter"}
            ]
    expected_arity = len(getattr(method, "parameter_types", ()) or ())
    if len(lexical_types) != expected_arity:
        raise ValueError(
            "stable Java method identity unavailable: lexical parameter arity "
            f"{len(lexical_types)} != detector arity {expected_arity}"
        )
    return (
        f"{getattr(method, 'method_name', '')}"
        f"({','.join(lexical_types)})"
    )


def stable_method_record_identity(method: Any) -> str:
    """Build a method key from lexical source, never from resolved types."""
    owner = str(
        getattr(method, "owner_qualified_name", "")
        or getattr(method, "class_name", "")
    ).strip()
    signature = stable_method_record_signature(method)
    return "#".join(
        (
            normalize_catalog_path(getattr(method, "file", "")),
            owner,
            signature,
        )
    )


def stable_clone_method_identity(value: Any) -> str:
    """Normalize one serialized ``file#owner#signature`` clone method key."""
    parts = str(value or "").split("#", 2)
    if len(parts) != 3:
        return str(value or "")
    file_name, owner, signature = parts
    return "#".join(
        (
            normalize_catalog_path(file_name),
            str(owner or "").strip(),
            stable_java_method_signature(
                signature,
                preserve_source_qualification=True,
            ),
        )
    )


def stable_refused_bequest_identity(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the source-level identity of one rejecting override finding."""
    inheritance_source = _stable_inheritance_source(entry.get("inheritance_source"))
    source_method = entry.get("source_method")
    method_identity = (
        stable_java_method_signature(
            source_method,
            preserve_source_qualification=True,
        )
        if str(source_method or "").strip()
        else stable_java_method_signature(entry.get("method"))
    )
    return (
        normalize_catalog_path(entry.get("file")),
        str(entry.get("class_name") or entry.get("class") or "").strip(),
        method_identity,
        str(entry.get("rule_id") or "").strip(),
        inheritance_source or _stable_type_name(entry.get("parent")),
    )


def clone_catalog_entries(value: Any) -> dict[str, set[str]]:
    """Read a clone catalog using source-stable method identities."""
    if not isinstance(value, list):
        return {}
    entries: dict[str, set[str]] = defaultdict(set)
    for item in value:
        if not isinstance(item, Mapping):
            continue
        fingerprint = str(item.get("fingerprint") or "")
        methods = item.get("methods")
        if not fingerprint or not isinstance(methods, list):
            continue
        entries[fingerprint].update(
            stable_clone_method_identity(method)
            for method in methods
            if str(method or "")
        )
    return dict(entries)


def feature_envy_catalog_additions_in_impact_cone(
    before_value: Any,
    after_value: Any,
    *,
    changed_files: Sequence[str],
) -> list[dict[str, Any]]:
    """Return new method-level Feature Envy findings in edited source files.

    A legal Move Method removes the detector finding. Merely moving the same
    envious method to another owner or file creates a new catalog identity in a
    production file touched by the patch and is therefore a regression.
    """
    before = _feature_envy_entries(before_value)
    after = _feature_envy_entries(after_value)
    changed = {
        normalize_catalog_path(path)
        for path in changed_files
        if str(path or "")
    }
    return [
        dict(after[identity])
        for identity in sorted(set(after).difference(before))
        if identity[0] in changed
    ]


def _feature_envy_entries(
    value: Any,
) -> dict[tuple[str, ...], Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    entries: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        item = {
            "file": normalize_catalog_path(raw.get("file")),
            "class_name": str(raw.get("class_name") or "").strip(),
            "method": stable_java_method_signature(
                raw.get("method"),
                preserve_source_qualification=True,
            ),
            "rule_id": str(raw.get("rule_id") or "").strip(),
            "envied_field": str(raw.get("envied_field") or "").strip(),
            "envied_type": str(raw.get("envied_type") or "").strip(),
        }
        identity = tuple(
            item[name]
            for name in ("file", "class_name", "method", "rule_id")
        )
        if all(identity):
            entries[identity] = item
    return entries


def clone_catalog_additions_in_impact_cone(
    before_value: Any,
    after_value: Any,
    *,
    changed_files: Sequence[str],
    affected_methods: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return new clone members whose source entities are diff-affected.

    ``affected_methods`` is for methods reached through the frozen structural
    graph even when their own file was not edited.  Unchanged entities outside
    that explicit cone cannot become additions solely because resolver output
    changed between detector sessions.
    """
    before = clone_catalog_entries(before_value)
    after = clone_catalog_entries(after_value)
    changed = {normalize_catalog_path(path) for path in changed_files if str(path or "")}
    affected = {
        stable_clone_method_identity(method)
        for method in affected_methods
        if str(method or "")
    }
    additions: list[dict[str, Any]] = []
    for fingerprint in sorted(after):
        new_methods = after[fingerprint].difference(before.get(fingerprint, set()))
        impacted = sorted(
            method
            for method in new_methods
            if _clone_method_file(method) in changed or method in affected
        )
        if not impacted:
            continue
        additions.append(
            {
                "fingerprint": fingerprint,
                "new_methods": impacted,
                "before_count": len(before.get(fingerprint, set())),
                "after_count": len(after[fingerprint]),
            }
        )
    return additions


def refused_bequest_catalog_additions_in_impact_cone(
    before_value: Any,
    after_value: Any,
    *,
    changed_files: Sequence[str],
    affected_classes: Iterable[str] = (),
    affected_methods: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return newly rejecting overrides inside the Java diff impact cone.

    A changed parent contract may create a genuine rejecting finding in an
    unchanged child.  ``affected_classes`` therefore accepts the class/type
    closure computed from a frozen hierarchy; both the finding owner and its
    parent are checked.  ``affected_methods`` accepts stable clone-style method
    keys for any additional call/hierarchy edges chosen by the caller.
    """
    before = _refused_entries(before_value)
    after = _refused_entries(after_value)
    changed = {normalize_catalog_path(path) for path in changed_files if str(path or "")}
    affected_types = {_stable_type_name(value) for value in affected_classes if str(value or "")}
    affected_method_ids = {
        stable_clone_method_identity(value)
        for value in affected_methods
        if str(value or "")
    }
    additions: list[dict[str, Any]] = []
    for identity in sorted(set(after).difference(before)):
        item = after[identity]
        file_name, owner, method, _rule, parent = identity
        method_id = f"{file_name}#{owner}#{method}"
        if not (
            file_name in changed
            or _stable_type_name(owner) in affected_types
            or parent in affected_types
            or method_id in affected_method_ids
        ):
            continue
        additions.append(dict(item))
    return additions


def _refused_entries(value: Any) -> dict[tuple[str, ...], Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    entries: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity = stable_refused_bequest_identity(item)
        if identity[0] and identity[1] and identity[2]:
            entries[identity] = item
    return entries


def _clone_method_file(value: str) -> str:
    return normalize_catalog_path(str(value or "").split("#", 1)[0])


def _stable_parameter_type(
    value: str,
    *,
    preserve_source_qualification: bool,
) -> str:
    text = _ANNOTATION.sub("", str(value or "").strip())
    text = re.sub(r"\bfinal\b\s*", "", text)
    trailing_arrays = ""
    match = _TRAILING_PARAMETER_NAME.search(text)
    prefix = text[: match.start()] if match else ""
    if match and prefix.strip() and not prefix.rstrip().endswith("."):
        trailing_arrays = re.sub(r"\s+", "", match.group(2) or "")
        text = prefix.strip()
    text = text.replace(_AMBIGUOUS_TYPE_PREFIX, "")
    text = text.replace("...", "[]")
    if not preserve_source_qualification:
        text = _QUALIFIED_TYPE.sub(r"\1", text)
    text = re.sub(r"\s+", "", text)
    return f"{text}{trailing_arrays}"


def _lexical_parameter_type(parameter: Any) -> str:
    type_node = parameter.child_by_field_name("type")
    if type_node is not None:
        text = _node_text(type_node)
    else:
        text = _node_text(parameter)
        name_node = parameter.child_by_field_name("name")
        if name_node is None:
            declarator = parameter.child_by_field_name("declarator") or next(
                (
                    child
                    for child in getattr(parameter, "named_children", ())
                    if getattr(child, "type", "") == "variable_declarator"
                ),
                None,
            )
            if declarator is not None:
                name_node = declarator.child_by_field_name("name")
        name = _node_text(name_node) if name_node is not None else ""
        if name:
            match = re.search(
                rf"\b{re.escape(name)}\b\s*((?:\[\s*\]\s*)*)$",
                text,
            )
            if match:
                suffix = re.sub(r"\s+", "", match.group(1) or "")
                text = f"{text[: match.start()].strip()}{suffix}"
    text = _ANNOTATION.sub("", text)
    text = re.sub(r"\bfinal\b\s*", "", text)
    text = text.replace("...", "[]")
    return re.sub(r"\s+", "", text)


def _node_text(node: Any) -> str:
    raw = getattr(node, "text", None)
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")


def _stable_type_name(value: Any) -> str:
    text = str(value or "").strip().replace(_AMBIGUOUS_TYPE_PREFIX, "")
    text = re.sub(r"<.*>", "", text).replace("[]", "")
    return text.rsplit(".", 1)[-1]


def _stable_inheritance_source(value: Any) -> str:
    """Normalize the lexical extends/implements clause without resolving it."""
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(re.sub(r"\s+", "", item) for item in parts)
    text = str(value or "").strip()
    return re.sub(r"\s+", "", text) if text else ""


def split_top_level_java_types(value: str) -> list[str]:
    """Split comma-separated Java types without cutting nested generics."""
    if not str(value or "").strip():
        return []
    items: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            items.append(value[start:index])
            start = index + 1
    items.append(value[start:])
    return items
