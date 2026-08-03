"""Metric adapters for the generic checkpoint contract."""
from __future__ import annotations

import ast
import hashlib
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
from .java.catalog_identity import CATALOG_IDENTITY_SCHEMA
from .java.semantic_detector import god_class_product_profile
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


_JAVA_GUARD_ENTRY_MODULES = {
    "long_method": ("smell_core.java.target_guard_predicates",),
    "nested_complexity": ("smell_core.java.target_guard_predicates",),
    "long_parameter_list": ("smell_core.java.target_relational_guards",),
    "feature_envy": (
        "smell_core.java.target_semantic_guards",
        "smell_core.java.target_feature_envy_scope",
    ),
    "data_clumps": ("smell_core.java.target_relational_guards",),
    "code_clone_type1": ("smell_core.java.target_clone_guard",),
    "god_class": ("smell_core.java.target_semantic_guards",),
    "refused_bequest": (
        "smell_core.java.target_semantic_guards",
        "smell_core.java.target_relation_scope",
    ),
    "switch_statements": ("smell_core.java.target_guard_predicates",),
    "mysterious_name": ("smell_core.java.target_guard_predicates",),
    "dead_code": ("smell_core.java.target_semantic_guards",),
}

_JAVA_GUARD_DISPATCH_MODULES = frozenset(
    module
    for modules in _JAVA_GUARD_ENTRY_MODULES.values()
    for module in modules
)
_JAVA_GUARD_ORCHESTRATOR_MODULE = "smell_core.java.target_guard"
_JAVA_GUARD_PROFILE_OWNER = "checkpoint_adapters.py"


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
    relative_files = sorted(
        {_JAVA_GUARD_PROFILE_OWNER, *_java_guard_dependency_closure(smell)}
    )
    files: list[dict[str, str]] = []
    digest = hashlib.sha256()
    for relative in relative_files:
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


def _java_guard_dependency_closure(smell: str) -> tuple[str, ...]:
    """Return the deterministic in-package import closure for one Guard route.

    ``target_guard`` is the shared orchestrator.  Its function-local imports
    are dispatch edges, so only the entries for the selected smell are
    followed.  Every other ``smell_core`` import is traversed transitively.
    Missing modules and invalid syntax are profile errors, never a reason to
    silently retain the previous hand-written file set.
    """
    entries = _JAVA_GUARD_ENTRY_MODULES.get(smell)
    if entries is None:
        raise ValueError(f"unsupported Java Guard implementation profile: {smell}")

    pending = [_JAVA_GUARD_ORCHESTRATOR_MODULE, *entries]
    visited: set[str] = set()
    relative_files: set[str] = set()
    allowed_dispatch = frozenset(entries)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = _smell_core_module_path(module)
        relative_files.add(path.relative_to(Path(__file__).resolve().parent).as_posix())
        # Importing a submodule executes every parent package initializer.
        # Treat those files as normal closure nodes so their imports are also
        # followed instead of silently omitting package-level behavior.
        parts = module.split(".")
        for length in range(1, len(parts)):
            package = ".".join(parts[:length])
            if package not in visited:
                pending.append(package)
        for imported, function_local in _smell_core_imports(module, path):
            if (
                module == _JAVA_GUARD_ORCHESTRATOR_MODULE
                and function_local
                and imported in _JAVA_GUARD_DISPATCH_MODULES
                and imported not in allowed_dispatch
            ):
                continue
            if imported not in visited:
                pending.append(imported)
    return tuple(sorted(relative_files))


def _smell_core_module_path(module: str) -> Path:
    package_root = Path(__file__).resolve().parent
    if module == "smell_core":
        candidates = (package_root / "__init__.py",)
    elif module.startswith("smell_core."):
        relative = Path(*module.split(".")[1:])
        candidates = (
            package_root / relative.with_suffix(".py"),
            package_root / relative / "__init__.py",
        )
    else:
        raise RuntimeError(f"Guard implementation import escaped smell_core: {module}")
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if len(existing) != 1:
        raise RuntimeError(
            f"Guard implementation module must resolve exactly once: {module}"
        )
    return existing[0]


def _smell_core_imports(module: str, path: Path) -> tuple[tuple[str, bool], ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise RuntimeError(f"Cannot inspect Guard implementation imports: {path}") from exc

    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[tuple[str, bool]] = set()

    class ImportVisitor(ast.NodeVisitor):
        function_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name == "smell_core" or alias.name.startswith("smell_core."):
                    _smell_core_module_path(alias.name)
                    imports.add((alias.name, self.function_depth > 0))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = _resolve_smell_core_import(package, node.level, node.module)
            if base is None:
                return
            _smell_core_module_path(base)
            imports.add((base, self.function_depth > 0))
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                try:
                    _smell_core_module_path(candidate)
                except RuntimeError:
                    if node.module is None and alias.name != "*":
                        raise
                    continue
                imports.add((candidate, self.function_depth > 0))

    ImportVisitor().visit(tree)
    return tuple(sorted(imports))


def _resolve_smell_core_import(
    package: str,
    level: int,
    imported_module: str | None,
) -> str | None:
    if level:
        parts = package.split(".")
        keep = len(parts) - level + 1
        if keep < 1:
            raise RuntimeError(
                f"Guard implementation relative import escapes smell_core: {package}"
            )
        resolved = parts[:keep]
        if imported_module:
            resolved.extend(imported_module.split("."))
        module = ".".join(resolved)
        if module != "smell_core" and not module.startswith("smell_core."):
            raise RuntimeError(
                f"Guard implementation relative import escaped smell_core: {module}"
            )
        return module
    if imported_module == "smell_core" or str(imported_module).startswith("smell_core."):
        return str(imported_module)
    return None


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


_ADAPTERS: dict[str, Callable[[Any, str], dict[str, Any]]] = {
    "long_method": _long_method,
    "nested_complexity": _nested_complexity,
    "long_parameter_list": _long_parameter_list,
    "feature_envy": _feature_envy,
    "data_clumps": _data_clumps,
    "code_clone_type1": _code_clone,
    "god_class": _god_class,
    "switch_statements": _switch_statements,
    "mysterious_name": _mysterious_name,
    "dead_code": _dead_code,
}
