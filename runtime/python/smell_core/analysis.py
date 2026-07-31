from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from .location import LocationTarget

# Tree-sitter is imported lazily inside the functions that need it so that
# modules which only use the text-based helpers (e.g. ``count_meaningful_lines``)
# are not forced onto the tree-sitter dependency chain.
_TREE_SITTER_IMPORT_ERROR: Optional[Exception] = None
_get_tree_sitter_parser = None  # resolved on first use

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node


CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}
C_EXTENSIONS = {".c", ".h"}
LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "java": {".java"},
    "c": C_EXTENSIONS,
    "cpp": CPP_EXTENSIONS,
}

FUNCTION_NODE_TYPES = {
    "python": {"function_definition"},
    "java": {"method_declaration", "constructor_declaration"},
    "c": {"function_definition"},
    "cpp": {"function_definition"},
}

COMPLEXITY_NODE_TYPES = {
    "python": {"if_statement", "elif_clause", "for_statement", "while_statement", "except_clause"},
    "java": {
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "case",
        "catch_clause",
    },
    "c": {
        "if_statement",
        "for_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "case_statement",
        "case",
    },
    "cpp": {
        "if_statement",
        "for_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "case_statement",
        "case",
        "catch_clause",
    },
}

PARAMETER_NODE_TYPES = {
    "python": {"identifier", "typed_parameter", "typed_default_parameter", "default_parameter", "list_splat_pattern", "dictionary_splat_pattern"},
    "java": {"formal_parameter", "spread_parameter"},
    "c": {"parameter_declaration", "variadic_parameter"},
    "cpp": {"parameter_declaration", "variadic_parameter", "optional_parameter_declaration"},
}


@dataclass
class SourceSnippet:
    start_line: int
    end_line: int
    signature_text: str
    body_text: str
    parameter_count: Optional[int] = None
    complexity_hint: Optional[int] = None


@dataclass
class FunctionSignature:
    file_path: Path
    start_line: int
    end_line: int
    name: str
    signature_text: str
    parameter_fingerprints: list[str]


@dataclass(frozen=True)
class MemberAccess:
    receiver: str
    member: str
    line: int


def signature_parameter_type_fingerprint(signature_text: str, language: str) -> Optional[str]:
    if language != "java":
        return None
    if "(" not in signature_text or ")" not in signature_text:
        return None
    inner = signature_text.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inner:
        return ""
    parts = split_top_level_params(inner)
    normalized: List[str] = []
    for raw in parts:
        part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
        part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
        part = re.sub(r"\s+", " ", part).strip()
        if not part:
            continue
        chunks = part.split(" ")
        type_text = " ".join(chunks[:-1]).strip()
        normalized.append(_normalize_java_signature_type(type_text))
    return ",".join(normalized)


def method_basename(method: Optional[str]) -> Optional[str]:
    if not method:
        return None
    text = re.sub(r"\s+", " ", method).strip()
    if not text:
        return None
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    for separator in ("::", "."):
        if separator in text:
            text = text.rsplit(separator, 1)[-1].strip()
    if " " in text:
        text = text.rsplit(" ", 1)[-1].strip()
    return text or None


def detect_language_from_path(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".java":
        return "java"
    if ext == ".py":
        return "python"
    if ext in CPP_EXTENSIONS:
        return "cpp"
    if ext in C_EXTENSIONS:
        return "c"
    return None


def tree_sitter_ready(languages: Optional[Iterable[str]] = None) -> List[str]:
    issues: List[str] = []
    if _TREE_SITTER_IMPORT_ERROR is not None:
        issues.append(
            "tree-sitter runtime unavailable: "
            f"{_TREE_SITTER_IMPORT_ERROR}. Install 'tree-sitter' and 'tree-sitter-language-pack'."
        )
        return issues
    for language in sorted(set(languages or [])):
        try:
            _get_parser(language)
        except RuntimeError as exc:
            issues.append(str(exc))
    return issues


def _strip_brace_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _strip_python_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        lines.append(re.sub(r"#.*", "", line))
    return "\n".join(lines)


def strip_comments(text: str, language: str) -> str:
    if language == "python":
        return _strip_python_comments(text)
    return _strip_brace_comments(text)


def count_meaningful_lines(text: str, language: str) -> int:
    cleaned = strip_comments(text, language)
    return sum(1 for line in cleaned.splitlines() if line.strip())


def normalize_for_clone(text: str, language: str) -> str:
    cleaned = strip_comments(text, language)
    return re.sub(r"\s+", "", cleaned)


def split_top_level_params(signature: str) -> List[str]:
    params: List[str] = []
    current: List[str] = []
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    depth_angle = 0
    for char in signature:
        if char == "," and not any([depth_paren, depth_bracket, depth_brace, depth_angle]):
            piece = "".join(current).strip()
            if piece:
                params.append(piece)
            current = []
            continue
        current.append(char)
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren = max(0, depth_paren - 1)
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace = max(0, depth_brace - 1)
        elif char == "<":
            depth_angle += 1
        elif char == ">":
            depth_angle = max(0, depth_angle - 1)
    piece = "".join(current).strip()
    if piece:
        params.append(piece)
    return params


def count_parameters(signature_text: str, language: str) -> int:
    wrapped_source = _wrap_signature_source(signature_text, language)
    if wrapped_source is None:
        return _count_parameters_from_signature_text(signature_text, language)
    function_node, source_bytes = _find_first_function_node(wrapped_source, language)
    if function_node is None:
        return _count_parameters_from_signature_text(signature_text, language)
    return _count_parameters_from_node(function_node, language, source_bytes)


def estimate_complexity(snippet: SourceSnippet, language: str) -> int:
    if snippet.complexity_hint is not None:
        return snippet.complexity_hint
    if language == "java":
        name_match = re.search(
            r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            snippet.signature_text,
        )
        method_name = name_match.group(1) if name_match else ""
        return java_cognitive_complexity_from_text(snippet.body_text, method_name)
    wrapped_source = _wrap_body_source(snippet.body_text, language)
    if wrapped_source is None:
        return _estimate_complexity_from_text(snippet.body_text, language)
    function_node, source_bytes = _find_first_function_node(wrapped_source, language)
    if function_node is None:
        return _estimate_complexity_from_text(snippet.body_text, language)
    body_node = function_node.child_by_field_name("body")
    if body_node is None:
        return _estimate_complexity_from_text(snippet.body_text, language)
    return _estimate_complexity_from_node(body_node, language, source_bytes)


def java_cognitive_complexity_from_text(
    body_text: str,
    method_name: str = "",
) -> int:
    """Compute the PMD Java CognitiveComplexity metric used by the dataset.

    This mirrors PMD's ``CognitiveComplexityVisitor`` instead of maintaining a
    generic nesting approximation. Keeping it public lets the fast Java
    detector and checkpoint adapter share the same metric implementation.
    """
    wrapped = _wrap_body_source(body_text, "java")
    if wrapped is None:
        return _estimate_complexity_from_text(body_text, "java")
    function_node, source_bytes = _find_first_function_node(wrapped, "java")
    if function_node is None:
        return _estimate_complexity_from_text(body_text, "java")
    body_node = function_node.child_by_field_name("body")
    if body_node is None:
        return _estimate_complexity_from_text(body_text, "java")
    name = method_name or _extract_declared_name(function_node, "java", source_bytes) or ""
    return _java_cognitive_complexity(body_node, source_bytes, name)


def estimate_switch_branches(snippet: SourceSnippet, language: str) -> int:
    wrapped_source = _wrap_body_source(snippet.body_text, language)
    if wrapped_source is None:
        return _estimate_switch_branches_from_text(snippet.body_text, language)
    function_node, source_bytes = _find_first_function_node(wrapped_source, language)
    if function_node is None:
        return _estimate_switch_branches_from_text(snippet.body_text, language)
    body_node = function_node.child_by_field_name("body")
    if body_node is None:
        return _estimate_switch_branches_from_text(snippet.body_text, language)
    return _estimate_switch_branches_from_node(body_node, language, source_bytes)


def python_switch_metrics(snippet: SourceSnippet) -> Tuple[int, int, float]:
    """Switch-equivalent metrics for Python, which has no switch statement.

    The switch_statements smell maps to type-code dispatch: multi-branch
    if/elif/else chains and match statements.  Returns (switch_count,
    case_count, density); case_count mirrors estimate_switch_branches so the
    checkpoint objective and the ordinary guard read the same number.
    """
    wrapped_source = _wrap_body_source(snippet.body_text, "python")
    if wrapped_source is None:
        return _python_switch_metrics_from_text(snippet.body_text)
    function_node, source_bytes = _find_first_function_node(wrapped_source, "python")
    if function_node is None:
        return _python_switch_metrics_from_text(snippet.body_text)
    body_node = function_node.child_by_field_name("body")
    if body_node is None:
        return _python_switch_metrics_from_text(snippet.body_text)
    return _python_dispatch_metrics(body_node)


def extract_snippet(target: LocationTarget, language: str) -> Optional[SourceSnippet]:
    source_bytes = target.file_path.read_bytes()
    root = _parse_tree(target.file_path, language, source_bytes)
    function_node = _find_matching_function(root, source_bytes, target, language)
    if function_node is None:
        return None
    return _build_source_snippet(function_node, source_bytes, language)


def extract_function_signature(
    target: LocationTarget,
    language: str,
) -> Optional[FunctionSignature]:
    """Resolve one declaration, including interface/abstract methods without bodies."""
    source_bytes = target.file_path.read_bytes()
    root = _parse_tree(target.file_path, language, source_bytes)
    function_node = _find_matching_function(root, source_bytes, target, language)
    if function_node is None:
        return None
    body_node = function_node.child_by_field_name("body")
    signature_end = body_node.start_byte if body_node is not None else function_node.end_byte
    if body_node is not None and source_bytes[body_node.start_byte : body_node.start_byte + 1] == b"{":
        signature_end += 1
    signature_text = _decode(source_bytes[function_node.start_byte:signature_end]).rstrip().removesuffix(";")
    return FunctionSignature(
        file_path=target.file_path,
        start_line=_node_start_line(function_node),
        end_line=_node_end_line(function_node),
        name=_extract_declared_name(function_node, language, source_bytes) or "",
        signature_text=signature_text,
        parameter_fingerprints=_parameter_fingerprints_from_node(
            function_node,
            language,
            source_bytes,
        ),
    )


def extract_pair_snippets(targets: List[LocationTarget], language: str) -> Tuple[Optional[SourceSnippet], Optional[SourceSnippet]]:
    if len(targets) < 2:
        return None, None
    return extract_snippet(targets[0], language), extract_snippet(targets[1], language)


def extract_class_text(target: LocationTarget, language: str) -> Optional[str]:
    """Return the labeled class body, or the C translation unit used as its module surrogate."""
    if not target.file_path.is_file():
        return None
    source_bytes = target.file_path.read_bytes()
    if language == "c":
        # C datasets use `class=<module>` for file-level god-class candidates.
        return _decode(source_bytes)
    node_types = {
        "python": {"class_definition"},
        "cpp": {"class_specifier", "struct_specifier"},
    }.get(language, set())
    if not node_types:
        return None
    root = _parse_tree(target.file_path, language, source_bytes)
    candidates = [node for node in _iter_nodes(root) if node.type in node_types]
    class_name = str(target.class_name or "").rsplit(".", 1)[-1]
    if class_name:
        named = []
        for node in candidates:
            name_node = node.child_by_field_name("name")
            name = _node_text(source_bytes, name_node).strip() if name_node is not None else ""
            if name.rsplit("::", 1)[-1] == class_name:
                named.append(node)
        if named:
            candidates = named
    node = _select_best_node(candidates, target.line)
    return _node_text(source_bytes, node) if node is not None else None


def iter_function_signatures(project_root: Path, language: str) -> list[FunctionSignature]:
    extensions = LANGUAGE_EXTENSIONS.get(language, set())
    if not extensions:
        return []
    signatures: list[FunctionSignature] = []
    for source_path in sorted(_iter_source_files(project_root, extensions)):
        source_bytes = source_path.read_bytes()
        root = _parse_tree(source_path, language, source_bytes)
        for node in _iter_nodes(root):
            if node.type not in FUNCTION_NODE_TYPES.get(language, set()):
                continue
            snippet = _build_source_snippet(node, source_bytes, language)
            if snippet is None:
                continue
            name = _extract_declared_name(node, language, source_bytes) or ""
            fingerprints = _parameter_fingerprints_from_node(node, language, source_bytes)
            signatures.append(
                FunctionSignature(
                    file_path=source_path,
                    start_line=_node_start_line(node),
                    end_line=_node_end_line(node),
                    name=name,
                    signature_text=snippet.signature_text,
                    parameter_fingerprints=fingerprints,
                )
            )
    return signatures


def parse_function_nodes(file_path: Path, language: str) -> list[tuple[Node, bytes]]:
    """Parse one file and return each function definition node with the raw source."""
    source_bytes = file_path.read_bytes()
    root = _parse_tree(file_path, language, source_bytes)
    function_types = FUNCTION_NODE_TYPES.get(language, set())
    return [(node, source_bytes) for node in _iter_nodes(root) if node.type in function_types]


def extract_member_accesses(target: LocationTarget, language: str) -> Optional[list[MemberAccess]]:
    """Member accesses of the function matching the target, or None when unresolved."""
    return _extract_accesses(target, language, iter_member_accesses)


def extract_effective_member_accesses(target: LocationTarget, language: str) -> Optional[list[MemberAccess]]:
    """Alias-folded member accesses of the function matching the target."""
    return _extract_accesses(target, language, iter_effective_member_accesses)


def extract_simple_aliases(target: LocationTarget, language: str) -> Optional[dict[str, str]]:
    """Final simple-alias map (alias local -> root receiver) of the target function."""
    return _extract_accesses(target, language, final_simple_aliases)


def _extract_accesses(target: LocationTarget, language: str, iter_fn):
    if not target.file_path.is_file():
        return None
    source_bytes = target.file_path.read_bytes()
    root = _parse_tree(target.file_path, language, source_bytes)
    function_node = _find_matching_function(root, source_bytes, target, language)
    if function_node is None:
        return None
    body_node = function_node.child_by_field_name("body")
    if body_node is None:
        return None
    return iter_fn(body_node, source_bytes, language)


_MEMBER_ACCESS_NODE_TYPES = {
    "python": {"attribute"},
    "c": {"field_expression"},
    "cpp": {"field_expression"},
}


def iter_member_accesses(body_node: Node, source_bytes: bytes, language: str) -> list[MemberAccess]:
    """Member accesses inside a function body, tagged with the root receiver identifier."""
    node_types = _MEMBER_ACCESS_NODE_TYPES.get(language, set())
    accesses: list[MemberAccess] = []
    for node in _iter_nodes(body_node):
        if node.type not in node_types:
            continue
        receiver_node = node.child_by_field_name("object") or node.child_by_field_name("argument")
        member_node = node.child_by_field_name("attribute") or node.child_by_field_name("field")
        accesses.append(
            MemberAccess(
                receiver=_root_receiver_identifier(receiver_node, source_bytes),
                member=_node_text(source_bytes, member_node).strip(),
                line=_node_start_line(node),
            )
        )
    return accesses


def iter_effective_member_accesses(body_node: Node, source_bytes: bytes, language: str) -> list[MemberAccess]:
    """iter_member_accesses with simple-alias folding applied.

    A simple alias assignment (python ``x = r.f`` / ``x = r.f.g`` — including
    pairwise tuple unpacking and walrus; c/cpp an initializer or plain
    assignment_expression whose value is exactly one call-free member-access
    chain) makes the local ``x`` stand for the chain's root receiver: every
    later read of ``x`` counts as one access to that receiver, whether bare
    (condition, operand, call argument, return) or as the root of a member
    access (``x.f2`` counts once, not twice).  Rebinding ``x`` to a non-alias
    value ends the alias; rebinding to another chain repoints it.  Compound
    updates (``x += 1``, ``x++``) count one read and then end the alias;
    ``del x`` counts one read and drops the name.  Anything uncertain (calls,
    operators, mismatched destructuring) is not an alias.
    """
    access_types = _MEMBER_ACCESS_NODE_TYPES.get(language, set())
    if not access_types:
        return []
    access_ids: set[int] = set()
    store_ids: set[int] = set()
    bindings: dict[int, list[tuple[str, Optional[str], bool]]] = {}
    for node in _iter_nodes(body_node):
        if node.type in access_types:
            access_ids.add(node.id)
        node_bindings = _simple_alias_bindings(node, source_bytes, language)
        if node_bindings:
            bindings[node.id] = [(name, root, read_first) for name, _, root, read_first in node_bindings]
            for _, target_node, _, _ in node_bindings:
                if target_node is not None:
                    store_ids.add(target_node.id)
    aliases: dict[str, str] = {}
    folded: list[MemberAccess] = []
    for node in _iter_nodes(body_node):
        for name, root, read_first in bindings.get(node.id, ()):
            if read_first:
                target = aliases.get(name)
                if target:
                    folded.append(MemberAccess(receiver=target, member=name, line=_node_start_line(node)))
            if root:
                aliases[name] = _resolve_alias(root, aliases)
            else:
                aliases.pop(name, None)
        if node.id in access_ids:
            receiver_node = node.child_by_field_name("object") or node.child_by_field_name("argument")
            member_node = node.child_by_field_name("attribute") or node.child_by_field_name("field")
            receiver = _root_receiver_identifier(receiver_node, source_bytes)
            folded.append(
                MemberAccess(
                    receiver=_resolve_alias(receiver, aliases),
                    member=_node_text(source_bytes, member_node).strip(),
                    line=_node_start_line(node),
                )
            )
        elif node.type == "identifier" and node.id not in store_ids:
            name = _node_text(source_bytes, node).strip()
            target = aliases.get(name)
            if target and _is_alias_read(node, access_ids, body_node):
                folded.append(MemberAccess(receiver=target, member=name, line=_node_start_line(node)))
    return folded


def final_simple_aliases(body_node: Node, source_bytes: bytes, language: str) -> dict[str, str]:
    """Final simple-alias map (alias local name -> resolved root receiver)."""
    aliases: dict[str, str] = {}
    for node in _iter_nodes(body_node):
        for name, _, root, _ in _simple_alias_bindings(node, source_bytes, language):
            if root:
                aliases[name] = _resolve_alias(root, aliases)
            else:
                aliases.pop(name, None)
    return aliases


def _simple_alias_bindings(node: Node, source_bytes: bytes, language: str) -> list[tuple[str, Optional[Node], Optional[str], bool]]:
    """(name, store_target_node, alias_root, read_first) bindings of one statement.

    alias_root is the root receiver identifier when the bound value is exactly
    one call-free member-access chain; None means "bound to a non-alias value"
    and ends any previous alias for that name.  read_first marks compound
    updates and deletions (``x += 1``, ``x++``, ``del x``): the current alias
    value is read once before the name is rebound.
    """
    if language == "python":
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is None:
                return []
            if left.type == "identifier":
                root = _alias_chain_root(node.child_by_field_name("right"), source_bytes)
                return [(_node_text(source_bytes, left).strip(), left, root, False)]
            if left.type == "pattern_list":
                return _python_unpack_bindings(node, left, source_bytes)
            return [
                (_node_text(source_bytes, ident).strip(), ident, None, False)
                for ident in _iter_identifier_nodes(left)
            ]
        if node.type == "named_expression":
            named = node.named_children
            if len(named) >= 2 and named[0].type == "identifier":
                root = _alias_chain_root(named[-1], source_bytes)
                return [(_node_text(source_bytes, named[0]).strip(), named[0], root, False)]
            return []
        if node.type == "augmented_assignment":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                return [(_node_text(source_bytes, left).strip(), left, None, True)]
            # ``x.f += 1`` / ``x[i] += 1`` reads x without rebinding it.
            return []
        if node.type == "delete_statement":
            return [
                (_node_text(source_bytes, child).strip(), child, None, True)
                for child in node.named_children
                if child.type == "identifier"
            ]
        if node.type == "for_statement":
            left = node.child_by_field_name("left")
            if left is None:
                return []
            return [
                (_node_text(source_bytes, ident).strip(), ident, None, False)
                for ident in _iter_identifier_nodes(left)
            ]
        return []
    if language in {"c", "cpp"}:
        if node.type == "declaration":
            type_node = node.child_by_field_name("type")
            type_id = type_node.id if type_node is not None else None
            results: list[tuple[str, Optional[Node], Optional[str], bool]] = []
            for child in node.named_children:
                if type_id is not None and child.id == type_id:
                    continue
                name_node = _find_declarator_name_node(child)
                if name_node is None:
                    continue
                root = None
                if child.type == "init_declarator":
                    root = _alias_chain_root(child.child_by_field_name("value"), source_bytes)
                results.append((_node_text(source_bytes, name_node).strip(), name_node, root, False))
            return results
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is None or left.type != "identifier":
                return []
            name = _node_text(source_bytes, left).strip()
            operator = node.children[1].type if len(node.children) > 1 else "="
            if operator != "=":
                # Compound assignment (+=, -=, ...): reads the current value,
                # then the name holds a computed non-alias value.
                return [(name, left, None, True)]
            root = _alias_chain_root(node.child_by_field_name("right"), source_bytes)
            return [(name, left, root, False)]
        if node.type == "update_expression":
            argument = node.child_by_field_name("argument")
            if argument is not None and argument.type == "identifier":
                return [(_node_text(source_bytes, argument).strip(), argument, None, True)]
        return []
    return []


def _python_unpack_bindings(node: Node, left: Node, source_bytes: bytes) -> list[tuple[str, Optional[Node], Optional[str], bool]]:
    """Pairwise tuple-unpack bindings: ``x, y = r.f, r.g`` aliases x->r, y->r."""
    right = node.child_by_field_name("right")
    targets = [child for child in left.named_children]
    values = list(right.named_children) if right is not None and right.type == "expression_list" else []
    pairwise = (
        len(targets) > 1
        and len(targets) == len(values)
        and all(child.type == "identifier" for child in targets)
    )
    if not pairwise:
        return [
            (_node_text(source_bytes, ident).strip(), ident, None, False)
            for ident in _iter_identifier_nodes(left)
        ]
    return [
        (
            _node_text(source_bytes, target).strip(),
            target,
            _alias_chain_root(value, source_bytes),
            False,
        )
        for target, value in zip(targets, values)
    ]


def _alias_chain_root(node: Optional[Node], source_bytes: bytes) -> Optional[str]:
    """Root identifier when node is exactly one call-free member-access chain."""
    if node is None or node.type not in {"attribute", "field_expression"}:
        return None
    current: Optional[Node] = node
    while current is not None and current.type in {"attribute", "field_expression"}:
        current = current.child_by_field_name("object") or current.child_by_field_name("argument")
    if current is not None and current.type == "identifier":
        return _node_text(source_bytes, current).strip()
    return None


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    current = name
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _is_alias_read(node: Node, access_ids: set[int], body_node: Node) -> bool:
    parent = node.parent
    if parent is not None:
        if parent.type == "keyword_argument":
            name_node = parent.child_by_field_name("name")
            if name_node is not None and name_node.id == node.id:
                return False
        if parent.type == "function_definition":
            name_node = parent.child_by_field_name("name")
            if name_node is not None and name_node.id == node.id:
                return False
    current = parent
    while current is not None and current is not body_node:
        if current.id in access_ids:
            return False
        current = current.parent
    return True


def _iter_identifier_nodes(node: Node):
    for child in _iter_nodes(node):
        if child.type == "identifier":
            yield child


def iter_local_variable_names(body_node: Node, source_bytes: bytes, language: str) -> list[tuple[str, int]]:
    """Local variable names assigned or declared inside a function body."""
    names: list[tuple[str, int]] = []
    if language == "python":
        for node in _iter_nodes(body_node):
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                names.append((_node_text(source_bytes, left).strip(), _node_start_line(node)))
        return names
    if language in {"c", "cpp"}:
        for node in _iter_nodes(body_node):
            if node.type != "declaration":
                continue
            type_node = node.child_by_field_name("type")
            type_id = type_node.id if type_node is not None else None
            for child in node.named_children:
                if type_id is not None and child.id == type_id:
                    continue
                name_node = _find_declarator_name_node(child)
                if name_node is not None:
                    names.append((_node_text(source_bytes, name_node).strip(), _node_start_line(child)))
        return names
    return names


def _root_receiver_identifier(node: Optional[Node], source_bytes: bytes) -> str:
    while node is not None:
        if node.type in {"identifier", "this"}:
            return _node_text(source_bytes, node).strip()
        if node.type == "attribute":
            node = node.child_by_field_name("object")
            continue
        if node.type == "field_expression":
            node = node.child_by_field_name("argument")
            continue
        if node.type == "call":
            node = node.child_by_field_name("function")
            continue
        if node.type == "parenthesized_expression":
            node = node.named_children[0] if node.named_children else None
            continue
        return ""
    return ""


@lru_cache(maxsize=None)
def _get_parser(language: str):
    global _TREE_SITTER_IMPORT_ERROR, _get_tree_sitter_parser
    if _get_tree_sitter_parser is None and _TREE_SITTER_IMPORT_ERROR is None:
        try:
            from tree_sitter_language_pack import get_parser as _ts_get_parser
            _get_tree_sitter_parser = _ts_get_parser
        except Exception as exc:
            _TREE_SITTER_IMPORT_ERROR = exc
    if _TREE_SITTER_IMPORT_ERROR is not None:
        raise RuntimeError(
            "tree-sitter runtime unavailable: "
            f"{_TREE_SITTER_IMPORT_ERROR}. Install 'tree-sitter' and 'tree-sitter-language-pack'."
        )
    try:
        return _get_tree_sitter_parser(language)
    except Exception as exc:
        raise RuntimeError(
            f"tree-sitter grammar unavailable for language '{language}': {exc}"
        ) from exc


def _parse_tree(path: Path, language: str, source_bytes: bytes):
    parser = _get_parser(language)
    try:
        return parser.parse(source_bytes).root_node
    except Exception as exc:
        raise RuntimeError(f"Failed to parse '{path}' with tree-sitter language '{language}': {exc}") from exc


def _find_first_function_node(source_text: str, language: str) -> Tuple[Optional[Node], bytes]:
    source_bytes = source_text.encode("utf-8")
    root = _parse_tree(Path("<memory>"), language, source_bytes)
    for node in _iter_nodes(root):
        if node.type in FUNCTION_NODE_TYPES.get(language, set()):
            return node, source_bytes
    return None, source_bytes


def _find_matching_function(root: Node, source_bytes: bytes, target: LocationTarget, language: str) -> Optional[Node]:
    function_nodes = [node for node in _iter_nodes(root) if node.type in FUNCTION_NODE_TYPES.get(language, set())]
    if not function_nodes:
        return None
    method_name = method_basename(target.method)
    if method_name:
        named_matches = [
            node
            for node in function_nodes
            if _extract_declared_name(node, language, source_bytes) == method_name
        ]
        if not named_matches:
            return None
        return _select_best_node(named_matches, target.line)
    if target.line is not None:
        line_matches = [node for node in function_nodes if _node_contains_line(node, target.line)]
        if line_matches:
            return _select_smallest_span(line_matches)
    return _select_best_node(function_nodes, target.line)


def _iter_nodes(node: Node):
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _extract_declared_name(node: Node, language: str, source_bytes: bytes) -> Optional[str]:
    if language in {"python", "java"}:
        name_node = node.child_by_field_name("name")
        return _node_text(source_bytes, name_node).strip() if name_node is not None else None
    declarator_node = node.child_by_field_name("declarator")
    if declarator_node is None:
        return None
    name_node = _find_declarator_name_node(declarator_node)
    return _node_text(source_bytes, name_node).strip() if name_node is not None else None


def _find_declarator_name_node(node: Node) -> Optional[Node]:
    if node.type in {"identifier", "field_identifier", "destructor_name", "operator_name"}:
        return node
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        nested = _find_declarator_name_node(declarator)
        if nested is not None:
            return nested
    for child in reversed(node.named_children):
        nested = _find_declarator_name_node(child)
        if nested is not None:
            return nested
    return None


def _select_best_node(nodes: List[Node], target_line: Optional[int]) -> Optional[Node]:
    if not nodes:
        return None
    if target_line is None:
        return _select_smallest_span(nodes)
    containing = [node for node in nodes if _node_contains_line(node, target_line)]
    if containing:
        return _select_smallest_span(containing)
    return min(
        nodes,
        key=lambda node: (
            abs((_node_start_line(node) or 0) - target_line),
            _node_line_span(node),
            _node_start_line(node),
        ),
    )


def _select_smallest_span(nodes: List[Node]) -> Node:
    return min(nodes, key=lambda node: (_node_line_span(node), _node_start_line(node), node.start_byte))


def _node_contains_line(node: Node, line_number: int) -> bool:
    return _node_start_line(node) <= line_number <= _node_end_line(node)


def _node_start_line(node: Node) -> int:
    return node.start_point.row + 1


def _node_end_line(node: Node) -> int:
    return node.end_point.row + 1


def _node_line_span(node: Node) -> int:
    return _node_end_line(node) - _node_start_line(node)


def _build_source_snippet(node: Node, source_bytes: bytes, language: str) -> Optional[SourceSnippet]:
    body_node = node.child_by_field_name("body")
    if body_node is None:
        return None
    signature_end = body_node.start_byte
    if source_bytes[body_node.start_byte : body_node.start_byte + 1] == b"{":
        signature_end += 1
    signature_text = _decode(source_bytes[node.start_byte:signature_end]).rstrip()
    body_text = _extract_body_text(body_node, source_bytes, language)
    return SourceSnippet(
        start_line=_node_start_line(node),
        end_line=_node_end_line(node),
        signature_text=signature_text,
        body_text=body_text,
        parameter_count=_count_parameters_from_node(node, language, source_bytes),
        complexity_hint=_estimate_complexity_from_node(body_node, language, source_bytes),
    )


def _extract_body_text(body_node: Node, source_bytes: bytes, language: str) -> str:
    body_text = _node_text(source_bytes, body_node)
    if language == "python":
        return body_text
    if body_text.startswith("{") and body_text.endswith("}"):
        return body_text[1:-1]
    return body_text


def _count_parameters_from_node(node: Node, language: str, source_bytes: bytes) -> int:
    parameters_node = _find_parameters_node(node, language)
    if parameters_node is None:
        return 0
    params = [
        child
        for child in parameters_node.named_children
        if child.type in PARAMETER_NODE_TYPES.get(language, set())
    ]
    if language in {"c", "cpp"} and len(params) == 1:
        only = _node_text(source_bytes, params[0]).strip()
        if only == "void":
            return 0
    if language == "python" and params:
        first_param = _python_parameter_name(params[0], source_bytes)
        if first_param in {"self", "cls"}:
            params = params[1:]
    return len(params)


def _parameter_fingerprints_from_node(node: Node, language: str, source_bytes: bytes) -> list[str]:
    parameters_node = _find_parameters_node(node, language)
    if parameters_node is None:
        return []
    fingerprints: list[str] = []
    for child in parameters_node.named_children:
        if child.type not in PARAMETER_NODE_TYPES.get(language, set()):
            continue
        fingerprint = _parameter_fingerprint(child, language, source_bytes)
        if fingerprint:
            fingerprints.append(fingerprint)
    if language in {"c", "cpp"} and fingerprints == ["void"]:
        return []
    return fingerprints


def _parameter_fingerprint(node: Node, language: str, source_bytes: bytes) -> str:
    if language == "python":
        return _python_parameter_fingerprint(node, source_bytes)
    if language == "java":
        raw = _node_text(source_bytes, node).strip()
        return _java_parameter_fingerprint(raw)
    if language in {"c", "cpp"}:
        raw = _node_text(source_bytes, node).strip()
        if raw == "void":
            return "void"
        return _c_family_parameter_fingerprint(node, source_bytes, raw)
    return ""


def _python_parameter_fingerprint(node: Node, source_bytes: bytes) -> str:
    raw = _node_text(source_bytes, node).strip()
    raw = re.sub(r"=.*", "", raw).strip()
    type_text = ""
    if ":" in raw:
        name_text, type_text = raw.split(":", 1)
        raw = name_text.strip()
        type_text = type_text.strip()
    raw = re.sub(r"^[*]+", "", raw).strip()
    if not raw:
        return ""
    return f"{type_text}:{raw}"


def _java_parameter_fingerprint(raw: str) -> str:
    part = re.sub(r"@\w+(?:\([^)]*\))?", " ", raw.strip())
    part = re.sub(r"\b(?:final|volatile|transient)\b", " ", part)
    part = re.sub(r"\s+", " ", part).strip()
    if not part:
        return ""
    chunks = part.split(" ")
    name = chunks[-1].strip()
    type_text = " ".join(chunks[:-1]).strip()
    if not name or not type_text:
        return name.lower()
    return f"{_normalize_java_signature_type(type_text)}:{name}"


def _c_family_parameter_fingerprint(node: Node, source_bytes: bytes, raw: str) -> str:
    raw = re.sub(r"=.*", "", raw).strip()
    fn_ptr = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", raw)
    if fn_ptr:
        name = fn_ptr.group(1)
        return f"{raw.replace(name, '').strip()}:{name}"
    array_match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", raw)
    if array_match:
        name = array_match.group(1)
        type_text = raw[: array_match.start()].strip()
        type_text = re.sub(r"\s+[*&]+\s*$", "", type_text).strip()
        return f"{type_text}:{name}"
    declarator = node.child_by_field_name("declarator")
    name_node = _find_declarator_name_node(declarator) if declarator is not None else None
    name = _node_text(source_bytes, name_node).strip() if name_node is not None else ""
    if not name:
        return re.sub(r"\s+", "", raw)
    before_name = raw.rsplit(name, 1)[0]
    type_text = re.sub(r"\s+", " ", before_name).strip()
    type_text = type_text.rstrip("*&").strip() + before_name[len(before_name.rstrip("*&")) :]
    type_text = re.sub(r"\s+", "", type_text)
    return f"{type_text or 'unknown'}:{name}"


def _find_parameters_node(node: Node, language: str) -> Optional[Node]:
    if language in {"python", "java"}:
        return node.child_by_field_name("parameters")
    declarator = node.child_by_field_name("declarator")
    while declarator is not None:
        params = declarator.child_by_field_name("parameters")
        if params is not None:
            return params
        declarator = declarator.child_by_field_name("declarator")
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        return _find_first_node_type(declarator, {"parameter_list"})
    return None


def _find_first_node_type(node: Node, node_types: set[str]) -> Optional[Node]:
    if node.type in node_types:
        return node
    for child in node.children:
        match = _find_first_node_type(child, node_types)
        if match is not None:
            return match
    return None


def _python_parameter_name(node: Node, source_bytes: bytes) -> Optional[str]:
    if node.type == "identifier":
        return _node_text(source_bytes, node).strip()
    for child in node.named_children:
        if child.type == "identifier":
            return _node_text(source_bytes, child).strip()
    return None


def _estimate_complexity_from_node(body_node: Node, language: str, source_bytes: bytes) -> int:
    if language == "java":
        function = body_node.parent
        method_name = (
            _extract_declared_name(function, "java", source_bytes)
            if function is not None
            else ""
        ) or ""
        return _java_cognitive_complexity(body_node, source_bytes, method_name)
    control_types = COMPLEXITY_NODE_TYPES.get(language, set())
    total = 0
    for node in _iter_nodes(body_node):
        if node.type not in control_types:
            continue
        total += 1 + _control_nesting_depth(node, control_types)
    return total


_JAVA_COGNITIVE_CONTROLS = {
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "switch_expression",
    "switch_statement",
    "catch_clause",
    "ternary_expression",
}


def _java_cognitive_complexity(
    body_node: Node,
    source_bytes: bytes,
    method_name: str,
) -> int:
    def logical_operator(node: Node) -> str:
        if node.type != "binary_expression":
            return ""
        for child in node.children:
            if child.is_named:
                continue
            token = _node_text(source_bytes, child).strip()
            if token in {"&&", "||"}:
                return token
        return ""

    def is_recursive_call(node: Node) -> bool:
        if not method_name or node.type != "method_invocation":
            return False
        name_node = node.child_by_field_name("name")
        return bool(
            name_node is not None
            and _node_text(source_bytes, name_node).strip() == method_name
        )

    complexity = 0
    nesting = 0
    current_boolean_operator = ""

    def structural() -> None:
        nonlocal complexity, nesting
        complexity += 1 + nesting
        nesting += 1

    def reset_boolean_sequence() -> None:
        nonlocal current_boolean_operator
        current_boolean_operator = ""

    def walk(node: Node) -> None:
        nonlocal complexity, nesting, current_boolean_operator

        if node.type == "block":
            for child in node.named_children:
                # PMD ends a boolean-operation run at every block statement.
                reset_boolean_sequence()
                walk(child)
            return

        if is_recursive_call(node):
            complexity += 1

        if node.type == "if_statement":
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if condition is not None:
                walk(condition)
            is_else_if = node.parent is not None and node.parent.type == "if_statement"
            if not is_else_if:
                structural()
            if consequence is not None:
                walk(consequence)
            if not is_else_if:
                nesting -= 1
            if alternative is not None:
                # PMD treats every else as hybrid complexity: +1 and one
                # nesting level while visiting the complete alternative.
                complexity += 1
                nesting += 1
                walk(alternative)
                nesting -= 1
            return

        if node.type in _JAVA_COGNITIVE_CONTROLS:
            structural()
            for child in node.named_children:
                walk(child)
            nesting -= 1
            return

        if node.type in {"lambda_expression", "class_body"}:
            nesting += 1
            for child in node.named_children:
                walk(child)
            nesting -= 1
            return

        if node.type == "binary_expression":
            operator = logical_operator(node)
            if operator and operator != current_boolean_operator:
                complexity += 1
                current_boolean_operator = operator

        if node.type == "unary_expression":
            text = _node_text(source_bytes, node).lstrip()
            if text.startswith("!"):
                reset_boolean_sequence()

        if node.type in {"break_statement", "continue_statement"}:
            text = _node_text(source_bytes, node)
            if re.search(r"\b(?:break|continue)\s+[A-Za-z_$][A-Za-z0-9_$]*", text):
                complexity += 1

        for child in node.named_children:
            walk(child)

    walk(body_node)
    return complexity


def _estimate_switch_branches_from_node(body_node: Node, language: str, source_bytes: bytes) -> int:
    if language == "python":
        return _count_python_dispatch_branches(body_node)
    return _estimate_switch_branches_from_text(_node_text(source_bytes, body_node), language)


def _python_match_case_count(match_node: Node) -> int:
    """case_clause children of a match_statement (they live inside its block)."""
    for child in match_node.children:
        if child.type == "block":
            return sum(1 for case in child.children if case.type == "case_clause")
    return sum(1 for case in match_node.children if case.type == "case_clause")


def _count_python_dispatch_branches(body_node: Node) -> int:
    """Largest branch fan-out across if/elif/else chains and match statements."""
    max_branches = 0
    for node in _iter_nodes(body_node):
        if node.type == "if_statement":
            branches = 1
            branches += sum(1 for child in node.children if child.type == "elif_clause")
            if any(child.type == "else_clause" for child in node.children):
                branches += 1
            max_branches = max(max_branches, branches)
        elif node.type == "match_statement":
            max_branches = max(max_branches, _python_match_case_count(node))
    return max_branches


def _python_dispatch_metrics(body_node: Node) -> Tuple[int, int, float]:
    """(switch_count, case_count, density) for Python dispatch constructs."""
    switches = 0
    max_branches = 0
    for node in _iter_nodes(body_node):
        if node.type == "if_statement":
            elif_count = sum(1 for child in node.children if child.type == "elif_clause")
            branches = 1 + elif_count
            if any(child.type == "else_clause" for child in node.children):
                branches += 1
            max_branches = max(max_branches, branches)
            if elif_count:
                switches += 1
        elif node.type == "match_statement":
            case_count = _python_match_case_count(node)
            max_branches = max(max_branches, case_count)
            if case_count:
                switches += 1
    density = (max_branches / switches) if switches else 0.0
    return switches, max_branches, density


def _python_switch_metrics_from_text(text: str) -> Tuple[int, int, float]:
    cleaned = strip_comments(text, "python")
    case_count = len(re.findall(r"^\s*(if|elif|else)\b", cleaned, flags=re.MULTILINE))
    return 0, case_count, 0.0


def _control_nesting_depth(node: Node, control_types: set) -> int:
    depth = 0
    current = node.parent
    while current is not None:
        if current.type in control_types and not _is_same_level_control(node, current):
            depth += 1
        current = current.parent
    return depth


def _is_same_level_control(node: Node, ancestor: Node) -> bool:
    if node.type == "elif_clause" and node.parent is ancestor and ancestor.type == "if_statement":
        return True
    if (
        node.type == "if_statement"
        and node.parent is not None
        and node.parent.type == "else_clause"
        and node.parent.parent is ancestor
        and ancestor.type == "if_statement"
    ):
        return True
    return False


def _wrap_signature_source(signature_text: str, language: str) -> Optional[str]:
    stripped = signature_text.strip()
    if not stripped:
        return None
    if language == "python":
        if not stripped.startswith("def "):
            return None
        if not stripped.rstrip().endswith(":"):
            stripped = stripped.rstrip() + ":"
        return stripped + "\n    pass\n"
    if language == "java":
        if not stripped.endswith("{"):
            stripped = stripped.rstrip() + " {"
        return "class __MiniRefactorSignatureWrapper {\n" + stripped + "\n}\n}\n"
    if language in {"c", "cpp"}:
        if not stripped.endswith("{"):
            stripped = stripped.rstrip() + " {"
        return stripped + "\n  return 0;\n}\n"
    return None


def _wrap_body_source(body_text: str, language: str) -> Optional[str]:
    stripped_body = body_text.strip("\n")
    if language == "python":
        indented = "\n".join(("    " + line) if line else "" for line in stripped_body.splitlines())
        if not indented:
            indented = "    pass"
        return "def __smell_core_complexity__():\n" + indented + "\n"
    if language == "java":
        return "class __SmellCoreBodyWrapper {\nvoid __smell_core_complexity__() {\n" + stripped_body + "\n}\n}\n"
    if language in {"c", "cpp"}:
        return "void __smell_core_complexity__() {\n" + stripped_body + "\n}\n"
    return None


def _count_parameters_from_signature_text(signature_text: str, language: str) -> int:
    if "(" not in signature_text or ")" not in signature_text:
        return 0
    inside = signature_text.split("(", 1)[1].rsplit(")", 1)[0]
    raw_params = [param for param in split_top_level_params(inside) if param and param not in {"*", "/", "void"}]
    if language == "python" and raw_params and raw_params[0].strip().split(":", 1)[0].strip() in {"self", "cls"}:
        raw_params = raw_params[1:]
    return len(raw_params)


def _normalize_java_signature_type(type_text: str) -> str:
    text = re.sub(r"\b(?:public|private|protected|static|final|transient|volatile)\b", " ", type_text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(?:[a-z_][a-z0-9_]*\.)+([A-Za-z_$][A-Za-z0-9_$]*)", r"\1", text)
    return text.replace(" ?", "?").replace("< ", "<").replace(" >", ">") or "Object"


def _estimate_complexity_from_text(text: str, language: str) -> int:
    cleaned_lines = strip_comments(text, language).splitlines()
    complexity = 0
    brace_depth = 0
    base_indent = None
    for line in cleaned_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if language == "python":
            indent = len(line) - len(line.lstrip(" "))
            if base_indent is None:
                base_indent = indent
            nesting = max(0, (indent - (base_indent or 0)) // 4)
        else:
            nesting = max(0, brace_depth)
        if re.match(r"^(if|elif|for|while|case|catch|except)\b", stripped):
            complexity += 1 + nesting
        elif re.match(r"^(else\s+if)\b", stripped):
            complexity += 1 + nesting
        if language in {"java", "c", "cpp"}:
            brace_depth += stripped.count("{")
            brace_depth -= stripped.count("}")
            brace_depth = max(0, brace_depth)
    return complexity


def _estimate_switch_branches_from_text(text: str, language: str) -> int:
    cleaned = strip_comments(text, language)
    if language == "python":
        return len(re.findall(r"^\s*(if|elif|else)\b", cleaned, flags=re.MULTILINE))
    return len(
        re.findall(
            r"^\s*(case\b|default\s*:)",
            cleaned,
            flags=re.MULTILINE,
        )
    )


def _node_text(source_bytes: bytes, node: Optional[Node]) -> str:
    if node is None:
        return ""
    return _decode(source_bytes[node.start_byte : node.end_byte])


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _iter_source_files(project_root: Path, extensions: set[str]):
    skip_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".opencode",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "test",
        "tests",
        "build",
        "build-refactoragent",
        "dist",
        "target",
        "cmake-build-debug",
        "cmake-build-release",
    }
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if any(part in skip_dirs for part in path.relative_to(project_root).parts[:-1]):
            continue
        yield path
