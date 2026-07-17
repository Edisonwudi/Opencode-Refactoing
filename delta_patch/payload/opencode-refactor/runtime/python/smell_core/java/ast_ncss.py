"""PMD-compatible Java AST-NCSS using the bundled tree-sitter parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from .syntactic_detector import JavaSyntacticFinding


# Mirrors the nodes incremented by PMD 7 NcssVisitor. Tree-sitter represents
# Java interfaces/records and enhanced-for loops as separate concrete nodes.
_COUNTED_NODE_TYPES = {
    "annotation_type_declaration",
    "assert_statement",
    "break_statement",
    "catch_clause",
    "class_declaration",
    "constructor_declaration",
    "continue_statement",
    "do_statement",
    "enhanced_for_statement",
    "enum_declaration",
    "explicit_constructor_invocation",
    "expression_statement",
    "field_declaration",
    "finally_clause",
    "for_statement",
    "if_statement",
    "interface_declaration",
    "labeled_statement",
    "local_variable_declaration",
    "method_declaration",
    "record_declaration",
    "resource",
    "static_initializer",
    "switch_label",
    "synchronized_statement",
    "throw_statement",
    "return_statement",
    "while_statement",
}
_METHOD_NODE_TYPES = {"method_declaration", "constructor_declaration"}
_TERMINAL_COUNTED_NODE_TYPES = {
    "break_statement",
    "continue_statement",
    "explicit_constructor_invocation",
    "expression_statement",
    "return_statement",
}


@dataclass(frozen=True)
class AstNcssResult:
    ok: bool
    findings: List[JavaSyntacticFinding] = field(default_factory=list)
    error: str = ""


def run_ast_ncss(source_file: Path, project_root: Path, threshold: int) -> AstNcssResult:
    source_file = source_file.resolve()
    project_root = project_root.resolve()
    if not source_file.is_file():
        return AstNcssResult(ok=False, error=f"Java source file does not exist: {source_file}")
    try:
        source = source_file.read_bytes()
        root = get_parser("java").parse(source).root_node
        if root.has_error:
            return AstNcssResult(ok=False, error=f"Java AST contains parse errors: {source_file}")
        try:
            relative_file = source_file.relative_to(project_root).as_posix()
        except ValueError:
            relative_file = source_file.as_posix()
        findings: List[JavaSyntacticFinding] = []
        for method in _walk(root):
            if method.type not in _METHOD_NODE_TYPES:
                continue
            score = count_method_ast_ncss(method)
            if score < int(threshold):
                continue
            name_node = method.child_by_field_name("name")
            method_name = _node_text(source, name_node) if name_node is not None else ""
            findings.append(
                JavaSyntacticFinding(
                    smell_type="long_method",
                    file=relative_file,
                    class_name="",
                    method=f"{method_name}()",
                    begin_line=method.start_point.row + 1,
                    end_line=method.end_point.row + 1,
                    score=float(score),
                    rule_id="AST:NcssCount",
                    evidence=f"ast_ncss={score}; threshold={int(threshold)}",
                )
            )
        return AstNcssResult(ok=True, findings=findings)
    except (OSError, RuntimeError, ValueError) as exc:
        return AstNcssResult(ok=False, error=f"Java AST-NCSS failed: {exc}")


def count_method_ast_ncss(method: Node) -> int:
    """Count one method subtree with PMD NcssVisitor semantics."""
    count = 0
    for node in _walk_metric(method):
        if node.type == "local_variable_declaration" and _is_for_initializer(node):
            continue
        if node.type == "expression_statement" and node.parent is not None and node.parent.type == "switch_rule":
            # The expression to the right of an arrow label is not a PMD
            # ASTExpressionStatement. The switch labels themselves are NCSS.
            continue
        if node.type in _COUNTED_NODE_TYPES:
            count += 1
        if node.type == "switch_expression" and _is_statement_switch(node):
            # tree-sitter uses switch_expression for both Java switch
            # statements and expressions. PMD counts only ASTSwitchStatement.
            count += 1
        if node.type == "if_statement" and node.child_by_field_name("alternative") is not None:
            count += 1
        if node.type == "block" and node.parent is not None and node.parent.type in {
            "class_body",
            "enum_body",
        }:
            # Instance initializer; static initializers have their own node type.
            count += 1
    return count


def _walk_metric(node: Node, *, root: bool = True) -> Iterable[Node]:
    yield node
    # PMD NcssVisitor counts these statements and returns without visiting
    # their expression subtree. This matters when a lambda or anonymous class
    # appears inside an invocation/assignment statement.
    if not root and node.type in _TERMINAL_COUNTED_NODE_TYPES:
        return
    for child in node.named_children:
        yield from _walk_metric(child, root=False)


def _is_for_initializer(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "for_statement"


def _is_statement_switch(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type in {
        "block",
        "do_statement",
        "enhanced_for_statement",
        "for_statement",
        "if_statement",
        "labeled_statement",
        "switch_block_statement_group",
        "switch_rule",
        "while_statement",
    }


def _walk(node: Node) -> Iterable[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
