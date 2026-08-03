"""Tree-sitter-based Java semantic smell detector.

Builds a full project model (classes, methods, fields, type hierarchy,
imports) via tree-sitter and then analyses it for four semantic smells:

* **feature_envy** — methods that disproportionately access foreign data.
* **refused_bequest** — child classes that mostly
  override parent methods with stubs or rejections.
* **data_clumps** — parameter groups that recur across many methods/classes.
* **god_class** — large classes with excessive foreign data access.
* **dead_code** — unused private methods with no project-local call/reference.

This is the versioned Java product detector used by checkpoint capture and
post-edit verification.
"""
from __future__ import annotations

import itertools
import hashlib
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from ..analysis import count_meaningful_lines
from .source_layout import (
    JavaSourceLayoutError,
    discover_java_source_layout,
    standard_test_root,
)
from .detector_utils import (
    erase_java_type,
    normalize_erased_qualified_group as _normalize_qualified_group,
    normalize_method as _normalize_method,
    normalize_path as _normalize_path,
    normalize_rel_path as _normalize_rel_path,
)
from .catalog_identity import (
    split_top_level_java_types,
    stable_java_method_signature,
    stable_method_record_signature,
)
from .syntactic_detector import tokenize_structural_window


JAVA_SYMBOL_RESOLVER_ID = "readonly-main-classpath-symbols-v4"
JAVA_SYMBOL_SNAPSHOT_SCHEMA = 1

# Target-parameter descriptors for the JDK functional interfaces whose
# source-level generic arguments completely determine a bound method
# reference's input types. Integer entries address generic arguments; string
# entries are primitive types. Return types are irrelevant to Java overloads
# because methods cannot differ by return type alone.
JAVA_SAM_PARAMETER_TEMPLATES: Mapping[str, Tuple[int | str, ...]] = {
    "BiConsumer": (0, 1),
    "BiFunction": (0, 1),
    "BinaryOperator": (0, 0),
    "BiPredicate": (0, 1),
    "BooleanSupplier": (),
    "Consumer": (0,),
    "DoubleBinaryOperator": ("double", "double"),
    "DoubleConsumer": ("double",),
    "DoubleFunction": ("double",),
    "DoublePredicate": ("double",),
    "DoubleSupplier": (),
    "DoubleToIntFunction": ("double",),
    "DoubleToLongFunction": ("double",),
    "DoubleUnaryOperator": ("double",),
    "Function": (0,),
    "IntBinaryOperator": ("int", "int"),
    "IntConsumer": ("int",),
    "IntFunction": ("int",),
    "IntPredicate": ("int",),
    "IntSupplier": (),
    "IntToDoubleFunction": ("int",),
    "IntToLongFunction": ("int",),
    "IntUnaryOperator": ("int",),
    "LongBinaryOperator": ("long", "long"),
    "LongConsumer": ("long",),
    "LongFunction": ("long",),
    "LongPredicate": ("long",),
    "LongSupplier": (),
    "LongToDoubleFunction": ("long",),
    "LongToIntFunction": ("long",),
    "LongUnaryOperator": ("long",),
    "ObjDoubleConsumer": (0, "double"),
    "ObjIntConsumer": (0, "int"),
    "ObjLongConsumer": (0, "long"),
    "Predicate": (0,),
    "Supplier": (),
    "ToDoubleBiFunction": (0, 1),
    "ToDoubleFunction": (0,),
    "ToIntBiFunction": (0, 1),
    "ToIntFunction": (0,),
    "ToLongBiFunction": (0, 1),
    "ToLongFunction": (0,),
    "UnaryOperator": (0,),
    # Other widely used JDK single-abstract-method interfaces.
    "Callable": (),
    "Comparator": (0, 0),
    "Runnable": (),
}


DEFAULT_THRESHOLDS = {
    "data_clumps_param_group_size": 3,
    "data_clumps_occurrences": 3,
    "data_clumps_min_classes": 3,
    # Versioned God Class product profile. Capture and verification use this
    # same predicate; dataset metadata never supplies these values at runtime.
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

GOD_CLASS_PROFILE_ID = "java-product/god-class/multi-metric-v1"
GOD_CLASS_RESPONSIBILITY_CLUSTER_SCHEMA = 1
GOD_CLASS_RESPONSIBILITY_CLUSTER_LIMIT = 12
GOD_CLASS_RESPONSIBILITY_MEMBER_LIMIT = 16


def god_class_product_profile(
    metrics: Optional[Mapping[str, Any]] = None,
    *,
    responsibility_clusters: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return the versioned God Class predicate and its current trigger state.

    The detector and checkpoint adapter read the same constants.  This is a
    descriptive view of the product predicate, not an alternate detector.
    """
    values = {
        name: int((metrics or {}).get(name) or 0)
        for name in ("nom", "nof", "wmc", "loc", "atfd")
    }
    mandatory = [
        {
            "name": "nom",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_min_nom"]),
            "value": values["nom"],
            "matched": values["nom"] >= int(DEFAULT_THRESHOLDS["god_class_min_nom"]),
        },
        {
            "name": "wmc",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_min_wmc"]),
            "value": values["wmc"],
            "matched": values["wmc"] >= int(DEFAULT_THRESHOLDS["god_class_min_wmc"]),
        },
    ]
    signals = [
        {
            "name": "nom",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_nom"]),
            "value": values["nom"],
            "matched": values["nom"] >= int(DEFAULT_THRESHOLDS["god_class_nom"]),
        },
        {
            "name": "wmc",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_wmc"]),
            "value": values["wmc"],
            "matched": values["wmc"] >= int(DEFAULT_THRESHOLDS["god_class_wmc"]),
        },
        {
            "name": "loc",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_loc"]),
            "value": values["loc"],
            "matched": values["loc"] >= int(DEFAULT_THRESHOLDS["god_class_loc"]),
        },
        {
            "name": "atfd",
            "operator": ">=",
            "boundary": int(DEFAULT_THRESHOLDS["god_class_atfd"]),
            "value": values["atfd"],
            "matched": values["atfd"] >= int(DEFAULT_THRESHOLDS["god_class_atfd"]),
        },
        {
            "name": "strong_nom_wmc",
            "operator": "nom>= and wmc>=",
            "boundaries": {
                "nom": int(DEFAULT_THRESHOLDS["god_class_strong_nom"]),
                "wmc": int(DEFAULT_THRESHOLDS["god_class_strong_wmc"]),
            },
            "values": {"nom": values["nom"], "wmc": values["wmc"]},
            "matched": (
                values["nom"] >= int(DEFAULT_THRESHOLDS["god_class_strong_nom"])
                and values["wmc"] >= int(DEFAULT_THRESHOLDS["god_class_strong_wmc"])
            ),
        },
    ]
    triggered = [str(item["name"]) for item in signals if item["matched"]]
    profile = {
        "id": GOD_CLASS_PROFILE_ID,
        "mandatory": mandatory,
        "signals": signals,
        "min_signals": int(DEFAULT_THRESHOLDS["god_class_min_signals"]),
        "triggered_signals": triggered,
        "finding_present": bool(
            all(item["matched"] for item in mandatory)
            and len(triggered) >= int(DEFAULT_THRESHOLDS["god_class_min_signals"])
        ),
    }
    if responsibility_clusters is not None:
        profile["responsibility_cluster_schema"] = GOD_CLASS_RESPONSIBILITY_CLUSTER_SCHEMA
        profile["responsibility_clusters"] = [
            dict(item) for item in responsibility_clusters
        ]
    return profile

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

DATA_CLUMP_INLINE_WINDOW_TOKENS = 24
DATA_CLUMP_INLINE_WINDOWS_PER_METHOD = 4

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
AMBIGUOUS_TYPE_PREFIX = "__ambiguous_java_type__"
METHOD_NODE_TYPES = {
    "method_declaration",
    "constructor_declaration",
    "compact_constructor_declaration",
}
CLASS_NODE_TYPES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
}
ENUM_MEMBER_CONTAINER_TYPE = "enum_body_declarations"
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
    attributes: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticDetectionResult:
    ok: bool
    findings: Dict[str, List[SemanticFinding]]
    error: str = ""
    project_model: Optional["ProjectModel"] = None
    unavailable: Optional[Dict[str, object]] = None


class JavaSymbolClasspathError(RuntimeError):
    """An explicitly supplied symbol archive cannot be inspected safely."""

    def __init__(self, reason: str, message: str, *, path: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.path = path

    def to_unavailable(self) -> Dict[str, object]:
        return {
            "status": "DETECTOR_UNAVAILABLE",
            "component": "java_external_symbols",
            "reason": self.reason,
            "message": self.message,
            "details": {"path": self.path},
        }


@dataclass
class JavaFileModel:
    path: Path
    rel_path: str
    source: bytes
    root: Node
    package: str = ""
    imports: Dict[str, str] = field(default_factory=dict)
    wildcard_imports: List[str] = field(default_factory=list)
    static_wildcard_imports: List[str] = field(default_factory=list)


@dataclass
class ClassRecord:
    file: str
    class_name: str
    qualified_name: str
    begin_line: int
    end_line: int
    kind: str
    source_superclass_name: str = ""
    source_interface_names: List[str] = field(default_factory=list)
    superclass_name: str = ""
    interface_names: List[str] = field(default_factory=list)
    modifiers: Set[str] = field(default_factory=set)
    type_parameters: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)
    field_modifiers: Dict[str, Set[str]] = field(default_factory=dict)
    record_components: Dict[str, str] = field(default_factory=dict)
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
    is_varargs: bool = False


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


@dataclass(frozen=True)
class FeatureEnvyProfile:
    envied_field: str = ""
    envied_type: str = ""
    envy_access_count: int = 0
    self_access_count: int = 0
    envy_access_diff: int = 0
    direct_field_count: int = 0
    field_member_count: int = 0
    fields_without_member_access: int = 0
    same_class_method_calls: int = 0


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
    classpath: str = "",
    timeout_seconds: int = 300,
) -> SemanticDetectionResult:
    del timeout_seconds
    try:
        model = _build_project_model(project_root, classpath=classpath)
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
        return SemanticDetectionResult(
            ok=True,
            findings=findings,
            project_model=model,
        )
    except JavaSourceLayoutError as exc:
        return _failed("DETECTOR_UNAVAILABLE", unavailable=exc.to_unavailable())
    except JavaSymbolClasspathError as exc:
        return _failed("DETECTOR_UNAVAILABLE", unavailable=exc.to_unavailable())
    except Exception as exc:
        return _failed(f"Python semantic detector failed: {exc}")


def analyze_data_clump_type_continuity(
    project_root: Path,
    *,
    group: str,
    baseline_occurrences: Sequence[Mapping[str, Any]],
    project_model: ProjectModel,
) -> Dict[str, Any]:
    """Track baseline declarations and business-body dispersion.

    The product finding uses normalized ``type:name`` groups.  A repair must not
    make that finding disappear by renaming the parameters on the same baseline
    declarations or by copying their business bodies into callers.  This profile
    reuses the semantic project model and frozen product-detector occurrences; it
    does not discover a second dataset-defined finding.
    """
    try:
        model = project_model
        normalized_group = _normalize_qualified_group(group)
        group_types = Counter(
            token.rsplit(":", 1)[0]
            for token in normalized_group.split("|")
            if ":" in token
        )
        if not group_types:
            return {
                "ok": False,
                "error": "data_clump_type_group_unavailable",
                "occurrence_count": 0,
                "occurrences": [],
            }

        contract_occurrences = [
            dict(item) for item in baseline_occurrences if isinstance(item, Mapping)
        ]
        if not all(
            isinstance(item.get("group_parameter_positions"), list)
            for item in contract_occurrences
        ):
            position_error = _freeze_data_clump_parameter_positions(
                contract_occurrences,
                model=model,
                normalized_group=normalized_group,
            )
            if position_error:
                return {
                    "ok": False,
                    "error": position_error,
                    "occurrence_count": 0,
                    "occurrences": [],
                    "occurrence_contract": contract_occurrences,
                    "inline_copy_contract_available": False,
                    "inline_copy_expansions": [],
                }
        methods_by_owner: Dict[Tuple[str, str, str], List[MethodRecord]] = {}
        for method in model.methods:
            key = (
                _normalize_path(method.file),
                str(method.class_name or "").lower(),
                _normalize_method(method.method_name),
            )
            methods_by_owner.setdefault(key, []).append(method)
        survivors: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str, str]] = set()
        for occurrence in contract_occurrences:
            owner_key = (
                _normalize_path(str(occurrence.get("file") or "")),
                str(occurrence.get("class") or occurrence.get("class_name") or "").lower(),
                _normalize_method(str(occurrence.get("method_name") or occurrence.get("method") or "")),
            )
            positions = occurrence.get("group_parameter_positions")
            if not isinstance(positions, list) or not positions:
                continue
            for method in methods_by_owner.get(owner_key, []):
                if not _method_retains_data_clump_positions(method, positions):
                    continue
                identity = (method.file, method.class_name, method.method_signature)
                if identity in seen:
                    continue
                seen.add(identity)
                survivors.append({
                    "file": method.file,
                    "class": method.class_name,
                    "method_name": method.method_name,
                    "method": method.method_signature,
                    "begin_line": method.begin_line,
                })
        survivors.sort(key=lambda item: (
            str(item.get("file") or ""),
            int(item.get("begin_line") or 0),
            str(item.get("method") or ""),
        ))
        method_streams = _data_clump_method_token_streams(model)
        if not any(
            isinstance(item.get("unique_body_windows"), list)
            for item in contract_occurrences
        ):
            _freeze_data_clump_body_windows(
                contract_occurrences,
                method_streams=method_streams,
                group_types=group_types,
            )
        inline_expansions = _expanded_data_clump_body_windows(
            contract_occurrences,
            method_streams=method_streams,
        )
        return {
            "ok": True,
            "group_types": dict(sorted(group_types.items())),
            "occurrence_count": len(survivors),
            "occurrences": survivors,
            "occurrence_contract": contract_occurrences,
            "inline_copy_contract_available": any(
                bool(item.get("unique_body_windows"))
                for item in contract_occurrences
            ),
            "inline_copy_expansions": inline_expansions,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"data_clump_type_continuity_failed: {exc}",
            "occurrence_count": 0,
            "occurrences": [],
            "occurrence_contract": [],
            "inline_copy_contract_available": False,
            "inline_copy_expansions": [],
        }


def _freeze_data_clump_parameter_positions(
    contract_occurrences: List[Dict[str, Any]],
    *,
    model: ProjectModel,
    normalized_group: str,
) -> str:
    """Pin the detector group's baseline parameter slots on each declaration."""
    methods_by_identity = {
        (
            _normalize_path(method.file),
            str(method.class_name or "").lower(),
            str(method.method_signature or "").strip().lower(),
        ): method
        for method in model.methods
    }
    group_members = [item for item in normalized_group.split("|") if item]
    if not group_members:
        return "data_clump_group_members_unavailable"
    for occurrence in contract_occurrences:
        identity = (
            _normalize_path(str(occurrence.get("file") or "")),
            str(occurrence.get("class") or occurrence.get("class_name") or "").lower(),
            str(occurrence.get("method") or "").strip().lower(),
        )
        method = methods_by_identity.get(identity)
        if method is None:
            return (
                "data_clump_baseline_declaration_unavailable:"
                f"{occurrence.get('file', '')}#{occurrence.get('method', '')}"
            )
        descriptors = [_normalize_qualified_group(item) for item in method.parameter_descriptors]
        unused = set(range(len(descriptors)))
        positions: List[Dict[str, Any]] = []
        for member in group_members:
            index = next(
                (candidate for candidate in sorted(unused) if descriptors[candidate] == member),
                None,
            )
            if index is None:
                return (
                    "data_clump_baseline_group_position_unavailable:"
                    f"{method.file}#{method.method_signature}:{member}"
                )
            unused.remove(index)
            type_name = member.rsplit(":", 1)[0]
            positions.append({
                "index": index,
                "type": type_name,
                "descriptor": member,
            })
        occurrence["baseline_parameter_count"] = len(method.parameter_types)
        occurrence["group_parameter_positions"] = sorted(
            positions,
            key=lambda item: int(item["index"]),
        )
    return ""


def _method_retains_data_clump_positions(
    method: MethodRecord,
    positions: Sequence[Mapping[str, Any]],
) -> bool:
    for item in positions:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(method.parameter_types):
            return False
        current_type = _normalize_qualified_group(
            f"{method.parameter_types[index]}:value"
        ).rsplit(":", 1)[0]
        if current_type != str(item.get("type") or ""):
            return False
    return True


def _data_clump_group_positions(
    method: MethodRecord,
    group_key: str,
) -> List[Dict[str, Any]]:
    """Return the current parameter slots occupied by one detector group."""
    members = [item for item in _normalize_qualified_group(group_key).split("|") if item]
    descriptors = [_normalize_qualified_group(item) for item in method.parameter_descriptors]
    unused = set(range(len(descriptors)))
    positions: List[Dict[str, Any]] = []
    for member in members:
        index = next(
            (candidate for candidate in sorted(unused) if descriptors[candidate] == member),
            None,
        )
        if index is None:
            return []
        unused.remove(index)
        positions.append({
            "index": index,
            "type": member.rsplit(":", 1)[0],
            "descriptor": member,
        })
    return sorted(positions, key=lambda item: int(item["index"]))


def _data_clump_method_token_streams(
    model: ProjectModel,
) -> List[Tuple[MethodRecord, List[str], str]]:
    streams: List[Tuple[MethodRecord, List[str], str]] = []
    separator = "\x1f"
    for method in model.methods:
        tokens = tokenize_structural_window(method.body_text)
        if len(tokens) < DATA_CLUMP_INLINE_WINDOW_TOKENS:
            continue
        encoded = separator + separator.join(tokens) + separator
        streams.append((method, tokens, encoded))
    return streams


def _freeze_data_clump_body_windows(
    contract_occurrences: List[Dict[str, Any]],
    *,
    method_streams: Sequence[Tuple[MethodRecord, List[str], str]],
    group_types: Counter[str],
) -> None:
    """Freeze source-unique windows from detector-owned baseline methods.

    A unique window may move once during a legitimate refactoring.  It becomes
    a regression only when the same frozen business logic is dispersed to more
    locations than it occupied at baseline.
    """
    streams_by_identity = {
        (
            _normalize_path(method.file),
            str(method.class_name or "").lower(),
            str(method.method_signature or "").strip().lower(),
        ): (method, tokens)
        for method, tokens, _ in method_streams
        if _method_contains_data_clump_types(method, group_types)
    }
    for occurrence in contract_occurrences:
        identity = (
            _normalize_path(str(occurrence.get("file") or "")),
            str(occurrence.get("class") or occurrence.get("class_name") or "").lower(),
            str(occurrence.get("method") or "").strip().lower(),
        )
        source = streams_by_identity.get(identity)
        if source is None:
            occurrence["unique_body_windows"] = []
            continue
        source_method, source_tokens = source
        frozen: List[Dict[str, Any]] = []
        for start in _sample_data_clump_window_starts(len(source_tokens)):
            window = source_tokens[
                start:start + DATA_CLUMP_INLINE_WINDOW_TOKENS
            ]
            baseline_count, _ = _count_data_clump_window(window, method_streams)
            if baseline_count != 1:
                continue
            frozen.append({
                "window_id": _java_hex_hash("\x1f".join(window)),
                "tokens": window,
                "baseline_occurrences": baseline_count,
                "source_file": source_method.file,
                "source_class": source_method.class_name,
                "source_method": source_method.method_signature,
            })
            if len(frozen) >= DATA_CLUMP_INLINE_WINDOWS_PER_METHOD:
                break
        occurrence["unique_body_windows"] = frozen


def _method_contains_data_clump_types(
    method: MethodRecord,
    group_types: Counter[str],
) -> bool:
    current_types = Counter(
        _normalize_qualified_group(f"{type_name}:value").rsplit(":", 1)[0]
        for type_name in method.parameter_types
    )
    return all(
        current_types[type_name] >= count
        for type_name, count in group_types.items()
    )


def _sample_data_clump_window_starts(token_count: int) -> List[int]:
    maximum = token_count - DATA_CLUMP_INLINE_WINDOW_TOKENS
    if maximum < 0:
        return []
    if maximum == 0:
        return [0]
    # Try more positions than we retain because common validation or delegation
    # syntax may not be globally unique even when the business slice is.
    slots = min(12, maximum + 1)
    return sorted({round(index * maximum / (slots - 1)) for index in range(slots)})


def _count_data_clump_window(
    window: Sequence[str],
    method_streams: Sequence[Tuple[MethodRecord, List[str], str]],
) -> Tuple[int, List[str]]:
    separator = "\x1f"
    needle = separator + separator.join(window) + separator
    total = 0
    locations: List[str] = []
    for method, _, encoded in method_streams:
        count = encoded.count(needle)
        if count <= 0:
            continue
        total += count
        label = f"{method.file}#{method.method_signature}"
        locations.extend([label] * min(count, 3))
    return total, locations[:8]


def _expanded_data_clump_body_windows(
    contract_occurrences: Sequence[Mapping[str, Any]],
    *,
    method_streams: Sequence[Tuple[MethodRecord, List[str], str]],
) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for occurrence in contract_occurrences:
        windows = occurrence.get("unique_body_windows")
        if not isinstance(windows, list):
            continue
        for item in windows:
            if not isinstance(item, Mapping):
                continue
            tokens = item.get("tokens")
            if not isinstance(tokens, list) or not all(
                isinstance(token, str) for token in tokens
            ):
                continue
            window_id = str(item.get("window_id") or "")
            if not window_id or window_id in seen:
                continue
            seen.add(window_id)
            baseline_count = int(item.get("baseline_occurrences") or 0)
            current_count, locations = _count_data_clump_window(tokens, method_streams)
            if current_count <= baseline_count:
                continue
            expanded.append({
                "window_id": window_id,
                "source_file": str(item.get("source_file") or occurrence.get("file") or ""),
                "source_method": str(item.get("source_method") or occurrence.get("method") or ""),
                "baseline_occurrences": baseline_count,
                "current_occurrences": current_count,
                "current_locations": locations,
            })
    return expanded[:20]


def analyze_feature_envy_target(
    project_root: Path,
    *,
    target_file: Path,
    method: Optional[str] = None,
    line: Optional[int] = None,
    expected_receiver_type: str = "",
    project_model: ProjectModel,
) -> Dict[str, Any]:
    """Return threshold-independent Feature Envy metrics for one method.

    The caller must reuse the model returned by ``run_java_semantic_detector``;
    closure evaluation never rescans the project or uses dataset evidence.
    """
    root = project_root.expanduser().resolve()
    model = project_model
    target_rel = _normalize_rel_path(target_file, root)
    raw_method = str(method or "").strip()
    target_method = _normalize_method(raw_method)
    candidates = [item for item in model.methods if _normalize_path(item.file) == target_rel]
    if raw_method and "(" in raw_method:
        stable_target = stable_java_method_signature(
            raw_method,
            preserve_source_qualification=True,
        )
        candidates = [
            item for item in candidates
            if stable_method_record_signature(item) == stable_target
        ]
    elif target_method:
        candidates = [item for item in candidates if _normalize_method(item.method_name) == target_method]
    elif line is not None:
        candidates = [item for item in candidates if item.begin_line <= line <= item.end_line]
    if not candidates:
        return {
            "ok": True,
            "file": target_rel,
            "method": raw_method,
            "line": line,
            "target_missing": True,
            "expected_receiver_type": expected_receiver_type,
            "expected_receiver_access": 0,
            "dominant_receiver_type": "",
            "dominant_receiver_access": 0,
            "envied_field": "",
            "envied_type": "",
            "envy_access_count": 0,
            "self_access_count": 0,
            "envy_access_excess": 0,
            "direct_field_count": 0,
            "field_member_count": 0,
            "fields_without_member_access": 0,
            "same_class_method_calls": 0,
            "receiver_access_worklist": [],
            "strict_detector_hit": False,
        }
    target = min(candidates, key=lambda item: (item.end_line - item.begin_line, item.begin_line))
    profiles = _designite_feature_envy_profiles(model, target)
    expected_simple = _erase_type(expected_receiver_type).rsplit(".", 1)[-1].strip()
    profile = next(
        (
            item
            for item in profiles
            if not expected_simple
            or _erase_type(item.envied_type).rsplit(".", 1)[-1] == expected_simple
        ),
        FeatureEnvyProfile(),
    )
    receiver_matches = (
        not expected_simple
        or _erase_type(profile.envied_type).rsplit(".", 1)[-1] == expected_simple
    )
    strict_hit = profile.envy_access_diff > 1 and receiver_matches
    receiver_access_worklist = _feature_envy_receiver_access_worklist(
        model,
        target,
        envied_field=profile.envied_field if receiver_matches else "",
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
        "target_missing": False,
        "expected_receiver_type": expected_receiver_type,
        "expected_receiver_access": profile.envy_access_count if receiver_matches else 0,
        "dominant_receiver_type": profile.envied_type,
        "dominant_receiver_access": profile.envy_access_count,
        "envied_field": profile.envied_field,
        "envied_type": profile.envied_type,
        "envy_access_count": profile.envy_access_count,
        "self_access_count": profile.self_access_count,
        "envy_access_excess": profile.envy_access_diff,
        "direct_field_count": profile.direct_field_count,
        "field_member_count": profile.field_member_count,
        "fields_without_member_access": profile.fields_without_member_access,
        "same_class_method_calls": profile.same_class_method_calls,
        "receiver_access_worklist": receiver_access_worklist,
        "strict_detector_hit": strict_hit,
    }


def _feature_envy_receiver_access_worklist(
    model: ProjectModel,
    method: MethodRecord,
    *,
    envied_field: str,
) -> List[Dict[str, Any]]:
    """Describe the exact member accesses counted for one detector finding."""
    if method.body is None or not envied_field:
        return []
    owner = model.classes.get(method.owner_qualified_name)
    if owner is None:
        return []
    owner = _feature_envy_owner_view(model, owner)
    if envied_field not in owner.fields:
        return []
    shadowed_names = set(method.parameters).union(method.local_variables)
    aliases = _stable_owner_field_aliases(method, owner)
    receiver_type = _normalized_receiver_type(model, owner.fields[envied_field])
    worklist: List[Dict[str, Any]] = []
    for node, receiver_node in _member_access_receiver_nodes(method.body):
        receiver = _node_text_from_node(receiver_node).strip()
        field_name = _owner_field_for_receiver_expression(
            receiver,
            owner,
            shadowed_names=shadowed_names,
            aliases=aliases,
        )
        if field_name != envied_field:
            continue
        name_node = node.child_by_field_name("name") or node.child_by_field_name("field")
        member = _node_text_from_node(name_node).strip() if name_node is not None else ""
        worklist.append(
            {
                "file": method.file,
                "class": method.owner_qualified_name,
                "method": method.method_signature,
                "line": _node_start_line(node),
                "expression": _node_text_from_node(node).strip(),
                "receiver": receiver,
                "field": envied_field,
                "receiver_type": receiver_type,
                "member": member,
                "access_kind": node.type,
            }
        )
    return worklist


def _designite_feature_envy_profile(
    model: ProjectModel,
    method: MethodRecord,
) -> FeatureEnvyProfile:
    profiles = _designite_feature_envy_profiles(model, method)
    return profiles[0] if profiles else FeatureEnvyProfile()


def _designite_feature_envy_profiles(
    model: ProjectModel,
    method: MethodRecord,
) -> List[FeatureEnvyProfile]:
    """Mirror Designite 2.8.6's ordinary-method Feature Envy metric."""
    if method.is_constructor or method.body is None or "abstract" in method.modifiers:
        return []
    owner = model.classes.get(method.owner_qualified_name)
    if owner is None:
        return []
    owner = _feature_envy_owner_view(model, owner)

    shadowed_names = set(method.parameters).union(method.local_variables)
    aliases = _stable_owner_field_aliases(method, owner)
    direct_fields = _feature_envy_direct_owner_fields(
        method,
        owner,
        shadowed_names=shadowed_names,
    )
    direct_fields.update(aliases.values())

    member_counts: Dict[str, int] = {}
    for expression in _member_access_receiver_expressions(method.body):
        field_name = _owner_field_for_receiver_expression(
            expression,
            owner,
            shadowed_names=shadowed_names,
            aliases=aliases,
        )
        if field_name:
            direct_fields.add(field_name)
            member_counts[field_name] = member_counts.get(field_name, 0) + 1

    if not direct_fields:
        return []

    fields_without_members = len(direct_fields.difference(member_counts))
    same_class_calls = len(
        set(_feature_envy_self_method_invocation_names(
            method.body,
            _owner_method_return_types(owner),
        ))
    )
    total_field_members = sum(member_counts.values())
    profiles: List[FeatureEnvyProfile] = []
    for field_name in sorted(direct_fields):
        field_type = owner.fields.get(field_name, "")
        if _model_type_is_ambiguous(model, field_type):
            continue
        if _is_primitive(field_type):
            continue
        if _resolve_model_type(model, field_type) == _resolve_model_type(model, method.owner_qualified_name):
            continue
        envy_count = member_counts.get(field_name, 0)
        # Designite's metric counts foreign member accesses, but represents
        # self interest by distinct directly used fields and owner methods.
        self_count = fields_without_members + same_class_calls
        difference = envy_count - self_count
        if difference > 1:
            profiles.append(FeatureEnvyProfile(
                envied_field=field_name,
                envied_type=_normalized_receiver_type(model, field_type),
                envy_access_count=envy_count,
                self_access_count=self_count,
                envy_access_diff=difference,
                direct_field_count=len(direct_fields),
                field_member_count=total_field_members,
                fields_without_member_access=fields_without_members,
                same_class_method_calls=same_class_calls,
            ))
    return sorted(
        profiles,
        key=lambda item: (-item.envy_access_diff, -item.envy_access_count, item.envied_field),
    )


def _feature_envy_owner_view(
    model: ProjectModel,
    owner: ClassRecord,
) -> ClassRecord:
    """Return the owner with fields inherited from already-loaded ancestors.

    Scope construction decides which exact source ancestors are available.
    This projection never discovers another file: it follows only uniquely
    resolved ``ClassRecord`` relations already present in ``model``.  Declared
    child fields hide every ancestor declaration with the same name.
    """
    inherited = _feature_envy_inherited_fields(model, owner)
    if not inherited:
        return owner
    return replace(owner, fields={**inherited, **owner.fields})


def _feature_envy_inherited_fields(
    model: ProjectModel,
    owner: ClassRecord,
) -> Dict[str, str]:
    inherited: Dict[str, str] = {}
    hidden_names = set(owner.fields)
    interface_roots: List[Tuple[str, int]] = [
        (name, 1) for name in owner.interface_names if name
    ]

    # Java class fields take precedence over interface fields. Walk the single
    # superclass chain nearest-first, retaining inaccessible declarations as
    # name blockers so a farther declaration cannot leak through it.
    seen_classes = {owner.qualified_name}
    parent_name = owner.superclass_name
    class_depth = 1
    while parent_name:
        parent = _class_record_for_type(model, parent_name)
        if parent is None or parent.qualified_name in seen_classes:
            break
        seen_classes.add(parent.qualified_name)
        for field_name, field_type in parent.fields.items():
            if field_name in hidden_names:
                continue
            hidden_names.add(field_name)
            if _field_is_inheritable(model, owner, parent, field_name):
                inherited[field_name] = field_type
        interface_roots.extend(
            (name, class_depth + 1)
            for name in parent.interface_names
            if name
        )
        parent_name = parent.superclass_name
        class_depth += 1

    # Interface diamonds can expose the same declaration through several
    # paths. Deduplicate that case, while excluding genuinely competing field
    # declarations at the same nearest depth (an unqualified Java use would be
    # ambiguous and must not become a detector input).
    candidates: Dict[str, Tuple[int, Dict[str, str]]] = {}
    pending = list(interface_roots)
    seen_interfaces: Set[str] = set()
    while pending:
        type_name, depth = pending.pop(0)
        interface = _class_record_for_type(model, type_name)
        if interface is None or interface.qualified_name in seen_interfaces:
            continue
        seen_interfaces.add(interface.qualified_name)
        for field_name, field_type in interface.fields.items():
            if field_name in hidden_names:
                continue
            if not _field_is_inheritable(model, owner, interface, field_name):
                continue
            current = candidates.get(field_name)
            declaration = {interface.qualified_name: field_type}
            if current is None or depth < current[0]:
                candidates[field_name] = (depth, declaration)
            elif depth == current[0]:
                current[1].update(declaration)
        pending.extend(
            (name, depth + 1)
            for name in interface.interface_names
            if name
        )

    for field_name, (_, declarations) in candidates.items():
        if len(declarations) == 1:
            inherited[field_name] = next(iter(declarations.values()))
    return inherited


def _field_is_inheritable(
    model: ProjectModel,
    descendant: ClassRecord,
    declaring: ClassRecord,
    field_name: str,
) -> bool:
    modifiers = declaring.field_modifiers.get(field_name, set())
    if "private" in modifiers:
        return False
    if "public" in modifiers or "protected" in modifiers:
        return True
    return _class_source_package(model, descendant) == _class_source_package(
        model,
        declaring,
    )


def _class_source_package(model: ProjectModel, record: ClassRecord) -> str:
    for file_model in model.files:
        if file_model.rel_path == record.file:
            return file_model.package
    return _package_of(record.qualified_name)


def _feature_envy_direct_owner_fields(
    method: MethodRecord,
    owner: ClassRecord,
    *,
    shadowed_names: Set[str],
) -> Set[str]:
    """Return owner fields referenced as fields, not same-spelled selectors."""
    if method.body is None:
        return set()
    direct: Set[str] = set()
    selector_parents = {
        "field_access",
        "method_invocation",
        "method_reference",
    }
    declaration_parents = {
        "catch_formal_parameter",
        "enhanced_for_statement",
        "formal_parameter",
        "inferred_parameters",
        "lambda_expression",
        "spread_parameter",
        "variable_declarator",
    }
    for node in _iter_nodes(method.body):
        if node.type != "identifier":
            continue
        name = _node_text_from_node(node).strip()
        if name not in owner.fields or name in shadowed_names:
            continue
        parent = node.parent
        if parent is not None:
            if parent.type in declaration_parents:
                continue
            if parent.type in selector_parents:
                selector = (
                    parent.child_by_field_name("name")
                    or parent.child_by_field_name("field")
                )
                if selector is not None and _same_source_node(selector, node):
                    continue
        direct.add(name)

    # Explicit self access is a selector syntactically, but still denotes an
    # owner field. Foreign member selectors are deliberately excluded above.
    for node in _iter_nodes(method.body):
        if node.type != "field_access":
            continue
        receiver = node.child_by_field_name("object")
        field_node = (
            node.child_by_field_name("field")
            or node.child_by_field_name("name")
        )
        if receiver is None or field_node is None:
            continue
        receiver_text = _node_text_from_node(receiver).strip()
        field_name = _node_text_from_node(field_node).strip()
        if field_name not in owner.fields or field_name in shadowed_names:
            continue
        if receiver_text == "this" or receiver_text in {
            owner.class_name,
            owner.qualified_name,
            f"{owner.class_name}.this",
            f"{owner.qualified_name}.this",
        }:
            direct.add(field_name)
    return direct


def _same_source_node(left: Node, right: Node) -> bool:
    return (
        left.type == right.type
        and left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
    )


def _stable_owner_field_aliases(
    method: MethodRecord,
    owner: ClassRecord,
) -> Dict[str, str]:
    """Return local aliases with one stable assignment from an owner field.

    This is deliberately provenance-based rather than name-based. A local
    assigned again is not treated as an alias, and only a direct field
    reference or another stable alias can establish provenance.
    """
    if method.body is None:
        return {}
    writes: Dict[str, List[str]] = {}
    for node in _iter_nodes(method.body):
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if name_node is not None and value_node is not None:
                name = _node_text_from_node(name_node).strip()
                if name:
                    writes.setdefault(name, []).append(_node_text_from_node(value_node).strip())
        elif node.type == "assignment_expression":
            left_node = node.child_by_field_name("left")
            right_node = node.child_by_field_name("right")
            if left_node is None or right_node is None or left_node.type != "identifier":
                continue
            name = _node_text_from_node(left_node).strip()
            if name:
                writes.setdefault(name, []).append(_node_text_from_node(right_node).strip())

    aliases: Dict[str, str] = {}
    unresolved = dict(writes)
    shadowed_fields = set(method.parameters).union(method.local_variables)
    while unresolved:
        progressed = False
        for name, expressions in list(unresolved.items()):
            source = ""
            valid = True
            for expression in expressions:
                candidate = _direct_owner_field_reference(
                    expression,
                    owner,
                    shadowed_fields=shadowed_fields,
                )
                root = _root_receiver(expression)
                if not candidate:
                    candidate = aliases.get(root, "")
                if not candidate and source and root == name:
                    # Preserve provenance while walking an object chain, for
                    # example ``scope = scope.parent`` in a loop update.
                    candidate = source
                if not candidate or (source and candidate != source):
                    valid = False
                    break
                source = candidate
            if not valid or not source:
                continue
            aliases[name] = source
            del unresolved[name]
            progressed = True
        if not progressed:
            break
    return aliases


def _feature_envy_self_method_invocation_names(
    body: Node,
    owner_method_returns: Mapping[str, str],
) -> Iterable[str]:
    """Yield equivalent unqualified and ``this.`` calls to owner methods."""
    if not owner_method_returns:
        return
    for node in _iter_nodes(body):
        if node.type != "method_invocation" or _is_receiver_operand(node):
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text_from_node(name_node).strip()
        if name not in owner_method_returns:
            continue
        receiver = node.child_by_field_name("object")
        if receiver is None or _node_text_from_node(receiver).strip() == "this":
            yield name


def _direct_owner_field_reference(
    expression: str,
    owner: ClassRecord,
    *,
    shadowed_fields: Set[str],
) -> str:
    value = str(expression or "").strip()
    explicit = re.fullmatch(r"(?:[A-Za-z_$][\w$]*\.)?this\s*\.\s*([A-Za-z_$][\w$]*)", value)
    if explicit and explicit.group(1) in owner.fields:
        return explicit.group(1)
    if re.fullmatch(r"[A-Za-z_$][\w$]*", value) and value in owner.fields and value not in shadowed_fields:
        return value
    return ""


def _owner_field_for_receiver_expression(
    expression: str,
    owner: ClassRecord,
    *,
    shadowed_names: Set[str],
    aliases: Mapping[str, str],
) -> str:
    root = _root_receiver(expression)
    if root in aliases:
        return aliases[root]
    if root in owner.fields and root not in shadowed_names:
        return root
    if root in {"this", "super"}:
        member = _root_receiver(expression.split(".", 1)[1] if "." in expression else "")
        if member in owner.fields:
            return member
    return ""


def build_long_parameter_list_migration_closure(
    project_root: Path,
    *,
    target_file: Path,
    method: str,
    parameter_types: Sequence[str],
    target_class_name: str = "",
    target_line: Optional[int] = None,
    project_model: ProjectModel,
) -> Dict[str, Any]:
    """Build the source-derived migration closure for one long signature.

    This is a projection of the product detector's existing semantic model.
    It resolves the frozen declaration, its override family, constructor-chain
    uses, production calls, and method references.  A potentially relevant
    use that cannot be bound uniquely makes ``analysis_ok`` false; no
    name-only fallback is used.
    """
    try:
        root = project_root.expanduser().resolve()
        model = project_model
        target_rel = _normalize_rel_path(target_file, root)
        target_name = _normalize_method(method)
        frozen_types = tuple(_lpl_type_identity(item) for item in parameter_types)
        declarations = [*model.methods, *_bodyless_method_records(model)]
        candidates = [
            item
            for item in declarations
            if _normalize_path(item.file) == target_rel
            and _normalize_method(item.method_name) == target_name
            and (
                not frozen_types
                or tuple(_lpl_type_identity(value) for value in item.parameter_types)
                == frozen_types
            )
            and (
                not target_class_name
                or _simple_type_name(item.owner_qualified_name)
                == _simple_type_name(target_class_name)
            )
            and (
                target_class_name
                or target_line is None
                or item.begin_line == int(target_line)
            )
        ]
        if len(candidates) != 1:
            return {
                "analysis_ok": False,
                "error": (
                    "target_declaration_not_found"
                    if not candidates
                    else "target_declaration_ambiguous"
                ),
                "target_candidate_count": len(candidates),
                "target": {
                    "file": target_rel,
                    "class": target_class_name,
                    "method": method,
                    "parameter_types": list(parameter_types),
                    "line": target_line,
                },
                "declarations": [],
                "constructor_chain": [],
                "production_call_sites": [],
                "method_references": [],
                "unresolved_sites": [],
            }
        target = candidates[0]
        target_owner = model.classes.get(target.owner_qualified_name)
        if target_owner is None:
            return {
                "analysis_ok": False,
                "error": "target_owner_not_resolved",
                "target_candidate_count": 1,
                "target": _lpl_method_payload(target, relationship="target"),
                "declarations": [],
                "constructor_chain": [],
                "production_call_sites": [],
                "method_references": [],
                "unresolved_sites": [],
            }
        if any(_model_type_is_ambiguous(model, item) for item in target.parameter_types):
            return {
                "analysis_ok": False,
                "error": "target_parameter_type_ambiguous",
                "target_candidate_count": 1,
                "target": _lpl_method_payload(target, relationship="target"),
                "declarations": [],
                "constructor_chain": [],
                "production_call_sites": [],
                "method_references": [],
                "unresolved_sites": [],
            }

        ancestor_names = _all_parent_type_names(model, target_owner)
        descendant_names = {
            item.qualified_name
            for item in model.classes.values()
            if target_owner.qualified_name in _all_parent_type_names(model, item)
        }
        family_owner_names = {
            target_owner.qualified_name,
            *ancestor_names,
            *descendant_names,
        }
        if target.is_constructor:
            family = [target]
        else:
            family = [
                item
                for item in declarations
                if item.owner_qualified_name in family_owner_names
                and not item.is_constructor
                and _normalize_method(item.method_name) == target_name
                and tuple(_lpl_resolved_type_identity(value) for value in item.parameter_types)
                == tuple(_lpl_resolved_type_identity(value) for value in target.parameter_types)
            ]
        family.sort(key=lambda item: (item.file, item.begin_line, item.owner_qualified_name))
        family_keys = {_lpl_method_key(item) for item in family}
        declaration_payloads = [
            _lpl_method_payload(
                item,
                relationship=(
                    "target"
                    if _lpl_method_key(item) == _lpl_method_key(target)
                    else "ancestor_contract"
                    if item.owner_qualified_name in ancestor_names
                    else "descendant_override"
                ),
            )
            for item in family
        ]

        candidates_by_owner_name: Dict[Tuple[str, str], List[MethodRecord]] = {}
        for item in declarations:
            candidates_by_owner_name.setdefault(
                (item.owner_qualified_name, item.method_name), []
            ).append(item)
        methods_by_file: Dict[str, List[MethodRecord]] = {}
        classes_by_file: Dict[str, List[ClassRecord]] = {}
        for item in model.methods:
            methods_by_file.setdefault(item.file, []).append(item)
        for item in model.classes.values():
            if item.file != "<classpath>":
                classes_by_file.setdefault(item.file, []).append(item)
        for items in methods_by_file.values():
            items.sort(key=lambda value: (value.end_line - value.begin_line, value.begin_line))
        for items in classes_by_file.values():
            items.sort(key=lambda value: (value.end_line - value.begin_line, value.begin_line))

        production_calls: List[Dict[str, Any]] = []
        method_references: List[Dict[str, Any]] = []
        constructor_chain: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        files_by_path = {item.rel_path: item for item in model.files}
        for file_model in model.files:
            for node in _iter_nodes(file_model.root):
                line = _node_start_line(node)
                enclosing_method = next(
                    (
                        item
                        for item in methods_by_file.get(file_model.rel_path, [])
                        if item.begin_line <= line <= item.end_line
                    ),
                    None,
                )
                enclosing_classes = [
                    item
                    for item in classes_by_file.get(file_model.rel_path, [])
                    if item.begin_line <= line <= item.end_line
                ]
                if target.is_constructor:
                    _collect_lpl_constructor_usage(
                        model,
                        file_model,
                        node,
                        target=target,
                        family_keys=family_keys,
                        candidates_by_owner_name=candidates_by_owner_name,
                        enclosing_method=enclosing_method,
                        production_calls=production_calls,
                        method_references=method_references,
                        constructor_chain=constructor_chain,
                        unresolved=unresolved,
                    )
                    continue
                if node.type not in {"method_invocation", "method_reference"}:
                    continue
                if _normalize_method(_method_usage_name(file_model.source, node)) != target_name:
                    continue
                receiver = _method_usage_receiver(file_model.source, node)
                owner_names = _private_usage_owner_names(
                    model,
                    file_model,
                    enclosing_method,
                    enclosing_classes,
                    receiver,
                )
                owner_candidates: List[MethodRecord] = []
                for owner_name in owner_names:
                    owner_candidates = _usage_method_candidates_for_owner(
                        model,
                        candidates_by_owner_name,
                        owner_name,
                        target.method_name,
                    )
                    if owner_candidates:
                        break
                relevant_candidates = [
                    item
                    for item in owner_candidates
                    if _lpl_method_key(item) in family_keys
                    and _lpl_usage_may_target(
                        model,
                        file_model,
                        enclosing_method,
                        node,
                        item,
                    )
                ]
                owner_is_related = bool(
                    enclosing_method is not None
                    and enclosing_method.owner_qualified_name in family_owner_names
                )
                if not owner_candidates:
                    static_type, receiver_resolution = _receiver_static_type(
                        model,
                        enclosing_method,
                        receiver,
                    )
                    resolved_receiver = _class_record_for_type(model, static_type)
                    receiver_binding_unreliable = bool(receiver) and (
                        receiver_resolution == "unresolved"
                        or resolved_receiver is None
                        or resolved_receiver.kind == "external"
                    )
                    imported = str(file_model.imports.get(target.method_name) or "")
                    statically_imported_target = bool(
                        not receiver
                        and imported.rsplit(".", 1)[0]
                        == target.owner_qualified_name
                    )
                    wildcard_imported_target = bool(
                        not receiver
                        and target.owner_qualified_name
                        in file_model.static_wildcard_imports
                    )
                    if _lpl_usage_may_target(
                        model,
                        file_model,
                        enclosing_method,
                        node,
                        target,
                    ) and (
                        owner_is_related
                        or receiver_binding_unreliable
                        or statically_imported_target
                        or wildcard_imported_target
                    ):
                        unresolved.append(
                            _lpl_usage_payload(
                                file_model,
                                node,
                                enclosing_method,
                                receiver=receiver,
                                reason="receiver_or_overload_set_unresolved",
                            )
                        )
                    continue
                resolved = _resolve_private_method_usage(
                    model,
                    files_by_path[file_model.rel_path],
                    enclosing_method,
                    node,
                    owner_candidates,
                )
                if resolved is not None and _lpl_method_key(resolved) in family_keys:
                    payload = _lpl_usage_payload(
                        file_model,
                        node,
                        enclosing_method,
                        receiver=receiver,
                        resolved_owner=resolved.owner_qualified_name,
                        resolved_signature=resolved.method_signature,
                    )
                    if node.type == "method_reference":
                        method_references.append(payload)
                    else:
                        production_calls.append(payload)
                elif resolved is None and relevant_candidates:
                    unresolved.append(
                        _lpl_usage_payload(
                            file_model,
                            node,
                            enclosing_method,
                            receiver=receiver,
                            reason="target_overload_or_method_reference_ambiguous",
                            candidate_signatures=[
                                item.method_signature for item in relevant_candidates
                            ],
                        )
                    )

        production_calls.sort(key=_lpl_usage_sort_key)
        method_references.sort(key=_lpl_usage_sort_key)
        constructor_chain.sort(key=_lpl_usage_sort_key)
        unresolved.sort(key=_lpl_usage_sort_key)
        analysis_ok = not unresolved
        return {
            "analysis_ok": analysis_ok,
            "error": "" if analysis_ok else "unresolved_migration_sites",
            "target_candidate_count": 1,
            "target": _lpl_method_payload(target, relationship="target"),
            "declarations": declaration_payloads,
            "constructor_chain": constructor_chain,
            "production_call_sites": production_calls,
            "method_references": method_references,
            "unresolved_sites": unresolved,
            "remaining_work_count": (
                len(declaration_payloads)
                + len(constructor_chain)
                + len(production_calls)
                + len(method_references)
                + len(unresolved)
            ),
            "authority": "java_product_semantic_model",
        }
    except Exception as exc:
        return {
            "analysis_ok": False,
            "error": f"long_parameter_list_migration_closure_failed: {exc}",
            "target_candidate_count": 0,
            "target": {},
            "declarations": [],
            "constructor_chain": [],
            "production_call_sites": [],
            "method_references": [],
            "unresolved_sites": [],
        }


def _bodyless_method_records(model: ProjectModel) -> List[MethodRecord]:
    records: List[MethodRecord] = []
    classes_by_file: Dict[str, List[ClassRecord]] = {}
    for item in model.classes.values():
        if item.file != "<classpath>":
            classes_by_file.setdefault(item.file, []).append(item)
    for file_model in model.files:
        for node in _iter_nodes(file_model.root):
            if node.type != "method_declaration" or node.child_by_field_name("body") is not None:
                continue
            line = _node_start_line(node)
            owners = [
                item
                for item in classes_by_file.get(file_model.rel_path, [])
                if item.begin_line <= line <= item.end_line
            ]
            if not owners:
                continue
            owner = min(owners, key=lambda item: (item.end_line - item.begin_line, item.begin_line))
            name = _declared_name(file_model.source, node)
            if not name:
                continue
            type_parameters = {
                **owner.type_parameters,
                **_type_parameters(file_model, node, model.classes_by_simple),
            }
            parameters = _parameter_map(
                file_model,
                node,
                model.classes_by_simple,
                type_parameters,
            )
            parameter_names = [item[0] for item in parameters]
            parameter_types = [item[1] for item in parameters]
            return_type_node = node.child_by_field_name("type")
            return_type = (
                _resolve_type_name(
                    file_model,
                    _node_text(file_model.source, return_type_node),
                    model.classes_by_simple,
                    type_parameters,
                )
                if return_type_node is not None
                else ""
            )
            records.append(
                MethodRecord(
                    file=file_model.rel_path,
                    class_name=owner.class_name,
                    owner_qualified_name=owner.qualified_name,
                    method_name=name,
                    method_signature=f"{name}({', '.join(f'{type_name} {parameter}' for parameter, type_name in parameters)})",
                    begin_line=_node_start_line(node),
                    end_line=_node_end_line(node),
                    body=None,
                    body_text="",
                    declaration_text=_node_text(file_model.source, node),
                    loc=0,
                    return_type=return_type,
                    parameter_descriptors=[
                        f"{type_name}:{_stem_name(parameter)}"
                        for parameter, type_name in zip(parameter_names, parameter_types)
                    ],
                    parameter_types=parameter_types,
                    parameters=dict(parameters),
                    local_variables={},
                    enhanced_for_variables=set(),
                    modifiers=_modifiers(file_model.source, node),
                    annotations=_annotations(file_model.source, node),
                    is_constructor=False,
                    super_access_count=0,
                    is_varargs=_declaration_has_varargs(node),
                )
            )
    return records


def _lpl_usage_may_target(
    model: ProjectModel,
    file_model: JavaFileModel,
    enclosing_method: Optional[MethodRecord],
    node: Node,
    target: MethodRecord,
) -> bool:
    if node.type == "method_reference":
        return True
    arguments = node.child_by_field_name("arguments")
    argument_nodes = list(arguments.named_children) if arguments is not None else []
    if not _private_method_accepts_arity(target, len(argument_nodes)):
        return False
    argument_types = [
        _java_expression_static_type(
            model,
            file_model,
            enclosing_method,
            argument,
        )
        for argument in argument_nodes
    ]
    return bool(
        (
            len(target.parameter_types) == len(argument_types)
            and _private_call_types_compatible(
                model,
                target,
                argument_types,
                variable_arity=False,
            )
        )
        or (
            target.is_varargs
            and _private_call_types_compatible(
                model,
                target,
                argument_types,
                variable_arity=True,
            )
        )
    )


def _collect_lpl_constructor_usage(
    model: ProjectModel,
    file_model: JavaFileModel,
    node: Node,
    *,
    target: MethodRecord,
    family_keys: Set[Tuple[str, int, str, Tuple[str, ...]]],
    candidates_by_owner_name: Mapping[Tuple[str, str], Sequence[MethodRecord]],
    enclosing_method: Optional[MethodRecord],
    production_calls: List[Dict[str, Any]],
    method_references: List[Dict[str, Any]],
    constructor_chain: List[Dict[str, Any]],
    unresolved: List[Dict[str, Any]],
) -> None:
    called_owner = ""
    destination: Optional[List[Dict[str, Any]]] = None
    if node.type == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        called_owner = _resolve_model_type(
            model,
            _resolve_type_name(
                file_model,
                _node_text(file_model.source, type_node),
                model.classes_by_simple,
            ),
        )
        destination = production_calls
    elif node.type == "explicit_constructor_invocation":
        constructor_node = node.child_by_field_name("constructor")
        constructor_kind = _node_text(file_model.source, constructor_node).strip()
        if enclosing_method is None or not enclosing_method.is_constructor:
            return
        owner = model.classes.get(enclosing_method.owner_qualified_name)
        called_owner = (
            enclosing_method.owner_qualified_name
            if constructor_kind == "this"
            else owner.superclass_name if owner is not None and constructor_kind == "super"
            else ""
        )
        called_owner = _resolve_model_type(model, called_owner)
        destination = constructor_chain
    elif node.type == "method_reference" and _node_text(file_model.source, node).strip().endswith("::new"):
        receiver = _node_text(file_model.source, node).strip().rsplit("::", 1)[0]
        called_owner = _resolve_model_type(
            model,
            _resolve_type_name(file_model, receiver, model.classes_by_simple),
        )
        destination = method_references
    else:
        return
    if called_owner != target.owner_qualified_name or destination is None:
        return
    owner_candidates = list(
        candidates_by_owner_name.get((called_owner, target.method_name), [])
    )
    relevant = [item for item in owner_candidates if _lpl_method_key(item) in family_keys]
    if node.type == "method_reference":
        resolved = owner_candidates[0] if len(owner_candidates) == 1 else None
    else:
        resolved = _resolve_private_method_usage(
            model,
            file_model,
            enclosing_method,
            node,
            owner_candidates,
        )
    if resolved is not None and _lpl_method_key(resolved) in family_keys:
        destination.append(
            _lpl_usage_payload(
                file_model,
                node,
                enclosing_method,
                receiver=called_owner,
                resolved_owner=resolved.owner_qualified_name,
                resolved_signature=resolved.method_signature,
            )
        )
    elif relevant:
        unresolved.append(
            _lpl_usage_payload(
                file_model,
                node,
                enclosing_method,
                receiver=called_owner,
                reason="target_constructor_overload_or_reference_ambiguous",
                candidate_signatures=[item.method_signature for item in relevant],
            )
        )


def _lpl_type_identity(value: str) -> str:
    erased = _erase_type(value)
    suffix = ""
    while erased.endswith("[]"):
        suffix += "[]"
        erased = erased[:-2]
    return f"{erased.rsplit('.', 1)[-1].lower()}{suffix}"


def _lpl_resolved_type_identity(value: str) -> str:
    return _erase_type(value).lower()


def _simple_type_name(value: str) -> str:
    return _erase_type(value).rsplit(".", 1)[-1].lower()


def _lpl_method_key(method: MethodRecord) -> Tuple[str, int, str, Tuple[str, ...]]:
    return (
        _normalize_path(method.file),
        int(method.begin_line),
        method.owner_qualified_name,
        tuple(_lpl_resolved_type_identity(item) for item in method.parameter_types),
    )


def _lpl_method_payload(
    method: MethodRecord,
    *,
    relationship: str,
) -> Dict[str, Any]:
    return {
        "file": method.file,
        "line": method.begin_line,
        "class": method.owner_qualified_name,
        "method": method.method_name,
        "signature": method.method_signature,
        "parameter_types": list(method.parameter_types),
        "relationship": relationship,
        "body_kind": "bodyless" if method.body is None else "source_body",
    }


def _lpl_usage_payload(
    file_model: JavaFileModel,
    node: Node,
    enclosing_method: Optional[MethodRecord],
    *,
    receiver: str,
    resolved_owner: str = "",
    resolved_signature: str = "",
    reason: str = "",
    candidate_signatures: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "file": file_model.rel_path,
        "line": _node_start_line(node),
        "enclosing_method": (
            enclosing_method.method_signature if enclosing_method is not None else ""
        ),
        "receiver": receiver or "<implicit-this>",
        "expression": _node_text(file_model.source, node).strip(),
        "usage_kind": node.type,
    }
    if resolved_owner:
        payload["resolved_owner"] = resolved_owner
    if resolved_signature:
        payload["resolved_signature"] = resolved_signature
    if reason:
        payload["reason"] = reason
    if candidate_signatures:
        payload["candidate_signatures"] = sorted(set(candidate_signatures))
    return payload


def _lpl_usage_sort_key(item: Mapping[str, Any]) -> Tuple[str, int, str]:
    return (
        str(item.get("file") or ""),
        int(item.get("line") or 0),
        str(item.get("expression") or ""),
    )


def _method_overrides_any_parent(
    model: ProjectModel,
    method: MethodRecord,
    owner: ClassRecord,
) -> bool:
    parent = model.classes.get(owner.superclass_name)
    seen: Set[str] = set()
    while parent is not None and parent.qualified_name not in seen:
        seen.add(parent.qualified_name)
        if any(_method_overrides(method, candidate) for candidate in parent.methods):
            return True
        parent = model.classes.get(parent.superclass_name)
    return False


def analyze_refused_bequest_target(
    project_root: Path,
    *,
    target_file: Path,
    method: Optional[str],
    line: Optional[int],
    reported_parent: str,
    target_parameter_count: Optional[int] = None,
    target_class_name: str = "",
    project_model: ProjectModel,
) -> Dict[str, Any]:
    """Return the positive hierarchy contract for one Refused Bequest target."""
    root = project_root.expanduser().resolve()
    model = project_model
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
    pinned_class = str(target_class_name or "").strip().lower()
    candidates = []
    if pinned_class:
        candidates = [
            item
            for item in file_classes
            if item.class_name.lower() == pinned_class
            or item.qualified_name.lower() == pinned_class
            or item.qualified_name.lower().endswith(f".{pinned_class}")
        ]
        if not candidates:
            return {
                "ok": False,
                "error": "pinned_target_class_not_found",
                "file": target_rel,
                "method": target_method,
                "target_class_name": target_class_name,
                "reported_parent": parent_name,
            }
    if not candidates:
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
        if item.qualified_name.lower() == parent_name.lower()
        or item.class_name.lower() == parent_simple
    ]
    if len(parent_candidates) > 1:
        inherited_parent_candidates = [
            item for item in parent_candidates
            if item.qualified_name in inherited_names
        ]
        if len(inherited_parent_candidates) == 1:
            parent_candidates = inherited_parent_candidates
    if len(parent_candidates) != 1:
        return {
            "ok": False,
            "error": (
                "reported_parent_ambiguous"
                if len(parent_candidates) > 1
                else "reported_parent_not_resolved"
            ),
            "file": target_rel,
            "method": target_method,
            "target_class": target_class.qualified_name,
            "reported_parent": parent_name,
            "parent_candidate_count": len(parent_candidates),
        }
    parent_record = parent_candidates[0]

    def declares_target(record: Optional[ClassRecord]) -> bool:
        if record is None:
            return False
        body_method_match = any(
            _normalize_method(item.method_name) == target_method
            and (
                target_parameter_count is None
                or len(item.parameter_descriptors) == target_parameter_count
            )
            for item in record.methods
        )
        bodyless_match = any(
            re.search(
                rf"\b{re.escape(target_method)}\s*\(",
                declaration,
                flags=re.IGNORECASE,
            )
            and (
                target_parameter_count is None
                or _declaration_parameter_count(declaration) == target_parameter_count
            )
            for declaration in record.bodyless_method_declarations
        )
        return body_method_match or bodyless_match

    child_declares_target = declares_target(target_class)
    parent_declares_target = declares_target(parent_record)
    inherited_rejecting_owners: List[str] = []
    for inherited_name in sorted(inherited_names):
        inherited_record = _class_record_for_type(model, inherited_name)
        if inherited_record is None:
            continue
        rejecting_target = any(
            _normalize_method(item.method_name) == target_method
            and (
                target_parameter_count is None
                or len(item.parameter_descriptors) == target_parameter_count
            )
            and _is_stub_method(item)
            for item in inherited_record.methods
        )
        if rejecting_target:
            inherited_rejecting_owners.append(inherited_record.qualified_name)
    descendant_rejecting_owners: List[str] = []
    for candidate in model.classes.values():
        if candidate.qualified_name == target_class.qualified_name:
            continue
        if target_class.qualified_name not in _all_parent_type_names(model, candidate):
            continue
        rejecting_target = any(
            _normalize_method(item.method_name) == target_method
            and (
                target_parameter_count is None
                or len(item.parameter_descriptors) == target_parameter_count
            )
            and _is_stub_method(item)
            for item in candidate.methods
        )
        if rejecting_target:
            descendant_rejecting_owners.append(candidate.qualified_name)
    orphaned_real_implementers: List[str] = []
    family_root = _class_record_for_type(model, target_class.superclass_name)
    if family_root is not None:
        for candidate in model.classes.values():
            if candidate.qualified_name == target_class.qualified_name:
                continue
            if (
                candidate.qualified_name != family_root.qualified_name
                and family_root.qualified_name not in _all_parent_type_names(model, candidate)
            ):
                continue
            declares_real_target = any(
                _normalize_method(item.method_name) == target_method
                and (
                    target_parameter_count is None
                    or len(item.parameter_descriptors) == target_parameter_count
                )
                and not _is_stub_method(item)
                for item in candidate.methods
            )
            if not declares_real_target:
                continue
            inherited_contract = any(
                declares_target(model.classes.get(inherited_name))
                for inherited_name in _all_parent_type_names(model, candidate)
            )
            if not inherited_contract:
                orphaned_real_implementers.append(candidate.qualified_name)
    capability_split_satisfied = bool(
        not child_declares_target
        and not descendant_rejecting_owners
        and not orphaned_real_implementers
        and (
            not inherits_parent
            or (
                parent_record
                and not parent_declares_target
                and not inherited_rejecting_owners
            )
        )
    )
    rejecting_override_removed = bool(
        parent_record
        and inherits_parent
        and parent_declares_target
        and not child_declares_target
    )
    return {
        "ok": True,
        "file": target_rel,
        "method": target_method,
        "target_parameter_count": target_parameter_count,
        "pinned_target_class": target_class_name,
        "target_class": target_class.qualified_name,
        "reported_parent": parent_name,
        "resolved_parent": parent_record.qualified_name,
        "parent_resolved": parent_record is not None,
        "inherited_types": sorted(inherited_names),
        "inherits_reported_parent": inherits_parent,
        "child_declares_target": child_declares_target,
        "parent_declares_target": parent_declares_target,
        "inherited_rejecting_owners": inherited_rejecting_owners,
        "descendant_rejecting_owners": sorted(descendant_rejecting_owners),
        "orphaned_real_implementers": sorted(orphaned_real_implementers),
        "capability_split_satisfied": capability_split_satisfied,
        "rejecting_override_removed": rejecting_override_removed,
    }


def build_refused_bequest_impact_map(
    project_root: Path,
    *,
    target_file: Path,
    method: Optional[str],
    line: Optional[int],
    reported_parent: str,
    target_parameter_count: Optional[int] = None,
    target_class_name: str = "",
    project_model: ProjectModel,
) -> Dict[str, Any]:
    """Build a read-only capability migration worklist from the semantic model.

    This deliberately reports declarations and production call sites instead of
    proposing sample-specific edits.  It gives a refactoring agent the closure
    boundary it must inspect before changing an inheritance contract.
    """
    root = project_root.expanduser().resolve()
    model = project_model
    profile = analyze_refused_bequest_target(
        root,
        target_file=target_file,
        method=method,
        line=line,
        reported_parent=reported_parent,
        target_parameter_count=target_parameter_count,
        target_class_name=target_class_name,
        project_model=model,
    )
    if not profile.get("ok"):
        return {
            "ok": False,
            "error": profile.get("error", "capability_profile_unavailable"),
            "profile": profile,
        }

    target_method = _normalize_method(method)
    arity = target_parameter_count
    parent_name = str(reported_parent or "").strip()
    resolved_parent = str(profile.get("resolved_parent") or "")
    target_qualified = str(profile.get("target_class") or "")

    def method_matches(item: MethodRecord) -> bool:
        return bool(
            _normalize_method(item.method_name) == target_method
            and (arity is None or len(item.parameter_descriptors) == arity)
        )

    def class_matches_parent(item: ClassRecord) -> bool:
        return bool(resolved_parent and item.qualified_name == resolved_parent)

    parent_candidates = [
        item for item in model.classes.values() if class_matches_parent(item)
    ]
    if len(parent_candidates) != 1:
        return {
            "ok": False,
            "error": "resolved_parent_identity_not_unique",
            "parent_candidate_count": len(parent_candidates),
            "profile": profile,
        }
    parent_record = parent_candidates[0]
    related_classes = [
        item
        for item in model.classes.values()
        if class_matches_parent(item)
        or resolved_parent in _all_parent_type_names(model, item)
    ]
    if target_qualified and all(item.qualified_name != target_qualified for item in related_classes):
        target_record = model.classes.get(target_qualified)
        if target_record is not None:
            related_classes.append(target_record)

    contract_declarations: List[Dict[str, Any]] = []
    declaration_owners: List[ClassRecord] = []
    if parent_record is not None:
        declaration_owners.append(parent_record)
        for inherited_name in sorted(_all_parent_type_names(model, parent_record)):
            inherited_record = model.classes.get(inherited_name)
            if inherited_record is not None:
                declaration_owners.append(inherited_record)
    seen_declarations: Set[Tuple[str, int, str]] = set()
    for owner in declaration_owners:
        for item in owner.methods:
            if not method_matches(item):
                continue
            key = (item.file, item.begin_line, item.method_signature)
            if key in seen_declarations:
                continue
            seen_declarations.add(key)
            contract_declarations.append(_impact_method_payload(item))
        file_model = next(
            (item for item in model.files if item.rel_path == owner.file),
            None,
        )
        for declaration in owner.bodyless_method_declarations:
            if not re.search(
                rf"\b{re.escape(target_method)}\s*\(",
                declaration,
                flags=re.IGNORECASE,
            ):
                continue
            if arity is not None and _declaration_parameter_count(declaration) != arity:
                continue
            line = 0
            if file_model is not None:
                offset = file_model.source.find(declaration.encode("utf-8"))
                if offset >= 0:
                    line = file_model.source[:offset].count(b"\n") + 1
            contract_declarations.append(
                {
                    "owner": owner.qualified_name,
                    "file": owner.file,
                    "line": line,
                    "signature": declaration.strip(),
                    "modifiers": sorted(owner.modifiers),
                    "body_kind": "abstract_or_bodyless",
                }
            )

    implementers: List[Dict[str, Any]] = []
    for item in sorted(related_classes, key=lambda record: record.qualified_name):
        declarations = [method for method in item.methods if method_matches(method)]
        has_bodyless_declaration = any(
            re.search(
                rf"\b{re.escape(target_method)}\s*\(",
                declaration,
                flags=re.IGNORECASE,
            )
            and (arity is None or _declaration_parameter_count(declaration) == arity)
            for declaration in item.bodyless_method_declarations
        )
        if item.qualified_name == getattr(parent_record, "qualified_name", ""):
            role = "contract_owner"
        elif declarations:
            has_real = any(method.body is None or not _is_stub_method(method) for method in declarations)
            role = "real_implementer" if has_real else "rejecting_or_stub_implementer"
        elif has_bodyless_declaration:
            role = "abstract_contract_declaration"
        else:
            role = "inherits_without_declaration"
        if item.qualified_name == target_qualified:
            role = f"refusing_target:{role}"
        implementers.append(
            {
                "class": item.qualified_name,
                "kind": item.kind,
                "file": item.file,
                "line": item.begin_line,
                "modifiers": sorted(item.modifiers),
                "role": role,
                "declared_target_methods": [
                    _impact_method_payload(method) for method in declarations
                ],
            }
        )

    call_sites, excluded_same_name_calls = _target_method_call_sites(
        model,
        target_method=target_method,
        parent_name=parent_name,
        related_classes=related_classes,
    )
    target_record = model.classes.get(target_qualified)
    inherited_surface: List[Dict[str, Any]] = []
    if target_record is not None:
        seen_ancestors: Set[str] = set()
        ancestor_name = target_record.superclass_name
        while ancestor_name and ancestor_name not in seen_ancestors:
            seen_ancestors.add(ancestor_name)
            ancestor = model.classes.get(ancestor_name)
            if ancestor is None:
                break
            inherited_methods = [
                _impact_method_payload(item)
                for item in ancestor.methods
                if not item.is_constructor
                and "private" not in item.modifiers
                and not method_matches(item)
            ]
            inherited_surface.append(
                {
                    "owner": ancestor.qualified_name,
                    "file": ancestor.file,
                    "state_fields": [
                        {"name": name, "type": type_name}
                        for name, type_name in sorted(ancestor.fields.items())
                    ],
                    "non_target_methods": inherited_methods,
                    "bodyless_non_target_methods": [
                        declaration.strip()
                        for declaration in ancestor.bodyless_method_declarations
                        if not re.search(
                            rf"\b{re.escape(target_method)}\s*\(",
                            declaration,
                            flags=re.IGNORECASE,
                        )
                    ],
                }
            )
            ancestor_name = ancestor.superclass_name
    target_contract = _refused_bequest_target_contract(
        model,
        target_record,
        target_method=target_method,
        target_parameter_count=arity,
    )
    return {
        "ok": True,
        "structural_expectation": "capability_split",
        "target": {
            "class": target_qualified,
            "file": profile.get("file"),
            "method": target_method,
            "parameter_count": arity,
            "reported_parent": parent_name,
        },
        "contract_declarations": contract_declarations,
        "implementers": implementers,
        "production_call_sites": call_sites,
        "inherited_surface_at_risk": inherited_surface,
        "target_contract": target_contract,
        "excluded_unrelated_same_name_calls": excluded_same_name_calls,
        "unresolved_receiver_call_sites": sum(
            1 for item in call_sites if item.get("receiver_resolution") == "unresolved"
        ),
        "dependency_order": [
            "reuse_or_declare_narrow_capability_with_usable_visibility",
            "migrate_real_implementers",
            "migrate_production_declarations_and_callers",
            "preserve_or_explicitly_migrate_inherited_non_target_state_and_api",
            "remove_rejected_operation_from_refusing_types",
            "search_production_tree_for_old_contract_and_target_signature",
        ],
        "completion_contract": (
            "Inspect every listed declaration, implementer, and production call site. "
            "If changing a superclass, preserve or deliberately migrate every listed "
            "inherited non-target field and method used by the target or its callers. "
            "Treat unresolved receiver sites as required manual review; do not declare "
            "the migration complete while one remains unexamined."
        ),
    }


def _refused_bequest_target_contract(
    model: ProjectModel,
    target: Optional[ClassRecord],
    *,
    target_method: str,
    target_parameter_count: Optional[int],
) -> Dict[str, Any]:
    """Describe the target type's externally visible non-target contract.

    The snapshot is deliberately independent of a particular project or
    refactoring route.  It records what callers can observe on the refusing
    type before an inheritance migration, regardless of which ancestor owns
    each method.  A later checkpoint can therefore distinguish a legitimate
    capability split from silently dropping unrelated inherited API.
    """
    if target is None:
        return {"ok": False, "error": "target_class_not_found"}

    def is_reported_target(method: MethodRecord) -> bool:
        return bool(
            _normalize_method(method.method_name) == target_method
            and (
                target_parameter_count is None
                or len(method.parameter_descriptors) == target_parameter_count
            )
        )

    def visibility(method: MethodRecord) -> str:
        if "public" in method.modifiers:
            return "public"
        if "protected" in method.modifiers:
            return "protected"
        return "package"

    def api_key(method: MethodRecord) -> str:
        parameter_types = ",".join(_erase_type(item) for item in method.parameter_types)
        return f"{method.method_name}({parameter_types})"

    # Walk the target before its ancestors so an override represents the
    # effective method. Interfaces and more distant ancestors are included
    # through the semantic model's transitive parent resolver.
    owners: List[ClassRecord] = [target]
    inherited_names = sorted(_all_parent_type_names(model, target))
    owners.extend(
        model.classes[name]
        for name in inherited_names
        if name in model.classes and name != target.qualified_name
    )
    effective: Dict[str, Dict[str, Any]] = {}
    for owner in owners:
        for method in owner.methods:
            if method.is_constructor or is_reported_target(method):
                continue
            method_visibility = visibility(method)
            if method_visibility not in {"public", "protected"}:
                continue
            key = api_key(method)
            candidate = {
                "api_key": key,
                "signature": method.method_signature,
                "return_type": _erase_type(method.return_type),
                "visibility": method_visibility,
                "owner": owner.qualified_name,
                "declared_on_target": owner.qualified_name == target.qualified_name,
            }
            previous = effective.get(key)
            if previous is None:
                effective[key] = candidate
            elif previous["visibility"] == "protected" and method_visibility == "public":
                # A public declaration remains public even when traversal order
                # encounters a narrower inherited declaration first.
                effective[key] = candidate

    constructors = []
    for method in target.methods:
        if not method.is_constructor:
            continue
        method_visibility = visibility(method)
        if method_visibility not in {"public", "protected"}:
            continue
        constructors.append(
            {
                "api_key": api_key(method),
                "signature": method.method_signature,
                "visibility": method_visibility,
            }
        )
    return {
        "ok": True,
        "class": target.qualified_name,
        "kind": target.kind,
        "direct_superclass": target.superclass_name,
        "interfaces": sorted(target.interface_names),
        "visible_non_target_methods": [
            effective[key] for key in sorted(effective)
        ],
        "declared_visible_constructors": sorted(
            constructors,
            key=lambda item: str(item["api_key"]),
        ),
        "comparison_policy": {
            "hard_scope": (
                "target_declared_public_or_protected_non_target_methods_and_constructors"
            ),
            "review_scope": "inherited_public_or_protected_non_target_methods",
            "allows_owner_migration": True,
            "allows_superclass_change": True,
            "ignores_reported_rejected_capability": True,
        },
    }


def _impact_method_payload(method: MethodRecord) -> Dict[str, Any]:
    if method.body is None:
        body_kind = "abstract_or_bodyless"
    elif _is_stub_method(method):
        body_kind = "rejecting_or_stub"
    else:
        body_kind = "real_behavior"
    return {
        "owner": method.owner_qualified_name,
        "file": method.file,
        "line": method.begin_line,
        "signature": method.method_signature,
        "modifiers": sorted(method.modifiers),
        "body_kind": body_kind,
    }


def _declaration_parameter_count(declaration: str) -> int:
    match = re.search(r"\((.*)\)", declaration, flags=re.DOTALL)
    if not match or not match.group(1).strip():
        return 0
    parameters = match.group(1)
    depth = 0
    count = 1
    for char in parameters:
        if char in "<[(":
            depth += 1
        elif char in ">])":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            count += 1
    return count


def _target_method_call_sites(
    model: ProjectModel,
    *,
    target_method: str,
    parent_name: str,
    related_classes: Sequence[ClassRecord],
) -> Tuple[List[Dict[str, Any]], int]:
    related_names = {
        name
        for item in related_classes
        for name in (item.qualified_name, item.class_name)
    }
    related_simple = {name.rsplit(".", 1)[-1] for name in related_names}
    parent_simple = parent_name.rsplit(".", 1)[-1]
    related_simple.add(parent_simple)
    call_sites: List[Dict[str, Any]] = []
    excluded_same_name_calls = 0
    related_files = {item.file for item in related_classes}
    related_qualified = {item.qualified_name for item in related_classes}

    for file_model in model.files:
        for node in _iter_nodes(file_model.root):
            if node.type not in {"method_invocation", "method_reference"}:
                continue
            if _normalize_method(_method_usage_name(file_model.source, node)) != target_method:
                continue
            call_line = _node_start_line(node)
            owner_method = next(
                (
                    item
                    for item in model.methods
                    if item.file == file_model.rel_path
                    and item.begin_line <= call_line <= item.end_line
                ),
                None,
            )
            receiver_node = node.child_by_field_name("object")
            receiver = _node_text(file_model.source, receiver_node).strip()
            static_type, resolution = _receiver_static_type(
                model,
                owner_method,
                receiver,
            )
            static_simple = _erase_type(static_type).rsplit(".", 1)[-1]
            exposes_contract: Optional[bool]
            if resolution == "unresolved":
                exposes_contract = None
            else:
                exposes_contract = static_simple in related_simple
            owner_is_related = bool(
                owner_method is not None
                and owner_method.owner_qualified_name in related_qualified
            )
            if exposes_contract is not True and not (
                resolution == "unresolved"
                and (owner_is_related or file_model.rel_path in related_files)
            ):
                excluded_same_name_calls += 1
                continue
            call_sites.append(
                {
                    "file": file_model.rel_path,
                    "line": call_line,
                    "enclosing_method": (
                        owner_method.method_signature if owner_method is not None else ""
                    ),
                    "receiver": receiver or "<implicit-this>",
                    "static_receiver_type": static_type,
                    "receiver_resolution": resolution,
                    "exposes_reported_contract": exposes_contract,
                    "expression": _node_text(file_model.source, node).strip(),
                }
            )
    return (
        sorted(call_sites, key=lambda item: (str(item["file"]), int(item["line"]))),
        excluded_same_name_calls,
    )


def _receiver_static_type(
    model: ProjectModel,
    owner_method: Optional[MethodRecord],
    receiver: str,
) -> Tuple[str, str]:
    if owner_method is None:
        return "", "unresolved"
    owner = model.classes.get(owner_method.owner_qualified_name)
    if not receiver or receiver == "this":
        return owner_method.owner_qualified_name, "owner_type"
    if receiver == "super":
        return (owner.superclass_name if owner is not None else ""), (
            "super_type" if owner is not None and owner.superclass_name else "unresolved"
        )
    cast_match = re.match(
        r"^\(+\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:<[^>]+>)?)\s*\)",
        receiver,
    )
    if cast_match:
        return cast_match.group(1), "explicit_cast"
    this_field_match = re.fullmatch(r"this\.([A-Za-z_$][\w$]*)", receiver)
    if this_field_match and owner is not None:
        field_name = this_field_match.group(1)
        if field_name in owner.fields:
            return owner.fields[field_name], "field"
    invocation_match = re.fullmatch(
        r"(?:(.+)\.)?([A-Za-z_$][\w$]*)\s*\([^()]*\)",
        receiver,
        flags=re.DOTALL,
    )
    if invocation_match:
        base_expression = str(invocation_match.group(1) or "").strip()
        invoked_name = invocation_match.group(2)
        if base_expression:
            base_type, _ = _receiver_static_type(model, owner_method, base_expression)
            base_record = _class_record_for_type(model, base_type)
        else:
            base_record = owner
        if base_record is not None:
            candidates = [
                item
                for item in base_record.methods
                if item.method_name == invoked_name and item.return_type
            ]
            if len(candidates) == 1:
                return candidates[0].return_type, "method_return"
            if len(candidates) > 1:
                return "", "unresolved"
    if re.fullmatch(r"[A-Za-z_$][\w$]*", receiver):
        if receiver in owner_method.local_variables:
            return owner_method.local_variables[receiver], "local_variable"
        if receiver in owner_method.parameters:
            return owner_method.parameters[receiver], "parameter"
        if owner is not None and receiver in owner.fields:
            return owner.fields[receiver], "field"
        if receiver in model.classes_by_simple:
            record = _class_record_for_type(model, receiver)
            if record is not None:
                return record.qualified_name, "type_name"
    return "", "unresolved"


def _class_record_for_type(
    model: ProjectModel,
    type_name: str,
) -> Optional[ClassRecord]:
    erased = _erase_type(type_name)
    simple = erased.rsplit(".", 1)[-1]
    candidates = model.classes_by_simple.get(simple, [])
    if "." in erased:
        candidates = [item for item in candidates if item.qualified_name == erased]
    return candidates[0] if len(candidates) == 1 else None


def find_data_clump_group_occurrences(
    project_root: Path,
    *,
    group: str,
) -> List[SemanticFinding]:
    """Filter one group from the ordinary full-project product detector."""
    target_group = _normalize_qualified_group(group)
    if not target_group:
        return []
    model = _build_project_model(project_root)
    return [
        item
        for item in _detect_data_clumps(model)
        if _normalize_qualified_group(str(item.attributes.get("group") or ""))
        == target_group
    ]


def build_scoped_project_model(
    project_root: Path,
    source_files: Iterable[str | Path],
    classpath: str = "",
) -> ProjectModel:
    """Build a semantic model from an explicit production-source scope.

    Unlike :func:`run_java_semantic_detector` and ``_build_project_model``, this
    entry point never discovers Java source files and never runs a smell
    detector.  Callers must supply the files needed by their frozen guard
    contract.  Paths may be absolute or relative to ``project_root``; paths
    outside the root and non-Java/non-file inputs are rejected, while test,
    generated/build-output, and duplicate inputs are excluded.
    """
    root = project_root.expanduser().resolve()
    selected: Set[Path] = set()
    for raw_path in source_files:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"SCOPED_SOURCE_OUTSIDE_PROJECT: {resolved} is outside {root}"
            ) from exc
        if resolved.suffix.casefold() != ".java" or not resolved.is_file():
            raise ValueError(
                f"SCOPED_SOURCE_NOT_JAVA_FILE: {relative.as_posix()}"
            )
        if _contains_excluded_part(relative, DEFAULT_EXCLUDE_PATHS):
            continue
        # A target Guard receives explicit files.  Applying the shared standard
        # test-root rule is sufficient here and avoids walking the repository
        # to discover every build descriptor before parsing two files.
        if standard_test_root(relative) is not None:
            continue
        selected.add(resolved)

    parser = get_parser("java")
    files: List[JavaFileModel] = []
    classes: Dict[str, ClassRecord] = {}
    classes_by_simple: Dict[str, List[ClassRecord]] = {}
    methods: List[MethodRecord] = []

    for path in sorted(selected):
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
            _collect_class_records(
                file_model,
                class_node,
                [],
                classes,
                classes_by_simple,
            )

    wildcard_packages = tuple(sorted({
        package
        for file_model in files
        for package in (
            *file_model.wildcard_imports,
            *file_model.static_wildcard_imports,
        )
    }))
    for qualified_name in _external_type_names(classpath, wildcard_packages):
        if qualified_name in classes:
            continue
        simple = qualified_name.rsplit(".", 1)[-1]
        classes_by_simple.setdefault(simple, []).append(
            ClassRecord(
                file="<classpath>",
                class_name=simple,
                qualified_name=qualified_name,
                begin_line=0,
                end_line=0,
                kind="external",
            )
        )

    qualified_counts = Counter(
        item.qualified_name
        for records in classes_by_simple.values()
        for item in records
    )
    for qualified_name, count in qualified_counts.items():
        if count > 1:
            classes.pop(qualified_name, None)

    for file_model in files:
        for class_node in _iter_top_level_class_nodes(file_model.root):
            _resolve_class_records(
                file_model,
                class_node,
                [],
                classes,
                classes_by_simple,
            )

    for file_model in files:
        for class_node in _iter_top_level_class_nodes(file_model.root):
            _collect_method_records(
                file_model,
                class_node,
                [],
                classes,
                classes_by_simple,
                methods,
            )

    return ProjectModel(
        root=root,
        files=files,
        classes=classes,
        classes_by_simple=classes_by_simple,
        methods=methods,
    )


def evaluate_scoped_guard_findings(
    model: ProjectModel,
    smell: str,
) -> List[SemanticFinding]:
    """Evaluate exactly one smell rule inside an explicit guard scope.

    This is deliberately not a discovery API: callers must first construct a
    :func:`build_scoped_project_model` from the frozen target and production
    diff.  No source files are discovered here and no unrelated smell rule is
    evaluated.  Data Clumps uses its own exact-group streaming query and is
    therefore intentionally excluded from this dispatcher.
    """
    evaluators = {
        "feature_envy": _detect_feature_envy,
        "refused_bequest": _detect_refused_bequest,
        "god_class": _detect_god_class,
        "dead_code": _detect_dead_code,
    }
    evaluator = evaluators.get(str(smell))
    if evaluator is None:
        raise ValueError(f"UNSUPPORTED_SCOPED_GUARD_RULE: {smell}")
    return _sort_findings(evaluator(model))


def _build_project_model(project_root: Path, *, classpath: str = "") -> ProjectModel:
    root = project_root.expanduser().resolve()
    parser = get_parser("java")
    files: List[JavaFileModel] = []
    classes: Dict[str, ClassRecord] = {}
    classes_by_simple: Dict[str, List[ClassRecord]] = {}
    methods: List[MethodRecord] = []

    for path in _list_java_files(root):
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

    # Build dependencies and generated main outputs contribute identities only;
    # they are never scanned as smell-bearing source. This lets wildcard imports
    # resolve to a real FQCN without coupling findings to tests or dataset rows.
    wildcard_packages = tuple(sorted({
        package
        for file_model in files
        for package in (
            *file_model.wildcard_imports,
            *file_model.static_wildcard_imports,
        )
    }))
    for qualified_name in _external_type_names(classpath, wildcard_packages):
        if qualified_name in classes:
            continue
        simple = qualified_name.rsplit(".", 1)[-1]
        classes_by_simple.setdefault(simple, []).append(
            ClassRecord(
                file="<classpath>",
                class_name=simple,
                qualified_name=qualified_name,
                begin_line=0,
                end_line=0,
                kind="external",
            )
        )

    # More than one source root can contain the same fully-qualified class.
    # Retaining the last dict assignment would make semantic findings depend on
    # filesystem order, so duplicate identities are excluded from the model.
    qualified_counts = Counter(
        item.qualified_name
        for records in classes_by_simple.values()
        for item in records
    )
    for qualified_name, count in qualified_counts.items():
        if count > 1:
            classes.pop(qualified_name, None)

    # Phase two resolves every relation and declared type against the complete
    # project symbol index. Resolving while files are still being discovered
    # makes results depend on traversal order and turns later same-package
    # classes into false wildcard ambiguities.
    for file_model in files:
        for class_node in _iter_top_level_class_nodes(file_model.root):
            _resolve_class_records(
                file_model,
                class_node,
                [],
                classes,
                classes_by_simple,
            )

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
            is_static = bool(re.match(r"\s*import\s+static\b", text))
            cleaned = text.replace("import", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
            if not cleaned:
                continue
            if cleaned.endswith(".*"):
                target = (
                    file_model.static_wildcard_imports
                    if is_static
                    else file_model.wildcard_imports
                )
                target.append(cleaned[:-2])
            else:
                file_model.imports[cleaned.rsplit(".", 1)[-1]] = cleaned


def _iter_top_level_class_nodes(root: Node) -> Iterable[Node]:
    for child in root.children:
        if child.type in CLASS_NODE_TYPES:
            yield child


@lru_cache(maxsize=32)
def _external_type_names(
    classpath: str,
    wildcard_packages: tuple[str, ...],
) -> frozenset[str]:
    """Read type identities from dependency JARs and generated main outputs."""
    names: Set[str] = set()
    reachable_packages = frozenset(wildcard_packages)
    entries = _explicit_classpath_entries(classpath)
    explicit_archives = validate_explicit_symbol_archives(classpath)
    dependency_jars: Set[Path] = set()
    for entry in entries:
        if entry.is_file() and entry.suffix.casefold() in {".jar", ".jmod"}:
            dependency_jars.add(entry)
            continue
        if not entry.is_dir():
            continue
        dependency_roots = {
            entry / "caches" / "modules-2" / "files-2.1",
            entry / "repository",
            entry / "offline-home" / ".gradle" / "caches" / "modules-2" / "files-2.1",
            entry / "offline-home" / ".m2" / "repository",
        }
        for dependency_root in dependency_roots:
            if dependency_root.is_dir():
                dependency_jars.update(dependency_root.rglob("*.jar"))
        jmods = entry / "jmods"
        if jmods.is_dir():
            dependency_jars.update(jmods.glob("*.jmod"))
        if _looks_like_java_project(entry):
            for output_root in _main_symbol_output_roots(entry):
                for path in output_root.rglob("*.class"):
                    relative = path.relative_to(output_root).as_posix()
                    qualified, package = _class_entry_symbol(relative)
                    if qualified and _symbol_is_reachable(
                        qualified,
                        package,
                        reachable_packages,
                    ):
                        names.add(qualified)
                for path in output_root.rglob("*.java"):
                    names.update(
                        qualified
                        for qualified in _declared_generated_types(path)
                        if _symbol_is_reachable(
                            qualified,
                            qualified.rsplit(".", 1)[0] if "." in qualified else "",
                            reachable_packages,
                        )
                    )

    for jar_path in sorted(dependency_jars):
        try:
            with zipfile.ZipFile(jar_path) as archive:
                for entry_name in archive.namelist():
                    qualified, package = _class_entry_symbol(entry_name)
                    if qualified and _symbol_is_reachable(
                        qualified,
                        package,
                        reachable_packages,
                    ):
                        names.add(qualified)
        except (OSError, zipfile.BadZipFile) as exc:
            if jar_path in explicit_archives:
                raise _classpath_archive_error(jar_path, exc) from exc
            continue
    return frozenset(names)


def _explicit_classpath_entries(classpath: str) -> Set[Path]:
    entries: Set[Path] = set()
    for raw in str(classpath or "").split(os.pathsep):
        if not raw.strip():
            continue
        try:
            entries.add(Path(raw).expanduser().resolve())
        except OSError as exc:
            path = str(Path(raw).expanduser())
            if Path(raw).suffix.casefold() in {".jar", ".jmod"}:
                raise JavaSymbolClasspathError(
                    "EXPLICIT_CLASSPATH_ARCHIVE_UNREADABLE",
                    f"cannot resolve explicit Java symbol archive {path}: {exc}",
                    path=path,
                ) from exc
    return entries


def validate_explicit_symbol_archives(classpath: str) -> Set[Path]:
    """Validate only archive entries named directly by the caller.

    Archives discovered underneath a dependency cache are best-effort inputs:
    an unrelated broken cache artifact must not disable an otherwise complete
    detector. A directly supplied ``.jar``/``.jmod`` is different—it is an
    explicit part of the symbol contract and therefore fails closed.
    """
    archives = {
        entry
        for entry in _explicit_classpath_entries(classpath)
        if entry.suffix.casefold() in {".jar", ".jmod"}
    }
    for archive_path in sorted(archives):
        if not archive_path.is_file():
            raise JavaSymbolClasspathError(
                "EXPLICIT_CLASSPATH_ARCHIVE_UNREADABLE",
                f"explicit Java symbol archive is not a readable file: {archive_path}",
                path=str(archive_path),
            )
        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise _classpath_archive_error(archive_path, exc) from exc
    return archives


def _classpath_archive_error(
    archive_path: Path,
    exc: BaseException,
) -> JavaSymbolClasspathError:
    return JavaSymbolClasspathError(
        "EXPLICIT_CLASSPATH_ARCHIVE_UNREADABLE",
        f"cannot read explicit Java symbol archive {archive_path}: {exc}",
        path=str(archive_path),
    )


def external_symbol_snapshot(project_root: Path, classpath: str) -> dict[str, object]:
    """Describe the resolver inputs observed for this detector invocation.

    This inventory is mutable project state: builds may create generated main
    outputs or populate dependency caches.  It is useful for diagnostics, but
    it must never be used as the immutable detector-profile identity.
    """
    packages = _project_wildcard_imports(str(Path(project_root).resolve()))
    names = _external_type_names(str(classpath or ""), packages)
    digest = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()
    return {
        "snapshot_schema": JAVA_SYMBOL_SNAPSHOT_SCHEMA,
        "resolver": JAVA_SYMBOL_RESOLVER_ID,
        "available": True,
        "type_count": len(names),
        "type_names_sha256": digest,
        "wildcard_packages_sha256": hashlib.sha256(
            "\n".join(packages).encode("utf-8")
        ).hexdigest(),
    }


@lru_cache(maxsize=32)
def _project_wildcard_imports(project_root: str) -> tuple[str, ...]:
    packages: Set[str] = set()
    for path in _list_java_files(Path(project_root)):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        packages.update(
            match.group(1)
            for match in re.finditer(
                r"(?m)^\s*import\s+(?:static\s+)?"
                r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.\*\s*;",
                text,
            )
        )
    return tuple(sorted(packages))


def _symbol_is_reachable(
    qualified_name: str,
    package: str,
    wildcard_packages: frozenset[str],
) -> bool:
    if package in wildcard_packages:
        return True

    # A wildcard may name an owning type (including a static import), so walk
    # the qualified owner chain without scanning every project import for every
    # classpath entry.
    owner = qualified_name.rsplit(".", 1)[0] if "." in qualified_name else ""
    while owner:
        if owner in wildcard_packages:
            return True
        if owner == package or "." not in owner:
            break
        owner = owner.rsplit(".", 1)[0]
    return False


def _looks_like_java_project(path: Path) -> bool:
    return any(
        (path / name).is_file()
        for name in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    ) or (path / ".git").exists()


def _main_symbol_output_roots(project_root: Path) -> Set[Path]:
    outputs: Set[Path] = set()
    ignored = {".git", ".gradle", ".idea", "node_modules", "out"}
    for directory, directory_names, _ in os.walk(project_root, topdown=True, followlinks=False):
        current = Path(directory)
        directory_names[:] = [
            name for name in directory_names
            if name.casefold() not in ignored
        ]
        if current.name == "build":
            for relative in (
                "classes/java/main",
                "classes/kotlin/main",
                "generated/source/kapt/main",
                "generated/sources/annotationProcessor/java/main",
            ):
                candidate = current / relative
                if candidate.is_dir():
                    outputs.add(candidate)
            directory_names[:] = []
        elif current.name == "target":
            for relative in ("classes", "generated-sources/annotations"):
                candidate = current / relative
                if candidate.is_dir():
                    outputs.add(candidate)
            directory_names[:] = []
    return outputs


def _class_entry_symbol(value: str) -> tuple[str, str]:
    normalized = str(value).replace("\\", "/").strip("/")
    if normalized.startswith("classes/"):
        normalized = normalized[len("classes/"):]
    if (
        not normalized.endswith(".class")
        or normalized.endswith("module-info.class")
        or normalized.endswith("package-info.class")
        or normalized.startswith("META-INF/versions/")
    ):
        return "", ""
    path_parts = normalized[:-6].split("/")
    binary_name = path_parts[-1]
    nested_parts = binary_name.split("$")
    if any(
        not part
        or not part.isidentifier()
        or part[0].isdigit()
        or part.startswith("Lambda")
        for part in nested_parts
    ):
        return "", ""
    package = ".".join(path_parts[:-1])
    class_name = ".".join(nested_parts)
    qualified = f"{package}.{class_name}" if package else class_name
    return qualified, package


def _declared_generated_types(path: Path) -> Set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    package_match = re.search(r"(?m)^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;", text)
    package = package_match.group(1) if package_match else ""
    declared = {
        match.group(1)
        for match in re.finditer(
            r"(?m)^\s*(?:(?:public|protected|private|abstract|final|sealed|non-sealed|static)\s+)*"
            r"(?:class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)\b",
            text,
        )
    }
    return {f"{package}.{name}" if package else name for name in declared}


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
        source_superclass_name=_superclass_text(file_model.source, class_node),
        source_interface_names=_interface_texts(file_model.source, class_node),
        modifiers=_modifiers(file_model.source, class_node),
    )
    classes[qualified_name] = rec
    classes_by_simple.setdefault(class_name, []).append(rec)

    body = class_node.child_by_field_name("body") or _first_child(class_node, "class_body") or _first_child(class_node, "enum_body")
    if body is None:
        return
    for child in _declared_type_members(body):
        if child.type in METHOD_NODE_TYPES:
            method_name = _declared_name(file_model.source, child)
            if method_name:
                rec.declared_method_names.add(method_name)
        if child.type in CLASS_NODE_TYPES:
            _collect_class_records(file_model, child, owners + [class_name], classes, classes_by_simple)


def _resolve_class_records(
    file_model: JavaFileModel,
    class_node: Node,
    owners: List[str],
    classes: Dict[str, ClassRecord],
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> None:
    """Populate class types only after the complete identity index exists."""
    class_name = _declared_name(file_model.source, class_node)
    if not class_name:
        return
    qualified_name = _qualified_class_name(file_model.package, owners, class_name)
    record = classes.get(qualified_name)
    if record is not None:
        record.superclass_name = _resolve_type_name(
            file_model,
            _superclass_text(file_model.source, class_node),
            classes_by_simple,
        )
        record.interface_names = [
            _resolve_type_name(file_model, item, classes_by_simple)
            for item in _interface_texts(file_model.source, class_node)
        ]
        record.type_parameters = _type_parameters(
            file_model,
            class_node,
            classes_by_simple,
        )
        record.record_components = (
            dict(
                _parameter_map(
                    file_model,
                    class_node,
                    classes_by_simple,
                    record.type_parameters,
                )
            )
            if class_node.type == "record_declaration"
            else {}
        )
        declared_field_modifiers: Dict[str, Set[str]] = {}
        record.fields = _collect_fields(
            file_model,
            class_node,
            classes_by_simple,
            record.type_parameters,
            field_modifiers=declared_field_modifiers,
        )
        record.fields = {**record.record_components, **record.fields}
        record.field_modifiers = {
            **{
                name: {"private", "final"}
                for name in record.record_components
            },
            **declared_field_modifiers,
        }

    body = (
        class_node.child_by_field_name("body")
        or _first_child(class_node, "class_body")
        or _first_child(class_node, "enum_body")
    )
    if body is None:
        return
    for child in _declared_type_members(body):
        if child.type in CLASS_NODE_TYPES:
            _resolve_class_records(
                file_model,
                child,
                owners + [class_name],
                classes,
                classes_by_simple,
            )


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
    for child in _declared_type_members(body):
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


def _declared_type_members(body: Node) -> Iterable[Node]:
    """Yield declarations from class/interface/enum bodies uniformly.

    Tree-sitter exposes class members directly, but Java enum members after
    the constant list are wrapped in ``enum_body_declarations``. Treating that
    grammar container as a declaration would silently drop every enum field,
    constructor, method, and nested type from the product semantic model.
    """
    for child in body.children:
        if child.type == ENUM_MEMBER_CONTAINER_TYPE:
            yield from child.children
        else:
            yield child


def _build_method_record(
    file_model: JavaFileModel,
    method_node: Node,
    owner: ClassRecord,
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> Optional[MethodRecord]:
    body = method_node.child_by_field_name("body")
    if body is None:
        return None
    is_constructor = method_node.type in {
        "constructor_declaration",
        "compact_constructor_declaration",
    }
    name = _declared_name(file_model.source, method_node) or (owner.class_name if is_constructor else "")
    if not name:
        return None
    type_parameters = {**owner.type_parameters, **_type_parameters(file_model, method_node, classes_by_simple)}
    parameters = (
        list(owner.record_components.items())
        if method_node.type == "compact_constructor_declaration"
        else _parameter_map(
            file_model,
            method_node,
            classes_by_simple,
            type_parameters,
        )
    )
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
        is_varargs=_declaration_has_varargs(method_node),
    )


def _detect_feature_envy(model: ProjectModel) -> List[SemanticFinding]:
    findings: List[SemanticFinding] = []
    for method in model.methods:
        # Designite reports Feature Envy at method granularity. A method may
        # access several foreign fields, but those are metric contributors,
        # not separate smell findings. Freeze the strongest deterministic
        # receiver so file+method context always identifies one finding.
        profile = _designite_feature_envy_profile(model, method)
        if profile.envy_access_diff <= 1:
            continue
        findings.append(
            SemanticFinding(
                smell_type="feature_envy",
                file=method.file,
                class_name=method.class_name,
                method=method.method_signature,
                begin_line=method.begin_line,
                end_line=method.end_line,
                score=float(profile.envy_access_diff),
                rule_id="designite-2.8.6:feature_envy",
                evidence=(
                    f"envied_field={profile.envied_field}; envied_type={profile.envied_type}; "
                    f"envy_access={profile.envy_access_count}; self_access={profile.self_access_count}; "
                    f"envy_access_diff={profile.envy_access_diff}; "
                    f"direct_fields={profile.direct_field_count}; "
                    f"field_members={profile.field_member_count}; "
                    f"fields_without_member_access={profile.fields_without_member_access}; "
                    f"same_class_method_calls={profile.same_class_method_calls}"
                ),
                attributes={
                    "envied_field": profile.envied_field,
                    "envied_type": profile.envied_type,
                    "envy_access": profile.envy_access_count,
                    "self_access": profile.self_access_count,
                    "envy_access_diff": profile.envy_access_diff,
                    "direct_fields": profile.direct_field_count,
                    "field_members": profile.field_member_count,
                    "fields_without_member_access": profile.fields_without_member_access,
                    "same_class_method_calls": profile.same_class_method_calls,
                },
            )
        )
    return findings


def _detect_refused_bequest(model: ProjectModel) -> List[SemanticFinding]:
    findings: List[SemanticFinding] = []
    for cls in model.classes.values():
        if _should_skip_refused_bequest_class(cls):
            continue
        for method in cls.methods:
            parent = _parent_contract_owner(model, cls, method)
            if parent is None:
                continue
            rejection_kind = _rejection_kind(method)
            if not rejection_kind:
                continue
            findings.append(
                SemanticFinding(
                    smell_type="refused_bequest",
                    file=method.file,
                    class_name=method.class_name,
                    method=method.method_signature,
                    begin_line=method.begin_line,
                    end_line=method.end_line,
                    score=1.0,
                    rule_id="symbol_solver:refused_bequest_method",
                    evidence=(
                        f"parent={parent.qualified_name}; target_class={cls.class_name}; "
                        f"signature={method.method_signature}; "
                        f"parameter_count={len(method.parameter_descriptors)}; "
                        f"rejection_kind={rejection_kind}; super_calls={method.super_access_count}"
                    ),
                    attributes={
                        "parent": parent.qualified_name,
                        "inheritance_source": _source_inheritance_identity(cls),
                        "target_class": cls.class_name,
                        "signature": method.method_signature,
                        "parameter_count": len(method.parameter_descriptors),
                        "rejection_kind": rejection_kind,
                        "super_calls": method.super_access_count,
                    },
                )
            )
    return findings


def _source_inheritance_identity(cls: ClassRecord) -> List[str]:
    """Return a resolver-independent identity for the direct source relation."""
    parts: List[str] = []
    if cls.source_superclass_name:
        parts.append(f"extends:{cls.source_superclass_name}")
    parts.extend(
        f"implements:{name}"
        for name in sorted(cls.source_interface_names)
        if name
    )
    return parts


def _parent_contract_owner(
    model: ProjectModel,
    child: ClassRecord,
    method: MethodRecord,
) -> Optional[ClassRecord]:
    pending = [child.superclass_name, *child.interface_names]
    seen: Set[str] = set()
    while pending:
        name = pending.pop(0)
        if not name or name in seen:
            continue
        seen.add(name)
        parent = model.classes.get(name)
        if parent is None:
            continue
        if _parent_declares_method_contract(parent, method):
            return parent
        pending.extend([parent.superclass_name, *parent.interface_names])
    return None


def _parent_declares_method_contract(parent: ClassRecord, method: MethodRecord) -> bool:
    if any(_method_overrides(method, candidate) for candidate in parent.methods):
        return True
    return any(
        re.search(rf"\b{re.escape(method.method_name)}\s*\(", declaration)
        and _declaration_parameter_count(declaration) == len(method.parameter_descriptors)
        for declaration in parent.bodyless_method_declarations
    )


def _rejection_kind(method: MethodRecord) -> str:
    body = method.body
    if body is None:
        return ""
    statements = [child for child in body.children if child.is_named and not _is_comment_node(child)]
    if not statements:
        return "empty_override"
    if len(statements) != 1:
        return ""
    statement = statements[0]
    text = _node_text_from_node(statement)
    if statement.type == "throw_statement" and "UnsupportedOperationException" in text:
        return "unsupported_operation"
    if statement.type == "return_statement":
        expression = next(
            (child for child in statement.children if child.is_named and child.type != "return"),
            None,
        )
        if expression is None:
            return "empty_return"
        if expression.type == "null_literal":
            return "null_stub"
        if _is_constant_literal(expression):
            return "constant_stub"
    if statement.type == "expression_statement" and _is_stub_method(method):
        return "logging_stub"
    return ""


def _detect_data_clumps(model: ProjectModel) -> List[SemanticFinding]:
    minimum_group_size = int(DEFAULT_THRESHOLDS["data_clumps_param_group_size"])
    occurrences_threshold = int(DEFAULT_THRESHOLDS["data_clumps_occurrences"])
    effective_min_classes = int(DEFAULT_THRESHOLDS["data_clumps_min_classes"])
    eligible_methods: List[MethodRecord] = []
    for method in model.methods:
        if len(method.parameter_descriptors) < minimum_group_size:
            continue
        eligible_methods.append(method)

    findings: List[SemanticFinding] = []
    # Product contract: a finding is one normalized parameter group with at
    # least three members. Larger frequent groups are first-class findings,
    # not silently reduced to arbitrary triplets.
    maximum_group_size = max(
        (len(set(method.parameter_descriptors)) for method in eligible_methods),
        default=minimum_group_size - 1,
    )
    previous_frequent: Set[frozenset[str]] = set()
    for group_size in range(minimum_group_size, maximum_group_size + 1):
        occurrences: Dict[str, List[MethodRecord]] = {}
        for method in eligible_methods:
            descriptors = tuple(dict.fromkeys(method.parameter_descriptors))
            if len(descriptors) < group_size:
                continue
            for combo in _parameter_combinations(descriptors, group_size):
                # An unrelated unresolved parameter must not discard every
                # well-resolved group in the method. Fail closed only for the
                # candidate combination that contains the ambiguous type.
                if any(
                    _model_type_is_ambiguous(model, item.rsplit(":", 1)[0])
                    for item in combo.split("|")
                ):
                    continue
                if group_size > minimum_group_size:
                    items = frozenset(combo.split("|"))
                    if any(
                        frozenset(subset) not in previous_frequent
                        for subset in itertools.combinations(items, group_size - 1)
                    ):
                        continue
                occurrences.setdefault(combo, []).append(method)

        frequent: Set[frozenset[str]] = set()
        for group_key, methods in occurrences.items():
            methods = [
                method for method in methods
                if not _is_parameter_group_owner_constructor(model, method, group_key)
            ]
            if len(methods) < occurrences_threshold:
                continue
            method_names = {method.method_name or "" for method in methods}
            if len(method_names) < 2:
                continue
            owner_names = {method.owner_qualified_name for method in methods}
            if len(owner_names) < effective_min_classes:
                continue
            frequent.add(frozenset(group_key.split("|")))
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
                        evidence=f"group={group_key}; occurrences={len(methods)}; classes={len(owner_names)}",
                        attributes={
                            "group": group_key,
                            "occurrences": len(methods),
                            "classes": len(owner_names),
                        },
                    )
                )
        if not frequent:
            break
        previous_frequent = frequent
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
        if cls.kind != "class":
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
                attributes={
                    "class": cls.class_name,
                    "nom": nom,
                    "nof": nof,
                    "wmc": wmc,
                    "loc": loc,
                    "atfd": atfd,
                    "signals": tuple(signals),
                },
            )
        )
    return findings


def god_class_responsibility_clusters(
    model: ProjectModel,
    cls: ClassRecord,
) -> List[Dict[str, Any]]:
    """Rank source-derived extraction candidates for one God Class.

    This is an advisory repair worklist, not a second God Class detector.  It
    connects concrete production methods through owner-field accesses and
    uniquely resolved same-owner calls, then reports the resulting connected
    components.  Constructors are omitted because moving initialization code
    is normally a consequence of moving state, not a responsibility in its own
    right.  Methods without state remain honest behavior-only components rather
    than being assigned to a fabricated field cluster.
    """
    del model  # Reserved for future source-symbol refinements of this worklist.
    methods = sorted(
        (
            method
            for method in cls.methods
            if method.body is not None and not method.is_constructor
        ),
        key=lambda item: (item.begin_line, item.method_signature),
    )
    if not methods:
        return []

    method_by_key = {
        _god_class_method_key(method): method
        for method in methods
    }
    accesses_by_key = {
        key: _god_class_owner_field_accesses(method, cls)
        for key, method in method_by_key.items()
    }
    methods_by_name_arity: Dict[Tuple[str, int], List[str]] = {}
    for key, method in method_by_key.items():
        methods_by_name_arity.setdefault(
            (method.method_name, len(method.parameter_types)),
            [],
        ).append(key)

    adjacency: Dict[str, Set[str]] = {key: set() for key in method_by_key}
    field_users: Dict[str, List[str]] = {}
    for key, fields in accesses_by_key.items():
        for field_name in fields:
            field_users.setdefault(field_name, []).append(key)
    for users in field_users.values():
        for left, right in itertools.combinations(sorted(set(users)), 2):
            adjacency[left].add(right)
            adjacency[right].add(left)

    directed_calls: Set[Tuple[str, str]] = set()
    for caller_key, method in method_by_key.items():
        for callee_key in _god_class_owner_method_calls(
            method,
            cls,
            methods_by_name_arity,
        ):
            if callee_key == caller_key:
                continue
            adjacency[caller_key].add(callee_key)
            adjacency[callee_key].add(caller_key)
            directed_calls.add((caller_key, callee_key))

    components: List[Set[str]] = []
    unseen = set(method_by_key)
    while unseen:
        seed = min(unseen)
        pending = [seed]
        component: Set[str] = set()
        while pending:
            key = pending.pop()
            if key in component:
                continue
            component.add(key)
            pending.extend(sorted(adjacency[key] - component, reverse=True))
        unseen.difference_update(component)
        components.append(component)

    candidates: List[Dict[str, Any]] = []
    for component in components:
        component_methods = [method_by_key[key] for key in component]
        component_fields = sorted({
            field_name
            for key in component
            for field_name in accesses_by_key[key]
        })
        complexities = {
            key: _god_class_method_complexity(method_by_key[key])
            for key in component
        }
        method_details = sorted(
            (
                {
                    "signature": method_by_key[key].method_signature,
                    "begin_line": method_by_key[key].begin_line,
                    "wmc": complexities[key],
                    "loc": method_by_key[key].loc,
                    "accessed_fields": sorted(accesses_by_key[key]),
                }
                for key in component
            ),
            key=lambda item: (
                -int(item["wmc"]),
                -int(item["loc"]),
                int(item["begin_line"]),
                str(item["signature"]),
            ),
        )
        field_details = sorted(
            (
                {
                    "name": field_name,
                    "type": str(cls.fields.get(field_name) or ""),
                    "method_count": sum(
                        1 for key in component if field_name in accesses_by_key[key]
                    ),
                }
                for field_name in component_fields
            ),
            key=lambda item: (-int(item["method_count"]), str(item["name"])),
        )
        field_method_edges = sum(
            len(accesses_by_key[key] & set(component_fields))
            for key in component
        )
        internal_call_edges = {
            tuple(sorted((caller, callee)))
            for caller, callee in directed_calls
            if caller in component and callee in component
        }
        possible_edges = (
            len(component) * len(component_fields)
            + (len(component) * (len(component) - 1) // 2)
        )
        wmc_reduction = sum(complexities.values())
        loc_reduction = sum(method.loc for method in component_methods)
        cluster_seed = "|".join((
            cls.qualified_name,
            *component_fields,
            *(method_by_key[key].method_signature for key in sorted(component)),
        ))
        candidates.append({
            "cluster_id": f"god-responsibility-{_java_hex_hash(cluster_seed)}",
            "kind": "state_and_behavior" if component_fields else "behavior_only",
            "method_count": len(component),
            "field_count": len(component_fields),
            "nom_reduction": len(component),
            "nof_reduction": len(component_fields),
            "wmc_reduction": wmc_reduction,
            "loc_reduction": loc_reduction,
            "field_method_edges": field_method_edges,
            "internal_call_edges": len(internal_call_edges),
            "cohesion": round(
                (field_method_edges + len(internal_call_edges)) / max(possible_edges, 1),
                3,
            ),
            "fields": field_details[:GOD_CLASS_RESPONSIBILITY_MEMBER_LIMIT],
            "methods": method_details[:GOD_CLASS_RESPONSIBILITY_MEMBER_LIMIT],
            "omitted_field_count": max(
                0,
                len(field_details) - GOD_CLASS_RESPONSIBILITY_MEMBER_LIMIT,
            ),
            "omitted_method_count": max(
                0,
                len(method_details) - GOD_CLASS_RESPONSIBILITY_MEMBER_LIMIT,
            ),
        })

    candidates.sort(key=lambda item: (
        -int(item["wmc_reduction"]),
        -int(item["method_count"]),
        -int(item["loc_reduction"]),
        -int(item["field_count"]),
        str(item["cluster_id"]),
    ))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates[:GOD_CLASS_RESPONSIBILITY_CLUSTER_LIMIT]


def _god_class_method_key(method: MethodRecord) -> str:
    return f"{method.method_signature}@{method.begin_line}"


def _god_class_owner_field_accesses(
    method: MethodRecord,
    owner: ClassRecord,
) -> Set[str]:
    if method.body is None or not owner.fields:
        return set()
    fields = set(owner.fields)
    shadowed = set(method.parameters) | set(method.local_variables)
    owner_names = {
        "this",
        owner.class_name,
        owner.qualified_name,
    }
    accesses: Set[str] = set()
    for node in _iter_nodes(method.body):
        if node.type != "identifier":
            continue
        name = _node_text_from_node(node).strip()
        if name not in fields:
            continue
        parent = node.parent
        if parent is None:
            continue
        declared_name = parent.child_by_field_name("name")
        if parent.type in {
            "variable_declarator",
            "formal_parameter",
            "catch_formal_parameter",
            "spread_parameter",
        } and _same_source_node(node, declared_name):
            continue
        if parent.type in {"method_invocation", "method_reference"}:
            method_name = parent.child_by_field_name("name")
            if _same_source_node(node, method_name):
                continue
        if parent.type == "field_access":
            field_node = parent.child_by_field_name("field")
            object_node = parent.child_by_field_name("object")
            if object_node is None and parent.children:
                object_node = parent.children[0]
            if _same_source_node(node, field_node):
                receiver = _node_text_from_node(object_node).strip() if object_node is not None else ""
                if receiver in owner_names:
                    accesses.add(name)
                continue
        if name in shadowed:
            continue
        accesses.add(name)
    return accesses


def _god_class_owner_method_calls(
    method: MethodRecord,
    owner: ClassRecord,
    methods_by_name_arity: Mapping[Tuple[str, int], Sequence[str]],
) -> Set[str]:
    if method.body is None:
        return set()
    owner_receivers = {"this", owner.class_name, owner.qualified_name}
    calls: Set[str] = set()
    for node in _iter_nodes(method.body):
        if node.type != "method_invocation":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        receiver_node = node.child_by_field_name("object")
        if receiver_node is not None:
            receiver = _node_text_from_node(receiver_node).strip()
            if receiver not in owner_receivers:
                continue
        arguments = node.child_by_field_name("arguments")
        arity = len(arguments.named_children) if arguments is not None else 0
        candidates = methods_by_name_arity.get(
            (_node_text_from_node(name_node).strip(), arity),
            (),
        )
        if len(candidates) == 1:
            calls.add(str(candidates[0]))
    return calls


def _same_source_node(left: Optional[Node], right: Optional[Node]) -> bool:
    return bool(
        left is not None
        and right is not None
        and left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
        and left.type == right.type
    )


def _god_class_method_complexity(method: MethodRecord) -> int:
    """Return the product profile's per-method cyclomatic proxy."""
    if method.body is None:
        return 1
    controls = sum(1 for node in _iter_nodes(method.body) if node.type in GOD_CLASS_CONTROL_NODE_TYPES)
    return max(controls, 1)


def _god_class_atfd(
    methods: Sequence[MethodRecord],
    bodyless_declarations: Sequence[str] = (),
) -> int:
    """Return the versioned product profile's distinct-access proxy.

    This uses distinct receiver/type tokens instead of the Feature Envy
    metric, which counts individual accesses.
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
        if reference_counts.get(_method_identity(method), 0) != 0:
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
                attributes={
                    "kind": "unused_private_method",
                    "class": method.class_name,
                    "method": method.method_signature,
                    "refs": 0,
                    "loc": method.loc,
                },
            )
        )
    return findings


MethodIdentity = Tuple[str, str, Tuple[str, ...]]


def _method_identity(method: MethodRecord) -> MethodIdentity:
    return (
        method.owner_qualified_name,
        method.method_name,
        tuple(_erase_type(item) for item in method.parameter_types),
    )


def _method_reference_counts(model: ProjectModel) -> Dict[MethodIdentity, int]:
    """Resolve call sites to unique private declarations.

    A bare method-name count makes an unused declaration appear live when an
    unrelated owner happens to use the same name, and it cannot distinguish
    overloads.  Dead-code findings are declaration-level, so every use must
    resolve to one owner and one signature before it contributes a reference.
    Ambiguous or unsupported expressions deliberately contribute nothing: an
    uncertain call must not hide a product finding.

    Direct self-recursion is excluded.  It does not make an otherwise
    unreachable private method externally reachable.
    """
    candidates_by_owner_name: Dict[Tuple[str, str], List[MethodRecord]] = {}
    for method in model.methods:
        if method.is_constructor:
            continue
        candidates_by_owner_name.setdefault(
            (method.owner_qualified_name, method.method_name), []
        ).append(method)

    methods_by_file: Dict[str, List[MethodRecord]] = {}
    classes_by_file: Dict[str, List[ClassRecord]] = {}
    files_by_path = {item.rel_path: item for item in model.files}
    for method in model.methods:
        methods_by_file.setdefault(method.file, []).append(method)
    for cls in model.classes.values():
        if cls.file == "<classpath>":
            continue
        classes_by_file.setdefault(cls.file, []).append(cls)
    for methods in methods_by_file.values():
        methods.sort(key=lambda item: (item.end_line - item.begin_line, item.begin_line))
    for classes in classes_by_file.values():
        classes.sort(key=lambda item: (item.end_line - item.begin_line, item.begin_line))

    counts: Dict[MethodIdentity, int] = {}
    for file_model in model.files:
        for node in _iter_nodes(file_model.root):
            if node.type not in {"method_invocation", "method_reference"}:
                continue
            name = _method_usage_name(file_model.source, node)
            if not name:
                continue
            line = _node_start_line(node)
            enclosing_method = next(
                (
                    item
                    for item in methods_by_file.get(file_model.rel_path, [])
                    if item.begin_line <= line <= item.end_line
                ),
                None,
            )
            enclosing_classes = [
                item
                for item in classes_by_file.get(file_model.rel_path, [])
                if item.begin_line <= line <= item.end_line
            ]
            receiver = _method_usage_receiver(file_model.source, node)
            owner_names = _private_usage_owner_names(
                model,
                file_model,
                enclosing_method,
                enclosing_classes,
                receiver,
            )
            owner_candidates: List[MethodRecord] = []
            for owner_name in owner_names:
                owner_candidates = _usage_method_candidates_for_owner(
                    model,
                    candidates_by_owner_name,
                    owner_name,
                    name,
                )
                if owner_candidates:
                    # Implicit lookup selects the nearest lexical owner. An
                    # explicit receiver has only one resolved owner.
                    break
            if not owner_candidates:
                continue
            target = _resolve_private_method_usage(
                model,
                files_by_path[file_model.rel_path],
                enclosing_method,
                node,
                owner_candidates,
            )
            if target is None:
                continue
            if "private" not in target.modifiers:
                continue
            target_identity = _method_identity(target)
            if (
                enclosing_method is not None
                and _method_identity(enclosing_method) == target_identity
            ):
                continue
            counts[target_identity] = counts.get(target_identity, 0) + 1
    return counts


def _usage_method_candidates_for_owner(
    model: ProjectModel,
    candidates_by_owner_name: Mapping[Tuple[str, str], Sequence[MethodRecord]],
    owner_name: str,
    method_name: str,
) -> List[MethodRecord]:
    """Return the owner's Java overload set, including inherited API.

    Parent private declarations are not inherited. Duplicate parent signatures
    are suppressed after the nearest declaration so an ordinary override does
    not make a call look ambiguous.
    """
    owner = model.classes.get(owner_name)
    owner_names = [owner_name]
    if owner is not None:
        owner_names.extend(sorted(_all_parent_type_names(model, owner)))
    out: List[MethodRecord] = []
    seen_signatures: Set[Tuple[str, ...]] = set()
    for candidate_owner in owner_names:
        for candidate in candidates_by_owner_name.get(
            (candidate_owner, method_name), []
        ):
            if candidate_owner != owner_name and "private" in candidate.modifiers:
                continue
            signature = tuple(_erase_type(item) for item in candidate.parameter_types)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            out.append(candidate)
    return out


def _method_usage_receiver(source: bytes, node: Node) -> str:
    if node.type == "method_invocation":
        return _node_text(source, node.child_by_field_name("object")).strip()
    text = _node_text(source, node)
    return text.rsplit("::", 1)[0].strip() if "::" in text else ""


def _private_usage_owner_names(
    model: ProjectModel,
    file_model: JavaFileModel,
    enclosing_method: Optional[MethodRecord],
    enclosing_classes: Sequence[ClassRecord],
    receiver: str,
) -> List[str]:
    lexical_owners = [item.qualified_name for item in enclosing_classes]
    if enclosing_method is not None:
        lexical_owners = [
            enclosing_method.owner_qualified_name,
            *(
                item
                for item in lexical_owners
                if item != enclosing_method.owner_qualified_name
            ),
        ]
    if not receiver:
        return lexical_owners
    if receiver == "this":
        return lexical_owners[:1]
    if receiver == "super":
        return []

    outer_this = re.fullmatch(
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.this",
        receiver,
    )
    if outer_this:
        requested = outer_this.group(1)
        matches = [
            item
            for item in lexical_owners
            if item == requested or item.endswith(f".{requested}")
        ]
        return matches[:1] if len(matches) == 1 else []

    created = re.match(
        r"^new\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*(?:<[^>]*>)?\s*\(",
        receiver,
    )
    if created:
        resolved = _resolve_type_name(
            file_model,
            created.group(1),
            model.classes_by_simple,
        )
        owner_name = _resolve_model_type(model, resolved)
        return [owner_name] if owner_name in model.classes else []

    static_type, resolution = _receiver_static_type(
        model,
        enclosing_method,
        receiver,
    )
    owner_name = _resolve_model_type(model, static_type)
    if resolution != "unresolved" and owner_name in model.classes:
        return [owner_name]

    # Exact and imported type-qualified static calls need no value receiver.
    # Only try this after environment lookup, so an uppercase local variable
    # is not mistaken for a class.
    resolved = _resolve_type_name(file_model, receiver, model.classes_by_simple)
    owner_name = _resolve_model_type(model, resolved)
    return [owner_name] if owner_name in model.classes else []


def _resolve_private_method_usage(
    model: ProjectModel,
    file_model: JavaFileModel,
    enclosing_method: Optional[MethodRecord],
    node: Node,
    candidates: Sequence[MethodRecord],
) -> Optional[MethodRecord]:
    candidates = [
        candidate
        for candidate in candidates
        if _method_accessible_from(
            model,
            enclosing_method,
            candidate,
            receiver=_method_usage_receiver(file_model.source, node),
        )
    ]
    if not candidates:
        return None
    if node.type == "method_reference":
        if len(candidates) == 1:
            return candidates[0]
        return _resolve_bound_method_reference_usage(
            model,
            file_model,
            enclosing_method,
            node,
            candidates,
        )

    arguments = node.child_by_field_name("arguments")
    argument_nodes = list(arguments.named_children) if arguments is not None else []
    argument_types = [
        _java_expression_static_type(
            model,
            file_model,
            enclosing_method,
            argument,
        )
        for argument in argument_nodes
    ]

    # Phase 1: fixed-arity applicability. A varargs declaration participates
    # here with its final parameter kept as the declared array type.
    strict_fixed_candidates = [
        item
        for item in candidates
        if len(item.parameter_types) == len(argument_nodes)
        and _private_call_types_compatible(
            model,
            item,
            argument_types,
            variable_arity=False,
            allow_boxing=False,
        )
    ]
    if strict_fixed_candidates:
        return _select_compatible_method(
            model,
            strict_fixed_candidates,
            argument_types,
            variable_arity=False,
        )

    # Phase 2 permits boxing/unboxing but still keeps fixed arity.
    loose_fixed_candidates = [
        item
        for item in candidates
        if len(item.parameter_types) == len(argument_nodes)
        and _private_call_types_compatible(
            model,
            item,
            argument_types,
            variable_arity=False,
            allow_boxing=True,
        )
    ]
    if loose_fixed_candidates:
        return _select_compatible_method(
            model,
            loose_fixed_candidates,
            argument_types,
            variable_arity=False,
        )

    # Phase 2: only when no fixed-arity declaration applies, expand varargs
    # components and test variable-arity applicability.
    variable_arity_candidates = [
        item
        for item in candidates
        if item.is_varargs
        and _private_method_accepts_arity(item, len(argument_nodes))
        and _private_call_types_compatible(
            model,
            item,
            argument_types,
            variable_arity=True,
            allow_boxing=True,
        )
    ]
    return _select_compatible_method(
        model,
        variable_arity_candidates,
        argument_types,
        variable_arity=True,
    )


def _method_accessible_from(
    model: ProjectModel,
    caller: Optional[MethodRecord],
    candidate: MethodRecord,
    *,
    receiver: str,
) -> bool:
    if "public" in candidate.modifiers:
        return True
    if caller is None:
        return False
    same_owner = caller.owner_qualified_name == candidate.owner_qualified_name
    same_package = _package_of(caller.owner_qualified_name) == _package_of(
        candidate.owner_qualified_name
    )
    if "private" in candidate.modifiers:
        # Same-owner access is proven. Nested-class private access is legal in
        # Java but needs a nest-host model; keep that uncommon case fail-closed.
        return same_owner
    if same_package:
        return True
    if "protected" not in candidate.modifiers:
        return False
    caller_owner = _resolve_model_type(model, caller.owner_qualified_name)
    candidate_owner = _resolve_model_type(model, candidate.owner_qualified_name)
    return bool(
        caller_owner
        and candidate_owner
        and _is_subtype(model, caller_owner, candidate_owner)
        and receiver in {"", "this", "super"}
    )


def _select_compatible_method(
    model: ProjectModel,
    compatible: Sequence[MethodRecord],
    argument_types: Sequence[str],
    *,
    variable_arity: bool,
) -> Optional[MethodRecord]:
    if not compatible:
        return None
    # Partial exactness is not a Java overload rule. If even one argument's
    # static type is outside this source model, multiple candidates remain
    # unresolved and the route graph must fail closed.
    if not all(argument_types):
        return None

    exact = [
        item
        for item in compatible
        if _private_call_is_exact(
            item,
            argument_types,
            variable_arity=variable_arity,
        )
    ]
    if len(exact) == 1:
        return exact[0]

    # Do not rank a candidate whose applicability depends on an unavailable
    # external type relation.  Unknown is deliberately distinct from false:
    # the caller becomes an unresolved project edge and verification fails
    # closed instead of silently selecting a broader overload.
    states = {
        id(item): _private_call_conversion_states(
            model,
            item,
            argument_types,
            variable_arity=variable_arity,
        )
        for item in compatible
    }
    proven = [
        item
        for item in compatible
        if all(state == "compatible" for state in states[id(item)])
    ]
    if len(proven) == 1 and len(compatible) == 1:
        return proven[0]
    if len(proven) != len(compatible):
        return None

    # Ties such as helper(String) / helper(Object) with a null argument still
    # have a unique target when parameter specificity is source-proven.
    return _most_specific_compatible_method(
        model,
        proven,
        len(argument_types),
        variable_arity=variable_arity,
    )


def _most_specific_compatible_method(
    model: ProjectModel,
    candidates: Sequence[MethodRecord],
    argument_count: int,
    *,
    variable_arity: bool,
) -> Optional[MethodRecord]:
    """Return the unique overload whose effective parameter types dominate."""
    if len(candidates) < 2:
        return candidates[0] if candidates else None

    def dominates(left: MethodRecord, right: MethodRecord) -> bool:
        left_types = _private_call_parameter_types(
            left,
            argument_count,
            variable_arity=variable_arity,
        )
        right_types = _private_call_parameter_types(
            right,
            argument_count,
            variable_arity=variable_arity,
        )
        if len(left_types) != len(right_types):
            return False
        weakly_more_specific = all(
            _java_type_conversion_state(
                model,
                left_type,
                right_type,
                allow_boxing=False,
            ) == "compatible"
            for left_type, right_type in zip(left_types, right_types)
        )
        strictly_more_specific = any(
            _erase_type(left_type) != _erase_type(right_type)
            for left_type, right_type in zip(left_types, right_types)
        )
        return weakly_more_specific and strictly_more_specific

    undominated = [
        candidate
        for candidate in candidates
        if all(
            other is candidate or dominates(candidate, other)
            for other in candidates
        )
    ]
    return undominated[0] if len(undominated) == 1 else None


def _resolve_bound_method_reference_usage(
    model: ProjectModel,
    file_model: JavaFileModel,
    enclosing_method: Optional[MethodRecord],
    node: Node,
    candidates: Sequence[MethodRecord],
) -> Optional[MethodRecord]:
    """Resolve an overloaded bound reference from its source target type."""
    receiver = _method_usage_receiver(file_model.source, node)
    _, receiver_resolution = _receiver_static_type(
        model,
        enclosing_method,
        receiver,
    )
    is_bound_value = (
        receiver in {"this", "super"}
        or receiver_resolution
        in {
            "explicit_cast",
            "field",
            "local_variable",
            "method_return",
            "owner_type",
            "parameter",
            "super_type",
        }
        or receiver.lstrip().startswith("new ")
    )
    if not is_bound_value:
        # Type::method can denote either a static reference or an unbound
        # instance reference. Without a complete target descriptor, guessing
        # between those modes would create a false route.
        return None
    target_type = _method_reference_target_type_text(file_model.source, node)
    target_parameters = _jdk_functional_target_parameter_types(
        file_model,
        target_type,
        model.classes_by_simple,
    )
    if target_parameters is None:
        return None
    strict_fixed_candidates = [
        item
        for item in candidates
        if len(item.parameter_types) == len(target_parameters)
        and _private_call_types_compatible(
            model,
            item,
            target_parameters,
            variable_arity=False,
            allow_boxing=False,
        )
    ]
    if strict_fixed_candidates:
        return _select_compatible_method(
            model,
            strict_fixed_candidates,
            target_parameters,
            variable_arity=False,
        )
    loose_fixed_candidates = [
        item
        for item in candidates
        if len(item.parameter_types) == len(target_parameters)
        and _private_call_types_compatible(
            model,
            item,
            target_parameters,
            variable_arity=False,
            allow_boxing=True,
        )
    ]
    if loose_fixed_candidates:
        return _select_compatible_method(
            model,
            loose_fixed_candidates,
            target_parameters,
            variable_arity=False,
        )
    variable_candidates = [
        item
        for item in candidates
        if item.is_varargs
        and _private_method_accepts_arity(item, len(target_parameters))
        and _private_call_types_compatible(
            model,
            item,
            target_parameters,
            variable_arity=True,
            allow_boxing=True,
        )
    ]
    return _select_compatible_method(
        model,
        variable_candidates,
        target_parameters,
        variable_arity=True,
    )


def _method_reference_target_type_text(source: bytes, node: Node) -> str:
    current = node.parent
    while current is not None and current.type not in {
        "constructor_declaration",
        "lambda_expression",
        "method_declaration",
    }:
        if current.type == "cast_expression":
            type_node = current.child_by_field_name("type") or _first_type_child(
                current
            )
            if type_node is not None:
                return _node_text(source, type_node).strip()
            return ""
        if current.type in {"field_declaration", "local_variable_declaration"}:
            type_node = current.child_by_field_name("type") or _first_type_child(
                current
            )
            if type_node is not None:
                return _node_text(source, type_node).strip()
            return ""
        if current.type not in {
            "parenthesized_expression",
            "variable_declarator",
        }:
            # Invocation arguments, conditional expressions, assignments and
            # returns each impose their own target-typing rules.  An outer
            # variable declaration must never leak through such a boundary.
            return ""
        current = current.parent
    return ""


def _jdk_functional_target_parameter_types(
    file_model: JavaFileModel,
    type_text: str,
    classes_by_simple: Dict[str, List[ClassRecord]],
) -> Optional[List[str]]:
    text = str(type_text or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*(?:<(.*)>)?",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    simple_name = match.group(1).rsplit(".", 1)[-1]
    template = JAVA_SAM_PARAMETER_TEMPLATES.get(simple_name)
    if template is None:
        return None
    resolved_interface = _resolve_type_name(
        file_model,
        match.group(1),
        classes_by_simple,
    )
    expected_interface = (
        f"java.util.function.{simple_name}"
        if simple_name not in {"Callable", "Comparator", "Runnable"}
        else "java.util.concurrent.Callable"
        if simple_name == "Callable"
        else "java.util.Comparator"
        if simple_name == "Comparator"
        else "java.lang.Runnable"
    )
    if _erase_type(resolved_interface) != expected_interface:
        return None
    generic_text = str(match.group(2) or "").strip()
    generic_arguments = (
        split_top_level_java_types(generic_text) if generic_text else []
    )
    if any("?" in argument for argument in generic_arguments):
        return None
    resolved_arguments = [
        _resolve_type_name(file_model, argument, classes_by_simple)
        for argument in generic_arguments
    ]
    parameters: List[str] = []
    for item in template:
        if isinstance(item, str):
            parameters.append(item)
            continue
        if item < 0 or item >= len(resolved_arguments):
            return None
        parameters.append(resolved_arguments[item])
    return parameters


def resolve_project_method_invocation(
    model: ProjectModel,
    caller: MethodRecord,
    node: Node,
    *,
    candidates_by_owner_name: Optional[
        Mapping[Tuple[str, str], Sequence[MethodRecord]]
    ] = None,
) -> Tuple[Optional[MethodRecord], List[MethodRecord]]:
    """Resolve one project-local invocation with Java overload compatibility.

    The second result is the complete project candidate set.  Callers can
    distinguish an external/unmodeled call (empty set) from an ambiguous
    project-local edge (non-empty set with no unique resolution) and fail
    closed only for the latter.
    """
    if node.type not in {"method_invocation", "method_reference"}:
        return None, []
    file_model = next(
        (item for item in model.files if item.rel_path == caller.file),
        None,
    )
    owner = model.classes.get(caller.owner_qualified_name)
    if file_model is None or owner is None:
        return None, []
    method_name = _method_usage_name(file_model.source, node)
    if not method_name:
        return None, []
    receiver = _method_usage_receiver(file_model.source, node)
    if receiver == "super":
        owner_names = sorted(_all_parent_type_names(model, owner))
    else:
        owner_names = _private_usage_owner_names(
            model,
            file_model,
            caller,
            [owner],
            receiver,
        )
    if not owner_names:
        return None, []
    index = candidates_by_owner_name
    if index is None:
        built: Dict[Tuple[str, str], List[MethodRecord]] = {}
        for method in model.methods:
            built.setdefault(
                (method.owner_qualified_name, method.method_name),
                [],
            ).append(method)
        index = built
    candidates: List[MethodRecord] = []
    seen: Set[Tuple[str, int, str]] = set()
    for owner_name in owner_names:
        for candidate in _usage_method_candidates_for_owner(
            model,
            index,
            owner_name,
            method_name,
        ):
            key = (candidate.file, candidate.begin_line, candidate.method_signature)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    if node.type == "method_reference":
        relevant = list(candidates)
    else:
        arguments = node.child_by_field_name("arguments")
        argument_count = len(arguments.named_children) if arguments is not None else 0
        relevant = [
            candidate
            for candidate in candidates
            if _private_method_accepts_arity(candidate, argument_count)
        ]
    return (
        _resolve_private_method_usage(
            model,
            file_model,
            caller,
            node,
            candidates,
        ),
        relevant,
    )


def _private_method_accepts_arity(method: MethodRecord, argument_count: int) -> bool:
    parameter_count = len(method.parameter_types)
    if not method.is_varargs:
        return argument_count == parameter_count
    return argument_count >= max(0, parameter_count - 1)


def _private_call_parameter_types(
    method: MethodRecord,
    argument_count: int,
    *,
    variable_arity: bool,
) -> List[str]:
    parameter_types = list(method.parameter_types)
    if (
        not variable_arity
        or not method.is_varargs
        or not parameter_types
    ):
        return parameter_types
    component = parameter_types[-1]
    if component.endswith("[]"):
        component = component[:-2]
    return [
        *parameter_types[:-1],
        *([component] * max(0, argument_count - len(parameter_types) + 1)),
    ]


def _private_call_types_compatible(
    model: ProjectModel,
    method: MethodRecord,
    argument_types: Sequence[str],
    *,
    variable_arity: bool,
    allow_boxing: bool = True,
) -> bool:
    parameter_types = _private_call_parameter_types(
        method,
        len(argument_types),
        variable_arity=variable_arity,
    )
    if len(parameter_types) != len(argument_types):
        return False
    return all(
        _java_reference_type_compatible(
            model,
            argument,
            parameter,
            allow_boxing=allow_boxing,
        )
        for argument, parameter in zip(argument_types, parameter_types)
    )


def _private_call_is_exact(
    method: MethodRecord,
    argument_types: Sequence[str],
    *,
    variable_arity: bool,
) -> bool:
    parameter_types = _private_call_parameter_types(
        method,
        len(argument_types),
        variable_arity=variable_arity,
    )
    return bool(
        len(parameter_types) == len(argument_types)
        and all(
            argument
            and argument != "<null>"
            and _erase_type(argument) == _erase_type(parameter)
            for argument, parameter in zip(argument_types, parameter_types)
        )
    )


def _private_call_conversion_states(
    model: ProjectModel,
    method: MethodRecord,
    argument_types: Sequence[str],
    *,
    variable_arity: bool,
) -> List[str]:
    parameter_types = _private_call_parameter_types(
        method,
        len(argument_types),
        variable_arity=variable_arity,
    )
    return [
        _java_type_conversion_state(model, argument, parameter)
        for argument, parameter in zip(argument_types, parameter_types)
    ]


def _java_reference_type_compatible(
    model: ProjectModel,
    argument_type: str,
    parameter_type: str,
    *,
    allow_boxing: bool = True,
) -> bool:
    return _java_type_conversion_state(
        model,
        argument_type,
        parameter_type,
        allow_boxing=allow_boxing,
    ) != "incompatible"


def _java_type_conversion_state(
    model: ProjectModel,
    argument_type: str,
    parameter_type: str,
    *,
    allow_boxing: bool = True,
) -> str:
    """Return compatible, incompatible, or unknown for one Java conversion."""
    if not argument_type:
        return "unknown"
    parameter = _erase_type(parameter_type)
    if argument_type == "<null>":
        return "compatible" if parameter not in PRIMITIVE_TYPES else "incompatible"
    argument = _erase_type(argument_type)
    if argument == parameter:
        return "compatible"
    primitive_widening = {
        "byte": {"short", "int", "long", "float", "double"},
        "short": {"int", "long", "float", "double"},
        "char": {"int", "long", "float", "double"},
        "int": {"long", "float", "double"},
        "long": {"float", "double"},
        "float": {"double"},
    }
    if parameter in primitive_widening.get(argument, set()):
        return "compatible"
    boxed = {
        "boolean": "java.lang.Boolean",
        "byte": "java.lang.Byte",
        "char": "java.lang.Character",
        "double": "java.lang.Double",
        "float": "java.lang.Float",
        "int": "java.lang.Integer",
        "long": "java.lang.Long",
        "short": "java.lang.Short",
    }
    if allow_boxing and (
        boxed.get(argument) == parameter or boxed.get(parameter) == argument
    ):
        return "compatible"
    if (
        allow_boxing
        and parameter == "java.lang.Object"
        and argument in PRIMITIVE_TYPES
    ):
        return "compatible"
    if argument in PRIMITIVE_TYPES or parameter in PRIMITIVE_TYPES:
        return "incompatible"
    argument_array = argument.endswith("[]")
    parameter_array = parameter.endswith("[]")
    if argument_array:
        if parameter in {
            "java.lang.Object",
            "java.lang.Cloneable",
            "java.io.Serializable",
        }:
            return "compatible"
        if not parameter_array:
            return "incompatible"
        argument_component = argument[:-2]
        parameter_component = parameter[:-2]
        if (
            argument_component in PRIMITIVE_TYPES
            or parameter_component in PRIMITIVE_TYPES
        ):
            return (
                "compatible"
                if argument_component == parameter_component
                else "incompatible"
            )
        return _java_type_conversion_state(
            model,
            argument_component,
            parameter_component,
            allow_boxing=False,
        )
    if parameter_array:
        return "incompatible"
    if parameter == "java.lang.Object" and argument not in PRIMITIVE_TYPES:
        return "compatible"
    resolved_argument = _resolve_model_type(model, argument)
    resolved_parameter = _resolve_model_type(model, parameter)
    if not resolved_argument or not resolved_parameter:
        return "unknown"
    if _is_subtype(model, resolved_argument, resolved_parameter):
        return "compatible"
    argument_record = _class_record_for_type(model, resolved_argument)
    parameter_record = _class_record_for_type(model, resolved_parameter)
    if argument_record is not None and parameter_record is not None:
        return "incompatible"
    return "unknown"


def _java_expression_static_type(
    model: ProjectModel,
    file_model: JavaFileModel,
    enclosing_method: Optional[MethodRecord],
    node: Node,
) -> str:
    literal_types = {
        "true": "boolean",
        "false": "boolean",
        "character_literal": "char",
        "string_literal": "java.lang.String",
    }
    if node.type in literal_types:
        return literal_types[node.type]
    if node.type == "null_literal":
        return "<null>"
    text = _node_text(file_model.source, node).strip()
    if node.type in {"decimal_integer_literal", "hex_integer_literal", "octal_integer_literal", "binary_integer_literal"}:
        return "long" if re.search(r"[lL]$", text) else "int"
    if node.type in {"decimal_floating_point_literal", "hex_floating_point_literal"}:
        return "float" if re.search(r"[fF]$", text) else "double"
    if node.type == "identifier" and enclosing_method is not None:
        if text in enclosing_method.local_variables:
            return enclosing_method.local_variables[text]
        if text in enclosing_method.parameters:
            return enclosing_method.parameters[text]
        owner = model.classes.get(enclosing_method.owner_qualified_name)
        if owner is not None and text in owner.fields:
            return owner.fields[text]
        return ""
    if node.type in {"cast_expression", "object_creation_expression", "array_creation_expression"}:
        type_node = node.child_by_field_name("type") or _first_type_child(node)
        if type_node is not None:
            resolved = _resolve_type_name(
                file_model,
                _node_text(file_model.source, type_node),
                model.classes_by_simple,
            )
            return f"{resolved}[]" if node.type == "array_creation_expression" else resolved
    if node.type == "parenthesized_expression":
        nested = next((item for item in node.named_children), None)
        if nested is not None:
            return _java_expression_static_type(
                model,
                file_model,
                enclosing_method,
                nested,
            )
    if node.type == "method_invocation" and enclosing_method is not None:
        return_type, resolution = _receiver_static_type(
            model,
            enclosing_method,
            text,
        )
        if resolution != "unresolved":
            return return_type
    return ""


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
    if method.is_constructor or "private" not in method.modifiers:
        return False
    if method.annotations:
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
    prefer_environment_symbols: bool = False,
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
            prefer_environment_symbols=prefer_environment_symbols,
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
    if _model_type_is_ambiguous(model, receiver_info.type_name):
        stats.unresolved += 1
        return
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
    candidates: Dict[str, List[str]] = {}
    for method in owner.methods:
        if method.is_constructor or not method.return_type:
            continue
        candidates.setdefault(method.method_name, []).append(method.return_type)
    # This lightweight resolver cannot distinguish overloads from receiver text
    # alone. Exclude overloaded names instead of freezing the first declaration.
    return {
        name: returns[0]
        for name, returns in candidates.items()
        if len(returns) == 1 and not _is_ambiguous_type(returns[0])
    }


def _receiver_info_for_expression(
    model: ProjectModel,
    method: MethodRecord,
    env: Dict[str, ReceiverInfo],
    owner: Optional[ClassRecord],
    owner_method_returns: Dict[str, str],
    expression: str,
    *,
    prefer_environment_symbols: bool = False,
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
    if prefer_environment_symbols and root in env:
        return env[root]
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


def _member_access_receiver_nodes(body: Node) -> Iterable[Tuple[Node, Node]]:
    for node in _iter_nodes(body):
        if node.type == "field_access":
            receiver = node.children[0] if node.children else None
            if receiver is not None:
                text = _node_text_from_node(receiver).strip()
                if text in {"this", "super"} and _is_receiver_operand(node):
                    continue
                if text:
                    yield node, receiver
        elif node.type == "method_invocation" and len(node.children) >= 3:
            if any(child.type == "." for child in node.children[:3]):
                receiver = node.children[0]
                text = _node_text_from_node(receiver).strip()
                if text in {"this", "super"} and _is_receiver_operand(node):
                    continue
                if text:
                    yield node, receiver


def _member_access_receiver_expressions(body: Node) -> Iterable[str]:
    for _, receiver in _member_access_receiver_nodes(body):
        yield _node_text_from_node(receiver).strip()


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
    return (not cls.superclass_name and not cls.interface_names) or cls.kind == "enum"


def _is_parameter_group_owner_constructor(
    model: ProjectModel,
    method: MethodRecord,
    group_key: str,
) -> bool:
    """Exclude a parameter object's own canonical constructor as a consumer.

    The constructor must own matching fields and directly initialize every
    member of the candidate group. Merely introducing an empty wrapper with a
    matching signature is therefore not enough to reduce the finding.
    """
    if not method.is_constructor or method.body is None:
        return False
    owner = model.classes.get(method.owner_qualified_name)
    if owner is None:
        return False
    group = set(_normalize_qualified_group(str(group_key or "")).split("|"))
    if not group:
        return False
    parameters = {
        _normalize_qualified_group(f"{type_name}:{_stem_name(name)}"): name
        for name, type_name in method.parameters.items()
    }
    fields = {
        _normalize_qualified_group(f"{type_name}:{_stem_name(name)}"): name
        for name, type_name in owner.fields.items()
    }
    if not group.issubset(parameters) or not group.issubset(fields):
        return False
    body_text = method.body_text
    for descriptor in group:
        parameter_name = parameters[descriptor]
        field_name = fields[descriptor]
        assignment = re.compile(
            rf"\bthis\s*\.\s*{re.escape(field_name)}\s*=\s*{re.escape(parameter_name)}\b"
        )
        if not assignment.search(body_text):
            return False
    return True


def _parameter_combinations(values: Sequence[str], group_size: int) -> Iterable[str]:
    seen: Set[str] = set()
    for combo in itertools.combinations(values, group_size):
        key = _normalize_qualified_group("|".join(combo))
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


def _declaration_has_varargs(method_node: Node) -> bool:
    parameters = method_node.child_by_field_name("parameters")
    return bool(
        parameters is not None
        and any(
            child.type == "spread_parameter"
            for child in parameters.named_children
        )
    )


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
    *,
    field_modifiers: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    body = class_node.child_by_field_name("body") or _first_child(class_node, "class_body") or _first_child(class_node, "enum_body")
    if body is None:
        return fields
    for child in _declared_type_members(body):
        if child.type != "field_declaration":
            continue
        type_node = child.child_by_field_name("type") or _first_type_child(child)
        if type_node is None:
            continue
        type_name = _resolve_type_name(file_model, _node_text(file_model.source, type_node), classes_by_simple, type_parameters)
        modifiers = _modifiers(file_model.source, child)
        if class_node.type == "interface_declaration":
            modifiers.update({"public", "static", "final"})
        for item in child.children:
            if item.type != "variable_declarator":
                continue
            name_node = item.child_by_field_name("name")
            if name_node is not None:
                name = _node_text(file_model.source, name_node)
                fields[name] = type_name
                if field_modifiers is not None:
                    field_modifiers[name] = set(modifiers)
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
        first, remainder = base.split(".", 1)
        if first[:1].isupper():
            outer = _resolve_type_name(
                file_model,
                first,
                classes_by_simple,
                type_parameters,
            )
            if outer and not outer.startswith(AMBIGUOUS_TYPE_PREFIX) and outer != first:
                return f"{outer}.{remainder}{suffix}"
        return cleaned
    if base in file_model.imports:
        return f"{file_model.imports[base]}{suffix}"
    if base in classes_by_simple:
        candidates = classes_by_simple[base]
        same_package = [
            cls for cls in candidates
            if _package_of(cls.qualified_name) == file_model.package
        ]
        if len(same_package) == 1:
            return f"{same_package[0].qualified_name}{suffix}"
        if len(same_package) > 1:
            return f"{AMBIGUOUS_TYPE_PREFIX}{base}{suffix}"
        if not file_model.package:
            default_package = [
                cls for cls in candidates if not _package_of(cls.qualified_name)
            ]
            if len(default_package) == 1:
                return f"{default_package[0].qualified_name}{suffix}"
            if len(default_package) > 1:
                return f"{AMBIGUOUS_TYPE_PREFIX}{base}{suffix}"
    if base in JAVA_LANG_TYPES:
        return f"java.lang.{base}{suffix}"
    wildcard_candidates = [
        cls
        for cls in classes_by_simple.get(base, [])
        if _package_of(cls.qualified_name) in {
            *file_model.wildcard_imports,
            *file_model.static_wildcard_imports,
        }
    ]
    if len(wildcard_candidates) == 1:
        return f"{wildcard_candidates[0].qualified_name}{suffix}"
    if len(wildcard_candidates) > 1 or len(file_model.wildcard_imports) > 1:
        return f"{AMBIGUOUS_TYPE_PREFIX}{base}{suffix}"
    if len(file_model.wildcard_imports) == 1:
        return f"{file_model.wildcard_imports[0]}.{base}{suffix}"
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
    return erase_java_type(type_text)


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


def _list_java_files(root: Path) -> List[Path]:
    files: List[Path] = []
    source_layout = discover_java_source_layout(root)
    for path in root.rglob("*.java"):
        if not path.is_file():
            continue
        if _contains_excluded_part(path, DEFAULT_EXCLUDE_PATHS):
            continue
        if source_layout.is_test_path(path):
            continue
        files.append(path)
    return sorted(files)


def _contains_excluded_part(path: Path, exclude_paths: Iterable[str]) -> bool:
    excluded = set(exclude_paths)
    return any(part in excluded for part in path.parts)


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
    erased = _erase_type(type_name)
    if not erased or _is_ambiguous_type(erased):
        return ""
    simple = erased.rsplit(".", 1)[-1]
    candidates = model.classes_by_simple.get(simple, [])
    if "." in erased:
        candidates = [item for item in candidates if item.qualified_name == erased]
    if len(candidates) == 1:
        return candidates[0].qualified_name
    if len(candidates) > 1:
        return ""
    return erased


def _all_parent_type_names(model: ProjectModel, child: ClassRecord) -> Set[str]:
    pending = [
        name for name in [child.superclass_name, *child.interface_names] if name
    ]
    parents: Set[str] = set()
    while pending:
        name = pending.pop()
        resolved = _resolve_model_type(model, name)
        if not resolved:
            continue
        if resolved in parents:
            continue
        parents.add(resolved)
        record = _class_record_for_type(model, resolved)
        if record is not None:
            pending.extend(
                parent
                for parent in [record.superclass_name, *record.interface_names]
                if parent
            )
    return parents


def _is_ambiguous_type(type_name: str) -> bool:
    return _erase_type(type_name).startswith(AMBIGUOUS_TYPE_PREFIX)


def _model_type_is_ambiguous(model: ProjectModel, type_name: str) -> bool:
    erased = _erase_type(type_name)
    if _is_ambiguous_type(erased):
        return True
    simple = erased.rsplit(".", 1)[-1]
    candidates = model.classes_by_simple.get(simple, [])
    if "." in erased:
        candidates = [item for item in candidates if item.qualified_name == erased]
    return len(candidates) > 1


def _is_subtype(model: ProjectModel, child_name: str, parent_name: str) -> bool:
    child = _resolve_model_type(model, child_name)
    parent = _resolve_model_type(model, parent_name)
    if not child or not parent:
        return False
    if child == parent:
        return True
    seen: Set[str] = set()
    pending = [child]
    while pending:
        current_name = pending.pop(0)
        if current_name in seen:
            continue
        seen.add(current_name)
        current = _class_record_for_type(model, current_name)
        if current is None:
            continue
        for declared_parent in [
            current.superclass_name,
            *current.interface_names,
        ]:
            resolved_parent = _resolve_model_type(model, declared_parent)
            if not resolved_parent:
                continue
            if resolved_parent == parent:
                return True
            if resolved_parent not in seen:
                pending.append(resolved_parent)
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


def _failed(
    message: str,
    *,
    unavailable: Optional[Dict[str, object]] = None,
) -> SemanticDetectionResult:
    return SemanticDetectionResult(
        ok=False,
        findings={"feature_envy": [], "refused_bequest": [], "data_clumps": [], "god_class": [], "dead_code": []},
        error=message,
        unavailable=unavailable,
    )
