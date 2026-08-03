"""Production-only structural closure for exact Java method clones.

The exact-clone detector answers whether two method bodies are still equal.
That alone is not a refactoring proof: changing one token, copying the bodies
to two new helpers, or moving both copies elsewhere also removes the original
pair.  This module derives a small, immutable graph from the *same* production
``ProjectModel`` used for both baseline and current snapshots.  No tests,
dataset evidence, git history, or live-HEAD lookup participates.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .catalog_identity import (
    CATALOG_IDENTITY_SCHEMA,
    split_top_level_java_types,
    stable_java_method_signature,
    stable_method_record_identity,
    stable_method_record_signature,
)
from .semantic_detector import (
    ClassRecord,
    MethodRecord,
    ProjectModel,
    resolve_project_method_invocation,
)
from .syntactic_detector import is_thin_forwarder, tokenize_clone_node


def analyze_exact_clone_closure(
    model: ProjectModel,
    *,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    min_tokens: int,
) -> dict[str, Any]:
    """Describe one frozen pair and the project-level deduplication graph."""
    methods = list(model.methods)
    body_profiles = {
        _method_key(method): _body_profile(method)
        for method in methods
        if method.body is not None
    }
    route_graph = _method_route_graph(model)
    catalog = _clone_catalog(body_profiles, min_tokens=min_tokens)
    left_method = _locate_endpoint(model, left)
    right_method = _locate_endpoint(model, right)
    left_effective = _effective_method(model, left, left_method)
    right_effective = _effective_method(model, right, right_method)
    left_summary = _endpoint_summary(model, left_method, left_effective)
    right_summary = _endpoint_summary(model, right_method, right_effective)
    left_profile = body_profiles.get(_method_key(left_method)) if left_method else None
    right_profile = body_profiles.get(_method_key(right_method)) if right_method else None
    pair_present = bool(
        left_profile
        and right_profile
        and left_profile["token_count"] >= min_tokens
        and left_profile["fingerprint"] == right_profile["fingerprint"]
    )
    common_callees = sorted(
        set(left_summary["reachable_callees"]).intersection(right_summary["reachable_callees"])
    )
    shared_implementation = ""
    if left_effective is not None and right_effective is not None:
        left_key = _method_key(left_effective)
        if left_key == _method_key(right_effective):
            shared_implementation = left_key
    return {
        "ok": True,
        "catalog_identity_schema": CATALOG_IDENTITY_SCHEMA,
        "pair_present": pair_present,
        "pair_token_count": (
            min(int(left_profile["token_count"]), int(right_profile["token_count"]))
            if pair_present and left_profile and right_profile
            else 0
        ),
        "pair_fingerprint": (
            str(left_profile["fingerprint"])
            if pair_present and left_profile
            else ""
        ),
        "endpoints": [left_summary, right_summary],
        "common_callees": common_callees,
        "shared_implementation": shared_implementation,
        "clone_catalog": catalog,
        "implementation_catalog": sorted(
            (
                {
                    "method": str(profile.get("method") or ""),
                    "token_count": int(profile.get("token_count") or 0),
                    "fingerprint": str(profile.get("fingerprint") or ""),
                    "body_tokens": list(profile.get("body_tokens") or []),
                    "thin_forwarder": bool(profile.get("thin_forwarder")),
                    "callees": sorted(
                        route_graph["callees"].get(method_key, set())
                    ),
                    "unresolved_call_count": len(
                        route_graph["unresolved"].get(method_key, [])
                    ),
                }
                for method_key, profile in body_profiles.items()
            ),
            key=lambda item: item["method"],
        ),
        "call_graph": _project_call_graph(model),
    }


def _body_profile(
    method: MethodRecord,
    *,
    include_method_tokens: bool = False,
) -> dict[str, Any]:
    raw_body_tokens = tokenize_clone_node(method.body)
    thin_forwarder = is_thin_forwarder(method.body_text)
    body_tokens = list(raw_body_tokens)
    if thin_forwarder:
        body_tokens = []
    # Keep the detector and closure on one exact contract.  The product
    # detector fingerprints the normalized body, but applies its minimum-size
    # boundary to declaration + body tokens so short overloads/constructors do
    # not change eligibility merely because a different parser represents the
    # declaration outside the body node.
    declaration = getattr(method.body, "parent", None)
    declaration_tokens = (
        tokenize_clone_node(declaration, exclude_nodes=(method.body,))
        if declaration is not None
        else []
    )
    encoded = "\x1f".join(body_tokens).encode("utf-8")
    result = {
        "method": _method_key(method),
        "body_tokens": raw_body_tokens,
        "thin_forwarder": thin_forwarder,
        "token_count": (
            len(body_tokens) + len(declaration_tokens)
            if body_tokens
            else 0
        ),
        "fingerprint": hashlib.sha256(encoded).hexdigest() if body_tokens else "",
    }
    if include_method_tokens:
        result["method_tokens"] = [*declaration_tokens, *raw_body_tokens]
    return result


def _clone_catalog(
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    min_tokens: int,
) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    token_counts: dict[str, int] = {}
    for key, profile in profiles.items():
        count = int(profile.get("token_count") or 0)
        fingerprint = str(profile.get("fingerprint") or "")
        if count < min_tokens or not fingerprint:
            continue
        groups[fingerprint].append(key)
        token_counts[fingerprint] = count
    return [
        {
            "fingerprint": fingerprint,
            "token_count": token_counts[fingerprint],
            "methods": sorted(method_keys),
        }
        for fingerprint, method_keys in sorted(groups.items())
        if len(method_keys) >= 2
    ]


def _endpoint_summary(
    model: ProjectModel,
    declared: MethodRecord | None,
    effective: MethodRecord | None,
) -> dict[str, Any]:
    implementation_kind = "missing"
    if effective is not None:
        implementation_kind = "declared" if effective is declared else "inherited"
    calls_from = declared or effective
    body_tokens = (
        tokenize_clone_node(declared.body)
        if declared is not None and declared.body is not None
        else []
    )
    direct_callees = (
        sorted(_resolved_callees(model, calls_from)) if calls_from is not None else []
    )
    return {
        "declared_method": _method_key(declared) if declared is not None else "",
        "effective_method": _method_key(effective) if effective is not None else "",
        "declared_identity": _method_identity(declared),
        "effective_identity": _method_identity(effective),
        "implementation_kind": implementation_kind,
        # Only the two frozen endpoints are stored.  Their normalized sequence
        # lets the checkpoint reject a wrapper that calls a shared helper but
        # keeps the complete old clone as a conditional fallback.
        "body_tokens": body_tokens,
        "resolved_callees": direct_callees,
        "reachable_callees": (
            sorted(_reachable_callees(model, calls_from)) if calls_from is not None else []
        ),
    }


def _method_identity(method: MethodRecord | None) -> dict[str, Any]:
    if method is None:
        return {}
    return {
        "file": _normalize_path(method.file),
        "class": str(method.owner_qualified_name or method.class_name or ""),
        "method": stable_method_record_signature(method),
        "line": int(method.begin_line),
    }


def _locate_endpoint(model: ProjectModel, anchor: Mapping[str, Any]) -> MethodRecord | None:
    file_name = _normalize_path(anchor.get("file"))
    class_selector = str(anchor.get("class") or "").strip()
    frozen_identity = anchor.get("frozen_identity") is True
    method_text = str(anchor.get("method") or "")
    method_name = _method_name(method_text)
    arity = _signature_arity(method_text)
    line = _as_int(anchor.get("line"))
    candidates = [
        method
        for method in model.methods
        if (not file_name or _normalize_path(method.file) == file_name)
        and (
            not class_selector
            or (
                method.owner_qualified_name == class_selector
                if frozen_identity or "." in class_selector
                else _simple_name(method.class_name) == _simple_name(class_selector)
            )
        )
        and (not method_name or method.method_name == method_name)
        and (arity is None or len(method.parameter_types) == arity)
    ]
    exact_signature = (
        stable_java_method_signature(
            method_text,
            preserve_source_qualification=True,
        )
        if "(" in method_text and ")" in method_text
        else ""
    )
    exact = [
        method for method in candidates
        if exact_signature and stable_method_record_signature(method) == exact_signature
    ]
    if len(exact) == 1:
        return exact[0]
    if exact_signature:
        # Once c000 freezes a complete signature, a same-name/arity overload
        # is a different entity. Line proximity must never substitute it.
        return None
    if line:
        containing = [
            method for method in candidates
            if method.begin_line <= line <= method.end_line
        ]
        if len(containing) == 1:
            return containing[0]
    return candidates[0] if len(candidates) == 1 else None


def _effective_method(
    model: ProjectModel,
    anchor: Mapping[str, Any],
    declared: MethodRecord | None,
) -> MethodRecord | None:
    if declared is not None:
        return declared
    owner = _locate_owner(model, anchor)
    if owner is None:
        return None
    method_name = _method_name(str(anchor.get("method") or ""))
    if method_name and _simple_name(method_name) == _simple_name(owner.class_name):
        # Constructors are never inherited. Their structural closure is an
        # explicit this/super constructor invocation, handled as a callee.
        return None
    anchor_method = str(anchor.get("method") or "")
    if "(" not in anchor_method or ")" not in anchor_method:
        return None
    exact_signature = stable_java_method_signature(
        anchor_method,
        preserve_source_qualification=True,
    )
    visited: set[str] = set()
    pending = list(_direct_ancestors(model, owner))
    while pending:
        level: list[ClassRecord] = []
        next_level: list[ClassRecord] = []
        for parent in pending:
            if parent.qualified_name in visited:
                continue
            visited.add(parent.qualified_name)
            level.append(parent)
            next_level.extend(_direct_ancestors(model, parent))
        candidates = [
            method
            for parent in level
            for method in parent.methods
            if not method.is_constructor
            and stable_method_record_signature(method) == exact_signature
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return None
        pending = next_level
    return None


def _locate_owner(model: ProjectModel, anchor: Mapping[str, Any]) -> ClassRecord | None:
    file_name = _normalize_path(anchor.get("file"))
    class_selector = str(anchor.get("class") or "").strip()
    frozen_identity = anchor.get("frozen_identity") is True
    candidates = [
        cls for cls in model.classes.values()
        if (not file_name or _normalize_path(cls.file) == file_name)
        and (
            not class_selector
            or (
                cls.qualified_name == class_selector
                if frozen_identity or "." in class_selector
                else _simple_name(cls.class_name) == _simple_name(class_selector)
            )
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    if file_name and class_selector:
        # A production source move preserves the frozen qualified owner. A
        # default-package Foo must never drift to package q.Foo merely because
        # the simple name is globally unique.
        moved = [
            cls for cls in model.classes.values()
            if cls.qualified_name == class_selector
        ]
        if len(moved) == 1:
            return moved[0]
    return None


def _resolved_callees(model: ProjectModel, caller: MethodRecord) -> set[str]:
    return set(
        _method_route_graph(model)["callees"].get(_method_key(caller), set())
    )


def _direct_callee_analysis(
    model: ProjectModel,
    caller: MethodRecord,
    *,
    index: Mapping[tuple[str, str], Sequence[MethodRecord]] | None = None,
) -> dict[str, Any]:
    if caller.body is None:
        return {"callees": set(), "unresolved_project_calls": []}
    result: set[str] = set()
    unresolved: list[dict[str, Any]] = []
    candidate_index = index or _method_candidate_index(model)
    for node in _iter_nodes(caller.body):
        if node.type == "explicit_constructor_invocation":
            resolved = _resolve_constructor_invocation(model, caller, node)
            if resolved is not None and _method_key(resolved) != _method_key(caller):
                result.add(_method_key(resolved))
            continue
        if node.type not in {"method_invocation", "method_reference"}:
            continue
        resolved, candidates = resolve_project_method_invocation(
            model,
            caller,
            node,
            candidates_by_owner_name=candidate_index,
        )
        if (
            resolved is not None
            and _method_key(resolved) != _method_key(caller)
            and _callee_dispatch_is_statically_proven(model, caller, node, resolved)
        ):
            result.add(_method_key(resolved))
        elif candidates or resolved is not None:
            possible = list(candidates) or ([resolved] if resolved is not None else [])
            unresolved.append({
                "caller": _method_key(caller),
                "line": int(getattr(node, "start_point", (0, 0))[0]) + 1,
                "invocation": _node_text(node).strip(),
                "candidate_methods": sorted(_method_key(item) for item in possible),
                "reason": (
                    "virtual_dispatch_not_a_clone_proof"
                    if resolved is not None
                    else "project_call_ambiguous"
                ),
            })
    return {"callees": result, "unresolved_project_calls": unresolved}


def _callee_dispatch_is_statically_proven(
    model: ProjectModel,
    caller: MethodRecord,
    node: Any,
    resolved: MethodRecord,
) -> bool:
    """Accept only call edges that prove one implementation declaration.

    Clone verification is not a general Java call-graph client. A normal
    virtual invocation may execute an override unrelated to the declaration
    selected from the receiver's static type, so it cannot prove that both
    clone endpoints were deduplicated into the same implementation.
    """
    if resolved.is_constructor:
        return True
    if "private" in resolved.modifiers or "final" in resolved.modifiers:
        return True
    owner = model.classes.get(resolved.owner_qualified_name)
    if owner is not None and (
        "final" in owner.modifiers or owner.kind == "record"
    ):
        return True
    if "static" not in resolved.modifiers:
        return False
    object_node = node.child_by_field_name("object")
    qualifier = _node_text(object_node).strip()
    if not qualifier:
        return caller.owner_qualified_name == resolved.owner_qualified_name
    qualifier = re.sub(r"<[^<>]*>", "", qualifier).strip()
    return _simple_name(qualifier) == _simple_name(resolved.owner_qualified_name)


def _reachable_callees(model: ProjectModel, caller: MethodRecord) -> set[str]:
    return set(_reachable_callee_analysis(model, caller)["callees"])


def _reachable_callee_analysis(
    model: ProjectModel,
    caller: MethodRecord,
) -> dict[str, Any]:
    graph = _method_route_graph(model)
    caller_key = _method_key(caller)
    reached: set[str] = set()
    unresolved: list[dict[str, Any]] = []
    pending = list(graph["callees"].get(caller_key, set()))
    unresolved.extend(graph["unresolved"].get(caller_key, []))
    while pending:
        key = pending.pop()
        if key == caller_key or key in reached:
            continue
        reached.add(key)
        pending.extend(graph["callees"].get(key, set()))
        unresolved.extend(graph["unresolved"].get(key, []))
    return {
        "callees": reached,
        "unresolved_project_calls": unresolved,
    }


def _method_candidate_index(
    model: ProjectModel,
) -> dict[tuple[str, str], list[MethodRecord]]:
    index: dict[tuple[str, str], list[MethodRecord]] = defaultdict(list)
    for method in model.methods:
        index[(method.owner_qualified_name, method.method_name)].append(method)
    return dict(index)


def _method_route_graph(model: ProjectModel) -> dict[str, Any]:
    cached = getattr(model, "_method_route_graph_cache", None)
    if isinstance(cached, dict):
        return cached
    index = _method_candidate_index(model)
    callees: dict[str, set[str]] = {}
    unresolved: dict[str, list[dict[str, Any]]] = {}
    for method in model.methods:
        key = _method_key(method)
        analysis = _direct_callee_analysis(model, method, index=index)
        callees[key] = set(analysis["callees"])
        unresolved[key] = list(analysis["unresolved_project_calls"])
    graph = {"callees": callees, "unresolved": unresolved}
    setattr(model, "_method_route_graph_cache", graph)
    return graph


def _project_call_graph(model: ProjectModel) -> list[dict[str, Any]]:
    """Freeze direct production edges; transitive closure belongs to contract comparison."""
    return [
        {
            "caller": _method_key(method),
            "callees": sorted(_resolved_callees(model, method)),
        }
        for method in sorted(model.methods, key=_method_key)
    ]


def _resolve_constructor_invocation(
    model: ProjectModel,
    caller: MethodRecord,
    node: Any,
) -> MethodRecord | None:
    arguments = node.child_by_field_name("arguments")
    arity = len(arguments.named_children) if arguments is not None else 0
    invocation = _node_text(node).lstrip()
    owner = model.classes.get(caller.owner_qualified_name)
    if owner is None:
        return None
    if invocation.startswith("super"):
        owners = _direct_ancestors(model, owner)
    elif invocation.startswith("this"):
        owners = [owner]
    else:
        return None
    owner_names = {item.qualified_name for item in owners}
    candidates = [
        method for method in model.methods
        if method.is_constructor
        and method.owner_qualified_name in owner_names
        and len(method.parameter_types) == arity
    ]
    return candidates[0] if len(candidates) == 1 else None


def _classes_for_type(model: ProjectModel, value: str) -> Sequence[ClassRecord]:
    erased = re.sub(r"<.*>", "", str(value or "")).replace("[]", "").strip()
    if not erased:
        return []
    direct = model.classes.get(erased)
    if direct is not None:
        return [direct]
    return model.classes_by_simple.get(erased.rsplit(".", 1)[-1], [])


def _direct_ancestors(model: ProjectModel, cls: ClassRecord) -> list[ClassRecord]:
    ancestors: list[ClassRecord] = []
    for type_name in [cls.superclass_name, *cls.interface_names]:
        candidates = _classes_for_type(model, type_name)
        if len(candidates) == 1 and all(
            item.qualified_name != candidates[0].qualified_name for item in ancestors
        ):
            ancestors.append(candidates[0])
    return ancestors


def _method_key(method: MethodRecord | None) -> str:
    if method is None:
        return ""
    return stable_method_record_identity(method)


def _method_name(signature: str) -> str:
    return str(signature or "").split("(", 1)[0].strip().rsplit(".", 1)[-1]


def _signature_arity(signature: str) -> int | None:
    text = str(signature or "")
    if "(" not in text or ")" not in text:
        return None
    inner = text.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inner:
        return 0
    return len(split_top_level_java_types(inner))


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def _simple_name(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _node_text(node: Any) -> str:
    raw = getattr(node, "text", None)
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")


def _iter_nodes(node: Any) -> Iterable[Any]:
    yield node
    for child in getattr(node, "children", ()):
        yield from _iter_nodes(child)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
