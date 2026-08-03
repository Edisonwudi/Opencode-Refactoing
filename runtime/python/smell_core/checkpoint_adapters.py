"""Metric adapters for the generic checkpoint contract."""
from __future__ import annotations

import hashlib
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
    extract_pair_snippets,
    extract_snippet,
    method_basename,
    normalize_for_clone,
    python_switch_metrics,
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
from .java.source_layout import standard_test_root
from .java.catalog_identity import (
    CATALOG_IDENTITY_SCHEMA,
    stable_method_record_signature,
)
from .java.semantic_detector import (
    god_class_product_profile,
    run_java_semantic_detector,
)
from .java.target_guard import capture_java_target_guard, evaluate_java_target_guard
from .loop_policy import CHECKPOINT_SMELLS


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



def _target(config: Any) -> Any:
    if not config.locations:
        raise ValueError("target location is missing")
    return config.locations[0]



def _matching_syntactic(
    config: Any,
    smell: str,
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


def _objective_name(smell: str) -> str:
    return {
        "long_method": "ast_ncss",
        "nested_complexity": "cognitive_complexity",
        "long_parameter_list": "parameter_count",
    }[smell]


def _long_method(config: Any, evidence: str) -> dict[str, Any]:
    return _matching_syntactic(config, "long_method")


def _nested_complexity(config: Any, evidence: str) -> dict[str, Any]:
    return _matching_syntactic(config, "nested_complexity")


def _long_parameter_list(config: Any, evidence: str) -> dict[str, Any]:
    return _matching_syntactic(config, "long_parameter_list")


def _switch_statements(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
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



def _dead_code(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    target_name = method_basename(str(identity.get("method") or target.method or ""))
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


def _feature_envy(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    selector = _selector_context(config)
    expected_receiver = str(
        identity.get("envied_type")
        or selector.get("receiver_type")
        or ""
    )
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


def _data_clumps(config: Any, evidence: str) -> dict[str, Any]:
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


def _refused_bequest_semantic_finding(
    config: Any,
    detection: Any,
) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    selector = _selector_context(config)
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
    findings = detection.findings.get("refused_bequest", [])
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
    extra = (
        {
            "parent": _semantic_attribute(match, "parent"),
            "target_class": _semantic_attribute(match, "target_class"),
            "rejection_kind": _semantic_attribute(match, "rejection_kind"),
        }
        if match is not None
        else {}
    )
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
    # The immutable project-level catalog lets the strict guard distinguish a
    # newly relocated rejection from unrelated rejecting overrides at c000.
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
    snapshot = _refused_bequest_semantic_finding(config, detection)
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
