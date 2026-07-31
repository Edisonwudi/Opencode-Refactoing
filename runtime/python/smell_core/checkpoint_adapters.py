"""Metric adapters for the generic checkpoint contract."""
from __future__ import annotations

import re
from dataclasses import replace
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
)
from .data_clumps import (
    data_clump_occurrence_threshold as generic_data_clump_occurrence_threshold,
    detect_data_clump_occurrences as detect_generic_data_clump_occurrences,
)
from .feature_envy import analyze_feature_envy_target as analyze_generic_feature_envy_target
from .mysterious_name import (
    detect_mysterious_names as detect_generic_mysterious_names,
)
from .java.ast_ncss import run_ast_ncss
from .java.data_clumps import (
    data_clump_group_from_evidence,
    data_clump_occurrence_payloads,
    data_clump_occurrence_threshold,
    same_group_data_clump_findings,
)
from .java.semantic_detector import (
    analyze_feature_envy_target,
    build_refused_bequest_impact_map,
    run_java_semantic_detector,
)
from .java.syntactic_detector import (
    compute_switch_metrics,
    find_matching_clone_pair,
    find_matching_syntactic_finding,
    load_project_model,
    mask_comments_and_strings,
    parse_mysterious_evidence,
    run_java_syntactic_detector,
)


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

DETECTOR_PROFILE_VERSION = "java-oracle-aligned-v3"
PRODUCT_THRESHOLDS = {
    "long_method": 60,
    "long_parameter_list": 6,
    "nested_complexity": 20,
    "code_clone_type1": 30,
    "data_clumps": 3,
}


DETECTOR_PROFILES = {
    "long_method": {"metric": "ast_ncss", "finding_min": 60},
    "long_parameter_list": {"metric": "parameter_count", "finding_min": 6},
    "nested_complexity": {"metric": "cognitive_complexity", "finding_min": 20},
    "switch_statements": {"definition": "target_method_contains_switch"},
    "code_clone_type1": {"definition": "exact_normalized_tokens", "finding_min_tokens": 30},
    "feature_envy": {
        "definition": "designite_2.8.6_envy_access_diff_alias_provenance_self_symbols",
        "finding_min_exclusive": 1,
    },
    "data_clumps": {
        "group_size": 3,
        "min_occurrences": 3,
        "min_classes": 3,
        "min_method_names": 2,
        "exclude_parameter_object_owner_constructor": True,
    },
    "mysterious_name": {"definition": "strict_symbol_name", "profile": "strict"},
    "refused_bequest": {
        "definition": "method_level_rejecting_override_baseline_delta",
    },
    "dead_code": {"definition": "unused_private_declaration_refs_zero"},
    "god_class": {"definition": "multi_metric_profile", "profile": "delivery-v1"},
}


def capture_metric_snapshot(config: Any, evidence: str) -> dict[str, Any]:
    """Capture a product-detector snapshot.

    ``evidence`` is accepted for CLI compatibility and audit logging only. It
    must never influence candidate discovery, selection, metrics, or verdicts.
    """
    del evidence
    adapter = _ADAPTERS.get(str(config.smell))
    if adapter is None:
        return {"ok": False, "adapter": "unsupported", "objectives": {}, "error": "unsupported_smell"}
    try:
        snapshot = adapter(config, "")
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
    snapshot.setdefault(
        "detector_profile",
        {
            "version": DETECTOR_PROFILE_VERSION,
            "smell": str(config.smell),
            "language": str(config.language),
            **DETECTOR_PROFILES.get(str(config.smell), {}),
        },
    )
    snapshot.setdefault("candidate_count", 1 if snapshot.get("finding_present") is True else 0)
    return snapshot


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
            }
        score = count_parameters(signature.signature_text, "java")
        return {
            "ok": True,
            "detector": "java_target_signature",
            "objectives": {"parameter_count": float(score)},
            "target_missing": False,
            "finding_present": score >= PRODUCT_THRESHOLDS["long_parameter_list"],
            "finding_identity": _identity(config, target),
        }
    if smell == "long_method":
        detection = run_ast_ncss(target.file_path, config.project_root, -1)
        findings = detection.findings
        error = detection.error
        ok = detection.ok
        detector = "java_ast_ncss"
    else:
        detection = run_java_syntactic_detector(
            config.project_root,
            target_files=[target.file_path],
            thresholds=thresholds,
            include_code_clone=False,
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
    match = find_matching_syntactic_finding(
        findings,
        target_file=target.file_path,
        project_root=config.project_root,
        method=method,
        line=target.line,
        original_start_line=target.start_line,
        original_param_count=target.parameter_count if smell == "long_parameter_list" else None,
        original_param_type_fingerprint=target.param_type_fingerprint if smell == "long_parameter_list" else None,
    )
    score = float(match.score) if match is not None else 0.0
    finding_present = bool(
        match is not None and score >= float(PRODUCT_THRESHOLDS[smell])
    )
    return {
        "ok": True,
        "detector": detector,
        "objectives": {_objective_name(smell): score},
        "target_missing": match is None and not _method_exists_in_zero_threshold_scan(findings, target, config),
        "finding_present": finding_present,
        "finding_identity": _identity(
            config,
            target,
            method=match.method if match is not None else method,
            rule_id=match.rule_id if match is not None else "",
        ),
    }


def _method_exists_in_zero_threshold_scan(findings: Any, target: Any, config: Any) -> bool:
    return find_matching_syntactic_finding(
        findings,
        target_file=target.file_path,
        project_root=config.project_root,
        method=target.method,
        line=target.line,
        original_start_line=target.start_line,
    ) is not None


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


def _long_parameter_list(config: Any, evidence: str) -> dict[str, Any]:
    if config.language == "java":
        target = _target(config)
        if not target.file_path.is_file():
            return {
                "ok": True,
                "detector": "tree_sitter_java_declaration",
                "objectives": {"parameter_count": 0},
                "target_missing": True,
                "finding_present": False,
                "finding_identity": _identity(config, target),
            }
        signature = extract_function_signature(target, "java")
        if signature is None:
            return {
                "ok": True,
                "detector": "tree_sitter_java_declaration",
                "objectives": {"parameter_count": 0},
                "target_missing": True,
                "finding_present": False,
                "finding_identity": _identity(config, target),
            }
        parameter_count = len(signature.parameter_fingerprints)
        return {
            "ok": True,
            "detector": "tree_sitter_java_declaration",
            "objectives": {
                "parameter_count": parameter_count,
            },
            "target_missing": False,
            "target_method": signature.name,
            "target_start_line": signature.start_line,
            "target_signature": signature.signature_text,
            "finding_present": parameter_count >= PRODUCT_THRESHOLDS["long_parameter_list"],
            "finding_identity": _identity(
                config,
                target,
                method=signature.name,
                parameter_types=list(signature.parameter_fingerprints),
            ),
        }
    return _matching_syntactic(config, "long_parameter_list", {"long_parameter_list": -1}, evidence)


def _switch_statements(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    if not target.file_path.is_file():
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"switch_case_count": 0, "switch_density": 0.0},
            "target_missing": True,
            "finding_present": False,
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
            "finding_identity": _identity(config, target),
        }
    if config.language == "python":
        # Python has no switch; count dispatch branches (if/elif chains, match
        # statements) via tree-sitter.  The regex metric counted the word "case"
        # inside '#' comments (django special-case false positive).
        switch_count, case_count, density = python_switch_metrics(snippet)
        detector = "tree_sitter_generic"
    else:
        switch_count, case_count, density = compute_switch_metrics(snippet.body_text)
        detector = "java_syntactic_detector"
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
        "finding_identity": _identity(config, target),
    }


def _mysterious_name(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    contract_identity = _contract_identity(config)
    selector = _selector_context(config)
    kind = str(contract_identity.get("symbol_kind") or selector.get("symbol_kind") or "")
    name = str(contract_identity.get("symbol_name") or selector.get("symbol_name") or "")
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
            include_code_clone=False,
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
            if _same_file(item.file, target.project_path)
        ]
        if contract_identity:
            candidates = [
                item
                for item in candidates
                if parse_mysterious_evidence(item.evidence) == (kind, name)
                and (
                    kind == "method"
                    or not contract_identity.get("container_method")
                    or str(item.method) == str(contract_identity["container_method"])
                )
            ]
        else:
            if kind or name:
                candidates = [
                    item
                    for item in candidates
                    if (not kind or parse_mysterious_evidence(item.evidence)[0] == kind)
                    and (not name or parse_mysterious_evidence(item.evidence)[1] == name)
                ]
            if target.method:
                candidates = [item for item in candidates if _same_method(item.method, target.method)]
            elif target.line and candidates:
                target_line = int(target.line)
                containing = [
                    item
                    for item in candidates
                    if item.begin_line <= target_line <= item.end_line
                ]
                if containing:
                    candidates = containing
                else:
                    distances = [
                        min(abs(item.begin_line - target_line), abs(item.end_line - target_line))
                        for item in candidates
                    ]
                    nearest = min(distances)
                    candidates = [
                        item for item, distance in zip(candidates, distances)
                        if distance == nearest
                    ]
        match = candidates[0] if len(candidates) == 1 else None
        matched_kind, matched_name = (
            parse_mysterious_evidence(match.evidence) if match is not None else (kind, name)
        )
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"target_suspicious_name_present": 1 if match else 0},
            "finding_present": match is not None,
            "candidate_count": len(candidates),
            "finding_identity": (
                _semantic_identity(
                    config,
                    match,
                    symbol_kind=matched_kind,
                    symbol_name=matched_name,
                    container_method=match.method,
                )
                if match is not None
                else contract_identity or _identity(config, target, symbol_kind=kind, symbol_name=name)
            ),
            "target_kind": matched_kind,
            "target_name": matched_name,
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
            "target_missing": not present,
            "target_name": target_name,
            "finding_identity": identity or _identity(config, target, method=target.method or target_name),
        }
    detection = run_java_semantic_detector(config.project_root, include_tests=False)
    if not detection.ok:
        return {"ok": False, "detector": "python_semantic_detector", "objectives": {}, "error": detection.error}
    candidates = [
        item
        for item in detection.findings.get("dead_code", [])
        if _same_file(item.file, target.project_path)
        and (
            str(identity.get("method") or "") == str(item.method)
            if identity.get("method")
            else not target_name or _same_method(item.method, target_name)
        )
    ]
    if not identity and target.line:
        containing = [
            item for item in candidates
            if item.begin_line <= int(target.line) <= item.end_line
        ]
        if containing:
            candidates = containing
        elif candidates:
            nearest = min(candidates, key=lambda item: abs(item.begin_line - int(target.line)))
            candidates = [nearest]
    match = candidates[0] if len(candidates) == 1 else None
    return {
        "ok": True,
        "detector": "python_semantic_detector",
        "objectives": {
            "unused_private_finding_present": 1 if match else 0,
            "target_declaration_present": 1 if match else 0,
        },
        "finding_present": match is not None,
        "candidate_count": len(candidates),
        "finding_identity": _semantic_identity(config, match) if match else identity or _identity(config, target),
        "target_missing": not target.file_path.is_file(),
        "target_name": target_name or (method_basename(match.method) if match else ""),
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
            "finding_identity": identity or _identity(
                config,
                target,
                envied_type=str(profile.get("dominant_receiver_type") or ""),
            ),
        }
    detection = run_java_semantic_detector(config.project_root, include_tests=False)
    if not detection.ok:
        return {
            "ok": False,
            "detector": "python_semantic_detector",
            "objectives": {},
            "error": detection.error,
        }
    findings = detection.findings.get("feature_envy", [])
    if identity:
        candidates = [
            item for item in findings
            if (
                _same_file(item.file, str(identity.get("file") or ""))
                and str(identity.get("method") or "") == str(item.method)
                and _evidence_value(item.evidence, "envied_field") == str(identity.get("envied_field") or "")
                and _simple_type(_evidence_value(item.evidence, "envied_type"))
                == _simple_type(str(identity.get("envied_type") or ""))
            )
        ]
    else:
        candidates = [
            item for item in findings
            if _same_file(item.file, target.project_path)
            and (not target.method or _same_method(item.method, target.method))
            and (
                not expected_receiver
                or _simple_type(_evidence_value(item.evidence, "envied_type"))
                == _simple_type(expected_receiver)
            )
        ]
    match = candidates[0] if len(candidates) == 1 else None
    profile = analyze_feature_envy_target(
        config.project_root,
        target_file=target.file_path,
        method=target.method,
        line=target.line,
        expected_receiver_type=expected_receiver,
    )
    if not profile.get("ok"):
        if profile.get("error") == "target_method_not_found":
            return {
                "ok": True,
                "detector": "python_semantic_detector",
                "expected_receiver_type": expected_receiver,
                "objectives": {"expected_receiver_access": 0},
                "target_missing": True,
            }
        return {"ok": False, "objectives": {}, "error": profile.get("error", "unknown")}
    if match is not None:
        expected_receiver = _evidence_value(match.evidence, "envied_type")
        profile["expected_receiver_type"] = expected_receiver
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
        "finding_identity": (
            _semantic_identity(
                config,
                match,
                envied_field=_evidence_value(match.evidence, "envied_field"),
                envied_type=_evidence_value(match.evidence, "envied_type"),
            )
            if match is not None
            else identity or _identity(config, target)
        ),
    }


def _data_clumps(config: Any, evidence: str) -> dict[str, Any]:
    if config.language == "java":
        detection = run_java_semantic_detector(config.project_root, include_tests=False)
        if not detection.ok:
            return {
                "ok": False,
                "detector": "python_semantic_detector",
                "objectives": {},
                "error": detection.error,
            }
        target = _target(config)
        identity = _contract_identity(config)
        selector = _selector_context(config)
        raw_group = str(identity.get("group") or selector.get("group") or "")
        group = data_clump_group_from_evidence(f"group={raw_group}")
        all_findings = detection.findings.get("data_clumps", [])
        if identity:
            matches = same_group_data_clump_findings(all_findings, evidence=f"group={group}")
            candidate_groups = {group} if matches else set()
        else:
            anchored = [
                item for item in all_findings
                if _same_file(item.file, target.project_path)
                and (not target.method or _same_method(item.method, target.method))
            ]
            groups = {
                data_clump_group_from_evidence(
                    f"group={_evidence_value(item.evidence, 'group')}"
                )
                for item in anchored
                if _evidence_value(item.evidence, "group")
            }
            if group:
                groups = {item for item in groups if item == group}
            candidate_groups = groups
            matches = (
                same_group_data_clump_findings(
                    all_findings,
                    evidence=f"group={next(iter(groups))}",
                )
                if len(groups) == 1
                else []
            )
            group = next(iter(groups)) if len(groups) == 1 else group
        occurrence_count = len(matches)
        threshold = data_clump_occurrence_threshold()
        return {
            "ok": True,
            "detector": "python_semantic_detector",
            "group": group,
            "objectives": {"occurrence_count": occurrence_count},
            "passing_max": threshold - 1,
            "remaining_reductions": max(0, occurrence_count - (threshold - 1)),
            "occurrences": data_clump_occurrence_payloads(matches, limit=20),
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
        "error": analysis.get("error", ""),
    }


def _code_clone(config: Any, evidence: str) -> dict[str, Any]:
    if len(config.locations) < 2:
        return {"ok": False, "objectives": {}, "error": "clone pair requires two locations"}
    left, right = config.locations[:2]
    if not left.file_path.is_file() or not right.file_path.is_file():
        return {"ok": True, "objectives": {"clone_token_count": 0}, "target_missing": True}
    if config.language != "java":
        left_snippet, right_snippet = extract_pair_snippets(config.locations, config.language)
        if left_snippet is None or right_snippet is None:
            return {
                "ok": True,
                "detector": "tree_sitter_generic",
                "objectives": {"clone_token_count": 0},
                "target_missing": True,
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
            "finding_identity": _contract_identity(config) or {
                "smell": str(config.smell),
                "left_file": str(left.project_path).replace("\\", "/"),
                "left_method": str(left.method or ""),
                "right_file": str(right.project_path).replace("\\", "/"),
                "right_method": str(right.method or ""),
            },
        }
    detection = run_java_syntactic_detector(
        config.project_root,
        target_files=[left.file_path, right.file_path],
        thresholds={"code_clone_min_tokens": PRODUCT_THRESHOLDS["code_clone_type1"]},
        include_mysterious_name=False,
    )
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
    identity = _contract_identity(config)
    if identity:
        clone_findings = detection.findings.get("code_clone_type1", [])
        left_candidates = [
            item for item in clone_findings
            if _same_file(item.file, str(identity.get("left_file") or ""))
            and str(item.method) == str(identity.get("left_method") or "")
            and (
                not identity.get("left_class")
                or str(item.class_name) == str(identity.get("left_class"))
            )
        ]
        right_candidates = [
            item for item in clone_findings
            if _same_file(item.file, str(identity.get("right_file") or ""))
            and str(item.method) == str(identity.get("right_method") or "")
            and (
                not identity.get("right_class")
                or str(item.class_name) == str(identity.get("right_class"))
            )
        ]
        pairs = [
            (left_item, right_item)
            for left_item in left_candidates
            for right_item in right_candidates
            if left_item.rule_id == right_item.rule_id
        ]
        match = pairs[0] if len(pairs) == 1 else None
    else:
        match = find_matching_clone_pair(
            detection.findings.get("code_clone_type1", []),
            left_file=left.file_path,
            right_file=right.file_path,
            project_root=config.project_root,
            left_method=left.method,
            right_method=right.method,
            left_line=left.line,
            right_line=right.line,
        )
    score = min(float(match[0].score), float(match[1].score)) if match else 0.0
    return {
        "ok": True,
        "detector": "java_syntactic_detector",
        "objectives": {"clone_token_count": score},
        "clone_pair_present": match is not None,
        "finding_present": match is not None,
        "candidate_count": 1 if match is not None else 0,
        "finding_identity": (
            {
                "smell": str(config.smell),
                "left_file": str(left.project_path).replace("\\", "/"),
                "left_class": str(match[0].class_name or ""),
                "left_method": str(match[0].method),
                "right_file": str(right.project_path).replace("\\", "/"),
                "right_class": str(match[1].class_name or ""),
                "right_method": str(match[1].method),
                "clone_group": str(match[0].rule_id),
            }
            if match is not None
            else _contract_identity(config)
        ),
    }


def _semantic_finding(config: Any, smell: str, evidence: str) -> dict[str, Any]:
    target = _target(config)
    identity = _contract_identity(config)
    selector = _selector_context(config)
    detection = run_java_semantic_detector(config.project_root, include_tests=False)
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
    findings = detection.findings.get(smell, [])
    if smell == "god_class":
        class_name = str(
            identity.get("class")
            or selector.get("target_class")
            or target.class_name
            or ""
        )
        matches = [
            item for item in findings
            if (identity or _same_file(item.file, target.project_path))
            and (not class_name or item.class_name.rsplit(".", 1)[-1].lower() == class_name.rsplit(".", 1)[-1].lower())
        ]
        match = matches[0] if len(matches) == 1 else None
        metrics = _integer_metrics(match.evidence if match else "", ("nom", "nof", "wmc", "loc", "atfd"))
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
                    or _simple_type(_evidence_value(item.evidence, "parent"))
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
            "parent": _evidence_value(match.evidence, "parent"),
            "target_class": _evidence_value(match.evidence, "target_class"),
            "rejection_kind": _evidence_value(match.evidence, "rejection_kind"),
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
                "rule_id": str(item.rule_id or ""),
                "parent": _evidence_value(item.evidence, "parent"),
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
            "finding_identity": _contract_identity(config) or _identity(
                config,
                target,
                class_name=target.class_name or "",
            ),
            "error": "target_class_not_found" if text is None else "",
        }
    return _semantic_finding(config, "god_class", evidence)


def _refused_bequest(config: Any, evidence: str) -> dict[str, Any]:
    snapshot = _semantic_finding(config, "refused_bequest", evidence)
    if config.language != "java" or not config.locations:
        return snapshot
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
    )
    snapshot["contract_snapshot"] = (
        impact_map.get("target_contract")
        if impact_map.get("ok")
        else {
            "ok": False,
            "error": impact_map.get("error", "capability_impact_map_unavailable"),
        }
    )
    return snapshot


def _integer_metrics(evidence: str, names: tuple[str, ...]) -> dict[str, int]:
    allowed = "|".join(re.escape(name) for name in names)
    return {name: int(value) for name, value in re.findall(rf"\b({allowed})=(\d+)\b", evidence)}


def _evidence_value(evidence: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}=([^;,\s]+)", evidence)
    return match.group(1).strip() if match else ""


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
