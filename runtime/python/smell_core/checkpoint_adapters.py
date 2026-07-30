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
from .feature_envy import (
    analyze_feature_envy_target as analyze_generic_feature_envy_target,
    feature_envy_receiver_from_evidence,
)
from .mysterious_name import (
    detect_mysterious_names as detect_generic_mysterious_names,
    find_matching_name_finding,
)
from .java.ast_ncss import run_ast_ncss
from .java.data_clumps import data_clump_occurrence_threshold, detect_data_clump_occurrences
from .java.detector_utils import (
    parse_parent_from_evidence,
    parse_structural_expectation,
    parse_target_class,
    parse_target_parameter_count,
)
from .java.semantic_detector import (
    analyze_feature_envy_target,
    build_refused_bequest_impact_map,
    find_matching_semantic_finding,
    run_java_semantic_detector,
)
from .java.syntactic_detector import (
    VAR_DECL_RE,
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


def capture_metric_snapshot(config: Any, evidence: str) -> dict[str, Any]:
    adapter = _ADAPTERS.get(str(config.smell))
    if adapter is None:
        return {"ok": False, "adapter": "unsupported", "objectives": {}, "error": "unsupported_smell"}
    try:
        snapshot = adapter(config, str(evidence or ""))
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
    return snapshot


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
        return {"ok": True, "objectives": {_objective_name(smell): 0}, "target_missing": True}
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
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {_objective_name(smell): float(score)},
            "target_missing": False,
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
    method = target.method or _method_hint(evidence)
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
    return {
        "ok": True,
        "detector": detector,
        "objectives": {_objective_name(smell): score},
        "target_missing": match is None and not _method_exists_in_zero_threshold_scan(findings, target, config),
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
            }
        signature = extract_function_signature(target, "java")
        if signature is None:
            return {
                "ok": True,
                "detector": "tree_sitter_java_declaration",
                "objectives": {"parameter_count": 0},
                "target_missing": True,
            }
        return {
            "ok": True,
            "detector": "tree_sitter_java_declaration",
            "objectives": {
                "parameter_count": len(signature.parameter_fingerprints),
            },
            "target_missing": False,
            "target_method": signature.name,
            "target_start_line": signature.start_line,
            "target_signature": signature.signature_text,
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
        }
    snippet = extract_snippet(target, config.language)
    if snippet is None:
        return {
            "ok": True,
            "detector": "java_syntactic_detector",
            "objectives": {"switch_case_count": 0, "switch_density": 0.0},
            "target_missing": True,
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
        },
        "switch_count": switch_count,
    }


def _mysterious_name(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    kind, name = parse_mysterious_evidence(evidence)
    if not name:
        return {
            "ok": False,
            "detector": "java_syntactic_detector",
            "objectives": {},
            "error": "target_name_missing_from_evidence",
        }
    if config.language == "java":
        if not target.file_path.is_file():
            return {
                "ok": True,
                "detector": "java_exact_identifier_anchor",
                "objectives": {"target_suspicious_name_present": 0},
                "target_missing": True,
                "target_kind": kind,
                "target_name": name,
            }
        try:
            _, methods = load_project_model(config.project_root, [target.file_path])
        except Exception as exc:
            return {
                "ok": False,
                "detector": "java_exact_identifier_anchor",
                "objectives": {},
                "error": str(exc),
            }
        target_method = str(target.method or "").split("(", 1)[0].strip()
        target_line = int(target.line or target.start_line or 0)
        candidates = list(methods)
        if target_method:
            candidates = [method for method in candidates if method.method_name == target_method]
        elif target_line:
            containing = [
                method
                for method in candidates
                if method.begin_line <= target_line <= method.end_line
            ]
            if containing:
                candidates = containing
            elif candidates:
                candidates = [
                    min(candidates, key=lambda method: abs(method.begin_line - target_line))
                ]
        if not candidates:
            return {
                "ok": True,
                "detector": "java_exact_identifier_anchor",
                "objectives": {"target_suspicious_name_present": 0},
                "target_missing": True,
                "target_kind": kind,
                "target_name": name,
            }
        if kind == "method":
            present = any(method.method_name == name for method in candidates)
        elif kind == "param":
            present = any(name in method.parameter_names for method in candidates)
        else:
            present = any(
                name in VAR_DECL_RE.findall(mask_comments_and_strings(method.body_text))
                for method in candidates
            )
        return {
            "ok": True,
            "detector": "java_exact_identifier_anchor",
            "objectives": {"target_suspicious_name_present": 1 if present else 0},
            "finding_present": present,
            "target_kind": kind,
            "target_name": name,
        }
    if config.language != "java":
        if not target.file_path.is_file():
            return {
                "ok": True,
                "detector": "tree_sitter_generic",
                "objectives": {"target_suspicious_name_present": 0},
                "target_missing": True,
                "target_kind": kind,
                "target_name": name,
            }
        snippet = extract_snippet(target, config.language)
        if snippet is None:
            return {
                "ok": True,
                "detector": "tree_sitter_generic",
                "objectives": {"target_suspicious_name_present": 0},
                "target_missing": True,
                "target_kind": kind,
                "target_name": name,
            }
        findings = detect_generic_mysterious_names(target.file_path, language=config.language)
        match = find_matching_name_finding(
            findings,
            kind=kind,
            name=name,
            scope=(snippet.start_line, snippet.end_line),
        )
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {"target_suspicious_name_present": 1 if match else 0},
            "finding_present": match is not None,
            "target_kind": kind,
            "target_name": name,
        }


def _dead_code(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    target_name = method_basename(target.method) or _method_hint(evidence)
    if not target_name:
        return {
            "ok": False,
            "detector": "target_declaration_resolver",
            "objectives": {},
            "error": "target_name_missing_from_location_and_evidence",
        }
    anchored_target = target if target.method else replace(target, method=target_name)
    if not target.file_path.is_file():
        present = False
    else:
        present = extract_snippet(anchored_target, config.language) is not None
    return {
        "ok": True,
        "detector": "target_declaration_resolver",
        "objectives": {"target_declaration_present": 1 if present else 0},
        "target_missing": not present,
        "target_name": target_name,
    }


def _feature_envy(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    if config.language != "java":
        expected_receiver = feature_envy_receiver_from_evidence(evidence)
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
        }
    receiver_match = re.search(r"(?:^|;\s*)envied_type=([^;]+)", evidence, flags=re.IGNORECASE)
    expected_receiver = receiver_match.group(1).strip() if receiver_match else ""
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
    if not expected_receiver:
        expected_receiver = str(profile.get("dominant_receiver_type") or "")
        profile["expected_receiver_type"] = expected_receiver
        profile["expected_receiver_access"] = int(profile.get("dominant_receiver_access") or 0)
    return {
        **profile,
        "adapter": "feature_envy",
        "detector": "python_semantic_detector",
        "objectives": {"expected_receiver_access": int(profile.get("expected_receiver_access") or 0)},
    }


def _data_clumps(config: Any, evidence: str) -> dict[str, Any]:
    if config.language == "java":
        analysis = detect_data_clump_occurrences(config.project_root, evidence=evidence, limit=20)
        threshold = data_clump_occurrence_threshold()
        detector = "python_semantic_detector"
    else:
        analysis = detect_generic_data_clump_occurrences(
            config.project_root,
            language=config.language,
            evidence=evidence,
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
            "clone_pair_present": score > 0,
        }
    detection = run_java_syntactic_detector(
        config.project_root,
        target_files=[left.file_path, right.file_path],
        thresholds={"code_clone_min_tokens": 1},
        include_mysterious_name=False,
    )
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
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
    }


def _semantic_finding(config: Any, smell: str, evidence: str) -> dict[str, Any]:
    target = _target(config)
    detection = run_java_semantic_detector(config.project_root)
    if not detection.ok:
        return {"ok": False, "objectives": {}, "error": detection.error}
    findings = detection.findings.get(smell, [])
    if smell == "god_class":
        class_name = str(target.class_name or _evidence_value(evidence, "class"))
        matches = [
            item for item in findings
            if _same_file(item.file, target.project_path)
            and (not class_name or item.class_name.rsplit(".", 1)[-1].lower() == class_name.rsplit(".", 1)[-1].lower())
        ]
        match = matches[0] if matches else None
        metrics = _integer_metrics(match.evidence if match else "", ("nom", "nof", "wmc", "loc", "atfd"))
        baseline_hint = _integer_metrics(evidence, ("nom", "nof", "wmc", "loc", "atfd"))
        names = tuple(baseline_hint) or ("nom", "wmc", "loc", "atfd")
        objectives = {name: float(metrics.get(name, 0)) for name in names}
    else:
        parent = parse_parent_from_evidence(evidence)
        match = find_matching_semantic_finding(
            findings,
            target_file=target.file_path,
            project_root=config.project_root,
            method=None,
            line=target.line,
            evidence_parent=parent,
        )
        values = _integer_metrics(match.evidence if match else "", ("suspicious_overrides", "overrides", "super_calls"))
        semantic_objectives = {
            "refusal_score": round(float(match.score), 6) if match else 0.0,
            "suspicious_overrides": float(values.get("suspicious_overrides", 0)),
        }
        rejection_signals = _target_rejection_signals(config, target, evidence)
        objectives = {**semantic_objectives, "rejection_signals": float(rejection_signals)}
    return {
        "ok": True,
        "detector": "python_semantic_detector",
        "objectives": objectives,
        "finding_present": match is not None,
        "evidence": match.evidence if match else "",
    }


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
            "error": "target_class_not_found" if text is None else "",
        }
    return _semantic_finding(config, "god_class", evidence)


def _refused_bequest(config: Any, evidence: str) -> dict[str, Any]:
    snapshot = _semantic_finding(config, "refused_bequest", evidence)
    if (
        config.language != "java"
        or parse_structural_expectation(evidence) != "capability_split"
        or not config.locations
    ):
        return snapshot
    target = _target(config)
    impact_map = build_refused_bequest_impact_map(
        config.project_root,
        target_file=target.file_path,
        method=target.method,
        line=target.line,
        reported_parent=parse_parent_from_evidence(evidence),
        target_parameter_count=parse_target_parameter_count(evidence),
        target_class_name=parse_target_class(evidence) or str(target.class_name or ""),
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


def _method_hint(evidence: str) -> str:
    for pattern in (
        r"\bmethod\s+'([^'(]+)\s*\(",
        r"\bmethod=([^;,(\s]+)",
    ):
        match = re.search(pattern, str(evidence or ""), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _target_rejection_signals(config: Any, target: Any, evidence: str) -> int:
    """Continuous target-level semantics for weak Refused Bequest labels."""
    try:
        snippet = extract_snippet(target, config.language)
    except Exception:
        snippet = None
    if snippet is None:
        return 0
    body = snippet.body_text
    signals = 0
    if re.search(r"\bthrow\s+new\s+UnsupportedOperationException\b", body):
        signals += 1
    requires_empty = bool(re.search(r"(?:empty_override|resource_leak_contract)", evidence, re.IGNORECASE))
    if requires_empty and count_meaningful_lines(body, config.language) == 0:
        signals += 1
    requires_null_return = bool(re.search(r"\breturns_null\b", evidence, re.IGNORECASE))
    if requires_null_return and re.search(r"\breturn\s+null\s*;", body):
        signals += 1
    return signals


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
