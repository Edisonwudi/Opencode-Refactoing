"""Source-derived non-Java God Class metrics and product predicate.

Python and C++ are evaluated on the one caller-selected class definition.  C
datasets label a source module as the class-equivalent, so C is evaluated on
the complete caller-selected file.  Capture and verification both call the
same metric extractor and predicate below; ATFD is intentionally absent until
the non-Java target Guard has a resolved foreign-owner model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .analysis import (
    COMPLEXITY_NODE_TYPES,
    FUNCTION_NODE_TYPES,
    _actual_function_owner,
    _extract_declared_name,
    _find_declarator_name_node,
    _iter_nodes,
    _node_text,
    _parse_tree,
    count_meaningful_lines,
)


NONJAVA_GOD_CLASS_PROFILE_ID = "nonjava-product/god-class/source-multi-metric-v1"
NONJAVA_GOD_CLASS_THRESHOLDS = {
    "min_nom": 5,
    "min_wmc": 20,
    "nom": 10,
    "wmc": 30,
    "loc": 100,
    "strong_nom": 15,
    "strong_wmc": 50,
    "min_signals": 2,
}
NONJAVA_GOD_CLASS_UNSUPPORTED_METRICS = (
    {
        "name": "atfd",
        "participates_in_finding": False,
        "reason": "nonjava_foreign_owner_resolution_unavailable",
    },
)


def nonjava_god_class_product_profile(
    metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe and evaluate the one non-Java God Class predicate."""
    values = {
        name: int((metrics or {}).get(name) or 0)
        for name in ("nom", "nof", "wmc", "loc")
    }
    thresholds = NONJAVA_GOD_CLASS_THRESHOLDS
    mandatory = [
        _condition("nom", values["nom"], thresholds["min_nom"]),
        _condition("wmc", values["wmc"], thresholds["min_wmc"]),
    ]
    signals = [
        _condition("nom", values["nom"], thresholds["nom"]),
        _condition("wmc", values["wmc"], thresholds["wmc"]),
        _condition("loc", values["loc"], thresholds["loc"]),
        {
            "name": "strong_nom_wmc",
            "operator": "nom>= and wmc>=",
            "boundaries": {
                "nom": thresholds["strong_nom"],
                "wmc": thresholds["strong_wmc"],
            },
            "values": {"nom": values["nom"], "wmc": values["wmc"]},
            "matched": (
                values["nom"] >= thresholds["strong_nom"]
                and values["wmc"] >= thresholds["strong_wmc"]
            ),
        },
    ]
    triggered = [str(item["name"]) for item in signals if item["matched"]]
    finding_present = bool(
        all(item["matched"] for item in mandatory)
        and len(triggered) >= thresholds["min_signals"]
    )
    return {
        "id": NONJAVA_GOD_CLASS_PROFILE_ID,
        "metric_definitions": {
            "nom": "direct target methods or C module function definitions",
            "nof": "declared target fields or C module global variables",
            "wmc": "sum of max(control-flow-node-count, 1) per operation",
            "loc": "nonblank noncomment lines in the selected target",
        },
        "metrics": values,
        "mandatory": mandatory,
        "signals": signals,
        "min_signals": thresholds["min_signals"],
        "triggered_signals": triggered,
        "unsupported_metrics": [
            dict(item) for item in NONJAVA_GOD_CLASS_UNSUPPORTED_METRICS
        ],
        "finding_present": finding_present,
        "pass": not finding_present,
    }


def nonjava_god_class_metrics(
    source_text: str,
    language: str,
    *,
    class_name: str = "",
) -> dict[str, int]:
    """Extract NOM, NOF, WMC and meaningful LOC from one frozen target."""
    if language not in {"python", "c", "cpp"}:
        raise ValueError(f"unsupported non-Java God Class language: {language}")
    source_bytes = source_text.encode("utf-8", errors="surrogateescape")
    root = _parse_tree(Path("<god-class-target>"), language, source_bytes)
    owner = (
        None
        if language == "c"
        else _selected_class(root, language, source_bytes, class_name)
    )
    methods = _method_definitions(root, owner, language)
    declarations = (
        _cpp_bodyless_method_declarations(root, owner)
        if language == "cpp"
        else []
    )
    external_methods = (
        _cpp_out_of_class_method_definitions(
            root,
            owner,
            source_bytes,
            declarations,
        )
        if language == "cpp"
        else []
    )
    nof = (
        _python_fields(root, owner, source_bytes)
        if language == "python"
        else _cpp_fields(root, owner)
        if language == "cpp"
        else _c_module_fields(root)
    )
    # A bodyless class declaration contributes the minimum WMC of one.  When
    # its definition is written as ``Owner::method`` in the same explicit
    # source file, replace that minimum with the real body complexity instead
    # of either ignoring the body (Guard bypass) or counting the operation
    # twice.  Inline definitions are already in ``methods`` and therefore
    # never enter ``external_methods``.
    wmc = (
        len(declarations)
        + sum(_method_complexity(method, language) for method in methods)
        + sum(
            max(0, _method_complexity(method, language) - 1)
            for method in external_methods
        )
    )
    loc_text = source_text
    if language == "cpp":
        loc_text = "\n".join([
            _node_text(source_bytes, owner),
            *(_node_text(source_bytes, method) for method in external_methods),
        ])
    return {
        "nom": len(methods) + len(declarations),
        "nof": nof,
        "wmc": wmc,
        "loc": count_meaningful_lines(loc_text, language),
    }


def _condition(name: str, value: int, boundary: int) -> dict[str, Any]:
    return {
        "name": name,
        "operator": ">=",
        "boundary": boundary,
        "value": value,
        "matched": value >= boundary,
    }


def _selected_class(
    root: Any,
    language: str,
    source_bytes: bytes,
    class_name: str,
) -> Any:
    if language == "cpp" and str(class_name or "").strip():
        requested = str(class_name).strip().replace(".", "::")
        requested_qualified = "::" in requested
        matches = []
        for node in _iter_nodes(root):
            if node.type not in {"class_specifier", "struct_specifier"}:
                continue
            if node.child_by_field_name("body") is None:
                continue
            qualified = _cpp_class_qualified_name(node, source_bytes)
            declared = qualified.rsplit("::", 1)[-1]
            if (
                requested_qualified
                and qualified == requested
            ) or (
                not requested_qualified
                and declared == requested
            ):
                matches.append(node)
        if len(matches) != 1:
            raise ValueError(
                "selected C++ God Class owner is missing or ambiguous"
            )
        return matches[0]
    return _single_outer_class(root, language)


def _single_outer_class(root: Any, language: str) -> Any:
    class_types = (
        {"class_definition"}
        if language == "python"
        else {"class_specifier", "struct_specifier"}
    )
    outer = [
        node
        for node in _iter_nodes(root)
        if node.type in class_types
        and _nearest_ancestor(node, class_types) is None
    ]
    if len(outer) != 1:
        raise ValueError(
            "selected God Class text must contain exactly one outer class definition"
        )
    return outer[0]


def _cpp_class_qualified_name(owner: Any, source_bytes: bytes) -> str:
    parts: list[str] = []
    current = owner
    owner_types = {
        "namespace_definition",
        "class_specifier",
        "struct_specifier",
        "union_specifier",
    }
    while current is not None:
        if current.type in owner_types:
            name_node = current.child_by_field_name("name")
            name = (
                _node_text(source_bytes, name_node).strip()
                if name_node is not None
                else ""
            )
            if name:
                parts.append(name)
        current = current.parent
    parts.reverse()
    qualified = "::".join(parts)
    if not qualified:
        raise ValueError("selected C++ God Class owner has no declared name")
    return qualified


def _method_definitions(root: Any, owner: Any, language: str) -> list[Any]:
    nodes: list[Any] = []
    for node in _iter_nodes(root):
        if node.type not in FUNCTION_NODE_TYPES.get(language, set()):
            continue
        if language == "c":
            if _nearest_ancestor(node, FUNCTION_NODE_TYPES[language]) is None:
                nodes.append(node)
            continue
        if _is_direct_class_member(node, owner, language):
            nodes.append(node)
    return nodes


def _cpp_bodyless_method_declarations(root: Any, owner: Any) -> list[Any]:
    declarations: list[Any] = []
    for node in _iter_nodes(root):
        if node.type not in {"field_declaration", "declaration"}:
            continue
        if not _is_direct_class_member(node, owner, "cpp"):
            continue
        for declarator in _declarators(node):
            if _is_callable_declarator(declarator):
                declarations.append(declarator)
    return declarations


def _cpp_out_of_class_method_definitions(
    root: Any,
    owner: Any,
    source_bytes: bytes,
    declarations: list[Any],
) -> list[Any]:
    """Return same-file definitions provably owned by the selected class.

    Name multiplicity is checked against bodyless declarations.  An unmatched
    or over-subscribed definition makes the metric unavailable instead of
    silently lowering WMC or guessing across overloads.
    """
    owner_name = _canonical_cpp_owner(
        _cpp_class_qualified_name(owner, source_bytes)
    )
    declaration_counts: dict[str, int] = {}
    for declarator in declarations:
        name_node = _find_declarator_name_node(declarator)
        name = (
            _node_text(source_bytes, name_node).strip().rsplit("::", 1)[-1]
            if name_node is not None
            else ""
        )
        if not name:
            raise ValueError(
                "selected C++ God Class method declaration is ambiguous"
            )
        declaration_counts[name] = declaration_counts.get(name, 0) + 1

    external: list[Any] = []
    matched_counts: dict[str, int] = {}
    for node in _iter_nodes(root):
        if node.type not in FUNCTION_NODE_TYPES.get("cpp", set()):
            continue
        if _is_direct_class_member(node, owner, "cpp"):
            continue
        actual_owner = _canonical_cpp_owner(
            _actual_function_owner(node, "cpp", source_bytes)
        )
        if actual_owner != owner_name:
            continue
        declared_name = (
            _extract_declared_name(node, "cpp", source_bytes) or ""
        ).strip().rsplit("::", 1)[-1]
        if not declared_name:
            raise ValueError(
                "selected C++ God Class out-of-class definition is ambiguous"
            )
        matched_counts[declared_name] = (
            matched_counts.get(declared_name, 0) + 1
        )
        if matched_counts[declared_name] > declaration_counts.get(
            declared_name, 0
        ):
            raise ValueError(
                "selected C++ God Class out-of-class definition has no unique "
                "class declaration"
            )
        external.append(node)
    return external


def _canonical_cpp_owner(value: str) -> str:
    """Remove template argument spellings without erasing owner segments."""
    output: list[str] = []
    depth = 0
    for character in str(value or ""):
        if character == "<":
            depth += 1
        elif character == ">" and depth:
            depth -= 1
        elif depth == 0 and not character.isspace():
            output.append(character)
    return "".join(output)


def _method_complexity(method: Any, language: str) -> int:
    body = method.child_by_field_name("body")
    if body is None:
        return 1
    control_types = COMPLEXITY_NODE_TYPES.get(language, set())
    controls = sum(
        1
        for node in _iter_callable_body(body, language)
        if node.type in control_types
    )
    return max(controls, 1)


def _iter_callable_body(node: Any, language: str):
    yield node
    nested_callable_types = FUNCTION_NODE_TYPES.get(language, set())
    for child in node.children:
        if child.type in nested_callable_types or child.type == "lambda_expression":
            continue
        yield from _iter_callable_body(child, language)


def _python_fields(root: Any, owner: Any, source_bytes: bytes) -> int:
    fields: set[str] = set()
    for node in _iter_nodes(root):
        if node.type not in {"assignment", "augmented_assignment"}:
            continue
        if _nearest_ancestor(node, {"class_definition"}) != owner:
            continue
        left = node.child_by_field_name("left")
        if left is None:
            continue
        enclosing_method = _nearest_ancestor(node, {"function_definition"})
        if enclosing_method is None:
            for target in _iter_nodes(left):
                if target.type == "identifier":
                    fields.add(_node_text(source_bytes, target).strip())
        else:
            for target in _iter_nodes(left):
                if target.type != "attribute":
                    continue
                receiver = target.child_by_field_name("object")
                attribute = target.child_by_field_name("attribute")
                if (
                    receiver is not None
                    and attribute is not None
                    and _node_text(source_bytes, receiver).strip() in {"self", "cls"}
                ):
                    fields.add(_node_text(source_bytes, attribute).strip())
    fields.discard("")
    return len(fields)


def _cpp_fields(root: Any, owner: Any) -> int:
    count = 0
    for node in _iter_nodes(root):
        if node.type not in {"field_declaration", "declaration"}:
            continue
        if not _is_direct_class_member(node, owner, "cpp"):
            continue
        count += sum(
            1 for declarator in _declarators(node)
            if not _is_callable_declarator(declarator)
            and _find_declarator_name_node(declarator) is not None
        )
    return count


def _c_module_fields(root: Any) -> int:
    count = 0
    for node in root.named_children:
        if node.type != "declaration":
            continue
        count += sum(
            1 for declarator in _declarators(node)
            if not _is_callable_declarator(declarator)
            and _find_declarator_name_node(declarator) is not None
        )
    return count


def _declarators(node: Any) -> list[Any]:
    return [
        child
        for index, child in enumerate(node.children)
        if node.field_name_for_child(index) == "declarator"
    ]


def _is_callable_declarator(declarator: Any) -> bool:
    current = declarator
    while current is not None:
        if current.type == "function_declarator":
            inner = current.child_by_field_name("declarator")
            return not bool(
                inner is not None
                and any(
                    node.type == "pointer_declarator"
                    for node in _iter_nodes(inner)
                )
            )
        current = current.child_by_field_name("declarator")
    return False


def _is_direct_class_member(node: Any, owner: Any, language: str) -> bool:
    class_types = (
        {"class_definition"}
        if language == "python"
        else {"class_specifier", "struct_specifier"}
    )
    if _nearest_ancestor(node, class_types) != owner:
        return False
    return _nearest_ancestor(node, FUNCTION_NODE_TYPES.get(language, set())) is None


def _nearest_ancestor(node: Any, node_types: set[str]) -> Any:
    current = node.parent
    while current is not None:
        if current.type in node_types:
            return current
        current = current.parent
    return None
