"""Tree-sitter-based Java semantic smell detector.

Builds a full project model (classes, methods, fields, type hierarchy,
imports) via tree-sitter and then analyses it for four semantic smells:

* **feature_envy** — methods that disproportionately access foreign data.
* **refused_bequest** — child classes that mostly
  override parent methods with stubs or rejections.
* **data_clumps** — parameter groups that recur across many methods/classes.
* **god_class** — large classes with excessive foreign data access.
* **dead_code** — unused private methods with no project-local call/reference.

This is the "Python semantic detector" referenced in guard messages,
so-named because it is implemented in Python (vs. the legacy
SemanticSmellSolver.java oracle).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from ..analysis import count_meaningful_lines
from .detector_utils import (
    normalize_group as _normalize_group,
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
    parse_group_from_evidence as _parse_group_from_evidence,
    parse_parent_from_evidence as _parse_parent_from_evidence,
)


DEFAULT_THRESHOLDS = {
    "feature_envy_foreign_ratio": 0.6,
    "feature_envy_foreign_access": 4,
    "feature_envy_min_loc": 5,
    "refused_bequest_score": 0.7,
    "data_clumps_param_group_size": 3,
    "data_clumps_occurrences": 3,
    "data_clumps_min_classes": 2,
    # Keep these values and the predicate in ``_detect_god_class`` aligned
    # with smell_datasets/scripts/collect_god_class.py.  The delivery rows were
    # selected with that detector; using a second threshold family here makes
    # the post-refactoring oracle measure a different smell from the baseline.
    "god_class_min_nom": 5,
    "god_class_min_wmc": 20,
    "god_class_nom": 10,
    "god_class_wmc": 30,
    "god_class_loc": 100,
    "god_class_atfd": 3,
    "god_class_strong_nom": 15,
    "god_class_strong_wmc": 50,
    "god_class_min_signals": 2,
}

DEFAULT_EXCLUDE_PATHS = [
    ".git",
    ".gradle",
    ".idea",
    ".settings",
    "target",
    "build",
    "out",
    "bin",
    "dist",
    "node_modules",
]

DATA_CLUMP_COORD_STEMS = {
    "x",
    "y",
    "z",
    "w",
    "h",
    "x1",
    "x2",
    "y1",
    "y2",
    "z1",
    "z2",
    "startx",
    "starty",
    "endx",
    "endy",
    "width",
    "height",
    "left",
    "right",
    "top",
    "bottom",
    "rotation",
    "angle",
    "originx",
    "originy",
    "alpha",
    "opacity",
    "radius",
    "margin",
    "pointer",
}

DATA_CLUMP_FRAMEWORK_TYPES = {
    "java.util.Locale",
    "java.util.TimeZone",
    "java.lang.StringBuffer",
}

JAVA_LANG_TYPES = {
    "Appendable",
    "Boolean",
    "Byte",
    "Character",
    "CharSequence",
    "Class",
    "Double",
    "Enum",
    "Exception",
    "Float",
    "IllegalArgumentException",
    "IllegalStateException",
    "Integer",
    "Iterable",
    "Long",
    "Number",
    "Object",
    "RuntimeException",
    "Short",
    "String",
    "StringBuilder",
    "StringBuffer",
    "Throwable",
    "UnsupportedOperationException",
}

PRIMITIVE_TYPES = {"boolean", "byte", "char", "double", "float", "int", "long", "short", "void"}
METHOD_NODE_TYPES = {"method_declaration", "constructor_declaration"}
CLASS_NODE_TYPES = {"class_declaration", "interface_declaration", "enum_declaration"}
DECLARATION_TYPES = {
    "annotation",
    "class",
    "constructor",
    "enum",
    "interface",
    "method",
    "record",
}
CONSTANT_LITERAL_TYPES = {
    "null_literal",
    "true",
    "false",
    "decimal_integer_literal",
    "hex_integer_literal",
    "octal_integer_literal",
    "binary_integer_literal",
    "decimal_floating_point_literal",
    "hex_floating_point_literal",
    "character_literal",
    "string_literal",
}

GOD_CLASS_CONTROL_NODE_TYPES = {
    "if_statement",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "case",
    "catch_clause",
}

GOD_CLASS_ATFD_EXCLUDED_RECEIVERS = {
    "this",
    "super",
    "get",
    "set",
    "is",
    "add",
    "remove",
    "if",
    "while",
    "for",
    "return",
    "new",
    "null",
    "true",
    "false",
}

GOD_CLASS_ATFD_EXCLUDED_TYPES = {
    "String",
    "Integer",
    "Long",
    "Boolean",
    "Double",
    "Float",
    "List",
    "Map",
    "Set",
    "Object",
    "Class",
    "Exception",
    "Override",
    "Public",
    "Private",
    "Protected",
    "Static",
    "Final",
    "Void",
}


@dataclass(frozen=True)
class SemanticFinding:
    smell_type: str
    file: str
    class_name: str
    method: str
    begin_line: int
    end_line: int
    score: float
    rule_id: str
    evidence: str


@dataclass(frozen=True)
class SemanticDetectionResult:
    ok: bool
    findings: Dict[str, List[SemanticFinding]]
    error: str = ""


@dataclass
class JavaFileModel:
    path: Path
    rel_path: str
    source: bytes
    root: Node
    package: str = ""
    imports: Dict[str, str] = field(default_factory=dict)
    wildcard_imports: List[str] = field(default_factory=list)


@dataclass
class ClassRecord:
    file: str
    class_name: str
    qualified_name: str
    begin_line: int
    end_line: int
    kind: str
    superclass_name: str = ""
    interface_names: List[str] = field(default_factory=list)
    modifiers: Set[str] = field(default_factory=set)
    type_parameters: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)
    methods: List["MethodRecord"] = field(default_factory=list)
    declared_method_names: Set[str] = field(default_factory=set)
    bodyless_method_declarations: List[str] = field(default_factory=list)


@dataclass
class MethodRecord:
    file: str
    class_name: str
    owner_qualified_name: str
    method_name: str
    method_signature: str
    begin_line: int
    end_line: int
    body: Optional[Node]
    body_text: str
    declaration_text: str
    loc: int
    return_type: str
    parameter_descriptors: List[str]
    parameter_types: List[str]
    parameters: Dict[str, str]
    local_variables: Dict[str, str]
    enhanced_for_variables: Set[str]
    modifiers: Set[str]
    annotations: Set[str]
    is_constructor: bool
    super_access_count: int


@dataclass(frozen=True)
class ReceiverInfo:
    type_name: str
    origin: str


@dataclass
class MemberAccessStats:
    total: int = 0
    foreign: int = 0
    local: int = 0
    foreign_by_origin: Dict[str, int] = field(default_factory=dict)
    local_by_origin: Dict[str, int] = field(default_factory=dict)
    ignored_by_origin: Dict[str, int] = field(default_factory=dict)
    foreign_by_type: Dict[str, int] = field(default_factory=dict)
    local_by_type: Dict[str, int] = field(default_factory=dict)
    ignored_by_type: Dict[str, int] = field(default_factory=dict)
    unresolved: int = 0


@dataclass
class ProjectModel:
    root: Path
    files: List[JavaFileModel]
    classes: Dict[str, ClassRecord]
    classes_by_simple: Dict[str, List[ClassRecord]]
    methods: List[MethodRecord]


def run_java_semantic_detector(
    project_root: Path,
    *,
    include_tests: bool = True,
    classpath: str = "",
    timeout_seconds: int = 300,
) -> SemanticDetectionResult:
    del classpath, timeout_seconds
    try:
        model = _build_project_model(project_root, include_tests=include_tests)
        feature_envy = _detect_feature_envy(model)
        refused_bequest = _detect_refused_bequest(model)
        data_clumps = _detect_data_clumps(model)
        god_class = _detect_god_class(model)
        dead_code = _detect_dead_code(model)
        findings = {
            "feature_envy": _sort_findings(feature_envy),
            "refused_bequest": _sort_findings(refused_bequest),
            "data_clumps": _sort_findings(data_clumps),
            "god_class": _sort_findings(god_class),
            "dead_code": _sort_findings(dead_code),
        }
        return SemanticDetectionResult(ok=True, findings=findings)
    except Exception as exc:
        return _failed(f"Python semantic detector failed: {exc}")


def analyze_feature_envy_target(
    project_root: Path,
    *,
    target_file: Path,
    method: Optional[str] = None,
    line: Optional[int] = None,
    expected_receiver_type: str = "",
) -> Dict[str, Any]:
    """Return threshold-independent Feature Envy metrics for one method.

    Delivery rows may be trusted review candidates even when they do not cross
    the strict detector threshold.  This profile exposes the continuous values
    used by the detector so a verifier can compare the immutable baseline with
    the edited source instead of treating an initial non-finding as a repair.
    """
    root = project_root.expanduser().resolve()
    model = _build_project_model(root, include_tests=False)
    target_rel = _normalize_rel_path(target_file, root)
    target_method = _normalize_method(method)
    candidates = [item for item in model.methods if _normalize_path(item.file) == target_rel]
    if target_method:
        candidates = [item for item in candidates if _normalize_method(item.method_name) == target_method]
    elif line is not None:
        candidates = [item for item in candidates if item.begin_line <= line <= item.end_line]
    if not candidates:
        return {
            "ok": False,
            "error": "target_method_not_found",
            "file": target_rel,
            "method": str(method or ""),
            "line": line,
        }
    target = min(candidates, key=lambda item: (item.end_line - item.begin_line, item.begin_line))
    stats = _member_access_stats(model, target, feature_envy_semantics=True)
    dominant_type, dominant_count = _dominant_access(stats.foreign_by_type)
    expected_simple = _erase_type(expected_receiver_type).rsplit(".", 1)[-1].strip()
    expected_count = 0
    expected_types: Dict[str, int] = {}
    if expected_simple:
        for type_name, count in stats.foreign_by_type.items():
            if _erase_type(type_name).rsplit(".", 1)[-1] == expected_simple:
                expected_types[type_name] = count
                expected_count += count
    ratio = dominant_count / stats.total if stats.total else 0.0
    expected_ratio = expected_count / stats.total if stats.total else 0.0
    strict_hit = (
        target.loc >= int(DEFAULT_THRESHOLDS["feature_envy_min_loc"])
        and dominant_count >= int(DEFAULT_THRESHOLDS["feature_envy_foreign_access"])
        and ratio >= float(DEFAULT_THRESHOLDS["feature_envy_foreign_ratio"])
    )
    return {
        "ok": True,
        "file": target.file,
        "class_name": target.class_name,
        "method": target.method_signature,
        "method_name": target.method_name,
        "begin_line": target.begin_line,
        "end_line": target.end_line,
        "method_loc": target.loc,
        "expected_receiver_type": expected_receiver_type,
        "expected_receiver_access": expected_count,
        "expected_receiver_ratio": round(expected_ratio, 6),
        "matched_expected_types": expected_types,
        "dominant_receiver_type": dominant_type,
        "dominant_receiver_access": dominant_count,
        "dominant_receiver_ratio": round(ratio, 6),
        "total_member_access": stats.total,
        "aggregate_foreign_access": stats.foreign,
        "local_access": stats.local,
        "unresolved_access": stats.unresolved,
        "foreign_type_count": len(stats.foreign_by_type),
        "foreign_by_type": dict(sorted(stats.foreign_by_type.items())),
        "foreign_by_origin": dict(sorted(stats.foreign_by_origin.items())),
        "ignored_by_type": dict(sorted(stats.ignored_by_type.items())),
        "strict_detector_hit": strict_hit,
    }


def analyze_refused_bequest_target(
    project_root: Path,
    *,
    target_file: Path,
    method: Optional[str],
    line: Optional[int],
    reported_parent: str,
) -> Dict[str, Any]:
    """Return the positive hierarchy contract for one Refused Bequest target."""
    root = project_root.expanduser().resolve()
    model = _build_project_model(root, include_tests=False)
    target_rel = _normalize_rel_path(target_file, root)
    target_method = _normalize_method(method)
    parent_name = str(reported_parent or "").strip()
    if not target_method or not parent_name:
        return {
            "ok": False,
            "error": "target_method_or_parent_missing",
            "file": target_rel,
            "method": target_method,
            "reported_parent": parent_name,
        }

    file_classes = [
        item for item in model.classes.values() if _normalize_path(item.file) == target_rel
    ]
    method_owners = {
        item.owner_qualified_name
        for item in model.methods
        if _normalize_path(item.file) == target_rel
        and _normalize_method(item.method_name) == target_method
    }
    candidates = (
        [item for item in file_classes if item.begin_line <= line <= item.end_line]
        if line is not None
        else []
    )
    if not candidates:
        candidates = [item for item in file_classes if item.qualified_name in method_owners]
    if not candidates:
        candidates = file_classes
    if not candidates:
        return {
            "ok": False,
            "error": "target_class_not_found",
            "file": target_rel,
            "method": target_method,
            "reported_parent": parent_name,
        }
    target_class = min(
        candidates,
        key=lambda item: (
            0 if line is not None and item.begin_line <= line <= item.end_line else 1,
            abs(item.begin_line - int(line or item.begin_line)),
            item.end_line - item.begin_line,
        ),
    )

    parent_simple = parent_name.rsplit(".", 1)[-1].lower()
    inherited_names = _all_parent_type_names(model, target_class)
    inherits_parent = any(
        name.rsplit(".", 1)[-1].lower() == parent_simple for name in inherited_names
    )
    parent_candidates = [
        item
        for item in model.classes.values()
        if item.class_name.lower() == parent_simple
        or item.qualified_name.lower() == parent_name.lower()
    ]
    parent_record = parent_candidates[0] if parent_candidates else None
    child_declares_target = target_method in {
        _normalize_method(name) for name in target_class.declared_method_names
    }
    parent_declares_target = bool(
        parent_record
        and target_method
        in {_normalize_method(name) for name in parent_record.declared_method_names}
    )
    capability_split_satisfied = not inherits_parent or bool(
        parent_record and not child_declares_target and not parent_declares_target
    )
    return {
        "ok": True,
        "file": target_rel,
        "method": target_method,
        "target_class": target_class.qualified_name,
        "reported_parent": parent_name,
        "parent_resolved": parent_record is not None,
        "inherited_types": sorted(inherited_names),
        "inherits_reported_parent": inherits_parent,
        "child_declares_target": child_declares_target,
        "parent_declares_target": parent_declares_target,
        "capability_split_satisfied": capability_split_satisfied,
    }


def find_matching_semantic_finding(
    findings: List[SemanticFinding],
    *,
    target_file: Path,
    project_root: Path,
    method: Optional[str],
    line: Optional[int],
    evidence_group: str = "",
    evidence_parent: str = "",
) -> Optional[SemanticFinding]:
    target_rel = _normalize_rel_path(target_file, project_root)
    target_method = _normalize_method(method)
    target_group = _normalize_group(evidence_group)
    target_parent = str(evidence_parent or "").strip().lower()
    has_strong_anchor = bool(target_method or target_group or target_parent)
    candidates: List[SemanticFinding] = []
    for finding in findings:
        if _normalize_path(finding.file) != target_rel:
            continue
        if target_method and _normalize_method(finding.method) != target_method:
            continue
        if not has_strong_anchor and line and finding.begin_line and abs(finding.begin_line - line) > 3:
            continue
        if target_group and _normalize_group(_parse_group_from_evidence(finding.evidence)) != target_group:
            continue
        if target_parent:
            finding_parent = _parse_parent_from_evidence(finding.evidence)
            if finding_parent and finding_parent != target_parent:
                continue
        candidates.append(finding)
    if not candidates:
        return None
    if line:
        return min(candidates, key=lambda item: abs((item.begin_line or 0) - line))
    return candidates[0]


def find_data_clump_group_occurrences(
    project_root: Path,
    *,
    group: str,
    include_tests: bool = True,
) -> List[SemanticFinding]:
    """Find methods containing an explicitly requested data-clump parameter group.

    This intentionally does not apply ``_should_skip_data_clump_group`` because
    dataset evidence is already a concrete target. Method-level skips still
    remove constructors, tests, and overrides that are not useful repair targets.
    """
    target_group = _normalize_group(group)
    if not target_group:
        return []
    group_size = len([item for item in str(group or "").split("|") if item.strip()])
    if group_size <= 0:
        return []
    model = _build_project_model(project_root, include_tests=include_tests)
    matches: List[MethodRecord] = []
    for method in model.methods:
        if _should_skip_data_clump_method(method):
            continue
        if len(method.parameter_descriptors) < group_size:
            continue
        method_groups = {
            _normalize_group(combo)
            for combo in _parameter_combinations(method.parameter_descriptors, group_size)
        }
        if target_group in method_groups:
            matches.append(method)
    if not matches:
        return []
    rule_id = f"symbol_solver:data_clumps:{_java_hex_hash(target_group)}"
    return _sort_findings(
        [
            SemanticFinding(
                smell_type="data_clumps",
                file=method.file,
                class_name=method.class_name,
                method=method.method_signature,
                begin_line=method.begin_line,
                end_line=method.end_line,
                score=float(len(matches)),
                rule_id=rule_id,
                evidence=f"group={target_group}; occurrences={len(matches)}; explicit_group=true",
            )
            for method in matches
        ]
    )


def _build_project_model(project_root: Path, *, include_tests: bool) -> ProjectModel:
    root = project_root.expanduser().resolve()
    parser = get_parser("java")
    files: List[JavaFileModel] = []
    classes: Dict[str, ClassRecord] = {}
    classes_by_simple: Dict[str, List[ClassRecord]] = {}
    methods: List[MethodRecord] = []

    for path in _list_java_files(root, include_tests=include_tests):
        source = path.read_bytes()
        tree = parser.parse(source)
        file_model = JavaFileModel(
            path=path,
            rel_path=_relative_unix(root, path),
            source=source,
            root=tree.root_node,
        )
        _read_file_header(file_model)
        files.append(file_model)

    for file_model in files:
        for class_node in _iter_top_level_class_nodes(file_model.root):
            _collect_class_records(file_model, class_node, [], classes, classes_by_simple)

    for file_model in files:
        for class_node in _iter_top_level_class_nodes(file_model.root):
            _collect_method_records(file_model, class_node, [], classes, classes_by_simple, methods)

    return ProjectModel(root=root, files=files, classes=classes, classes_by_simple=classes_by_simple, methods=methods)


def _read_file_header(file_model: JavaFileModel) -> None:
    for child in file_model.root.children:
        if child.type == "package_declaration":
            for node in child.children:
                if node.type in {"identifier", "scoped_identifier"}:
                    file_model.package = _node_text(file_model.source, node)
                    break
        elif child.type == "import_declaration":
            text = _node_text(file_model.source, child)
            cleaned = text.replace("import", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
            if not cleaned:
                continue
            if cleaned.endswith(".*"):
                file_model.wildcard_imports.append(cleaned[:-2])
            else:
                file_model.imports[cleaned.rsplit(".", 1)[-1]] = cleaned


def _iter_top_level_class_nodes(root: Node) -> Iterable[Node]:
    for child in root.children:
        if child.type in CLASS_NODE_TYPES:
            yield child


def _collect_class_records(
    file_model: JavaFileModel,
    class_node: Node,
    owners: List[str],
    classes: Dict[str, ClassRecord],
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> None:
    class_name = _declared_name(file_model.source, class_node)
    if not class_name:
        return
    qualified_name = _qualified_class_name(file_model.package, owners, class_name)
    rec = ClassRecord(
        file=file_model.rel_path,
        class_name=class_name,
        qualified_name=qualified_name,
        begin_line=_node_start_line(class_node),
        end_line=_node_end_line(class_node),
        kind=class_node.type.replace("_declaration", ""),
        superclass_name=_resolve_type_name(file_model, _superclass_text(file_model.source, class_node), classes_by_simple),
        interface_names=[
            _resolve_type_name(file_model, item, classes_by_simple)
            for item in _interface_texts(file_model.source, class_node)
        ],
        modifiers=_modifiers(file_model.source, class_node),
        type_parameters=_type_parameters(file_model, class_node, classes_by_simple),
        fields={},
    )
    rec.fields = _collect_fields(file_model, class_node, classes_by_simple, rec.type_parameters)
    classes[qualified_name] = rec
    classes_by_simple.setdefault(class_name, []).append(rec)

    body = class_node.child_by_field_name("body") or _first_child(class_node, "class_body") or _first_child(class_node, "enum_body")
    if body is None:
        return
    for child in body.children:
        if child.type in METHOD_NODE_TYPES:
            method_name = _declared_name(file_model.source, child)
            if method_name:
                rec.declared_method_names.add(method_name)
        if child.type in CLASS_NODE_TYPES:
            _collect_class_records(file_model, child, owners + [class_name], classes, classes_by_simple)


def _collect_method_records(
    file_model: JavaFileModel,
    class_node: Node,
    owners: List[str],
    classes: Dict[str, ClassRecord],
    classes_by_simple: Dict[str, List[ClassRecord]],
    methods: List[MethodRecord],
) -> None:
    class_name = _declared_name(file_model.source, class_node)
    if not class_name:
        return
    qualified_name = _qualified_class_name(file_model.package, owners, class_name)
    class_record = classes.get(qualified_name)
    if class_record is None:
        return

    body = class_node.child_by_field_name("body") or _first_child(class_node, "class_body") or _first_child(class_node, "enum_body")
    if body is None:
        return
    for child in body.children:
        if child.type in METHOD_NODE_TYPES:
            record = _build_method_record(file_model, child, class_record, classes_by_simple)
            if record:
                class_record.methods.append(record)
                methods.append(record)
            elif child.type == "method_declaration":
                # The dataset collector counts abstract/native declarations as
                # methods with minimum complexity 1.
                class_record.bodyless_method_declarations.append(_node_text(file_model.source, child))
        elif child.type in CLASS_NODE_TYPES:
            _collect_method_records(file_model, child, owners + [class_name], classes, classes_by_simple, methods)


def _build_method_record(
    file_model: JavaFileModel,
    method_node: Node,
    owner: ClassRecord,
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> Optional[MethodRecord]:
    body = method_node.child_by_field_name("body")
    if body is None:
        return None
    is_constructor = method_node.type == "constructor_declaration"
    name = _declared_name(file_model.source, method_node) or (owner.class_name if is_constructor else "")
    if not name:
        return None
    type_parameters = {**owner.type_parameters, **_type_parameters(file_model, method_node, classes_by_simple)}
    parameters = _parameter_map(file_model, method_node, classes_by_simple, type_parameters)
    parameter_types = [item[1] for item in parameters]
    parameter_names = [item[0] for item in parameters]
    parameter_dict = dict(parameters)
    return_type = ""
    if not is_constructor:
        return_type_node = method_node.child_by_field_name("type")
        if return_type_node is not None:
            return_type = _resolve_type_name(
                file_model,
                _node_text(file_model.source, return_type_node),
                classes_by_simple,
                type_parameters,
            )
    method_signature = f"{name}({', '.join(f'{type_name} {param}' for param, type_name in parameters)})"
    body_text = _node_text(file_model.source, body)
    local_variables, enhanced_for_variables = _local_variable_info(file_model, body, classes_by_simple, type_parameters)
    return MethodRecord(
        file=file_model.rel_path,
        class_name=owner.class_name,
        owner_qualified_name=owner.qualified_name,
        method_name=name,
        method_signature=method_signature,
        begin_line=_node_start_line(method_node),
        end_line=_node_end_line(method_node),
        body=body,
        body_text=body_text,
        declaration_text=_node_text(file_model.source, method_node),
        loc=count_meaningful_lines(body_text, "java"),
        return_type=return_type,
        parameter_descriptors=[f"{type_name}:{_stem_name(param)}" for param, type_name in zip(parameter_names, parameter_types)],
        parameter_types=parameter_types,
        parameters=parameter_dict,
        local_variables=local_variables,
        enhanced_for_variables=enhanced_for_variables,
        modifiers=_modifiers(file_model.source, method_node),
        annotations=_annotations(file_model.source, method_node),
        is_constructor=is_constructor,
        super_access_count=_count_super_accesses(file_model.source, body),
    )


def _detect_feature_envy(model: ProjectModel) -> List[SemanticFinding]:
    findings: List[SemanticFinding] = []
    for method in model.methods:
        if method.loc < int(DEFAULT_THRESHOLDS["feature_envy_min_loc"]):
            continue
        access_stats = _member_access_stats(model, method, feature_envy_semantics=True)
        total_access = access_stats.total
        aggregate_foreign_access = access_stats.foreign
        if total_access <= 0:
            continue
        dominant_type, foreign_access = _dominant_access(access_stats.foreign_by_type)
        ratio = foreign_access / total_access
        if (
            foreign_access >= int(DEFAULT_THRESHOLDS["feature_envy_foreign_access"])
            and ratio >= float(DEFAULT_THRESHOLDS["feature_envy_foreign_ratio"])
        ):
            findings.append(
                SemanticFinding(
                    smell_type="feature_envy",
                    file=method.file,
                    class_name=method.class_name,
                    method=method.method_signature,
                    begin_line=method.begin_line,
                    end_line=method.end_line,
                    score=ratio,
                    rule_id="symbol_solver:feature_envy",
                    evidence=(
                        f"foreign_access={foreign_access}; total_access={total_access}; "
                        f"aggregate_foreign_access={aggregate_foreign_access}; "
                        f"local_access={access_stats.local}; ratio={ratio:.3f}; loc={method.loc}; "
                        f"dominant_foreign_type={dominant_type or 'none'}; "
                        f"foreign_by_type={_format_counter(access_stats.foreign_by_type)}; "
                        f"foreign_by_origin={_format_counter(access_stats.foreign_by_origin)}; "
                        f"local_by_origin={_format_counter(access_stats.local_by_origin)}"
                    ),
                )
            )
    return findings


def _detect_refused_bequest(model: ProjectModel) -> List[SemanticFinding]:
    findings: List[SemanticFinding] = []
    threshold = float(DEFAULT_THRESHOLDS["refused_bequest_score"])
    for cls in model.classes.values():
        if _should_skip_refused_bequest_class(cls):
            continue
        parent = model.classes.get(cls.superclass_name)
        if parent is None:
            continue
        parent_methods = _collect_parent_methods(model, parent)
        if not parent_methods:
            continue
        overrides = [
            method
            for method in cls.methods
            if any(_method_overrides(method, parent_method) for parent_method in parent_methods)
        ]
        if not overrides:
            continue
        suspicious_count = sum(1 for method in overrides if _is_stub_method(method))
        if suspicious_count < 2:
            continue
        suspicious_ratio = suspicious_count / len(overrides)
        override_ratio = len(overrides) / len(parent_methods)
        super_usage = sum(method.super_access_count for method in overrides)
        low_parent_use = 1.0 if super_usage == 0 else max(0.0, 1.0 - (super_usage / (len(overrides) + 1)))
        if suspicious_ratio < 0.66 or override_ratio < 0.40 or low_parent_use < 0.80:
            continue
        score = 0.65 * suspicious_ratio + 0.25 * override_ratio + 0.10 * low_parent_use
        if score < threshold:
            continue
        findings.append(
            SemanticFinding(
                smell_type="refused_bequest",
                file=cls.file,
                class_name=cls.class_name,
                method="",
                begin_line=cls.begin_line,
                end_line=cls.end_line,
                score=score,
                rule_id="symbol_solver:refused_bequest",
                evidence=(
                    f"parent={parent.class_name}; overrides={len(overrides)}; "
                    f"parent_methods={len(parent_methods)}; suspicious_overrides={suspicious_count}; "
                    f"super_calls={super_usage}; score={score:.3f}"
                ),
            )
        )
    return findings


def _detect_data_clumps(model: ProjectModel) -> List[SemanticFinding]:
    group_size = int(DEFAULT_THRESHOLDS["data_clumps_param_group_size"])
    occurrences_threshold = int(DEFAULT_THRESHOLDS["data_clumps_occurrences"])
    effective_min_classes = max(int(DEFAULT_THRESHOLDS["data_clumps_min_classes"]), 3)
    occurrences: Dict[str, List[MethodRecord]] = {}
    for method in model.methods:
        if _should_skip_data_clump_method(method):
            continue
        if len(method.parameter_descriptors) < group_size:
            continue
        for combo in _parameter_combinations(method.parameter_descriptors, group_size):
            occurrences.setdefault(combo, []).append(method)

    findings: List[SemanticFinding] = []
    for group_key, methods in occurrences.items():
        if _should_skip_data_clump_group(group_key):
            continue
        if len(methods) < occurrences_threshold:
            continue
        method_names = {method.method_name or "" for method in methods}
        if len(method_names) < 2:
            continue
        class_names = {method.class_name for method in methods}
        if len(class_names) < effective_min_classes:
            continue
        rule_id = f"symbol_solver:data_clumps:{_java_hex_hash(group_key)}"
        for method in methods:
            findings.append(
                SemanticFinding(
                    smell_type="data_clumps",
                    file=method.file,
                    class_name=method.class_name,
                    method=method.method_signature,
                    begin_line=method.begin_line,
                    end_line=method.end_line,
                    score=float(len(methods)),
                    rule_id=rule_id,
                    evidence=f"group={group_key}; occurrences={len(methods)}; classes={len(class_names)}",
                )
            )
    return findings


def _detect_god_class(model: ProjectModel) -> List[SemanticFinding]:
    min_nom = int(DEFAULT_THRESHOLDS["god_class_min_nom"])
    min_wmc = int(DEFAULT_THRESHOLDS["god_class_min_wmc"])
    nom_threshold = int(DEFAULT_THRESHOLDS["god_class_nom"])
    wmc_threshold = int(DEFAULT_THRESHOLDS["god_class_wmc"])
    loc_threshold = int(DEFAULT_THRESHOLDS["god_class_loc"])
    atfd_threshold = int(DEFAULT_THRESHOLDS["god_class_atfd"])
    strong_nom_threshold = int(DEFAULT_THRESHOLDS["god_class_strong_nom"])
    strong_wmc_threshold = int(DEFAULT_THRESHOLDS["god_class_strong_wmc"])
    min_signals = int(DEFAULT_THRESHOLDS["god_class_min_signals"])
    findings: List[SemanticFinding] = []
    for cls in model.classes.values():
        if cls.kind != "class" or _is_test_like_rel_path(cls.file):
            continue
        # Dataset NOM/WMC include constructors, so the guard must do the same.
        methods = list(cls.methods)
        if not methods:
            continue
        nom = len(methods) + len(cls.bodyless_method_declarations)
        nof = len(cls.fields)
        wmc = sum(_god_class_method_complexity(method) for method in methods) + len(cls.bodyless_method_declarations)
        loc = max(0, cls.end_line - cls.begin_line + 1)
        atfd = _god_class_atfd(methods, cls.bodyless_method_declarations)
        if nom < min_nom or wmc < min_wmc:
            continue
        signals = [
            name
            for name, matched in (
                ("nom", nom >= nom_threshold),
                ("wmc", wmc >= wmc_threshold),
                ("loc", loc >= loc_threshold),
                ("atfd", atfd >= atfd_threshold),
                ("strong_nom_wmc", nom >= strong_nom_threshold and wmc >= strong_wmc_threshold),
            )
            if matched
        ]
        if len(signals) < min_signals:
            continue
        score = float(len(signals)) + (wmc / max(wmc_threshold, 1))
        findings.append(
            SemanticFinding(
                smell_type="god_class",
                file=cls.file,
                class_name=cls.class_name,
                method="",
                begin_line=cls.begin_line,
                end_line=cls.end_line,
                score=score,
                rule_id="symbol_solver:god_class",
                evidence=(
                    f"class={cls.class_name}; nom={nom}; nof={nof}; wmc={wmc}; "
                    f"loc={loc}; atfd={atfd}; signals={','.join(signals)}; "
                    f"policy=nom>={min_nom}&wmc>={min_wmc}&signals>={min_signals}; "
                    f"signal_thresholds=nom>={nom_threshold}|wmc>={wmc_threshold}|"
                    f"loc>={loc_threshold}|atfd>={atfd_threshold}|"
                    f"strong=nom>={strong_nom_threshold}&wmc>={strong_wmc_threshold}"
                ),
            )
        )
    return findings


def _god_class_method_complexity(method: MethodRecord) -> int:
    """Return the dataset detector's per-method cyclomatic proxy."""
    if method.body is None:
        return 1
    controls = sum(1 for node in _iter_nodes(method.body) if node.type in GOD_CLASS_CONTROL_NODE_TYPES)
    return max(controls, 1)


def _god_class_atfd(
    methods: Sequence[MethodRecord],
    bodyless_declarations: Sequence[str] = (),
) -> int:
    """Return the distinct-access proxy used to create the delivery dataset.

    This intentionally mirrors the reviewed dataset collector instead of the
    feature-envy access-count metric.  The latter counts individual accesses,
    while the God Class dataset records distinct receiver/type tokens.
    """
    foreign_tokens: Set[str] = set()
    declarations = [method.declaration_text for method in methods]
    declarations.extend(bodyless_declarations)
    for text in declarations:
        for match in re.finditer(r"(\w+)\s*\.\s*\w+\s*\(", text):
            receiver = match.group(1)
            if receiver not in GOD_CLASS_ATFD_EXCLUDED_RECEIVERS:
                foreign_tokens.add(receiver)
        for match in re.finditer(r"\b([A-Z][a-zA-Z0-9]*)\b", text):
            type_name = match.group(1)
            if type_name not in GOD_CLASS_ATFD_EXCLUDED_TYPES:
                foreign_tokens.add(type_name)
    return len(foreign_tokens)


def _detect_dead_code(model: ProjectModel) -> List[SemanticFinding]:
    reference_counts = _method_reference_counts(model)
    findings: List[SemanticFinding] = []
    for method in model.methods:
        if not _is_unused_private_method_candidate(method):
            continue
        if reference_counts.get(method.method_name, 0) != 0:
            continue
        findings.append(
            SemanticFinding(
                smell_type="dead_code",
                file=method.file,
                class_name=method.class_name,
                method=method.method_signature,
                begin_line=method.begin_line,
                end_line=method.end_line,
                score=0.0,
                rule_id="symbol_solver:dead_code",
                evidence=(
                    "kind=unused_private_method; "
                    f"class={method.class_name}; method={method.method_signature}; "
                    f"refs=0; loc={method.loc}"
                ),
            )
        )
    return findings


def _method_reference_counts(model: ProjectModel) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for file_model in model.files:
        if _is_test_like_rel_path(file_model.rel_path):
            continue
        for node in _iter_nodes(file_model.root):
            if node.type not in {"method_invocation", "method_reference"}:
                continue
            name = _method_usage_name(file_model.source, node)
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def _method_usage_name(source: bytes, node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(source, name_node).strip()
    if node.type == "method_reference":
        for child in reversed(node.children):
            if child.type == "identifier":
                return _node_text(source, child).strip()
    return ""


def _is_unused_private_method_candidate(method: MethodRecord) -> bool:
    if _is_test_like_rel_path(method.file):
        return False
    if method.is_constructor or "private" not in method.modifiers:
        return False
    if method.annotations:
        return False
    if method.loc < 3:
        return False
    if method.method_name in {"readObject", "writeObject", "readObjectNoData", "readResolve", "writeReplace", "finalize"}:
        return False
    if re.match(r"^(main|setUp|tearDown|before|after|init|destroy)$", method.method_name, flags=re.IGNORECASE):
        return False
    return True


def _count_member_accesses(model: ProjectModel, method: MethodRecord) -> Tuple[int, int]:
    stats = _member_access_stats(model, method)
    return stats.total, stats.foreign


def _member_access_stats(
    model: ProjectModel,
    method: MethodRecord,
    *,
    feature_envy_semantics: bool = False,
) -> MemberAccessStats:
    stats = MemberAccessStats()
    if method.body is None:
        return stats
    env: Dict[str, ReceiverInfo] = {}
    owner = model.classes.get(method.owner_qualified_name)
    if owner is not None:
        env.update({name: ReceiverInfo(type_name, "field") for name, type_name in owner.fields.items()})
    owner_method_returns = _owner_method_return_types(owner)
    env.update({name: ReceiverInfo(type_name, "parameter") for name, type_name in method.parameters.items()})
    env.update(
        {
            name: ReceiverInfo(
                type_name,
                "enhanced_for_variable" if name in method.enhanced_for_variables else "local",
            )
            for name, type_name in method.local_variables.items()
        }
    )

    for receiver_expr in _member_access_receiver_expressions(method.body):
        receiver_info = _receiver_info_for_expression(
            model,
            method,
            env,
            owner,
            owner_method_returns,
            receiver_expr,
        )
        if receiver_info is None:
            stats.unresolved += 1
            continue
        _record_member_access(
            model,
            method,
            stats,
            receiver_info,
            feature_envy_semantics=feature_envy_semantics,
        )
    for _ in _implicit_owner_method_invocation_names(method.body, owner_method_returns):
        _record_member_access(
            model,
            method,
            stats,
            ReceiverInfo(method.owner_qualified_name, "owner_method"),
            feature_envy_semantics=feature_envy_semantics,
        )
    return stats


def _record_member_access(
    model: ProjectModel,
    method: MethodRecord,
    stats: MemberAccessStats,
    receiver_info: ReceiverInfo,
    *,
    feature_envy_semantics: bool = False,
) -> None:
    receiver_type = _normalized_receiver_type(model, receiver_info.type_name)
    if _should_ignore_feature_envy_receiver(
        receiver_info,
        classify_by_type=feature_envy_semantics,
    ):
        _increment_counter(stats.ignored_by_origin, receiver_info.origin)
        _increment_counter(stats.ignored_by_type, receiver_type)
        return
    stats.total += 1
    if not feature_envy_semantics and receiver_info.origin in {"local", "owner", "owner_method"}:
        stats.local += 1
        _increment_counter(stats.local_by_origin, receiver_info.origin)
        _increment_counter(stats.local_by_type, receiver_type)
        return
    if _is_foreign_type(model, receiver_info.type_name, method.owner_qualified_name):
        stats.foreign += 1
        _increment_counter(stats.foreign_by_origin, receiver_info.origin)
        _increment_counter(stats.foreign_by_type, receiver_type)
    else:
        stats.local += 1
        _increment_counter(stats.local_by_origin, receiver_info.origin)
        _increment_counter(stats.local_by_type, receiver_type)


def _increment_counter(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _format_counter(counter: Mapping[str, int]) -> str:
    if not counter:
        return "none"
    return ",".join(f"{key}:{counter[key]}" for key in sorted(counter))


def _dominant_access(counter: Mapping[str, int]) -> Tuple[str, int]:
    if not counter:
        return "", 0
    type_name, count = min(counter.items(), key=lambda item: (-item[1], item[0]))
    return type_name, count


def _normalized_receiver_type(model: ProjectModel, type_name: str) -> str:
    erased = _erase_type(type_name).strip()
    return _resolve_model_type(model, erased) if erased else "<unknown>"


def _owner_method_return_types(owner: Optional[ClassRecord]) -> Dict[str, str]:
    if owner is None:
        return {}
    out: Dict[str, str] = {}
    for method in owner.methods:
        if method.is_constructor or not method.return_type:
            continue
        out.setdefault(method.method_name, method.return_type)
    return out


def _receiver_info_for_expression(
    model: ProjectModel,
    method: MethodRecord,
    env: Dict[str, ReceiverInfo],
    owner: Optional[ClassRecord],
    owner_method_returns: Dict[str, str],
    expression: str,
) -> Optional[ReceiverInfo]:
    text = str(expression or "").strip()
    if not text:
        return None
    if text == "this":
        return ReceiverInfo(owner.qualified_name if owner is not None else method.owner_qualified_name, "owner")
    if text == "super":
        if owner is not None and owner.superclass_name:
            return ReceiverInfo(owner.superclass_name, "owner")
        return ReceiverInfo(method.owner_qualified_name, "owner")
    if text.startswith("this.") or text.startswith("super."):
        return _owner_member_receiver_info(owner, owner_method_returns, text.split(".", 1)[1])
    root = _root_receiver(text)
    if not root or root in {"this", "super"}:
        return None
    if _looks_like_type_name(root, model):
        return None
    if root in env:
        return env[root]
    if root in owner_method_returns and _looks_like_method_call_on_root(text, root):
        return ReceiverInfo(owner_method_returns[root], "method_return")
    return None


def _owner_member_receiver_info(
    owner: Optional[ClassRecord],
    owner_method_returns: Dict[str, str],
    expression: str,
) -> Optional[ReceiverInfo]:
    if owner is None:
        return None
    member = _root_receiver(expression)
    if not member:
        return None
    if member in owner.fields:
        return ReceiverInfo(owner.fields[member], "field")
    if member in owner_method_returns and _looks_like_method_call_on_root(expression, member):
        return ReceiverInfo(owner_method_returns[member], "method_return")
    return None


def _looks_like_method_call_on_root(expression: str, root: str) -> bool:
    return re.match(rf"\s*{re.escape(root)}\s*\(", str(expression or "")) is not None


def _implicit_owner_method_invocation_names(body: Node, owner_method_returns: Mapping[str, str]) -> Iterable[str]:
    if not owner_method_returns:
        return
    for node in _iter_nodes(body):
        if node.type != "method_invocation":
            continue
        if node.child_by_field_name("object") is not None:
            continue
        if _is_receiver_operand(node):
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text_from_node(name_node).strip()
        if name in owner_method_returns:
            yield name


def _is_receiver_operand(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in {"field_access", "method_invocation"}:
        return False
    receiver = parent.child_by_field_name("object")
    if receiver is None and parent.children:
        receiver = parent.children[0]
    return (
        receiver is not None
        and receiver.start_byte == node.start_byte
        and receiver.end_byte == node.end_byte
    )


def _should_ignore_feature_envy_receiver(
    receiver: ReceiverInfo,
    *,
    classify_by_type: bool = False,
) -> bool:
    if _is_primitive(receiver.type_name):
        return True
    if classify_by_type and _is_value_type(receiver.type_name):
        return True
    if _is_container_type(receiver.type_name):
        return True
    if not classify_by_type and receiver.origin == "enhanced_for_variable":
        return True
    if _is_local_tool_type(receiver.type_name) and (
        classify_by_type or receiver.origin == "local"
    ):
        return True
    return False


def _is_value_type(type_name: str) -> bool:
    base, _ = _split_array_suffix(_erase_type(type_name))
    simple = base.rsplit(".", 1)[-1]
    return simple in {
        "BigDecimal",
        "BigInteger",
        "Boolean",
        "Byte",
        "Character",
        "Class",
        "Double",
        "Float",
        "Integer",
        "Long",
        "Number",
        "Short",
        "String",
        "URI",
        "URL",
        "UUID",
        "var",
    }


def _is_container_type(type_name: str) -> bool:
    base, _ = _split_array_suffix(_erase_type(type_name))
    if not base:
        return False
    if type_name.endswith("[]") or base.endswith("[]"):
        return True
    simple = base.rsplit(".", 1)[-1]
    if base.startswith("java.util."):
        return simple in {
            "ArrayDeque",
            "ArrayList",
            "Collection",
            "Deque",
            "HashMap",
            "HashSet",
            "Iterable",
            "Iterator",
            "LinkedHashMap",
            "LinkedHashSet",
            "LinkedList",
            "List",
            "Map",
            "NavigableMap",
            "NavigableSet",
            "Optional",
            "Queue",
            "Set",
            "SortedMap",
            "SortedSet",
            "Spliterator",
            "Stream",
            "TreeMap",
            "TreeSet",
        }
    return simple in {
        "Array",
        "Bits",
        "BooleanSeq",
        "FloatSeq",
        "IntMap",
        "IntSeq",
        "IntSet",
        "LongMap",
        "LongSeq",
        "ObjectFloatMap",
        "ObjectIntMap",
        "ObjectMap",
        "ObjectSet",
        "OrderedMap",
        "OrderedSet",
        "Queue",
        "Seq",
        "SnapshotSeq",
        "StringMap",
    }


def _is_local_tool_type(type_name: str) -> bool:
    base, _ = _split_array_suffix(_erase_type(type_name))
    simple = base.rsplit(".", 1)[-1]
    if base in {
        "java.lang.StringBuilder",
        "java.lang.StringBuffer",
        "java.util.Formatter",
        "java.io.StringWriter",
        "java.io.PrintWriter",
    }:
        return True
    return simple.endswith("Builder") or simple.endswith("Formatter") or simple in {
        "Json",
        "JsonReader",
        "JsonValue",
        "StringJoiner",
    }


def _member_access_receiver_expressions(body: Node) -> Iterable[str]:
    for node in _iter_nodes(body):
        if node.type == "field_access":
            receiver = node.children[0] if node.children else None
            if receiver is not None:
                text = _node_text_from_node(receiver).strip()
                if text in {"this", "super"} and _is_receiver_operand(node):
                    continue
                if text:
                    yield text
        elif node.type == "method_invocation" and len(node.children) >= 3:
            if any(child.type == "." for child in node.children[:3]):
                receiver = node.children[0]
                text = _node_text_from_node(receiver).strip()
                if text in {"this", "super"} and _is_receiver_operand(node):
                    continue
                if text:
                    yield text


def _member_access_receivers(body: Node) -> Iterable[str]:
    for expression in _member_access_receiver_expressions(body):
        yield _root_receiver(expression)


def _is_foreign_type(model: ProjectModel, receiver_type: str, owner_type: str) -> bool:
    receiver = _resolve_model_type(model, receiver_type)
    owner = _resolve_model_type(model, owner_type)
    if not receiver or not owner:
        return True
    if receiver == owner:
        return False
    if _is_subtype(model, receiver, owner):
        return False
    if _is_subtype(model, owner, receiver):
        return False
    return True


def _collect_parent_methods(model: ProjectModel, parent: ClassRecord) -> List[MethodRecord]:
    methods: List[MethodRecord] = []
    current: Optional[ClassRecord] = parent
    seen: Set[str] = set()
    while current is not None and current.qualified_name not in seen:
        seen.add(current.qualified_name)
        for method in current.methods:
            if method.is_constructor:
                continue
            if {"private", "static", "final"} & method.modifiers:
                continue
            if "abstract" in method.modifiers:
                continue
            methods.append(method)
        current = model.classes.get(current.superclass_name)
    return methods


def _method_overrides(method: MethodRecord, parent_method: MethodRecord) -> bool:
    return (
        not method.is_constructor
        and method.method_name == parent_method.method_name
        and method.parameter_types == parent_method.parameter_types
    )


def _is_stub_method(method: MethodRecord) -> bool:
    body = method.body
    if body is None:
        return False
    statements = [child for child in body.children if child.is_named and not _is_comment_node(child)]
    if not statements:
        return True
    if len(statements) != 1:
        return False
    stmt = statements[0]
    if stmt.type == "throw_statement":
        return "UnsupportedOperationException" in _node_text_from_node(stmt)
    if stmt.type == "return_statement":
        expr = next((child for child in stmt.children if child.is_named and child.type != "return"), None)
        return expr is None or _is_constant_literal(expr)
    if stmt.type == "expression_statement":
        text = _node_text_from_node(stmt)
        return bool(
            re.match(
                r"^\s*(?:(?:LOG|LOGGER|log|logger)|System\.(?:out|err))\s*\.\s*"
                r"(?:trace|debug|info|warn|warning|error|print|println)\s*\(",
                text,
            )
        )
    return False


def _is_comment_node(node: Node) -> bool:
    return node.type in {"line_comment", "block_comment"}


def _is_constant_literal(node: Node) -> bool:
    return node.type in CONSTANT_LITERAL_TYPES or node.type.endswith("_literal")


def _should_skip_refused_bequest_class(cls: ClassRecord) -> bool:
    return not cls.superclass_name or cls.kind == "enum"


def _should_skip_data_clump_method(method: MethodRecord) -> bool:
    return (
        _is_test_like_rel_path(method.file)
        or method.is_constructor
        or "Override" in method.annotations
        or "java.lang.Override" in method.annotations
    )


def _should_skip_data_clump_group(group_key: str) -> bool:
    tokens = _parse_group_tokens(group_key)
    if not tokens:
        return True
    unique_stems = set()
    coord_hits = 0
    framework_hits = 0
    for type_name, stem in tokens:
        unique_stems.add(stem)
        if stem in DATA_CLUMP_COORD_STEMS:
            coord_hits += 1
        if _is_framework_like_type(type_name):
            framework_hits += 1
    if len(unique_stems) < len(tokens):
        return True
    if coord_hits >= 1:
        return True
    if framework_hits >= 2:
        return True
    if {"propertyname", "oldvalue", "newvalue"} <= unique_stems:
        return True
    if {"lhs", "rhs"} <= unique_stems:
        return True
    return False


def _parse_group_tokens(group_key: str) -> List[Tuple[str, str]]:
    tokens = []
    for token in str(group_key or "").split("|"):
        if ":" not in token:
            continue
        type_name, stem = token.split(":", 1)
        type_name = type_name.strip()
        stem = stem.strip().lower()
        if type_name and stem:
            tokens.append((type_name, stem))
    return tokens


def _is_framework_like_type(type_name: str) -> bool:
    if not type_name:
        return False
    return (
        type_name in DATA_CLUMP_FRAMEWORK_TYPES
        or type_name.startswith("java.awt.")
        or type_name.startswith("javax.servlet.")
        or type_name.startswith("jakarta.servlet.")
        or type_name.startswith("org.elasticsearch.")
        or type_name.startswith("org.springframework.")
        or type_name.startswith("org.eclipse.jetty.")
        or type_name.endswith("HttpServletRequest")
        or type_name.endswith("HttpServletResponse")
        or type_name.endswith("InputEvent")
        or type_name.endswith("Graphics")
        or type_name.endswith("Component")
        or type_name.endswith("Request")
        or type_name.endswith("Response")
    )


def _parameter_combinations(values: Sequence[str], group_size: int) -> Iterable[str]:
    seen: Set[str] = set()
    for combo in itertools.combinations(values, group_size):
        key = "|".join(sorted(combo))
        if key in seen:
            continue
        seen.add(key)
        yield key


def _parameter_map(
    file_model: JavaFileModel,
    method_node: Node,
    classes_by_simple: Dict[str, List[ClassRecord]],
    type_parameters: Dict[str, str],
) -> List[Tuple[str, str]]:
    params_node = method_node.child_by_field_name("parameters")
    if params_node is None:
        return []
    params: List[Tuple[str, str]] = []
    for child in params_node.children:
        if child.type not in {"formal_parameter", "spread_parameter"}:
            continue
        name = _parameter_name(file_model.source, child)
        type_text = _parameter_type_text(file_model.source, child)
        if name and type_text:
            params.append((name, _resolve_type_name(file_model, type_text, classes_by_simple, type_parameters)))
    return params


def _local_variable_info(
    file_model: JavaFileModel,
    body: Node,
    classes_by_simple: Dict[str, List[ClassRecord]],
    type_parameters: Dict[str, str],
) -> Tuple[Dict[str, str], Set[str]]:
    out: Dict[str, str] = {}
    enhanced_for_variables: Set[str] = set()
    for node in _iter_nodes(body):
        if node.type != "local_variable_declaration":
            if node.type == "enhanced_for_statement":
                type_node = node.child_by_field_name("type") or _first_type_child(node)
                name_node = node.child_by_field_name("name")
                if type_node is not None and name_node is not None:
                    name = _node_text(file_model.source, name_node)
                    out[name] = _resolve_type_name(
                        file_model,
                        _node_text(file_model.source, type_node),
                        classes_by_simple,
                        type_parameters,
                    )
                    enhanced_for_variables.add(name)
            continue
        type_node = node.child_by_field_name("type") or _first_type_child(node)
        if type_node is None:
            continue
        type_name = _resolve_type_name(file_model, _node_text(file_model.source, type_node), classes_by_simple, type_parameters)
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    out[_node_text(file_model.source, name_node)] = type_name
    return out, enhanced_for_variables


def _collect_fields(
    file_model: JavaFileModel,
    class_node: Node,
    classes_by_simple: Dict[str, List[ClassRecord]],
    type_parameters: Dict[str, str],
) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    body = class_node.child_by_field_name("body") or _first_child(class_node, "class_body") or _first_child(class_node, "enum_body")
    if body is None:
        return fields
    for child in body.children:
        if child.type != "field_declaration":
            continue
        type_node = child.child_by_field_name("type") or _first_type_child(child)
        if type_node is None:
            continue
        type_name = _resolve_type_name(file_model, _node_text(file_model.source, type_node), classes_by_simple, type_parameters)
        for item in child.children:
            if item.type != "variable_declarator":
                continue
            name_node = item.child_by_field_name("name")
            if name_node is not None:
                fields[_node_text(file_model.source, name_node)] = type_name
    return fields


def _resolve_type_name(
    file_model: JavaFileModel,
    type_text: str,
    classes_by_simple: Dict[str, List[ClassRecord]],
    type_parameters: Optional[Dict[str, str]] = None,
) -> str:
    cleaned = _erase_type(type_text)
    if not cleaned:
        return "Object"
    base, suffix = _split_array_suffix(cleaned)
    if type_parameters and base in type_parameters:
        return f"{type_parameters[base]}{suffix}"
    if cleaned in PRIMITIVE_TYPES:
        return cleaned
    if base in PRIMITIVE_TYPES:
        return cleaned
    if "." in base:
        return cleaned
    if base in file_model.imports:
        return f"{file_model.imports[base]}{suffix}"
    if base in classes_by_simple:
        candidates = classes_by_simple[base]
        same_package = [cls for cls in candidates if _package_of(cls.qualified_name) == file_model.package]
        return f"{(same_package[0] if same_package else candidates[0]).qualified_name}{suffix}"
    if base in JAVA_LANG_TYPES:
        return f"java.lang.{base}{suffix}"
    for wildcard in file_model.wildcard_imports:
        if wildcard.startswith("java."):
            return f"{wildcard}.{base}{suffix}"
    if file_model.package:
        return f"{file_model.package}.{base}{suffix}"
    return cleaned


def _split_array_suffix(type_name: str) -> Tuple[str, str]:
    suffix = ""
    base = type_name
    while base.endswith("[]"):
        base = base[:-2]
        suffix += "[]"
    return base, suffix


def _erase_type(type_text: str) -> str:
    text = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", str(type_text or ""))
    text = re.sub(r"\b(final|public|protected|private|static|volatile|transient)\b", "", text)
    text = text.replace("...", "[]")
    text = re.sub(r"<.*>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" []", "[]")
    return text


def _superclass_text(source: bytes, class_node: Node) -> str:
    super_node = _first_child(class_node, "superclass")
    if super_node is None:
        return ""
    for child in super_node.children:
        if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
            return _node_text(source, child)
    return ""


def _interface_texts(source: bytes, class_node: Node) -> List[str]:
    container = _first_child(class_node, "super_interfaces") or _first_child(
        class_node, "extends_interfaces"
    )
    if container is None:
        return []
    type_list = _first_child(container, "type_list") or container
    return [
        _node_text(source, child)
        for child in type_list.children
        if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}
    ]


def _parameter_name(source: bytes, node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        declarator = node.child_by_field_name("declarator") or _first_child(node, "variable_declarator")
        name_node = declarator.child_by_field_name("name") if declarator is not None else None
    return _node_text(source, name_node).strip() if name_node is not None else ""


def _parameter_type_text(source: bytes, node: Node) -> str:
    type_node = node.child_by_field_name("type") or _first_type_child(node)
    text = _node_text(source, type_node).strip() if type_node is not None else ""
    if text and node.type == "spread_parameter":
        return f"{text}[]"
    return text


def _type_parameters(
    file_model: JavaFileModel,
    node: Node,
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> Dict[str, str]:
    params_node = _first_child(node, "type_parameters")
    if params_node is None:
        return {}
    out: Dict[str, str] = {}
    for param in params_node.children:
        if param.type != "type_parameter":
            continue
        name_node = _first_type_child(param)
        if name_node is None:
            continue
        name = _node_text(file_model.source, name_node)
        bound = "java.lang.Object"
        bound_node = _first_child(param, "type_bound")
        if bound_node is not None:
            for child in bound_node.children:
                if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                    bound = _resolve_type_name(file_model, _node_text(file_model.source, child), classes_by_simple, out)
                    break
        out[name] = bound
    return out


def _first_type_child(node: Node) -> Optional[Node]:
    for child in node.children:
        if child.type in {
            "type_identifier",
            "scoped_type_identifier",
            "generic_type",
            "integral_type",
            "floating_point_type",
            "boolean_type",
            "void_type",
            "array_type",
        }:
            return child
    return None


def _declared_name(source: bytes, node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.children:
            if child.type == "identifier":
                name_node = child
                break
    return _node_text(source, name_node).strip() if name_node is not None else ""


def _modifiers(source: bytes, node: Node) -> Set[str]:
    modifiers_node = _first_child(node, "modifiers")
    if modifiers_node is None:
        return set()
    return set(re.findall(r"\b(public|protected|private|static|final|abstract|native|synchronized|strictfp)\b", _node_text(source, modifiers_node)))


def _annotations(source: bytes, node: Node) -> Set[str]:
    modifiers_node = _first_child(node, "modifiers")
    if modifiers_node is None:
        return set()
    names = set()
    for match in re.finditer(r"@([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)", _node_text(source, modifiers_node)):
        names.add(match.group(1))
    return names


def _count_super_accesses(source: bytes, body: Node) -> int:
    count = 0
    for node in _iter_nodes(body):
        if node.type in {"field_access", "method_invocation"}:
            text = _node_text(source, node)
            if re.search(r"(?<![\w$])super\s*\.", text):
                count += 1
    return count


def _list_java_files(root: Path, *, include_tests: bool) -> List[Path]:
    files: List[Path] = []
    for path in root.rglob("*.java"):
        if not path.is_file():
            continue
        if _contains_excluded_part(path, DEFAULT_EXCLUDE_PATHS):
            continue
        if not include_tests and _is_test_path(path):
            continue
        files.append(path)
    return sorted(files)


def _contains_excluded_part(path: Path, exclude_paths: Iterable[str]) -> bool:
    excluded = set(exclude_paths)
    return any(part in excluded for part in path.parts)


def _is_test_path(path: Path) -> bool:
    lowered = str(path).lower()
    return "/test/" in lowered or "\\test\\" in lowered or "/tests/" in lowered or "\\tests\\" in lowered


def _is_test_like_rel_path(rel_path: str) -> bool:
    lowered = str(rel_path or "").lower().replace("\\", "/")
    return "/test/" in lowered or "/tests/" in lowered


def _sort_findings(findings: List[SemanticFinding]) -> List[SemanticFinding]:
    return sorted(findings, key=lambda item: (item.file, item.begin_line, item.class_name, item.method))


def _iter_nodes(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _first_child(node: Node, node_type: str) -> Optional[Node]:
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def _node_text(source: bytes, node: Optional[Node]) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_text_from_node(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def _node_start_line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _node_end_line(node: Node) -> int:
    return int(node.end_point[0]) + 1


def _relative_unix(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _qualified_class_name(package: str, owners: List[str], class_name: str) -> str:
    pieces = [piece for piece in [package, ".".join(owners + [class_name])] if piece]
    return ".".join(pieces)


def _package_of(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[0] if "." in qualified_name else ""


def _looks_like_type_name(value: str, model: ProjectModel) -> bool:
    if not value:
        return False
    if value in model.classes_by_simple:
        return True
    return value[:1].isupper()


def _resolve_model_type(model: ProjectModel, type_name: str) -> str:
    if type_name in model.classes:
        return type_name
    simple = type_name.rsplit(".", 1)[-1]
    candidates = model.classes_by_simple.get(simple, [])
    return candidates[0].qualified_name if candidates else type_name


def _all_parent_type_names(model: ProjectModel, child: ClassRecord) -> Set[str]:
    pending = [
        name for name in [child.superclass_name, *child.interface_names] if name
    ]
    parents: Set[str] = set()
    while pending:
        name = pending.pop()
        resolved = _resolve_model_type(model, name)
        if resolved in parents:
            continue
        parents.add(resolved)
        record = model.classes.get(resolved)
        if record is None:
            simple = resolved.rsplit(".", 1)[-1]
            candidates = model.classes_by_simple.get(simple, [])
            record = candidates[0] if candidates else None
        if record is not None:
            pending.extend(
                parent
                for parent in [record.superclass_name, *record.interface_names]
                if parent
            )
    return parents


def _is_subtype(model: ProjectModel, child_name: str, parent_name: str) -> bool:
    current = model.classes.get(child_name)
    seen: Set[str] = set()
    while current and current.superclass_name and current.superclass_name not in seen:
        seen.add(current.superclass_name)
        if current.superclass_name == parent_name:
            return True
        current = model.classes.get(current.superclass_name)
    return False


def _is_primitive(type_name: str) -> bool:
    return _erase_type(type_name) in PRIMITIVE_TYPES


def _root_receiver(text: str) -> str:
    value = str(text or "")
    cast_match = re.match(r"\s*\([^)]+\)\s*([A-Za-z_$][\w$]*)", value)
    if cast_match:
        return cast_match.group(1)
    match = re.match(r"\s*([A-Za-z_$][\w$]*)", value)
    return match.group(1) if match else ""


def _stem_name(name: str) -> str:
    lowered = str(name or "").lower()
    cleaned = re.sub(r"[^a-z0-9_]", "", lowered)
    return cleaned or lowered


def _java_hex_hash(value: str) -> str:
    h = 0
    for char in value:
        h = (31 * h + ord(char)) & 0xFFFFFFFF
    return f"{h:x}"


def _failed(message: str) -> SemanticDetectionResult:
    return SemanticDetectionResult(
        ok=False,
        findings={"feature_envy": [], "refused_bequest": [], "data_clumps": [], "god_class": [], "dead_code": []},
        error=message,
    )
