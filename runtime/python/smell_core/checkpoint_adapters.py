"""Metric adapters for the generic checkpoint contract."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .analysis import (
    count_meaningful_lines,
    count_parameters,
    estimate_complexity,
    extract_class_text,
    extract_function_signature,
    extract_pair_snippets,
    extract_snippet,
    method_basename,
    normalize_for_clone,
    python_switch_metrics,
    iter_function_signatures,
)
from .data_clumps import (
    data_clump_occurrence_threshold as generic_data_clump_occurrence_threshold,
    detect_data_clump_occurrences as detect_generic_data_clump_occurrences,
)
from .feature_envy import analyze_feature_envy_target as analyze_generic_feature_envy_target
from .mysterious_name import (
    detect_mysterious_names as detect_generic_mysterious_names,
)
from .guard_scope import (
    GuardScopeError,
    MAX_GUARD_ANALYSIS_BYTES,
    MAX_GUARD_ANALYSIS_FILES,
)
from .java.source_layout import (
    JavaSourceLayoutError,
    discover_java_source_layout,
    standard_test_root,
)
from .java.ast_ncss import run_ast_ncss
from .java.catalog_identity import (
    CATALOG_IDENTITY_SCHEMA,
    stable_java_method_signature,
    stable_method_record_identity,
    stable_method_record_signature,
)
from .java.clone_closure import analyze_exact_clone_closure
from .java.data_clumps import (
    data_clump_finding_group,
    data_clump_occurrence_payloads,
    data_clump_occurrence_threshold,
    matching_data_clump_groups,
    normalize_data_clump_group,
    same_group_data_clump_findings,
)
from .java.semantic_detector import (
    JAVA_SYMBOL_RESOLVER_ID,
    JAVA_SYMBOL_SNAPSHOT_SCHEMA,
    JavaSymbolClasspathError,
    analyze_data_clump_type_continuity,
    analyze_feature_envy_target,
    build_long_parameter_list_migration_closure,
    build_refused_bequest_impact_map,
    external_symbol_snapshot,
    god_class_product_profile,
    god_class_responsibility_clusters,
    run_java_semantic_detector,
    validate_explicit_symbol_archives,
)
from .java.syntactic_detector import (
    find_matching_syntactic_findings,
    load_project_model,
    method_parameter_type_fingerprint,
    run_java_syntactic_detector,
)
from .java.target_guard import capture_java_target_guard, evaluate_java_target_guard


CHECKPOINT_SMELLS = frozenset({
    "long_method",
    "nested_complexity",
    "long_parameter_list",
    "feature_envy",
    "data_clumps",
    "code_clone_type1",
    "god_class",
    "refused_bequest",
    "switch_statements",
    "mysterious_name",
    "dead_code",
})

LEGACY_DETECTOR_PROFILE_VERSION = "java-oracle-aligned-v3"
PRODUCT_THRESHOLDS = {
    "long_method": 60,
    "long_parameter_list": 6,
    "nested_complexity": 20,
    "code_clone_type1": 30,
    "data_clumps": 3,
}


DETECTOR_PROFILES = {
    "long_method": {
        "metric": "ast_ncss",
        "finding_min": 60,
        "selection_contract": "unique_signature_types_containing_line-v2",
    },
    "long_parameter_list": {"metric": "parameter_count", "finding_min": 6},
    "nested_complexity": {
        "metric": "cognitive_complexity",
        "finding_min": 20,
        "selection_contract": "unique_signature_types_containing_line-v2",
    },
    "switch_statements": {"definition": "target_method_contains_switch"},
    "code_clone_type1": {
        "definition": "exact_contiguous_token_window_in_target_method_pair",
        "finding_min_tokens": 30,
        "selection_contract": "body_window_then_complete_method_window-v2",
        "size_metric": "selected_exact_window_tokens",
        "relocation_check": "target_endpoints_plus_changed_methods_near_copy_count-v2",
        "catalog_identity_schema": CATALOG_IDENTITY_SCHEMA,
    },
    "feature_envy": {
        "definition": "designite_2.8.6_envy_access_diff_alias_provenance_self_symbols",
        "finding_min_exclusive": 1,
        "type_resolution": "two-phase-source-and-classpath-symbols-v4",
        "catalog_identity_schema": CATALOG_IDENTITY_SCHEMA,
    },
    "data_clumps": {
        "minimum_group_size": 3,
        "min_occurrences": 3,
        "min_classes": 3,
        "min_method_names": 2,
        "exclude_parameter_object_owner_constructor": True,
        "track_baseline_parameter_types": True,
        "track_baseline_parameter_positions": True,
        "track_baseline_body_dispersion": True,
        "group_identity": "erased-qualified-type-and-parameter-stem-v5",
        "type_resolution": "target-package-import-and-type-variable-erasure-v5",
        "relation_query": "case-insensitive-fixed-stem-intersection-v1",
        "candidate_evaluation": "streamed-complete-signature-projection-v1",
        "active_parse_file_limit": 1,
        "active_parse_byte_limit": MAX_GUARD_ANALYSIS_BYTES,
    },
    "mysterious_name": {
        "definition": "strict_symbol_name",
        "profile": "strict",
        "selection_contract": "symbol-container-or-structural-scope-v4",
    },
    "refused_bequest": {
        "definition": "method_level_rejecting_override_baseline_delta",
        "hierarchy_resolution": "two-phase-unique-qualified-parent-v4",
        "catalog_identity_schema": CATALOG_IDENTITY_SCHEMA,
    },
    "dead_code": {
        "definition": "unused_private_declaration_refs_zero",
        "selection_contract": "exact_identity_or_containing_line-v2",
    },
    "god_class": {
        "definition": "multi_metric_profile",
        "profile": god_class_product_profile(),
    },
}


_JAVA_GUARD_COMMON_IMPLEMENTATION_FILES = (
    "checkpoint_adapters.py",
    "guard_scope.py",
    "java/source_layout.py",
    "java/target_guard.py",
)


_JAVA_GUARD_IMPLEMENTATION_FILES = {
    "long_method": ("java/target_guard_predicates.py", "java/ast_ncss.py"),
    "nested_complexity": ("java/target_guard_predicates.py", "java/syntactic_detector.py"),
    "long_parameter_list": ("java/target_relational_guards.py", "java/syntactic_detector.py"),
    "feature_envy": (
        "java/target_feature_envy_scope.py",
        "java/target_relation_scope.py",
        "java/target_semantic_guards.py",
        "java/semantic_detector.py",
        "java/catalog_identity.py",
    ),
    "data_clumps": (
        "java/target_relational_guards.py",
        "java/data_clumps.py",
    ),
    "code_clone_type1": (
        "java/target_clone_guard.py",
        "java/clone_closure.py",
    ),
    "god_class": ("java/target_semantic_guards.py", "java/semantic_detector.py"),
    "refused_bequest": (
        "java/target_relation_scope.py",
        "java/target_semantic_guards.py",
        "java/semantic_detector.py",
    ),
    "switch_statements": ("java/target_guard_predicates.py", "java/syntactic_detector.py"),
    "mysterious_name": ("java/target_guard_predicates.py", "java/syntactic_detector.py"),
    "dead_code": ("java/target_semantic_guards.py", "java/semantic_detector.py"),
}


_JAVA_SYMBOL_AWARE_SMELLS = frozenset({
    "long_parameter_list",
    "feature_envy",
    "data_clumps",
    "code_clone_type1",
    "god_class",
    "refused_bequest",
    "mysterious_name",
    "dead_code",
})


def capture_metric_snapshot(config: Any, evidence: str) -> dict[str, Any]:
    """Capture or evaluate one target Guard snapshot.

    ``evidence`` is accepted for CLI compatibility and audit logging only. It
    must never influence candidate discovery, selection, metrics, or verdicts.
    """
    del evidence
    if str(config.language).lower() == "java":
        return _capture_java_guard_snapshot(config)
    adapter = _ADAPTERS.get(str(config.smell))
    if adapter is None:
        return {"ok": False, "adapter": "unsupported", "objectives": {}, "error": "unsupported_smell"}
    try:
        snapshot = adapter(config, "")
    except GuardScopeError as exc:
        return {
            "ok": False,
            "adapter": str(config.smell),
            "objectives": {},
            "error": exc.status,
            "guard_scope": dict(exc.details),
        }
    except Exception as exc:
        return {
            "ok": False,
            "adapter": str(config.smell),
            "objectives": {},
            "error": f"metric adapter failed: {exc}",
        }
    snapshot.setdefault("adapter", str(config.smell))
    snapshot.setdefault("objectives", {})
    snapshot.setdefault("ok", bool(snapshot["objectives"]))
    snapshot.setdefault("detector_profile", detector_profile_for(config))
    if snapshot.get("ok") is True and "candidate_count" not in snapshot:
        snapshot["ok"] = False
        snapshot.setdefault("error", "DETECTOR_CANDIDATE_COUNT_UNAVAILABLE")
    return snapshot


def _capture_java_guard_snapshot(config: Any) -> dict[str, Any]:
    """Run one Java predicate without source or smell discovery."""
    try:
        for location in config.locations:
            try:
                relative = location.file_path.resolve().relative_to(
                    config.project_root.expanduser().resolve()
                ).as_posix()
            except (OSError, ValueError):
                relative = str(location.project_path or location.file_path)
            if standard_test_root(relative) is not None:
                return {
                    "ok": False,
                    "adapter": str(config.smell),
                    "objectives": {},
                    "error": "TARGET_NOT_PRODUCTION_SOURCE",
                    "target": relative,
                }
        snapshot = (
            evaluate_java_target_guard(config)
            if bool(getattr(config, "guard_contract", {}) or {})
            else capture_java_target_guard(config)
        )
    except GuardScopeError as exc:
        return {
            "ok": False,
            "adapter": str(config.smell),
            "objectives": {},
            "error": exc.status,
            "guard_scope": dict(exc.details),
        }
    except Exception as exc:
        return {
            "ok": False,
            "adapter": str(config.smell),
            "objectives": {},
            "error": f"guard evaluation failed: {exc}",
        }
    snapshot.setdefault("adapter", str(config.smell))
    snapshot.setdefault("objectives", {})
    snapshot.setdefault("ok", bool(snapshot["objectives"]))
    guard_profile = detector_profile_for(config)
    snapshot.setdefault("guard_profile", guard_profile)
    # Wire alias for existing compact result consumers; c000 freezes only the
    # canonical guard_profile field.
    snapshot.setdefault("detector_profile", guard_profile)
    if snapshot.get("ok") is True and "target_match_count" not in snapshot:
        snapshot["ok"] = False
        snapshot.setdefault("error", "GUARD_TARGET_MATCH_COUNT_UNAVAILABLE")
    return snapshot


def _detector_unavailable_snapshot(
    config: Any,
    unavailable: dict[str, object],
) -> dict[str, Any]:
    """Normalize detector prerequisites into one fail-closed snapshot."""
    return {
        "ok": False,
        "adapter": str(config.smell),
        "objectives": {},
        "error": "GUARD_UNAVAILABLE",
        "unavailable": dict(unavailable),
        "target_smell_present": False,
        "target_match_count": 0,
        "entity_identity": {},
        "witness": {},
        "guard_violations": [],
        "finding_present": False,
        "candidate_count": 0,
    }


def detector_profile_for(config: Any) -> dict[str, Any]:
    """Return one immutable Guard profile per Java smell.

    A profile is Guard configuration, not task evidence.  Its identifier
    changes only when that smell's product semantics change; the canonical hash
    makes accidental implementation drift visible to checkpoint verification.
    """
    smell = str(config.smell)
    language = str(config.language).lower()
    if language == "java":
        profile = {
            "id": f"java-target-guard/{smell}/v5",
            "schema": 5,
            "language": "java",
            "smell": smell,
            "source_layout": "static-build-descriptor-roles-v4",
            "selector_input": "caller-target-context-only-v5",
            "scope": "target-base-plus-smell-exact-relations-v3",
            "scope_file_limit": MAX_GUARD_ANALYSIS_FILES,
            "scope_byte_limit": MAX_GUARD_ANALYSIS_BYTES,
            "source_discovery": "forbidden",
            "smell_discovery": "forbidden",
            "smell_evidence": "audit-only",
            **DETECTOR_PROFILES.get(smell, {}),
        }
        if smell == "data_clumps":
            profile["scope"] = "target-plus-streamed-parameter-relations-v4"
            profile.pop("scope_file_limit", None)
            profile.pop("scope_byte_limit", None)
    else:
        # Non-Java adapters are outside the Java product-profile migration;
        # preserve their existing checkpoint identity byte-for-byte.
        profile = {
            "version": LEGACY_DETECTOR_PROFILE_VERSION,
            "smell": smell,
            "language": language,
            **DETECTOR_PROFILES.get(smell, {}),
        }
    if smell == "feature_envy":
        # Relocation analysis is implemented only by the Java target Guard.
        profile["reject_finding_relocation_in_impact_cone"] = language == "java"
    if language != "java":
        return profile
    profile["implementation"] = _java_guard_implementation_profile(smell)
    return profile


def _java_guard_implementation_profile(smell: str) -> dict[str, Any]:
    """Hash the executable target-Guard surface so drift recaptures c000."""
    package_root = Path(__file__).resolve().parent
    relative_files = (
        *_JAVA_GUARD_COMMON_IMPLEMENTATION_FILES,
        *_JAVA_GUARD_IMPLEMENTATION_FILES.get(smell, ()),
    )
    files: list[dict[str, str]] = []
    digest = hashlib.sha256()
    for relative in dict.fromkeys(relative_files):
        path = package_root / relative
        content = path.read_bytes()
        content_sha256 = hashlib.sha256(content).hexdigest()
        files.append({"path": relative, "sha256": content_sha256})
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\n")
    return {
        "files": files,
        "sha256": digest.hexdigest(),
    }


def _identity(config: Any, target: Any, **extra: Any) -> dict[str, Any]:
    identity = {
        "smell": str(config.smell),
        "file": str(target.project_path).replace("\\", "/"),
        "method": str(target.method or ""),
        "class": str(target.class_name or ""),
    }
    identity.update({key: value for key, value in extra.items() if value not in (None, "")})
    return identity


def _contract_identity(config: Any) -> dict[str, Any]:
    contract = getattr(config, "finding_contract", None)
    if not isinstance(contract, dict):
        return {}
    identity = contract.get("entity_identity")
    return dict(identity) if isinstance(identity, dict) else {}


def _selector_context(config: Any) -> dict[str, Any]:
    value = getattr(config, "target_context", None)
    return dict(value) if isinstance(value, dict) else {}


def _java_symbol_classpath(config: Any) -> str:
    entries: list[str] = [str(config.project_root)]
    override = getattr(config, "project_override", None)
    override_root = getattr(override, "root", None)
    if override_root:
        entries.append(str(override_root))
    env = getattr(config, "env", None)
    if isinstance(env, dict):
        for key in ("GRADLE_USER_HOME", "MAVEN_USER_HOME", "BUILDENV", "JAVA_HOME"):
            value = str(env.get(key) or "").strip()
            if value:
                entries.append(value)
    return os.pathsep.join(dict.fromkeys(entries))


def _java_symbol_resolution_snapshot(config: Any) -> dict[str, Any]:
    """Capture mutable resolver state for diagnostics without gating a verdict."""
    try:
        return external_symbol_snapshot(
            config.project_root,
            _java_symbol_classpath(config),
        )
    except JavaSymbolClasspathError as exc:
        return {
            "snapshot_schema": JAVA_SYMBOL_SNAPSHOT_SCHEMA,
            "resolver": JAVA_SYMBOL_RESOLVER_ID,
            "available": False,
            "unavailable": exc.to_unavailable(),
        }
    except Exception as exc:
        return {
            "snapshot_schema": JAVA_SYMBOL_SNAPSHOT_SCHEMA,
            "resolver": JAVA_SYMBOL_RESOLVER_ID,
            "available": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


def _java_semantic_detection(config: Any) -> Any:
    """Run the product detector with read-only build symbol roots."""
    return run_java_semantic_detector(
        config.project_root,
        classpath=_java_symbol_classpath(config),
    )


def _same_method(left: str, right: str) -> bool:
    return method_basename(left).lower() == method_basename(right).lower()


def _simple_type(value: str) -> str:
    return str(value or "").strip().split("<", 1)[0].rsplit(".", 1)[-1].lower()


def _product_method_key(method: Any) -> str:
    return "#".join(
        (
            str(method.file).replace("\\", "/").lstrip("/"),
            str(method.class_name or ""),
            re.sub(r"\s+", "", str(method.method_signature or "")),
        )
    )


def _signature_parameter_types(fingerprints: list[str]) -> list[str]:
    return [str(item).rsplit(":", 1)[0] for item in fingerprints]


def _declaration_key(config: Any, signature: Any) -> str:
    relative = signature.file_path.resolve().relative_to(config.project_root.resolve()).as_posix()
    return "#".join(
        (
            relative,
            str(signature.name or ""),
            ",".join(_signature_parameter_types(signature.parameter_fingerprints)),
        )
    )


def _lpl_declaration_owner(signature: Any, classes: list[Any]) -> str:
    enclosing = [
        item for item in classes
        if int(item.begin_line) <= int(signature.start_line) <= int(item.end_line)
    ]
    if not enclosing:
        return ""
    owner = min(
        enclosing,
        key=lambda item: (
            int(item.end_line) - int(item.begin_line),
            -int(item.begin_line),
        ),
    )
    return str(owner.class_name or "")


def _same_lpl_owner(left: str, right: str) -> bool:
    frozen = str(left or "").strip().replace("$", ".")
    current = str(right or "").strip().replace("$", ".")
    return bool(frozen and current and frozen == current)


def _lpl_declaration_key(config: Any, signature: Any, owner: str) -> str:
    return "#".join((_declaration_key(config, signature), str(owner or "")))


def _semantic_identity(config: Any, finding: Any, **extra: Any) -> dict[str, Any]:
    identity = {
        "smell": str(config.smell),
        "file": str(finding.file).replace("\\", "/"),
        "class": str(finding.class_name or ""),
        "method": str(finding.method or ""),
        "rule_id": str(finding.rule_id or ""),
    }
    identity.update({key: value for key, value in extra.items() if value not in (None, "")})
    return identity


def _feature_envy_method_record(project_model: Any, finding: Any) -> Any | None:
    records = [
        method
        for method in project_model.methods
        if _same_file(method.file, finding.file)
        and str(method.class_name or "") == str(finding.class_name or "")
        and str(method.method_name or "") == method_basename(str(finding.method or ""))
        and int(method.begin_line or 0) == int(finding.begin_line or 0)
    ]
    return records[0] if len(records) == 1 else None


def _feature_envy_source_method_signature(project_model: Any, finding: Any) -> str:
    """Map a finding back to its grammar-owned lexical method signature."""
    record = _feature_envy_method_record(project_model, finding)
    return stable_method_record_signature(record) if record is not None else ""


def _feature_envy_semantic_method_signature(value: Any) -> str:
    """Return a parameter-name-free signature using detector-resolved types."""
    return stable_java_method_signature(
        value,
        preserve_source_qualification=True,
    )


def _feature_envy_identity_method_record(
    project_model: Any,
    identity: dict[str, Any],
) -> Any | None:
    file_name = str(identity.get("file") or "")
    class_name = str(identity.get("class") or "")
    method_identity = str(identity.get("method") or "")
    if not file_name or not class_name or not method_identity:
        return None
    records = [
        method
        for method in project_model.methods
        if _same_file(method.file, file_name)
        and str(method.owner_qualified_name or method.class_name or "") == class_name
        and _feature_envy_semantic_method_signature(method.method_signature)
        == method_identity
    ]
    return records[0] if len(records) == 1 else None


def _feature_envy_method_identity(
    config: Any,
    finding: Any,
    project_model: Any,
) -> dict[str, Any]:
    """Return the stable method-level identity of one Feature Envy finding.

    The detector emits at most one Feature Envy finding per method.  The
    dominant receiver and field are metric contributors and may legitimately
    change while that same method-level finding remains, so they must not be
    part of the frozen finding identity.
    """
    record = _feature_envy_method_record(project_model, finding)
    return {
        "smell": str(config.smell),
        "file": str(finding.file).replace("\\", "/"),
        "class": str(
            record.owner_qualified_name or record.class_name or ""
            if record is not None
            else ""
        ),
        "method": _feature_envy_semantic_method_signature(finding.method),
    }


def _feature_envy_method_candidates(
    config: Any,
    findings: list[Any],
    identity: dict[str, Any],
    project_model: Any,
) -> list[Any]:
    """Select a detector finding by source method, never by receiver metrics."""
    target = _target(config)
    if identity:
        file_name = str(identity.get("file") or "")
        class_name = str(identity.get("class") or "")
        method = str(identity.get("method") or "")
        if not file_name or not class_name or not method:
            return []
        return [
            item
            for item in findings
            if _same_file(item.file, file_name)
            and (
                (record := _feature_envy_method_record(project_model, item))
                is not None
            )
            and str(record.owner_qualified_name or record.class_name or "")
            == class_name
            and _feature_envy_semantic_method_signature(item.method) == method
        ]

    candidates = [
        item
        for item in findings
        if _same_file(item.file, target.project_path)
        and (
            not target.class_name
            or str(item.class_name or "") == str(target.class_name)
        )
    ]
    raw_method = str(target.method or "").strip()
    if raw_method:
        stable_method = stable_java_method_signature(
            raw_method,
            preserve_source_qualification=True,
        )
        exact_candidates = [
            item
            for item in candidates
            if (
                _feature_envy_source_method_signature(project_model, item) == stable_method
                if "(" in raw_method
                else _same_method(item.method, raw_method)
            )
        ]
        if "(" not in raw_method or exact_candidates:
            candidates = exact_candidates
        else:
            semantic_candidates = [
                item
                for item in candidates
                if _feature_envy_semantic_method_signature(item.method)
                == stable_method
            ]
            if semantic_candidates:
                candidates = (
                    semantic_candidates if len(semantic_candidates) == 1 else []
                )
                if len(candidates) > 1 and target.line:
                    candidates = [
                        item
                        for item in candidates
                        if int(item.begin_line or 0)
                        <= int(target.line)
                        <= int(item.end_line or 0)
                    ]
                return candidates
            # A selector may carry resolver-added qualification while the
            # grammar-owned identity keeps lexical source types.  It may narrow
            # only when that qualification-insensitive view is still unique.
            unqualified_target = stable_java_method_signature(raw_method)
            relaxed_candidates = [
                item
                for item in candidates
                if stable_java_method_signature(
                    _feature_envy_source_method_signature(project_model, item)
                ) == unqualified_target
            ]
            if len(relaxed_candidates) > 1 and target.line:
                relaxed_candidates = [
                    item
                    for item in relaxed_candidates
                    if int(item.begin_line or 0)
                    <= int(target.line)
                    <= int(item.end_line or 0)
                ]
            candidates = relaxed_candidates if len(relaxed_candidates) == 1 else []
    if len(candidates) > 1 and target.line:
        candidates = [
            item
            for item in candidates
            if int(item.begin_line or 0) <= int(target.line) <= int(item.end_line or 0)
        ]
    return candidates


def _target(config: Any) -> Any:
    if not config.locations:
        raise ValueError("target location is missing")
    return config.locations[0]


def _with_source_method_identities(
    findings: list[Any],
    *,
    source_file: Any,
    project_root: Any,
) -> list[Any]:
    """Attach source signatures to metric findings without positional guessing."""
    _, methods = load_project_model(project_root, [source_file])
    enriched: list[Any] = []
    for finding in findings:
        finding_name = method_basename(str(finding.method or ""))
        owners = [
            method
            for method in methods
            if _same_file(method.rel_path, finding.file)
            and method.method_name == finding_name
            and method.begin_line <= int(finding.end_line or finding.begin_line)
            and int(finding.begin_line) <= method.end_line
        ]
        if len(owners) == 1:
            owner = owners[0]
            enriched.append(
                replace(
                    finding,
                    class_name=owner.class_name,
                    method=owner.signature,
                )
            )
        else:
            enriched.append(finding)
    return enriched


def _matching_syntactic(
    config: Any,
    smell: str,
    thresholds: dict[str, object],
    evidence: str,
) -> dict[str, Any]:
    target = _target(config)
    if not target.file_path.is_file():
        return {
            "ok": True,
            "objectives": {_objective_name(smell): 0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": _identity(config, target),
        }
    if config.language != "java":
        snippet = extract_snippet(target, config.language)
        if snippet is None:
            return {
                "ok": True,
                "detector": "tree_sitter_generic",
                "objectives": {_objective_name(smell): 0},
                "target_missing": True,
                "finding_present": False,
                "candidate_count": 0,
                "finding_identity": _identity(config, target),
            }
        score = {
            "long_method": count_meaningful_lines(snippet.body_text, config.language),
            "nested_complexity": estimate_complexity(snippet, config.language),
            "long_parameter_list": count_parameters(snippet.signature_text, config.language),
        }[smell]
        threshold = float(PRODUCT_THRESHOLDS[smell])
        finding_present = score >= threshold
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {_objective_name(smell): float(score)},
            "target_missing": False,
            "finding_present": finding_present,
            "candidate_count": 1,
            "finding_identity": _identity(config, target),
        }
    if smell == "long_parameter_list":
        signature = extract_function_signature(target, "java")
        if signature is None:
            return {
                "ok": True,
                "detector": "java_target_signature",
                "objectives": {"parameter_count": 0},
                "target_missing": True,
                "finding_present": False,
                "candidate_count": 0,
                "finding_identity": _identity(config, target),
            }
        score = count_parameters(signature.signature_text, "java")
        return {
            "ok": True,
            "detector": "java_target_signature",
            "objectives": {"parameter_count": float(score)},
            "target_missing": False,
            "finding_present": score >= PRODUCT_THRESHOLDS["long_parameter_list"],
            "candidate_count": 1,
            "finding_identity": _identity(config, target),
        }
    if smell == "long_method":
        detection = run_ast_ncss(target.file_path, config.project_root, -1)
        findings = (
            _with_source_method_identities(
                detection.findings,
                source_file=target.file_path,
                project_root=config.project_root,
            )
            if detection.ok
            else detection.findings
        )
        error = detection.error
        ok = detection.ok
        detector = "java_ast_ncss"
    else:
        detection = run_java_syntactic_detector(
            config.project_root,
            target_files=[target.file_path],
            thresholds=thresholds,
            include_mysterious_name=False,
        )
        findings = detection.findings.get(smell, [])
        error = detection.error
        ok = detection.ok
        detector = "java_syntactic_detector"
    if not ok:
        return {"ok": False, "objectives": {}, "detector": detector, "error": error}
    contract_identity = _contract_identity(config)
    method = str(contract_identity.get("method") or target.method or "")
    class_name = str(contract_identity.get("class") or target.class_name or "")
    parameter_type_fingerprint = (
        str(contract_identity["parameter_type_fingerprint"])
        if "parameter_type_fingerprint" in contract_identity
        else method_parameter_type_fingerprint(method)
    )
    candidates = find_matching_syntactic_findings(
        findings,
        target_file=target.file_path,
        project_root=config.project_root,
        method=method,
        line=target.line,
        class_name=class_name,
        original_param_type_fingerprint=parameter_type_fingerprint,
    )
    match = candidates[0] if len(candidates) == 1 else None
    score = float(match.score) if match is not None else 0.0
    finding_present = bool(
        match is not None and score >= float(PRODUCT_THRESHOLDS[smell])
    )
    return {
        "ok": True,
        "detector": detector,
        "objectives": {_objective_name(smell): score},
        "target_missing": not candidates,
        "finding_present": finding_present,
        "candidate_count": len(candidates),
        "finding_identity": (
            {
                **_identity(
                    config,
                    target,
                    method=match.method,
                    rule_id=match.rule_id,
                ),
                "class": str(match.class_name or class_name),
                "parameter_type_fingerprint": method_parameter_type_fingerprint(
                    match.method
                ),
            }
            if match is not None
            else contract_identity or _identity(config, target, method=method)
        ),
    }


def _objective_name(smell: str) -> str:
    return {
        "long_method": "ast_ncss",
        "nested_complexity": "cognitive_complexity",
        "long_parameter_list": "parameter_count",
    }[smell]


def _long_method(config: Any, evidence: str) -> dict[str, Any]:
    return _matching_syntactic(config, "long_method", {}, evidence)


def _nested_complexity(config: Any, evidence: str) -> dict[str, Any]:
    return _matching_syntactic(config, "nested_complexity", {"cognitive_complexity": -1}, evidence)


def _lpl_migration_closure(
    config: Any,
    *,
    model: Any,
    target_file: str | Path,
    method: str,
    parameter_types: list[str],
    target_class_name: str,
    target_line: int | None = None,
) -> dict[str, Any]:
    file_path = Path(target_file)
    if not file_path.is_absolute():
        file_path = config.project_root / file_path
    return build_long_parameter_list_migration_closure(
        config.project_root,
        target_file=file_path,
        method=method,
        parameter_types=parameter_types,
        target_class_name=target_class_name,
        target_line=target_line,
        project_model=model,
    )


def _long_parameter_list(config: Any, evidence: str) -> dict[str, Any]:
    if config.language == "java":
        target = _target(config)
        identity = _contract_identity(config)
        detection = _java_semantic_detection(config)
        if not detection.ok or detection.project_model is None:
            return {
                "ok": False,
                "detector": "python_semantic_detector",
                "objectives": {},
                "error": detection.error or "production_detection_session_unavailable",
            }
        model = detection.project_model
        target_file = str(identity.get("file") or target.project_path)
        target_method = method_basename(str(identity.get("method") or target.method or ""))
        frozen_types = [str(item) for item in identity.get("parameter_types", [])] if isinstance(
            identity.get("parameter_types"), list
        ) else []
        source_layout = discover_java_source_layout(config.project_root)
        declarations = [
            signature
            for signature in iter_function_signatures(config.project_root, "java")
            if not source_layout.is_test_path(signature.file_path)
            and _same_file(
                signature.file_path.resolve().relative_to(config.project_root.resolve()).as_posix(),
                target_file,
            )
        ]
        target_source_file = config.project_root / target_file
        declaration_classes: list[Any] = []
        if target_source_file.is_file():
            declaration_classes, _ = load_project_model(
                config.project_root,
                [target_source_file],
            )
        candidates = [
            signature for signature in declarations
            if (not target_method or signature.name == target_method)
            and (
                not frozen_types
                or _signature_parameter_types(signature.parameter_fingerprints) == frozen_types
            )
        ]
        if not identity and target.line:
            containing = [
                signature for signature in candidates
                if signature.start_line <= int(target.line) <= signature.end_line
            ]
            if containing:
                candidates = containing
        signature = candidates[0] if len(candidates) == 1 else None
        if signature is None and identity:
            # The old long signature may disappear only through one explicit,
            # same-owner short successor. Multiple overloads are ambiguous and
            # no name/line fallback is attempted.
            frozen_owner = str(identity.get("class") or target.class_name or "")
            successors = [
                item for item in declarations
                if item.name == target_method
                and _same_lpl_owner(
                    frozen_owner,
                    _lpl_declaration_owner(item, declaration_classes),
                )
                and len(item.parameter_fingerprints) < PRODUCT_THRESHOLDS["long_parameter_list"]
            ]
            frozen_short = {
                str(item)
                for item in identity.get("baseline_short_overloads", [])
            } if isinstance(identity.get("baseline_short_overloads"), list) else set()
            successors = [
                item for item in successors
                if _lpl_declaration_key(
                    config,
                    item,
                    _lpl_declaration_owner(item, declaration_classes),
                ) not in frozen_short
            ]
            successor = successors[0] if len(successors) == 1 else None
            migration_closure = _lpl_migration_closure(
                config,
                model=model,
                target_file=target_file,
                method=target_method,
                parameter_types=frozen_types,
                target_class_name=str(identity.get("class") or target.class_name or ""),
            )
            return {
                "ok": True,
                "detector": "python_semantic_detector",
                "objectives": {"parameter_count": 0},
                "measured_parameter_count": 0,
                "target_missing": True,
                "target_absence_allowed": successor is not None,
                "successor_candidate_count": len(successors),
                "successor": (
                    {
                        "file": successor.file_path.resolve().relative_to(config.project_root.resolve()).as_posix(),
                        "class": _lpl_declaration_owner(successor, declaration_classes),
                        "method": successor.name,
                        "parameter_types": _signature_parameter_types(successor.parameter_fingerprints),
                    }
                    if successor is not None
                    else {}
                ),
                "finding_present": False,
                "candidate_count": 0,
                "finding_identity": identity,
                "migration_closure": migration_closure,
            }
        if signature is None:
            migration_closure = _lpl_migration_closure(
                config,
                model=model,
                target_file=target_file,
                method=target_method,
                parameter_types=frozen_types,
                target_class_name=str(target.class_name or ""),
            )
            return {
                "ok": True,
                "detector": "python_semantic_detector",
                "objectives": {"parameter_count": 0},
                "target_missing": True,
                "finding_present": False,
                "candidate_count": len(candidates),
                "finding_identity": identity or _identity(config, target),
                "migration_closure": migration_closure,
            }
        parameter_count = len(signature.parameter_fingerprints)
        signature_types = _signature_parameter_types(signature.parameter_fingerprints)
        migration_closure = _lpl_migration_closure(
            config,
            model=model,
            target_file=signature.file_path,
            method=signature.name,
            parameter_types=signature_types,
            target_class_name=str(identity.get("class") or target.class_name or ""),
            target_line=int(signature.start_line),
        )
        closure_target = migration_closure.get("target")
        closure_target = closure_target if isinstance(closure_target, dict) else {}
        return {
            "ok": True,
            "detector": "python_semantic_detector",
            "objectives": {
                "parameter_count": parameter_count,
            },
            "measured_parameter_count": parameter_count,
            "target_missing": False,
            "target_method": signature.name,
            "target_start_line": signature.start_line,
            "target_signature": signature.signature_text,
            "migration_closure": migration_closure,
            "finding_present": parameter_count >= PRODUCT_THRESHOLDS["long_parameter_list"],
            "candidate_count": 1 if parameter_count >= PRODUCT_THRESHOLDS["long_parameter_list"] else 0,
            "finding_identity": {
                **_identity(config, target),
                "file": signature.file_path.resolve().relative_to(config.project_root.resolve()).as_posix(),
                "method": signature.name,
                "class": str(closure_target.get("class") or target.class_name or ""),
                "parameter_types": signature_types,
                "baseline_short_overloads": sorted(
                    _lpl_declaration_key(
                        config,
                        item,
                        _lpl_declaration_owner(item, declaration_classes),
                    )
                    for item in declarations
                    if item.name == signature.name
                    and _same_lpl_owner(
                        str(closure_target.get("class") or target.class_name or ""),
                        _lpl_declaration_owner(item, declaration_classes),
                    )
                    and len(item.parameter_fingerprints) < PRODUCT_THRESHOLDS["long_parameter_list"]
                ),
            },
        }
    return _matching_syntactic(config, "long_parameter_list", {"long_parameter_list": -1}, evidence)


def _switch_statements(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    if config.language == "java":
        identity = _contract_identity(config)
        selector = _selector_context(config)
        if not target.file_path.is_file():
            return {
                "ok": True,
                "detector": "java_syntactic_detector",
                "objectives": {
                    "switch_case_count": 0,
                    "switch_density": 0.0,
                    "switch_count": 0,
                },
                "target_missing": True,
                "finding_present": False,
                "candidate_count": 0,
                "finding_identity": identity or _identity(config, target),
            }
        detection = run_java_syntactic_detector(
            config.project_root,
            target_files=[target.file_path],
            include_mysterious_name=False,
        )
        if not detection.ok:
            return {
                "ok": False,
                "detector": "java_syntactic_detector",
                "objectives": {},
                "error": detection.error,
            }
        candidates = [
            item
            for item in detection.findings.get("switch_statements", [])
            if _same_file(item.file, str(identity.get("file") or target.project_path))
        ]
        if identity:
            frozen_class = str(identity.get("class") or "")
            frozen_method = re.sub(r"\s+", "", str(identity.get("method") or ""))
            frozen_rule = str(identity.get("rule_id") or "")
            candidates = [
                item
                for item in candidates
                if (not frozen_class or str(item.class_name or "") == frozen_class)
                and (
                    not frozen_method
                    or re.sub(r"\s+", "", str(item.method or "")) == frozen_method
                )
                and (not frozen_rule or str(item.rule_id or "") == frozen_rule)
            ]
        else:
            target_class = str(selector.get("target_class") or target.class_name or "")
            if target_class:
                candidates = [
                    item
                    for item in candidates
                    if _simple_type(item.class_name) == _simple_type(target_class)
                ]
            raw_method = str(target.method or "").strip()
            if raw_method:
                candidates = [
                    item
                    for item in candidates
                    if (
                        re.sub(r"\s+", "", str(item.method or ""))
                        == re.sub(r"\s+", "", raw_method)
                        if "(" in raw_method
                        else _same_method(item.method, raw_method)
                    )
                ]
            if len(candidates) > 1 and target.line:
                candidates = [
                    item
                    for item in candidates
                    if item.begin_line <= int(target.line) <= item.end_line
                ]
        selected_candidate_count = len(candidates)
        match = candidates[0] if selected_candidate_count == 1 else None
        declaration_candidate_count = 1 if match is not None and not identity else 0
        if identity:
            _, source_methods = load_project_model(
                config.project_root,
                [target.file_path],
            )
            frozen_file = str(identity.get("file") or "")
            frozen_class = str(identity.get("class") or "")
            frozen_signature = re.sub(
                r"\s+", "", str(identity.get("method") or "")
            )
            declarations = [
                method
                for method in source_methods
                if _same_file(method.rel_path, frozen_file)
                and str(method.class_name or "") == frozen_class
                and re.sub(r"\s+", "", str(method.signature or ""))
                == frozen_signature
            ]
            declaration_candidate_count = len(declarations)
        metric_source = match or (candidates[0] if candidates else None)
        switch_count = int(metric_source.switch_count) if metric_source is not None else 0
        case_count = int(metric_source.switch_case_count) if metric_source is not None else 0
        density = float(metric_source.switch_density) if metric_source is not None else 0.0
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {
                "switch_case_count": float(case_count),
                "switch_density": round(density, 6),
                "switch_count": float(switch_count),
            },
            "switch_count": switch_count,
            "finding_present": match is not None,
            "candidate_count": selected_candidate_count,
            "declaration_candidate_count": declaration_candidate_count,
            "selection_reason": (
                "MATCHED"
                if match is not None
                else "AMBIGUOUS"
                if selected_candidate_count > 1
                else "NOT_FOUND"
            ),
            # Resolution requires both zero matching product findings and the
            # exact frozen declaration to remain uniquely present. There is no
            # nearest method, moved-method, or structural-fingerprint fallback.
            "target_missing": (
                selected_candidate_count > 1
                or (bool(identity) and declaration_candidate_count != 1)
            ),
            "finding_identity": (
                _semantic_identity(config, match)
                if match is not None
                else identity or _identity(config, target)
            ),
        }
    if not target.file_path.is_file():
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"switch_case_count": 0, "switch_density": 0.0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": _identity(config, target),
        }
    snippet = extract_snippet(target, config.language)
    if snippet is None:
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"switch_case_count": 0, "switch_density": 0.0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": _identity(config, target),
        }
    # Python has no switch; count dispatch branches (if/elif chains, match
    # statements) via tree-sitter.  Keep this existing non-Java contract.
    switch_count, case_count, density = python_switch_metrics(snippet)
    detector = "tree_sitter_generic"
    return {
        "ok": True,
        "detector": detector,
        "objectives": {
            "switch_case_count": float(case_count),
            "switch_density": round(float(density), 6),
            "switch_count": float(switch_count),
        },
        "switch_count": switch_count,
        "finding_present": switch_count > 0,
        "candidate_count": 1 if switch_count > 0 else 0,
        "finding_identity": _identity(config, target),
    }


def _mysterious_name(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    contract_identity = _contract_identity(config)
    selector = _selector_context(config)
    kind = str(contract_identity.get("symbol_kind") or selector.get("symbol_kind") or "")
    name = str(contract_identity.get("symbol_name") or selector.get("symbol_name") or "")
    selector_class = str(selector.get("target_class") or "")
    selector_method = str(selector.get("container_method") or "")
    if config.language == "java":
        if not target.file_path.is_file():
            return {
                "ok": True,
                "detector": "java_syntactic_detector",
                "objectives": {"target_suspicious_name_present": 0},
                "target_missing": True,
                "finding_present": False,
                "candidate_count": 0,
                "finding_identity": contract_identity or _identity(config, target),
            }
        detection = run_java_syntactic_detector(
            config.project_root,
            target_files=[target.file_path],
            include_mysterious_name=True,
            thresholds={"mysterious_name_profile": "strict"},
        )
        if not detection.ok:
            return {
                "ok": False,
                "detector": "java_syntactic_detector",
                "objectives": {},
                "error": detection.error,
            }
        candidates = [
            item
            for item in detection.findings.get("mysterious_name", [])
            if _same_file(
                item.file,
                str(contract_identity.get("file") or target.project_path),
            )
        ]
        if contract_identity:
            frozen_class = str(contract_identity.get("class") or "")
            frozen_method = str(
                contract_identity.get("container_method")
                or contract_identity.get("method")
                or ""
            )
            frozen_rule = str(contract_identity.get("rule_id") or "")
            candidates = [
                item
                for item in candidates
                if (item.symbol_kind, item.symbol_name) == (kind, name)
                and (not frozen_class or str(item.class_name or "") == frozen_class)
                and (
                    not frozen_method
                    or _same_mysterious_container(item.method, frozen_method)
                )
                and (not frozen_rule or str(item.rule_id or "") == frozen_rule)
            ]
        else:
            if kind or name:
                candidates = [
                    item
                    for item in candidates
                    if (not kind or item.symbol_kind == kind)
                    and (not name or item.symbol_name == name)
                ]
            target_class = selector_class or str(target.class_name or "")
            if target_class:
                candidates = [
                    item for item in candidates
                    if _simple_type(item.class_name) == _simple_type(target_class)
                ]
            raw_method = selector_method or str(target.method or "").strip()
            if raw_method:
                candidates = [
                    item for item in candidates
                    if (
                        _same_mysterious_container(item.method, raw_method)
                        if "(" in raw_method
                        else _same_method(item.method, raw_method)
                    )
                ]
            # A container method is a stable selector for a nested symbol; its
            # first line is not that local/parameter's declaration line.
            if target.line and not raw_method:
                target_line = int(target.line)
                candidates = [
                    item for item in candidates
                    if (
                        item.begin_line <= target_line <= item.end_line
                        or (
                            str(item.method or "").startswith("<initializer:")
                            and target_line in item.scope_starts
                        )
                    )
                ]
            elif not raw_method:
                candidates = []
        selected_candidate_count = len(candidates)
        match = candidates[0] if len(candidates) == 1 else None
        matched_kind, matched_name = (
            (match.symbol_kind, match.symbol_name) if match is not None else (kind, name)
        )
        scope = (
            _mysterious_name_scope(
                config,
                match=match,
                identity=contract_identity,
                kind=matched_kind,
                name=matched_name,
            )
            if selected_candidate_count <= 1
            else {"ok": False, "absence_allowed": False}
        )
        scope_error = str(scope.get("error") or "")
        if selected_candidate_count <= 1 and (
            scope_error
            or (match is not None and scope.get("ok") is not True)
        ):
            unavailable = scope.get("unavailable")
            return {
                "ok": False,
                "detector": "java_syntactic_detector",
                "objectives": {},
                "error": scope_error or "DETECTOR_SCOPE_UNAVAILABLE",
                "candidate_count": selected_candidate_count,
                **(
                    {"unavailable": dict(unavailable)}
                    if isinstance(unavailable, dict)
                    else {}
                ),
            }
        selection_reason = (
            "MATCHED"
            if match is not None
            else "AMBIGUOUS"
            if selected_candidate_count > 1
            else "NOT_FOUND"
        )
        target_missing = match is None
        finding_identity = (
            _semantic_identity(
                config,
                match,
                symbol_kind=matched_kind,
                symbol_name=matched_name,
                container_method=match.method,
                **dict(scope.get("frozen_identity") or {}),
            )
            if match is not None
            else contract_identity or _identity(config, target, symbol_kind=kind, symbol_name=name)
        )
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"target_suspicious_name_present": 1 if match else 0},
            "finding_present": match is not None,
            "candidate_count": selected_candidate_count,
            "selection_reason": selection_reason,
            "finding_identity": finding_identity,
            "target_kind": matched_kind,
            "target_name": matched_name,
            "target_missing": target_missing,
            "target_absence_allowed": bool(
                selected_candidate_count == 0
                and target_missing
                and scope.get("absence_allowed") is True
            ),
            "scope_analysis_ok": scope.get("ok") is True,
            "scope_successor": dict(scope.get("successor") or {}),
        }
    if not target.file_path.is_file():
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": contract_identity or _identity(config, target),
        }
    snippet = extract_snippet(target, config.language)
    if snippet is None:
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": contract_identity or _identity(config, target),
        }
    findings = detect_generic_mysterious_names(target.file_path, language=config.language)
    candidates = [
        item for item in findings
        if snippet.start_line <= int(item.line or 0) <= snippet.end_line
        and (not kind or str(item.kind or "") == kind)
        and (not name or str(item.name or "") == name)
    ]
    match = candidates[0] if len(candidates) == 1 else None
    return {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {"target_suspicious_name_present": 1 if match else 0},
        "finding_present": match is not None,
        "candidate_count": len(candidates),
        "finding_identity": contract_identity or _identity(
            config,
            target,
            symbol_kind=str(match.kind or "") if match else kind,
            symbol_name=str(match.name or "") if match else name,
        ),
        "target_kind": kind,
        "target_name": name,
    }


def _mysterious_name_scope(
    config: Any,
    *,
    match: Any,
    identity: dict[str, Any],
    kind: str,
    name: str,
) -> dict[str, Any]:
    """Freeze/resolve the production scope for a strict Java name finding."""
    detection = _java_semantic_detection(config)
    if not detection.ok or detection.project_model is None:
        return {
            "ok": False,
            "absence_allowed": False,
            "error": detection.error or "DETECTOR_SCOPE_UNAVAILABLE",
            "unavailable": dict(detection.unavailable or {}),
        }
    model = detection.project_model
    target = _target(config)
    file_name = str(identity.get("file") or (match.file if match is not None else target.project_path))
    class_name = str(identity.get("class") or (match.class_name if match is not None else target.class_name) or "")
    container = str(
        identity.get("container_method")
        or (match.method if match is not None else target.method)
        or ""
    )
    frozen_types = [str(item) for item in identity.get("container_parameter_types", [])] if isinstance(
        identity.get("container_parameter_types"), list
    ) else []
    method_name = str(identity.get("container_method_name") or method_basename(container))
    if kind == "method":
        method_name = str(identity.get("symbol_name") or name or method_name)
    records = [
        method for method in model.methods
        if (kind != "method" or not method.is_constructor)
        and _same_file(method.file, file_name)
        and (not class_name or _simple_type(method.class_name) == _simple_type(class_name))
        and (not method_name or method.method_name == method_name)
        and (not frozen_types or list(method.parameter_types) == frozen_types)
    ]
    if match is not None and match.begin_line:
        containing = [
            method for method in records
            if method.begin_line <= int(match.begin_line) <= method.end_line
        ]
        if containing:
            records = containing
    record = records[0] if len(records) == 1 else None
    if match is not None:
        if str(match.method or "").startswith("<initializer:"):
            classes = [
                cls for cls in model.classes.values()
                if _same_file(cls.file, file_name)
                and (not class_name or _simple_type(cls.class_name) == _simple_type(class_name))
            ]
            return {
                "ok": len(classes) == 1,
                "absence_allowed": False,
                "frozen_identity": {},
            }
        return {
            "ok": record is not None,
            "absence_allowed": False,
            "frozen_identity": (
                {
                    "container_method_name": record.method_name,
                    "container_parameter_types": list(record.parameter_types),
                    "container_return_type": record.return_type,
                    "container_class": record.class_name,
                    "baseline_rename_peers": sorted(
                        _product_method_key(item)
                        for item in model.methods
                        if item.file == record.file
                        and item.class_name == record.class_name
                        and item.method_name != record.method_name
                        and list(item.parameter_types) == list(record.parameter_types)
                        and item.return_type == record.return_type
                    ),
                }
                if record is not None
                else {}
            ),
        }
    if not identity:
        return {"ok": True, "absence_allowed": False}
    if kind == "method":
        old_name = str(identity.get("symbol_name") or name)
        return_type = str(identity.get("container_return_type") or "")
        successors = [
            method for method in model.methods
            if not method.is_constructor
            and _same_file(method.file, file_name)
            and (not class_name or _simple_type(method.class_name) == _simple_type(class_name))
            and method.method_name != old_name
            and (not frozen_types or list(method.parameter_types) == frozen_types)
            and (not return_type or method.return_type == return_type)
        ]
        frozen_peers = {
            str(item)
            for item in identity.get("baseline_rename_peers", [])
        } if isinstance(identity.get("baseline_rename_peers"), list) else set()
        successors = [
            method for method in successors
            if _product_method_key(method) not in frozen_peers
        ]
        successor = successors[0] if len(successors) == 1 else None
        return {
            "ok": True,
            "absence_allowed": successor is not None,
            "successor": (
                {
                    "file": successor.file,
                    "class": successor.class_name,
                    "method": successor.method_signature,
                }
                if successor is not None
                else {}
            ),
        }
    if container.startswith("<initializer:"):
        classes = [
            cls for cls in model.classes.values()
            if _same_file(cls.file, file_name)
            and (not class_name or _simple_type(cls.class_name) == _simple_type(class_name))
        ]
        return {"ok": True, "absence_allowed": len(classes) == 1}
    # Parameter/local renames are valid only while their exact production
    # method scope (owner, name, and parameter types) still exists uniquely.
    return {
        "ok": True,
        "absence_allowed": record is not None,
        "successor": (
            {
                "file": record.file,
                "class": record.class_name,
                "method": record.method_signature,
            }
            if record is not None
            else {}
        ),
    }


def _same_mysterious_container(left: str, right: str) -> bool:
    """Compare source container signatures without parameter-name coupling."""
    if method_basename(str(left or "")) != method_basename(str(right or "")):
        return False
    left_types = method_parameter_type_fingerprint(str(left or ""))
    right_types = method_parameter_type_fingerprint(str(right or ""))
    return left_types == right_types


def _dead_code(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    target_name = method_basename(str(identity.get("method") or target.method or ""))
    if config.language != "java":
        if not target_name:
            return {
                "ok": False,
                "detector": "tree_sitter_generic",
                "objectives": {},
                "error": "target_method_missing_from_location",
            }
        anchored_target = target if target.method else replace(target, method=target_name)
        present = bool(target.file_path.is_file() and extract_snippet(anchored_target, config.language))
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {
                "unused_private_finding_present": 1 if present else 0,
                "target_declaration_present": 1 if present else 0,
            },
            "finding_present": present,
            "candidate_count": 1 if present else 0,
            "target_missing": not present,
            "target_name": target_name,
            "finding_identity": identity or _identity(config, target, method=target.method or target_name),
        }
    detection = _java_semantic_detection(config)
    if not detection.ok or detection.project_model is None:
        return {
            "ok": False,
            "detector": "python_semantic_detector",
            "objectives": {},
            "error": detection.error or "production_detection_session_unavailable",
        }
    candidate_file = str(identity.get("file") or target.project_path)
    candidates = [
        item
        for item in detection.findings.get("dead_code", [])
        if _same_file(item.file, candidate_file)
    ]
    if identity:
        frozen_class = str(identity.get("class") or "")
        frozen_method = str(identity.get("method") or "")
        frozen_rule = str(identity.get("rule_id") or "")
        candidates = [
            item for item in candidates
            if (not frozen_class or str(item.class_name or "") == frozen_class)
            and (not frozen_method or str(item.method or "") == frozen_method)
            and (not frozen_rule or str(item.rule_id or "") == frozen_rule)
        ]
    else:
        if target.class_name:
            candidates = [
                item for item in candidates
                if _simple_type(item.class_name) == _simple_type(target.class_name)
            ]
        if target.method:
            raw_method = str(target.method).strip()
            candidates = [
                item for item in candidates
                if (
                    re.sub(r"\s+", "", str(item.method or ""))
                    == re.sub(r"\s+", "", raw_method)
                    if "(" in raw_method
                    else _same_method(item.method, raw_method)
                )
            ]
        if target.line:
            candidates = [
                item for item in candidates
                if item.begin_line <= int(target.line) <= item.end_line
            ]
        else:
            candidates = []
    selected_candidate_count = len(candidates)
    match = candidates[0] if len(candidates) == 1 else None
    declaration_signature = str(
        identity.get("method") or (match.method if match is not None else "")
    )
    declaration_candidates = [
        method
        for method in detection.project_model.methods
        if _same_file(method.file, str(identity.get("file") or target.project_path))
        and (
            str(method.method_signature) == declaration_signature
            if declaration_signature
            else not target_name or _same_method(method.method_name, target_name)
        )
        and (
            not identity.get("class")
            or _simple_type(method.class_name) == _simple_type(str(identity.get("class")))
        )
    ]
    declaration = declaration_candidates[0] if len(declaration_candidates) == 1 else None
    target_missing = declaration is None or selected_candidate_count > 1
    selection_reason = (
        "MATCHED"
        if match is not None
        else "AMBIGUOUS"
        if selected_candidate_count > 1
        else "NOT_FOUND"
    )
    return {
        "ok": True,
        "detector": "python_semantic_detector",
        "objectives": {
            "unused_private_finding_present": 1 if match else 0,
            "target_declaration_present": 1 if declaration else 0,
        },
        "finding_present": match is not None,
        "candidate_count": selected_candidate_count,
        "selection_reason": selection_reason,
        "finding_identity": _semantic_identity(config, match) if match else identity or _identity(config, target),
        "target_missing": target_missing,
        "target_absence_allowed": bool(
            selected_candidate_count == 0 and target_missing
        ),
        "target_name": target_name or (method_basename(match.method) if match else ""),
        "project_finding_catalog": [
            {
                "file": str(item.file).replace("\\", "/"),
                "class_name": str(item.class_name or ""),
                "method": str(item.method or ""),
                "rule_id": str(item.rule_id or ""),
            }
            for item in detection.findings.get("dead_code", [])
        ],
    }


def _feature_envy(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    selector = _selector_context(config)
    expected_receiver = str(
        identity.get("envied_type")
        or selector.get("receiver_type")
        or ""
    )
    if config.language != "java":
        profile = analyze_generic_feature_envy_target(
            config.project_root,
            language=config.language,
            target_file=target.file_path,
            method=target.method,
            line=target.line,
            expected_receiver=expected_receiver,
        )
        if not profile.get("ok"):
            if profile.get("error") == "target_method_not_found":
                return {
                    "ok": True,
                    "detector": "tree_sitter_generic",
                    "expected_receiver_type": expected_receiver,
                    "objectives": {"expected_receiver_access": 0},
                    "target_missing": True,
                }
            return {"ok": False, "objectives": {}, "error": profile.get("error", "unknown")}
        if not expected_receiver:
            expected_receiver = str(profile.get("dominant_receiver_type") or "")
            profile["expected_receiver_type"] = expected_receiver
            profile["expected_receiver_access"] = int(profile.get("dominant_receiver_access") or 0)
        return {
            **profile,
            "adapter": "feature_envy",
            "detector": "tree_sitter_generic",
            "objectives": {"expected_receiver_access": int(profile.get("expected_receiver_access") or 0)},
            "finding_present": bool(profile.get("strict_detector_hit")),
            "candidate_count": 1 if profile.get("strict_detector_hit") else 0,
            "finding_identity": identity or _identity(
                config,
                target,
                envied_type=str(profile.get("dominant_receiver_type") or ""),
            ),
        }
    detection = _java_semantic_detection(config)
    if not detection.ok or detection.project_model is None:
        return {
            "ok": False,
            "detector": "python_semantic_detector",
            "objectives": {},
            "error": detection.error or "production_detection_session_unavailable",
        }
    findings = detection.findings.get("feature_envy", [])
    project_catalog: list[dict[str, Any]] = []
    for item in findings:
        record = _feature_envy_method_record(detection.project_model, item)
        if record is None:
            return {
                "ok": False,
                "detector": "python_semantic_detector",
                "objectives": {},
                "error": "project_feature_envy_catalog_unavailable",
            }
        project_catalog.append({
            "file": str(item.file).replace("\\", "/"),
            "class_name": str(record.owner_qualified_name or record.class_name or ""),
            "method": _feature_envy_semantic_method_signature(item.method),
            "method_key": stable_method_record_identity(record),
            "rule_id": str(item.rule_id or ""),
            "envied_field": _semantic_attribute(item, "envied_field"),
            "envied_type": _semantic_attribute(item, "envied_type"),
        })
    candidates = _feature_envy_method_candidates(
        config,
        findings,
        identity,
        detection.project_model,
    )
    match = candidates[0] if len(candidates) == 1 else None
    match_source_signature = (
        _feature_envy_source_method_signature(detection.project_model, match)
        if match is not None
        else ""
    )
    if match is not None and not match_source_signature:
        return {
            "ok": False,
            "detector": "python_semantic_detector",
            "objectives": {},
            "error": "source_method_identity_unavailable",
        }
    identity_record = (
        _feature_envy_identity_method_record(detection.project_model, identity)
        if match is None and identity
        else None
    )
    identity_source_signature = (
        stable_method_record_signature(identity_record)
        if identity_record is not None
        else ""
    )
    # Objectives and the actionable access worklist always describe the current
    # method's dominant detector finding.  A frozen receiver is not substituted
    # here: doing so would turn an A -> B dominant-receiver transfer into zero
    # current metrics even though the method still has Feature Envy.
    current_receiver = (
        _semantic_attribute(match, "envied_type")
        if match is not None
        else ""
    )
    profile = analyze_feature_envy_target(
        config.project_root,
        target_file=target.file_path,
        method=str(
            match_source_signature
            if match_source_signature
            else identity_source_signature
            or identity.get("method")
            or target.method
            or ""
        ),
        line=target.line,
        expected_receiver_type=current_receiver,
        project_model=detection.project_model,
    )
    if not profile.get("ok"):
        return {"ok": False, "objectives": {}, "error": profile.get("error", "unknown")}
    current_identity = (
        _feature_envy_method_identity(config, match, detection.project_model)
        if match is not None
        else identity or _identity(config, target)
    )
    return {
        **profile,
        "adapter": "feature_envy",
        "detector": "python_semantic_detector",
        "objectives": {
            "envy_access_diff": max(0, int(profile.get("envy_access_excess") or 0)),
            "envy_access_count": int(profile.get("envy_access_count") or 0),
            "expected_receiver_access": int(profile.get("expected_receiver_access") or 0),
        },
        "finding_present": match is not None,
        "candidate_count": len(candidates),
        "target_absence_allowed": True,
        "project_finding_catalog": project_catalog,
        "finding_identity": current_identity,
    }


def _data_clumps(config: Any, evidence: str) -> dict[str, Any]:
    if config.language == "java":
        detection = _java_semantic_detection(config)
        if not detection.ok or detection.project_model is None:
            return {
                "ok": False,
                "detector": "python_semantic_detector",
                "objectives": {},
                "error": detection.error or "production_detection_session_unavailable",
            }
        target = _target(config)
        identity = _contract_identity(config)
        selector = _selector_context(config)
        raw_group = str(identity.get("group") or selector.get("group") or "")
        group = normalize_data_clump_group(raw_group)
        all_findings = detection.findings.get("data_clumps", [])
        if identity:
            candidate_groups = matching_data_clump_groups(
                all_findings,
                group=group,
            )
            matches = (
                same_group_data_clump_findings(
                    all_findings,
                    group=next(iter(candidate_groups)),
                )
                if len(candidate_groups) == 1
                else []
            )
            if len(candidate_groups) == 1:
                group = next(iter(candidate_groups))
        else:
            anchored = [
                item for item in all_findings
                if _same_file(item.file, target.project_path)
                and (not target.method or _same_method(item.method, target.method))
            ]
            anchored_groups = {
                normalize_data_clump_group(
                    data_clump_finding_group(item)
                )
                for item in anchored
                if data_clump_finding_group(item)
            }
            if group:
                candidate_groups = matching_data_clump_groups(
                    anchored,
                    group=group,
                )
            else:
                candidate_groups = anchored_groups
            matches = (
                same_group_data_clump_findings(
                    all_findings,
                    group=next(iter(candidate_groups)),
                )
                if len(candidate_groups) == 1
                else []
            )
            group = (
                next(iter(candidate_groups))
                if len(candidate_groups) == 1
                else group
            )
        occurrence_catalog = data_clump_occurrence_payloads(matches)
        occurrence_count = len(occurrence_catalog)
        threshold = data_clump_occurrence_threshold()
        baseline_occurrences = []
        contract = getattr(config, "finding_contract", None)
        if isinstance(contract, dict):
            frozen = contract.get("baseline_occurrence_contract")
            if isinstance(frozen, list):
                baseline_occurrences = frozen
        if not baseline_occurrences:
            baseline_occurrences = list(occurrence_catalog)
        type_continuity = analyze_data_clump_type_continuity(
            config.project_root,
            group=group,
            baseline_occurrences=baseline_occurrences,
            project_model=detection.project_model,
        )
        return {
            "ok": True,
            "detector": "python_semantic_detector",
            "group": group,
            "objectives": {"occurrence_count": occurrence_count},
            "passing_max": threshold - 1,
            "remaining_reductions": max(0, occurrence_count - (threshold - 1)),
            # Keep the complete detector catalog as the repair authority.  The
            # bounded list is UI-only and must never become the plan's count or
            # checkpoint closure.
            "occurrence_catalog": occurrence_catalog,
            "occurrence_catalog_complete": True,
            "occurrences": occurrence_catalog[:20],
            "occurrence_preview_limit": 20,
            "occurrence_contract": list(
                type_continuity.get("occurrence_contract") or baseline_occurrences
            ),
            "continuity_ok": bool(type_continuity.get("ok")),
            "continuity_occurrence_count": int(
                type_continuity.get("occurrence_count") or 0
            ),
            "continuity_occurrences": list(type_continuity.get("occurrences") or []),
            "inline_copy_analysis_ok": bool(type_continuity.get("ok")),
            "inline_copy_contract_available": bool(
                type_continuity.get("inline_copy_contract_available")
            ),
            "inline_copy_expansions": list(
                type_continuity.get("inline_copy_expansions") or []
            ),
            "finding_present": occurrence_count >= threshold,
            "candidate_count": len(candidate_groups),
            "finding_identity": identity or _identity(config, target, group=group),
        }
    else:
        selector_group = str(_selector_context(config).get("group") or "")
        analysis = detect_generic_data_clump_occurrences(
            config.project_root,
            language=config.language,
            evidence=f"group={selector_group}" if selector_group else "",
            limit=20,
        )
        threshold = generic_data_clump_occurrence_threshold()
        detector = "tree_sitter_generic"
    occurrence_count = int(analysis.get("occurrence_count") or 0)
    passing_max = max(0, threshold - 1)
    return {
        "ok": bool(analysis.get("success")),
        "detector": detector,
        "group": analysis.get("group", ""),
        "objectives": {"occurrence_count": occurrence_count},
        "passing_max": passing_max,
        "remaining_reductions": max(0, occurrence_count - passing_max),
        "occurrences": list(analysis.get("occurrences") or []),
        "finding_present": occurrence_count >= threshold,
        "candidate_count": 1 if occurrence_count >= threshold else 0,
        "error": analysis.get("error", ""),
    }


def _code_clone(config: Any, evidence: str) -> dict[str, Any]:
    if len(config.locations) < 2:
        return {"ok": False, "objectives": {}, "error": "clone pair requires two locations"}
    left, right = config.locations[:2]
    if not left.file_path.is_file() or not right.file_path.is_file():
        return {
            "ok": True,
            "objectives": {"clone_token_count": 0},
            "target_missing": True,
            "finding_present": False,
            "candidate_count": 0,
        }
    if config.language != "java":
        left_snippet, right_snippet = extract_pair_snippets(config.locations, config.language)
        if left_snippet is None or right_snippet is None:
            return {
                "ok": True,
                "detector": "tree_sitter_generic",
                "objectives": {"clone_token_count": 0},
                "target_missing": True,
                "finding_present": False,
                "candidate_count": 0,
            }
        left_text = normalize_for_clone(left_snippet.body_text, config.language)
        right_text = normalize_for_clone(right_snippet.body_text, config.language)
        score = len(re.findall(r"\w+|[^\w\s]", left_text)) if left_text and left_text == right_text else 0
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"clone_token_count": float(score)},
            "clone_pair_present": score >= PRODUCT_THRESHOLDS["code_clone_type1"],
            "finding_present": score >= PRODUCT_THRESHOLDS["code_clone_type1"],
            "candidate_count": 1 if score >= PRODUCT_THRESHOLDS["code_clone_type1"] else 0,
            "finding_identity": _contract_identity(config) or {
                "smell": str(config.smell),
                "left_file": str(left.project_path).replace("\\", "/"),
                "left_method": str(left.method or ""),
                "right_file": str(right.project_path).replace("\\", "/"),
                "right_method": str(right.method or ""),
            },
        }
    identity = _contract_identity(config)
    detection = _java_semantic_detection(config)
    if not detection.ok or detection.project_model is None:
        return {
            "ok": False,
            "detector": "python_semantic_detector",
            "objectives": {},
            "error": detection.error or "production_detection_session_unavailable",
        }
    left_anchor = {
        "file": str(identity.get("left_file") or left.project_path),
        "class": str(identity.get("left_class") or left.class_name or ""),
        "method": str(identity.get("left_method") or left.method or ""),
        "line": left.line,
        "frozen_identity": bool(identity),
    }
    right_anchor = {
        "file": str(identity.get("right_file") or right.project_path),
        "class": str(identity.get("right_class") or right.class_name or ""),
        "method": str(identity.get("right_method") or right.method or ""),
        "line": right.line,
        "frozen_identity": bool(identity),
    }
    structure = analyze_exact_clone_closure(
        detection.project_model,
        left=left_anchor,
        right=right_anchor,
        min_tokens=PRODUCT_THRESHOLDS["code_clone_type1"],
    )
    endpoints = list(structure.get("endpoints") or [])
    left_found = (
        dict(endpoints[0].get("declared_identity") or {})
        if len(endpoints) > 0 and isinstance(endpoints[0], dict)
        else {}
    )
    right_found = (
        dict(endpoints[1].get("declared_identity") or {})
        if len(endpoints) > 1 and isinstance(endpoints[1], dict)
        else {}
    )
    pair_present = structure.get("pair_present") is True
    score = float(structure.get("pair_token_count") or 0)
    return {
        "ok": True,
        "detector": "java_exact_clone_product_detector",
        "objectives": {"clone_token_count": score},
        "clone_pair_present": pair_present,
        "finding_present": pair_present,
        "candidate_count": 1 if pair_present else 0,
        "clone_structure": structure,
        "finding_identity": (
            {
                "smell": str(config.smell),
                "left_file": str(left_found.get("file") or left_anchor["file"]).replace("\\", "/"),
                "left_class": str(left_found.get("class") or left_anchor["class"]),
                "left_method": str(left_found.get("method") or left_anchor["method"]),
                "right_file": str(right_found.get("file") or right_anchor["file"]).replace("\\", "/"),
                "right_class": str(right_found.get("class") or right_anchor["class"]),
                "right_method": str(right_found.get("method") or right_anchor["method"]),
                "clone_group": str(structure.get("pair_fingerprint") or ""),
            }
            if pair_present
            else _contract_identity(config)
        ),
    }


def _semantic_finding(
    config: Any,
    smell: str,
    detection: Any,
) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    selector = _selector_context(config)
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
    findings = detection.findings.get(smell, [])
    if smell == "god_class":
        frozen_file = str(identity.get("file") or "")
        class_name = str(
            identity.get("class")
            or selector.get("target_class")
            or target.class_name
            or ""
        )
        if identity:
            # A frozen God Class finding is the conjunction of its canonical
            # source file and class identity.  Never let the mere presence of a
            # finding_contract disable the file predicate: two packages may
            # legally contain the same simple class name, and a renamed target
            # must not bind to an unrelated namesake.
            matches = [
                item for item in findings
                if frozen_file
                and class_name
                and _same_file(item.file, frozen_file)
                and item.class_name.rsplit(".", 1)[-1].lower()
                == class_name.rsplit(".", 1)[-1].lower()
            ]
        else:
            matches = [
                item for item in findings
                if _same_file(item.file, target.project_path)
                and (
                    not class_name
                    or item.class_name.rsplit(".", 1)[-1].lower()
                    == class_name.rsplit(".", 1)[-1].lower()
                )
            ]
        match = matches[0] if len(matches) == 1 else None
        metrics = _semantic_integer_metrics(
            match,
            ("nom", "nof", "wmc", "loc", "atfd"),
        )
        objectives = {
            name: float(metrics.get(name, 0))
            for name in ("nom", "nof", "wmc", "loc", "atfd")
        }
    else:
        parent = str(identity.get("parent") or selector.get("parent") or "")
        method = str(identity.get("method") or target.method or "")
        target_class = str(
            identity.get("target_class")
            or identity.get("class")
            or selector.get("target_class")
            or target.class_name
            or ""
        )
        matches = [
            item for item in findings
            if (
                (
                    _same_file(item.file, str(identity.get("file") or ""))
                    and str(identity.get("method") or "") == str(item.method)
                    if identity
                    else _same_file(item.file, target.project_path)
                    and (not method or _same_method(item.method, method))
                )
                and (
                    not parent
                    or _simple_type(_semantic_attribute(item, "parent"))
                    == _simple_type(parent)
                )
                and (
                    not target_class
                    or _simple_type(item.class_name) == _simple_type(target_class)
                )
            )
        ]
        match = matches[0] if len(matches) == 1 else None
        objectives = {
            "refusal_score": 1.0 if match is not None else 0.0,
            "refusal_finding_present": 1.0 if match is not None else 0.0,
            "rejection_signals": 1.0 if match is not None else 0.0,
        }
    extra: dict[str, Any] = {}
    if match is not None and smell == "refused_bequest":
        extra = {
            "parent": _semantic_attribute(match, "parent"),
            "target_class": _semantic_attribute(match, "target_class"),
            "rejection_kind": _semantic_attribute(match, "rejection_kind"),
        }
    snapshot = {
        "ok": True,
        "detector": "python_semantic_detector",
        "objectives": objectives,
        "finding_present": match is not None,
        "candidate_count": len(matches),
        "finding_identity": (
            _semantic_identity(config, match, **extra)
            if match is not None
            else identity or _identity(config, target)
        ),
        "evidence": match.evidence if match else "",
    }
    if smell == "god_class":
        frozen_file = str(identity.get("file") or "")
        class_name = str(
            identity.get("class")
            or selector.get("target_class")
            or target.class_name
            or ""
        )
        model = detection.project_model
        classes = model.classes.values() if model is not None else []
        if identity:
            target_classes = [
                cls for cls in classes
                if frozen_file
                and class_name
                and _same_file(cls.file, frozen_file)
                and (
                    _simple_type(cls.class_name) == _simple_type(class_name)
                    or str(cls.qualified_name).lower() == class_name.lower()
                )
            ]
        else:
            target_classes = [
                cls for cls in classes
                if _same_file(cls.file, target.project_path)
                and (
                    not class_name
                    or _simple_type(cls.class_name) == _simple_type(class_name)
                    or str(cls.qualified_name).lower() == class_name.lower()
                )
            ]
        snapshot["target_missing"] = len(target_classes) != 1
        snapshot["target_class_identity"] = (
            {
                "file": str(target_classes[0].file).replace("\\", "/"),
                "class": str(target_classes[0].qualified_name or target_classes[0].class_name),
            }
            if len(target_classes) == 1
            else {}
        )
        responsibility_clusters = (
            god_class_responsibility_clusters(model, target_classes[0])
            if model is not None and len(target_classes) == 1
            else []
        )
        snapshot["responsibility_clusters"] = responsibility_clusters
        snapshot["god_class_profile"] = god_class_product_profile(
            metrics,
            responsibility_clusters=responsibility_clusters,
        )
    if smell == "refused_bequest":
        # The immutable project-level catalog lets the strict guard distinguish
        # a newly relocated rejection from unrelated rejecting overrides that
        # already existed at c000. It contains detector output only—no dataset
        # evidence or oracle labels.
        snapshot["project_finding_catalog"] = [
            {
                "file": str(item.file).replace("\\", "/"),
                "class_name": str(item.class_name or ""),
                "method": str(item.method or ""),
                "source_method": _semantic_source_method_signature(
                    detection.project_model,
                    item,
                ),
                "rule_id": str(item.rule_id or ""),
                "parent": _semantic_attribute(item, "parent"),
                "inheritance_source": (
                    list(item.attributes.get("inheritance_source") or [])
                    if isinstance(item.attributes, dict)
                    and isinstance(item.attributes.get("inheritance_source"), list)
                    else []
                ),
            }
            for item in findings
        ]
    return snapshot


def _god_class(config: Any, evidence: str) -> dict[str, Any]:
    if config.language != "java":
        target = _target(config)
        text = extract_class_text(target, config.language)
        loc = count_meaningful_lines(text or "", config.language)
        return {
            "ok": text is not None,
            "detector": "tree_sitter_generic",
            "objectives": {"class_loc": float(loc)},
            "target_missing": text is None,
            "finding_present": text is not None and loc >= 100,
            "candidate_count": 1 if text is not None and loc >= 100 else 0,
            "finding_identity": _contract_identity(config) or _identity(
                config,
                target,
                class_name=target.class_name or "",
            ),
            "error": "target_class_not_found" if text is None else "",
        }
    detection = _java_semantic_detection(config)
    return _semantic_finding(config, "god_class", detection)


def _compact_refused_bequest_impact_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("ok") is not True:
        return {
            "analysis_ok": False,
            "error": (
                str(value.get("error") or "capability_impact_map_unavailable")
                if isinstance(value, dict)
                else "capability_impact_map_unavailable"
            ),
            "contract_declarations": [],
            "implementers": [],
            "production_call_sites": [],
        }

    def select(item: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        return {
            key: item[key]
            for key in keys
            if key in item and item[key] not in (None, "", [], {})
        }

    declarations = [
        select(
            item,
            ("owner", "file", "line", "signature", "modifiers", "body_kind"),
        )
        for item in value.get("contract_declarations", [])
    ]
    implementers = []
    for item in value.get("implementers", []):
        payload = select(
            item,
            ("class", "kind", "file", "line", "modifiers", "role"),
        )
        declared = item.get("declared_target_methods") if isinstance(item, dict) else None
        if isinstance(declared, list) and declared:
            payload["declared_target_methods"] = [
                select(method, ("owner", "file", "line", "signature", "body_kind"))
                for method in declared
            ]
        implementers.append(payload)
    calls = [
        select(
            item,
            (
                "file", "line", "enclosing_method", "receiver",
                "static_receiver_type", "receiver_resolution",
                "exposes_reported_contract", "expression",
            ),
        )
        for item in value.get("production_call_sites", [])
    ]
    inherited_surface = [
        {
            **select(item, ("owner", "file")),
            "state_field_count": len(item.get("state_fields", [])),
            "non_target_method_count": len(item.get("non_target_methods", [])),
            "bodyless_non_target_method_count": len(
                item.get("bodyless_non_target_methods", [])
            ),
        }
        for item in value.get("inherited_surface_at_risk", [])
        if isinstance(item, dict)
    ]
    unresolved_count = int(value.get("unresolved_receiver_call_sites") or 0)
    return {
        "analysis_ok": unresolved_count == 0,
        "error": "" if unresolved_count == 0 else "unresolved_receiver_call_sites",
        "target": select(
            value.get("target"),
            ("class", "file", "method", "parameter_count", "reported_parent"),
        ),
        "contract_declarations": declarations,
        "implementers": implementers,
        "production_call_sites": calls,
        "inherited_surface_at_risk": inherited_surface,
        "unresolved_receiver_call_sites": unresolved_count,
        "excluded_unrelated_same_name_calls": int(
            value.get("excluded_unrelated_same_name_calls") or 0
        ),
        "dependency_order": list(value.get("dependency_order") or []),
        "remaining_work_count": len(declarations) + len(implementers) + len(calls),
        "authority": "java_product_semantic_model",
    }


def _refused_bequest(config: Any, evidence: str) -> dict[str, Any]:
    detection = _java_semantic_detection(config)
    snapshot = _semantic_finding(config, "refused_bequest", detection)
    if config.language != "java" or not config.locations:
        return snapshot
    if not detection.ok or detection.project_model is None:
        return {
            **snapshot,
            "ok": False,
            "error": detection.error or "production_detection_session_unavailable",
        }
    target = _target(config)
    identity = snapshot.get("finding_identity")
    identity = identity if isinstance(identity, dict) else _contract_identity(config)
    impact_map = build_refused_bequest_impact_map(
        config.project_root,
        target_file=target.file_path,
        method=str(identity.get("method") or target.method or ""),
        line=target.line,
        reported_parent=str(identity.get("parent") or ""),
        target_parameter_count=(
            int(_selector_context(config).get("target_parameter_count"))
            if str(_selector_context(config).get("target_parameter_count") or "").isdigit()
            else target.parameter_count
        ),
        target_class_name=str(identity.get("target_class") or target.class_name or ""),
        project_model=detection.project_model,
    )
    snapshot["contract_snapshot"] = (
        impact_map.get("target_contract")
        if impact_map.get("ok")
        else {
            "ok": False,
            "error": impact_map.get("error", "capability_impact_map_unavailable"),
        }
    )
    snapshot["migration_impact_map"] = _compact_refused_bequest_impact_map(
        impact_map
    )
    return snapshot


def _semantic_attribute(finding: Any, name: str) -> str:
    attributes = getattr(finding, "attributes", None)
    if not isinstance(attributes, dict):
        return ""
    value = attributes.get(name)
    return "" if value is None else str(value).strip()


def _semantic_source_method_signature(model: Any, finding: Any) -> str:
    """Return the finding declaration's lexical signature when uniquely bound."""
    if model is None:
        return ""
    file_name = str(getattr(finding, "file", "") or "")
    class_name = str(getattr(finding, "class_name", "") or "")
    begin_line = int(getattr(finding, "begin_line", 0) or 0)
    method_name = method_basename(str(getattr(finding, "method", "") or ""))
    candidates = [
        method
        for method in getattr(model, "methods", ())
        if _same_file(str(getattr(method, "file", "") or ""), file_name)
        and int(getattr(method, "begin_line", 0) or 0) == begin_line
        and _simple_type(str(getattr(method, "class_name", "") or ""))
        == _simple_type(class_name)
        and method_basename(str(getattr(method, "method_name", "") or ""))
        == method_name
    ]
    if len(candidates) != 1:
        return ""
    try:
        return stable_method_record_signature(candidates[0])
    except ValueError:
        return ""


def _semantic_integer_metrics(
    finding: Any,
    names: tuple[str, ...],
) -> dict[str, int]:
    attributes = getattr(finding, "attributes", None)
    if not isinstance(attributes, dict):
        return {}
    metrics: dict[str, int] = {}
    for name in names:
        value = attributes.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metrics[name] = int(value)
    return metrics


def _same_file(left: str, right: Any) -> bool:
    return str(left).replace("\\", "/").lstrip("/") == str(right).replace("\\", "/").lstrip("/")


_ADAPTERS: dict[str, Callable[[Any, str], dict[str, Any]]] = {
    "long_method": _long_method,
    "nested_complexity": _nested_complexity,
    "long_parameter_list": _long_parameter_list,
    "feature_envy": _feature_envy,
    "data_clumps": _data_clumps,
    "code_clone_type1": _code_clone,
    "god_class": _god_class,
    "refused_bequest": _refused_bequest,
    "switch_statements": _switch_statements,
    "mysterious_name": _mysterious_name,
    "dead_code": _dead_code,
}
