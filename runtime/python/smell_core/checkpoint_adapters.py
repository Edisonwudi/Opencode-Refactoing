"""Metric adapters for the generic checkpoint contract."""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .analysis import (
    clone_normalized_tokens,
    clone_normalized_tokens_with_lines,
    clone_normalized_token_score,
    count_meaningful_lines,
    count_parameters,
    estimate_nesting_depth,
    estimate_switch_branches,
    explicit_target_files_parseability,
    extract_class_definition_candidate_records,
    extract_snippet,
    extract_snippet_candidates,
    function_signatures_in_file,
    function_signatures_in_text,
    method_basename,
    nonjava_finding_threshold,
    python_switch_metrics,
    source_syntax_issue_witnesses,
    syntax_issue_witness_additions,
)
from .compatibility_contract import TARGET_LOCAL_COMPATIBILITY_CONTRACT
from .data_clumps import (
    data_clump_body_window_contract_available,
    data_clump_declaration_identity_contract_available,
    data_clump_occurrence_threshold as generic_data_clump_occurrence_threshold,
    evaluate_data_clump_checkpoint_contract,
    evaluate_data_clump_targets,
)
from .data_clump_migration import (
    DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
    DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT,
)
from .feature_envy_target_contract import (
    FEATURE_ENVY_TARGET_CONTRACT,
    feature_envy_target_snapshot,
)
from .feature_envy import (
    FEATURE_ENVY_FOREIGN_RATIO,
    FEATURE_ENVY_MIN_FOREIGN_ACCESS,
    FEATURE_ENVY_MIN_LOC,
    FEATURE_ENVY_RATIO_DENOMINATOR_CONTRACT,
)
from .mysterious_name import (
    MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT,
    MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT,
    MYSTERIOUS_NAME_SOURCE_PARSEABILITY_CONTRACT,
    MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
    evaluate_mysterious_name_target,
)
from .target_patch_identity import (
    AST_DECLARATION_IDENTITY_CONTRACT,
    CLONE_RETAINED_ENDPOINT_REANCHOR_CONTRACT,
    DATA_CLUMP_CONSTRUCTOR_REANCHOR_CONTRACT,
    FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT,
    SAME_HUNK_IDENTIFIER_REPLACEMENT_CONTRACT,
    TARGET_ANCHOR_DELETION_CONTRACT,
    TARGET_PATCH_IDENTITY_CONTRACT,
    ast_declaration_identity,
    current_target_added_blocks,
    evaluate_clone_target_patch_identity,
    evaluate_target_anchor_deletions,
    evaluate_target_patch_identity,
    previous_target_removed_blocks,
    target_declaration_deletion_witness,
    validate_ast_declaration_identity,
    validate_target_declaration_deletion_witness,
)
from .guard_scope import (
    GuardScopeError,
    MAX_GUARD_ANALYSIS_BYTES,
    MAX_GUARD_ANALYSIS_FILES,
    read_current_bytes,
    validate_guard_analysis_scope,
)
from .god_class import (
    nonjava_god_class_metrics,
    nonjava_god_class_product_profile,
)
from .java.source_layout import standard_test_root
from .java.catalog_identity import CATALOG_IDENTITY_SCHEMA
from .java.semantic_detector import god_class_product_profile
from .java.target_guard import capture_java_target_guard, evaluate_java_target_guard
from .loop_policy import CHECKPOINT_SMELLS


NONJAVA_TARGET_GUARD_PROFILE_VERSION = 2
DATA_CLUMPS_TARGET_GUARD_PROFILE_VERSION = 4
CODE_CLONE_TARGET_GUARD_PROFILE_VERSION = 5
MYSTERIOUS_NAME_TARGET_GUARD_PROFILE_VERSION = 6
FEATURE_ENVY_TARGET_GUARD_PROFILE_VERSION = 5
GOD_CLASS_TARGET_GUARD_PROFILE_VERSION = 6
DEAD_CODE_TARGET_GUARD_PROFILE_VERSION = 3
CLONE_RELATED_OCCURRENCE_CLOSURE_CONTRACT = (
    "frozen-complete-declaration-removed-occurrence-closure-v1"
)
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
        "selection_contract": "capture-line-verify-frozen-signature-v3",
    },
    "long_parameter_list": {"metric": "parameter_count", "finding_min": 6},
    "nested_complexity": {
        "metric": "cognitive_complexity",
        "finding_min": 20,
        "selection_contract": "capture-line-verify-frozen-signature-v3",
    },
    "switch_statements": {
        "definition": "target_method_contains_switch",
        "selection_contract": "capture-line-verify-frozen-signature-v3",
    },
    "code_clone_type1": {
        "definition": "exact_contiguous_token_window_in_target_method_pair",
        "finding_min_tokens": 30,
        "selection_contract": "body_window_then_complete_method_window-v2",
        "declaration_identity_contract": TARGET_PATCH_IDENTITY_CONTRACT,
        "ast_declaration_identity": AST_DECLARATION_IDENTITY_CONTRACT,
        "size_metric": "selected_exact_window_tokens",
        "relocation_check": "target_endpoints_plus_changed_methods_near_copy_count-v2",
        "catalog_identity_schema": CATALOG_IDENTITY_SCHEMA,
        "consolidation_contract": (
            "same-declaration-exact-body-single-production-hunk-v2"
        ),
        "related_occurrence_closure": (
            CLONE_RELATED_OCCURRENCE_CLOSURE_CONTRACT
        ),
        "removed_declaration_witness": (
            "normalized-complete-token-sha256-with-boundaries-v1"
        ),
        "target_deletion_contract": TARGET_ANCHOR_DELETION_CONTRACT,
        "retained_endpoint_reanchor_contract": (
            CLONE_RETAINED_ENDPOINT_REANCHOR_CONTRACT
        ),
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
        "definition": "frozen_target_symbol_has_valid_successor",
        "profile": "strict",
        "selection_contract": "parser-declared-container-and-symbol-slot-v1",
        "successor_contract": MYSTERIOUS_NAME_SUCCESSOR_CONTRACT,
        "parameter_successor": "same-declaration-slot-only-v1",
        "local_successor": "unique-one-to-one-same-target-hunk-v1",
        "same_hunk_witness_contract": (
            SAME_HUNK_IDENTIFIER_REPLACEMENT_CONTRACT
        ),
        "container_identity_contract": (
            MYSTERIOUS_NAME_CONTAINER_IDENTITY_CONTRACT
        ),
        "container_continuity_contract": (
            MYSTERIOUS_NAME_CONTAINER_CONTINUITY_CONTRACT
        ),
        "non_target_container_closure": (
            "complete-declaration-sha256-preserved-v1"
        ),
        "new_name_policy": "strict-symbol-name-must-be-clean-v1",
        "reference_closure": "target-container-exact-identifier-count-v1",
        "active_parse_file_limit": 1,
        "source_evaluation": "frozen-container-only-v1",
        "source_parseability_contract": (
            MYSTERIOUS_NAME_SOURCE_PARSEABILITY_CONTRACT
        ),
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


def capture_metric_snapshot(
    config: Any,
    evidence: str,
    *,
    changed_patch: str | None = None,
    compatibility_patch: str | None = None,
) -> dict[str, Any]:
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
        target_scope = _explicit_nonjava_target_scope(config)
        if str(config.smell) == "data_clumps":
            snapshot = _data_clumps(
                config,
                "",
                changed_patch=changed_patch,
                compatibility_patch=compatibility_patch,
            )
        elif str(config.smell) == "code_clone_type1":
            snapshot = _code_clone(config, "", changed_patch=changed_patch)
        elif str(config.smell) == "feature_envy":
            snapshot = _feature_envy(config, "", changed_patch=changed_patch)
        elif str(config.smell) == "mysterious_name":
            snapshot = _mysterious_name(
                config,
                "",
                changed_patch=changed_patch,
            )
        else:
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
    snapshot.setdefault("guard_scope", target_scope)
    if snapshot.get("ok") is True and "candidate_count" not in snapshot:
        snapshot["ok"] = False
        snapshot.setdefault("error", "DETECTOR_CANDIDATE_COUNT_UNAVAILABLE")
    return snapshot


def _explicit_nonjava_target_scope(config: Any) -> dict[str, Any]:
    """Freeze the only source files a non-Java Target Guard may inspect."""
    root = Path(config.project_root).expanduser().resolve()
    files: set[str] = set()
    for target in config.locations:
        try:
            files.add(target.file_path.resolve().relative_to(root).as_posix())
        except (OSError, ValueError) as exc:
            raise GuardScopeError(
                "TARGET_OUTSIDE_PROJECT_ROOT",
                "Non-Java Guard target is outside project root",
                target=str(target.file_path),
                project_root=str(root),
            ) from exc
    ordered = tuple(sorted(files))
    validate_guard_analysis_scope(root, ordered)
    source_bytes = sum(
        (root / relative).stat().st_size
        for relative in ordered
        if (root / relative).is_file()
    )
    return {
        "mode": "explicit_target_locations",
        "files": list(ordered),
        "file_count": len(ordered),
        "source_bytes": source_bytes,
        "source_discovery": "forbidden",
    }


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
        detector_specific = dict(DETECTOR_PROFILES.get(smell, {}))
        threshold = nonjava_finding_threshold(language, smell, -1)
        if threshold >= 0:
            threshold_key = "finding_min_tokens" if smell == "code_clone_type1" else "finding_min"
            detector_specific[threshold_key] = threshold
        if smell == "long_method":
            detector_specific["metric"] = "meaningful_line_count"
        elif smell == "nested_complexity":
            detector_specific["metric"] = "max_nesting_depth"
        if smell == "feature_envy":
            # The non-Java detector is target-local and receiver-root based;
            # it does not use Designite's Java type-resolution semantics.
            detector_specific = {
                "definition": (
                    "tree-sitter-explicit-target-access-and-ratio-predicate-v2"
                ),
                "metric": "alias-folded-expected-receiver-member-access",
                "receiver_identity": "canonical-root-identifier",
                "finding_min_method_loc": FEATURE_ENVY_MIN_LOC,
                "finding_min_receiver_access": (
                    FEATURE_ENVY_MIN_FOREIGN_ACCESS
                ),
                "finding_min_receiver_ratio": FEATURE_ENVY_FOREIGN_RATIO,
                "alias_folding": "simple-local-alias-root-provenance-v1",
                "ratio_denominator_contract": (
                    FEATURE_ENVY_RATIO_DENOMINATOR_CONTRACT
                ),
                "candidate_evaluation": "one-explicit-target-declaration-only",
                "source_parseability_contract": (
                    "explicit-target-declaration-subtree-no-errors-v1"
                ),
                "declaration_uniqueness_contract": (
                    "same-file-owner-name-full-parameter-fingerprint-exactly-one-v1"
                ),
            }
        elif smell == "data_clumps":
            detector_specific = {
                "definition": "frozen_group_present_at_explicit_function_locations",
                "minimum_group_size": 3,
                "min_occurrences": 3,
                "group_identity": "tree-sitter-normalized-type-and-name-v1",
                "relation_query": "caller-listed-function-locations-only-v1",
                "candidate_evaluation": "explicit-signatures-only-v1",
                "continuity_contract": "frozen-parameter-slot-name-or-type-v2",
                "target_identity_contract": (
                    "target-old-to-current-hunk-anchor-v1"
                ),
                "constructor_signature_reanchor_contract": (
                    DATA_CLUMP_CONSTRUCTOR_REANCHOR_CONTRACT
                ),
                "ast_declaration_identity": (
                    AST_DECLARATION_IDENTITY_CONTRACT
                ),
                "changed_hunk_group_contract": (
                    "added-target-hunk-signatures-v1"
                ),
                "inline_copy_contract": (
                    "lowest-frequency-body-windows-with-source-relocation-and-"
                    "target-added-lines-v2"
                ),
                "compatibility_contract": TARGET_LOCAL_COMPATIBILITY_CONTRACT,
                "declaration_migration_contract": (
                    DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT
                ),
                "migration_closure_contract": (
                    DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT
                ),
                "migration_final_verification": "project_full",
            }
            if language == "cpp":
                detector_specific["cpp_owner_resolution"] = (
                    "function-declarator-spine-v2"
                )
        elif smell == "dead_code":
            detector_specific = {
                "definition": "explicit_target_declaration_present",
                "selection_contract": "caller-selected-function-only-v1",
                "absence_transition": "exact-target-declaration-deletion-v2",
                "target_declaration_parseability": (
                    "selected-declaration-subtree-no-error-or-missing-v1"
                ),
                "parser_recovery_contract": (
                    "frozen-explicit-target-file-witness-multiset-no-additions-v1"
                ),
            }
        elif smell == "god_class":
            detector_specific = {
                "definition": "source_derived_multi_metric_profile",
                "profile": nonjava_god_class_product_profile(),
                "target_kind": (
                    "source_module" if language == "c" else "class_definition"
                ),
                "target_definition_contract": (
                    "caller-selected-complete-source-module-v1"
                    if language == "c"
                    else "unique-body-bearing-class-definition-v1"
                ),
                "forward_declarations": (
                    "not_applicable" if language == "c" else "excluded"
                ),
                "target_definition_parseability": (
                    "selected-target-frozen-parser-recovery-no-additions-v1"
                ),
            }
            if language == "cpp":
                detector_specific["cpp_owner_definition_closure"] = (
                    "same-explicit-file-exact-qualified-owner-v1"
                )
        profile_version = (
            CODE_CLONE_TARGET_GUARD_PROFILE_VERSION
            if smell == "code_clone_type1"
            else (
                DATA_CLUMPS_TARGET_GUARD_PROFILE_VERSION
                if smell == "data_clumps"
                else (
                    FEATURE_ENVY_TARGET_GUARD_PROFILE_VERSION
                    if smell == "feature_envy"
                    else (
                        MYSTERIOUS_NAME_TARGET_GUARD_PROFILE_VERSION
                        if smell == "mysterious_name"
                        else (
                            GOD_CLASS_TARGET_GUARD_PROFILE_VERSION
                            if smell == "god_class"
                            else (
                                DEAD_CODE_TARGET_GUARD_PROFILE_VERSION
                                if smell == "dead_code"
                                else NONJAVA_TARGET_GUARD_PROFILE_VERSION
                            )
                        )
                    )
                )
            )
        )
        profile = {
            "version": f"nonjava-target-guard/{language}/{smell}/v{profile_version}",
            "smell": smell,
            "language": language,
            "scope": "explicit-target-locations-v1",
            "scope_file_limit": MAX_GUARD_ANALYSIS_FILES,
            "scope_byte_limit": MAX_GUARD_ANALYSIS_BYTES,
            "source_discovery": "forbidden",
            "smell_evidence": "audit-only",
            **detector_specific,
        }
        if smell == "code_clone_type1":
            profile["scope"] = (
                "explicit-targets-plus-changed-production-hunks-v1"
            )
            profile["production_hunk_byte_limit"] = MAX_GUARD_ANALYSIS_BYTES
        elif smell == "mysterious_name":
            profile["scope"] = "explicit-target-plus-target-file-hunks-v1"
            profile["production_hunk_byte_limit"] = MAX_GUARD_ANALYSIS_BYTES
    if smell == "feature_envy":
        # Relocation analysis is implemented only by the Java target Guard.
        profile["reject_finding_relocation_in_impact_cone"] = language == "java"
        if language != "java":
            profile["selection_contract"] = (
                "target-context-explicit-receiver-root-v1"
            )
            profile["declaration_continuity_contract"] = (
                FEATURE_ENVY_TARGET_CONTRACT
            )
            profile["target_patch_identity_contract"] = (
                FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT
            )
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
        "nested_complexity": estimate_nesting_depth(snippet, config.language),
        "long_parameter_list": count_parameters(snippet.signature_text, config.language),
    }[smell]
    threshold = float(
        nonjava_finding_threshold(config.language, smell, PRODUCT_THRESHOLDS[smell])
    )
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
        # Keep checkpoint objective keys stable for existing consumers. The
        # v2 detector profile records the actual non-Java metric semantics.
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
    if config.language == "python":
        # Python has no switch; count dispatch branches (if/elif chains and
        # match statements) in the explicit target function.
        switch_count, case_count, density = python_switch_metrics(snippet)
    else:
        switch_count = len(re.findall(r"\bswitch\s*\(", snippet.body_text))
        case_count = estimate_switch_branches(snippet, config.language)
        body_lines = max(1, count_meaningful_lines(snippet.body_text, config.language))
        density = case_count / body_lines
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


def _mysterious_name(
    config: Any,
    evidence: str,
    *,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    del evidence
    if len(config.locations) != 1:
        return {
            "ok": False,
            "detector": "tree_sitter_generic",
            "objectives": {},
            "finding_present": False,
            "candidate_count": len(config.locations),
            "error": "MN_EXACTLY_ONE_TARGET_LOCATION_REQUIRED",
        }
    target = _target(config)
    contract_identity = _contract_identity(config)
    selector = _selector_context(config)
    frozen_selector = {
        "symbol_kind": str(
            contract_identity.get("symbol_kind")
            or selector.get("symbol_kind")
            or ""
        ),
        "symbol_name": str(
            contract_identity.get("symbol_name")
            or selector.get("symbol_name")
            or ""
        ),
    }
    return evaluate_mysterious_name_target(
        target,
        language=str(config.language),
        selector=frozen_selector,
        frozen_identity=contract_identity,
        changed_patch=changed_patch,
    )



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
    if not target.file_path.is_file():
        return {
            "ok": True,
            "detector": "tree_sitter_generic",
            "objectives": {
                "unused_private_finding_present": 0,
                "target_declaration_present": 0,
            },
            "finding_present": False,
            "candidate_count": 0,
            "target_missing": True,
            "target_file_exists": False,
            "target_file_parseable": False,
            "target_name": target_name,
            "finding_identity": identity
            or _identity(config, target, method=target.method or target_name),
        }
    syntax_witnesses = source_syntax_issue_witnesses(
        target.file_path,
        str(config.language),
    )
    if str(config.language) == "python" and syntax_witnesses:
        return {
            "ok": False,
            "detector": "tree_sitter_generic",
            "objectives": {
                "unused_private_finding_present": 0,
                "target_declaration_present": 0,
            },
            "error": "target_file_parse_failed",
            "finding_present": False,
            "candidate_count": 0,
            "target_missing": True,
            "target_file_exists": True,
            "target_file_parseable": False,
            "parser_recovery_required": True,
            "target_syntax_issue_witnesses": syntax_witnesses,
            "target_name": target_name,
            "finding_identity": identity
            or _identity(config, target, method=target.method or target_name),
        }
    candidates = extract_snippet_candidates(anchored_target, str(config.language))
    exact_candidates = [
        (snippet, parseable)
        for snippet, parseable in candidates
        if target.line is not None and int(snippet.start_line) == int(target.line)
    ]
    if len(exact_candidates) > 1:
        return {
            "ok": False,
            "detector": "tree_sitter_generic",
            "objectives": {},
            "error": "target_declaration_ambiguous",
            "candidate_count": len(exact_candidates),
            "target_missing": True,
            "target_file_exists": True,
            "target_file_parseable": not syntax_witnesses,
            "parser_recovery_required": bool(syntax_witnesses),
            "target_syntax_issue_witnesses": syntax_witnesses,
            "target_name": target_name,
            "finding_identity": identity
            or _identity(config, target, method=target.method or target_name),
        }
    if exact_candidates and exact_candidates[0][1] is not True:
        return {
            "ok": False,
            "detector": "tree_sitter_generic",
            "objectives": {
                "unused_private_finding_present": 0,
                "target_declaration_present": 0,
            },
            "error": "target_declaration_syntax_invalid",
            "finding_present": False,
            "candidate_count": 0,
            "target_missing": True,
            "target_file_exists": True,
            "target_file_parseable": not syntax_witnesses,
            "parser_recovery_required": bool(syntax_witnesses),
            "target_syntax_issue_witnesses": syntax_witnesses,
            "target_name": target_name,
            "finding_identity": identity
            or _identity(config, target, method=target.method or target_name),
        }
    snippet = exact_candidates[0][0] if exact_candidates else None
    present = snippet is not None
    result = {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {
            "unused_private_finding_present": 1 if present else 0,
            "target_declaration_present": 1 if present else 0,
        },
        "finding_present": present,
        "candidate_count": 1 if present else 0,
        "target_missing": not present,
        "target_file_exists": True,
        "target_file_parseable": not syntax_witnesses,
        "parser_recovery_required": bool(syntax_witnesses),
        "target_syntax_issue_witnesses": syntax_witnesses,
        "target_name": target_name,
        "finding_identity": identity or _identity(config, target, method=target.method or target_name),
    }
    if snippet is None:
        return result
    result["declaration_witness"] = _dead_code_declaration_witness(
        target.file_path,
        target.project_path.as_posix(),
        str(target.method or target_name),
        int(target.line),
        int(snippet.start_line),
        int(snippet.end_line),
        snippet.signature_text,
        snippet.body_text,
        str(config.language),
    )
    return result


def authorize_dead_code_target_absence(
    config: Any,
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    production_patch: str,
    changed_production_source_files: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Authorize only an exact, target-local Dead Code declaration deletion.

    The caller supplies a production-only patch already scoped to the frozen
    target path.  This function reads and parses only that current target file;
    it never searches for references or declarations elsewhere in the project.
    """
    result = dict(current)
    if (
        str(getattr(config, "smell", "")) != "dead_code"
        or str(getattr(config, "language", "")) not in {"python", "c", "cpp"}
        or result.get("target_missing") is not True
    ):
        return result
    evidence: dict[str, Any] = {
        "contract": "exact-target-declaration-deletion-v2",
        "allowed": False,
        "reason": "DEAD_CODE_EXACT_DELETION_NOT_PROVEN",
    }
    result["target_absence_allowed"] = False
    result["target_absence_evidence"] = evidence
    if result.get("ok") is not True:
        evidence["reason"] = "DEAD_CODE_CURRENT_TARGET_UNAVAILABLE"
        return result
    if len(getattr(config, "locations", ())) != 1:
        evidence["reason"] = "DEAD_CODE_EXACT_TARGET_REQUIRED"
        return result
    if str(getattr(config, "verification_mode", "") or "") != "project_full":
        evidence["reason"] = "DEAD_CODE_PROJECT_FULL_REQUIRED"
        return result
    target = config.locations[0]
    root = Path(config.project_root).expanduser().resolve()
    try:
        target_file = target.file_path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        evidence["reason"] = "DEAD_CODE_TARGET_OUTSIDE_PROJECT"
        return result
    evidence["target_file"] = target_file
    try:
        current_bytes = read_current_bytes(root, target_file)
    except GuardScopeError as exc:
        evidence["reason"] = str(exc.status)
        return result
    if current_bytes is None:
        evidence["reason"] = "DEAD_CODE_TARGET_FILE_MISSING"
        return result
    syntax_additions = syntax_issue_witness_additions(
        baseline.get("target_syntax_issue_witnesses"),
        result.get("target_syntax_issue_witnesses"),
    )
    if syntax_additions:
        evidence["reason"] = "DEAD_CODE_TARGET_FILE_SYNTAX_REGRESSION"
        evidence["new_syntax_issue_witnesses"] = syntax_additions
        return result
    witness = baseline.get("declaration_witness")
    if not isinstance(witness, dict) or int(witness.get("schema_version") or 0) != 2:
        evidence["reason"] = "DEAD_CODE_BASELINE_WITNESS_MISSING"
        return result
    target_name = method_basename(str(target.method or "")) or ""
    if (
        str(witness.get("target_file") or "") != target_file
        or str(witness.get("target_name") or "") != target_name
        or int(witness.get("target_line") or 0) != int(target.line or 0)
    ):
        evidence["reason"] = "DEAD_CODE_BASELINE_WITNESS_MISMATCH"
        return result
    changed_files = {str(path).replace("\\", "/") for path in changed_production_source_files}
    if target_file not in changed_files or not production_patch.strip():
        evidence["reason"] = "DEAD_CODE_TARGET_PRODUCTION_DIFF_MISSING"
        return result
    start_line = int(witness.get("start_line") or 0)
    end_line = int(witness.get("end_line") or 0)
    expected_hashes = witness.get("line_hashes")
    body_token_count = witness.get("body_token_count")
    body_token_sha256 = str(witness.get("body_token_sha256") or "")
    body_token_rolling64 = str(witness.get("body_token_rolling64") or "")
    if (
        start_line < 1
        or end_line < start_line
        or not isinstance(expected_hashes, list)
        or len(expected_hashes) != end_line - start_line + 1
        or any(not isinstance(item, str) or len(item) != 64 for item in expected_hashes)
        or str(witness.get("declaration_sha256") or "")
        != _dead_code_line_hashes_digest(expected_hashes)
        or str(witness.get("body_token_normalization") or "")
        != "clone-normalized-tokens-v1"
        or isinstance(body_token_count, bool)
        or not isinstance(body_token_count, int)
        or body_token_count < 0
        or re.fullmatch(r"[0-9a-f]{64}", body_token_sha256) is None
        or re.fullmatch(r"[0-9a-f]{16}", body_token_rolling64) is None
    ):
        evidence["reason"] = "DEAD_CODE_BASELINE_WITNESS_INVALID"
        return result
    (
        removed_lines,
        replacement_lines,
        added_blocks,
        added_current_lines,
    ) = _unified_patch_deletion_evidence(production_patch)
    replaced_target_lines = sorted(
        set(range(start_line, end_line + 1)).intersection(replacement_lines)
    )
    if replaced_target_lines:
        evidence["reason"] = "DEAD_CODE_REPLACEMENT_IS_NOT_DELETION"
        evidence["replacement_old_line"] = replaced_target_lines[0]
        return result
    actual_hashes: list[str] = []
    for line_number in range(start_line, end_line + 1):
        payload = removed_lines.get(line_number)
        if payload is None:
            evidence["reason"] = "DEAD_CODE_EXACT_DELETION_EVIDENCE_MISSING"
            evidence["missing_old_line"] = line_number
            return result
        actual_hashes.append(hashlib.sha256(payload).hexdigest())
    if actual_hashes != expected_hashes:
        evidence["reason"] = "DEAD_CODE_EXACT_DELETION_CONTENT_MISMATCH"
        return result
    relocation_block = _dead_code_relocation_block(
        added_blocks,
        language=str(config.language),
        body_token_count=body_token_count,
        body_token_sha256=body_token_sha256,
        body_token_rolling64=body_token_rolling64,
    )
    if relocation_block is not None:
        evidence["reason"] = "DEAD_CODE_RELOCATION_NOT_DELETION"
        evidence["relocation_added_block"] = relocation_block
        evidence["body_token_count"] = body_token_count
        return result
    try:
        relocation_function = _dead_code_relocation_changed_function(
            target,
            current_bytes,
            added_current_lines,
            language=str(config.language),
            body_token_count=body_token_count,
            body_token_sha256=body_token_sha256,
            body_token_rolling64=body_token_rolling64,
        )
    except Exception:
        evidence["reason"] = "DEAD_CODE_RELOCATION_ANALYSIS_UNAVAILABLE"
        return result
    if relocation_function is not None:
        evidence["reason"] = "DEAD_CODE_RELOCATION_NOT_DELETION"
        evidence.update(relocation_function)
        evidence["body_token_count"] = body_token_count
        return result
    evidence.update(
        {
            "allowed": True,
            "reason": "DEAD_CODE_EXACT_TARGET_DELETED",
            "target_line": int(target.line or 0),
            "declaration_start_line": start_line,
            "declaration_end_line": end_line,
            "removed_line_count": len(actual_hashes),
            "declaration_sha256": witness["declaration_sha256"],
            "target_file_exists": True,
            "target_file_parseable": True,
            "production_diff": True,
        }
    )
    result["target_absence_allowed"] = True
    return result


def _dead_code_declaration_witness(
    file_path: Path,
    target_file: str,
    target_method: str,
    target_line: int,
    start_line: int,
    end_line: int,
    signature_text: str,
    body_text: str,
    language: str,
) -> dict[str, Any]:
    source_lines = file_path.read_bytes().splitlines()
    if start_line < 1 or end_line < start_line or end_line > len(source_lines):
        raise ValueError("dead_code declaration span is outside the target file")
    line_hashes = [
        hashlib.sha256(payload).hexdigest()
        for payload in source_lines[start_line - 1 : end_line]
    ]
    body_tokens = clone_normalized_tokens(body_text, language)
    return {
        "schema_version": 2,
        "target_file": target_file,
        "target_method": target_method,
        "target_name": method_basename(target_method) or "",
        "target_line": target_line,
        "start_line": start_line,
        "end_line": end_line,
        "signature_sha256": hashlib.sha256(
            signature_text.encode("utf-8", errors="surrogateescape")
        ).hexdigest(),
        "line_hashes": line_hashes,
        "declaration_sha256": _dead_code_line_hashes_digest(line_hashes),
        "body_token_normalization": "clone-normalized-tokens-v1",
        "body_token_count": len(body_tokens),
        "body_token_sha256": _dead_code_token_sequence_sha256(body_tokens),
        "body_token_rolling64": f"{_dead_code_token_rolling64(body_tokens):016x}",
    }


def _dead_code_line_hashes_digest(line_hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(line_hashes).encode("ascii")).hexdigest()


_UNIFIED_HUNK = re.compile(
    rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_DEAD_CODE_ROLLING_BASE = 1_000_003
_DEAD_CODE_ROLLING_MASK = (1 << 64) - 1


def _unified_patch_deletion_evidence(
    production_patch: str,
) -> tuple[dict[int, bytes], set[int], list[bytes], set[int]]:
    removed: dict[int, bytes] = {}
    replacement_lines: set[int] = set()
    added_blocks: list[bytes] = []
    added_current_lines: set[int] = set()
    current_added_block: list[bytes] = []
    change_block_removed: list[int] = []
    old_line: int | None = None
    current_line: int | None = None

    def flush_added_block() -> None:
        if current_added_block:
            added_blocks.append(b"\n".join(current_added_block))
            current_added_block.clear()

    for raw_line in production_patch.encode(
        "utf-8", errors="surrogateescape"
    ).splitlines():
        match = _UNIFIED_HUNK.match(raw_line)
        if match is not None:
            flush_added_block()
            old_line = int(match.group(1))
            current_line = int(match.group(3))
            change_block_removed = []
            continue
        if raw_line.startswith(b"diff --git "):
            flush_added_block()
            old_line = None
            current_line = None
            change_block_removed = []
            continue
        if old_line is None or current_line is None or not raw_line:
            continue
        prefix = raw_line[:1]
        if prefix == b"-" and not raw_line.startswith(b"---"):
            flush_added_block()
            removed[old_line] = raw_line[1:]
            change_block_removed.append(old_line)
            old_line += 1
        elif prefix == b" ":
            flush_added_block()
            change_block_removed = []
            old_line += 1
            current_line += 1
        elif prefix == b"+":
            # A removal immediately paired with additions is a replacement or
            # rename, not exact deletion of the frozen declaration.  A blank
            # formatting line carries no declaration or body tokens and must
            # not turn an otherwise byte-exact deletion into a replacement.
            added_payload = raw_line[1:]
            if added_payload.strip():
                replacement_lines.update(change_block_removed)
            current_added_block.append(added_payload)
            added_current_lines.add(current_line)
            current_line += 1
        elif prefix == b"\\":
            continue
        else:
            flush_added_block()
            old_line = None
            current_line = None
            change_block_removed = []
    flush_added_block()
    return removed, replacement_lines, added_blocks, added_current_lines


def _dead_code_token_sequence_sha256(tokens: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for token in tokens:
        payload = token.encode("utf-8", errors="surrogateescape")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _dead_code_token_unit(token: str) -> int:
    payload = token.encode("utf-8", errors="surrogateescape")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _dead_code_token_rolling64(tokens: tuple[str, ...]) -> int:
    value = 0
    for token in tokens:
        value = (
            value * _DEAD_CODE_ROLLING_BASE + _dead_code_token_unit(token)
        ) & _DEAD_CODE_ROLLING_MASK
    return value


def _dead_code_relocation_block(
    added_blocks: list[bytes],
    *,
    language: str,
    body_token_count: int,
    body_token_sha256: str,
    body_token_rolling64: str,
) -> int | None:
    for block_index, block in enumerate(added_blocks, start=1):
        tokens = clone_normalized_tokens(
            block.decode("utf-8", errors="surrogateescape"),
            language,
        )
        if body_token_count == 0:
            # An empty frozen body has no discriminating token sequence. Any
            # substantive addition makes relocation possible, so fail closed.
            if tokens:
                return block_index
            continue
        if _dead_code_token_window_starts(
            tokens,
            body_token_count=body_token_count,
            body_token_sha256=body_token_sha256,
            body_token_rolling64=body_token_rolling64,
        ):
            return block_index
    return None


def _dead_code_relocation_changed_function(
    target: Any,
    current_bytes: bytes,
    added_current_lines: set[int],
    *,
    language: str,
    body_token_count: int,
    body_token_sha256: str,
    body_token_rolling64: str,
) -> dict[str, int] | None:
    """Check only current functions selected by ``+`` lines in the target patch."""
    if body_token_count == 0 or not added_current_lines:
        return None
    spans = {
        (signature.start_line, signature.end_line)
        for signature in function_signatures_in_file(target.file_path, language)
        if any(
            signature.start_line <= added_line <= signature.end_line
            for added_line in added_current_lines
        )
    }
    source_lines = current_bytes.splitlines()
    for function_start, function_end in sorted(spans):
        function_added_lines = {
            line
            for line in added_current_lines
            if function_start <= line <= function_end
        }
        if not function_added_lines or function_end > len(source_lines):
            continue
        function_text = b"\n".join(
            source_lines[function_start - 1 : function_end]
        ).decode("utf-8", errors="surrogateescape")
        located_tokens = clone_normalized_tokens_with_lines(function_text, language)
        tokens = tuple(token for token, _lines in located_tokens)
        window_starts = _dead_code_token_window_starts(
            tokens,
            body_token_count=body_token_count,
            body_token_sha256=body_token_sha256,
            body_token_rolling64=body_token_rolling64,
        )
        for window_start in window_starts:
            window = located_tokens[
                window_start : window_start + body_token_count
            ]
            window_lines = {
                function_start + relative_line - 1
                for _token, relative_lines in window
                for relative_line in relative_lines
            }
            matching_added = sorted(window_lines.intersection(function_added_lines))
            if matching_added:
                return {
                    "relocation_function_start_line": function_start,
                    "relocation_added_line": matching_added[0],
                }
    return None


def _dead_code_token_window_starts(
    tokens: tuple[str, ...],
    *,
    body_token_count: int,
    body_token_sha256: str,
    body_token_rolling64: str,
) -> tuple[int, ...]:
    if body_token_count <= 0 or len(tokens) < body_token_count:
        return ()
    expected_rolling = int(body_token_rolling64, 16)
    units = [_dead_code_token_unit(token) for token in tokens]
    power = pow(
        _DEAD_CODE_ROLLING_BASE,
        body_token_count - 1,
        1 << 64,
    )
    rolling = 0
    for unit in units[:body_token_count]:
        rolling = (
            rolling * _DEAD_CODE_ROLLING_BASE + unit
        ) & _DEAD_CODE_ROLLING_MASK
    matches: list[int] = []
    for start in range(0, len(tokens) - body_token_count + 1):
        if (
            rolling == expected_rolling
            and _dead_code_token_sequence_sha256(
                tokens[start : start + body_token_count]
            )
            == body_token_sha256
        ):
            matches.append(start)
        next_index = start + body_token_count
        if next_index >= len(tokens):
            break
        rolling = (
            (rolling - units[start] * power) * _DEAD_CODE_ROLLING_BASE
            + units[next_index]
        ) & _DEAD_CODE_ROLLING_MASK
    return tuple(matches)


def _feature_envy(
    config: Any,
    evidence: str,
    *,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    """Delegate to the explicit selector and target-hunk identity contract."""
    del evidence
    return feature_envy_target_snapshot(config, changed_patch=changed_patch)


def _data_clumps(
    config: Any,
    evidence: str,
    *,
    changed_patch: str | None = None,
    compatibility_patch: str | None = None,
) -> dict[str, Any]:
    selector_group = str(_selector_context(config).get("group") or "")
    finding_contract = getattr(config, "finding_contract", None)
    baseline_occurrence_contract = (
        finding_contract.get("baseline_occurrence_contract")
        if isinstance(finding_contract, dict)
        else None
    )
    analysis = evaluate_data_clump_targets(
        config.project_root,
        language=config.language,
        group=selector_group,
        targets=config.locations,
        baseline_occurrence_contract=baseline_occurrence_contract,
        changed_patch=changed_patch,
    )
    threshold = generic_data_clump_occurrence_threshold()
    detector = "tree_sitter_generic"
    occurrence_count = int(analysis.get("occurrence_count") or 0)
    passing_max = max(0, threshold - 1)
    occurrence_contract = list(analysis.get("occurrence_contract") or [])
    has_frozen_finding_contract = isinstance(finding_contract, dict) and bool(
        finding_contract
    )
    baseline_body_window_contract_available = (
        data_clump_body_window_contract_available(occurrence_contract)
    )
    baseline_declaration_identity_contract_available = (
        data_clump_declaration_identity_contract_available(
            occurrence_contract
        )
    )
    baseline_capture_ready = (
        baseline_body_window_contract_available
        and baseline_declaration_identity_contract_available
        and not list(analysis.get("unresolved_targets") or [])
    )
    if not has_frozen_finding_contract:
        checkpoint_error = (
            "baseline_body_window_contract_unavailable"
            if not baseline_body_window_contract_available
            else (
                "baseline_declaration_identity_contract_unavailable"
                if not baseline_declaration_identity_contract_available
                else ""
            )
        )
        checkpoint_closure = {
            "continuity_ok": True,
            "continuity_occurrence_count": occurrence_count,
            "continuity_occurrences": list(analysis.get("occurrences") or []),
            "inline_copy_contract_available": (
                baseline_body_window_contract_available
            ),
            "inline_copy_analysis_ok": baseline_body_window_contract_available,
            "inline_copy_expansions": [],
        }
        if checkpoint_error:
            checkpoint_closure["checkpoint_contract_error"] = checkpoint_error
    else:
        checkpoint_closure = evaluate_data_clump_checkpoint_contract(
            analysis,
            language=config.language,
            baseline_occurrence_contract=baseline_occurrence_contract,
            changed_patch=changed_patch,
            compatibility_patch=compatibility_patch,
        )
    target_patch_identity_failures = list(
        checkpoint_closure.get("target_patch_identity_failures") or []
    )
    target_missing = bool(
        analysis.get("unresolved_targets")
        or (
            has_frozen_finding_contract
            and checkpoint_closure.get("target_patch_identity_ok") is not True
        )
    )
    identity = _contract_identity(config) or {
        "smell": str(config.smell),
        "group": str(analysis.get("group") or selector_group),
        "targets": [
            {
                "file": str(target.project_path).replace("\\", "/"),
                "method": str(target.method or ""),
                "line": int(target.line or 0),
            }
            for target in config.locations
        ],
    }
    return {
        "ok": bool(
            analysis.get("success")
            and (
                has_frozen_finding_contract
                or baseline_capture_ready
            )
        ),
        "detector": detector,
        "group": analysis.get("group", ""),
        "objectives": {"occurrence_count": occurrence_count},
        "passing_max": passing_max,
        "remaining_reductions": max(0, occurrence_count - passing_max),
        "occurrences": list(analysis.get("occurrences") or []),
        "occurrence_contract": occurrence_contract,
        **checkpoint_closure,
        "target_missing": target_missing,
        "unresolved_targets": [
            *list(analysis.get("unresolved_targets") or []),
            *target_patch_identity_failures,
        ],
        "target_identity_collision": bool(
            analysis.get("target_identity_collision")
        ),
        "target_identity_collisions": list(
            analysis.get("target_identity_collisions") or []
        ),
        "scope_mode": analysis.get("scope_mode", "explicit_target_locations"),
        "scope_files": list(analysis.get("scope_files") or []),
        "parsed_file_count": len(analysis.get("scope_files") or []),
        "finding_present": occurrence_count >= threshold,
        "candidate_count": 1 if occurrence_count >= threshold else 0,
        "finding_identity": identity,
        "error": (
            analysis.get("error")
            or checkpoint_closure.get("checkpoint_contract_error", "")
        ),
    }


def _clone_token_sha256(tokens: tuple[str, ...]) -> str:
    return (
        hashlib.sha256("\0".join(tokens).encode("utf-8")).hexdigest()
        if tokens
        else ""
    )


def _clone_target_anchor_record(
    config: Any,
    target_index: int,
    target: Any,
    snippet: Any | None,
) -> dict[str, Any]:
    root = Path(config.project_root).expanduser().resolve()
    try:
        file_name = target.file_path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        file_name = str(target.project_path).replace("\\", "/")
    signature_tokens = (
        clone_normalized_tokens(snippet.signature_text, config.language)
        if snippet is not None
        else ()
    )
    body_tokens = (
        clone_normalized_tokens(snippet.body_text, config.language)
        if snippet is not None
        else ()
    )
    complete_tokens = (
        clone_normalized_tokens(
            snippet.declaration_text,
            config.language,
        )
        if snippet is not None
        else ()
    )
    return {
        "target_index": target_index,
        "file": file_name,
        "method": str(target.method or ""),
        "begin_line": int(snippet.start_line) if snippet is not None else 0,
        "resolved": snippet is not None,
        "signature_sha256": _clone_token_sha256(signature_tokens),
        "body_token_count": len(body_tokens),
        "body_sha256": _clone_token_sha256(body_tokens),
        "complete_token_count": len(complete_tokens),
        "complete_sha256": _clone_token_sha256(complete_tokens),
        "complete_first_token_sha256": _clone_token_sha256(
            complete_tokens[:1]
        ),
        "complete_last_token_sha256": _clone_token_sha256(
            complete_tokens[-1:]
        ),
        "declaration_deletion_witness": (
            target_declaration_deletion_witness(
                target.file_path.read_bytes(),
                int(snippet.declaration_start_line or snippet.start_line),
                int(snippet.end_line),
            )
            if snippet is not None
            else None
        ),
        "declaration_identity": (
            ast_declaration_identity(
                snippet.declared_name,
                snippet.owner_qualified_name,
            )
            if snippet is not None
            else None
        ),
    }


def _clone_target_anchor_records(
    config: Any,
    snippets: tuple[Any | None, Any | None],
) -> list[dict[str, Any]]:
    return [
        _clone_target_anchor_record(config, target_index, target, snippet)
        for target_index, (target, snippet) in enumerate(
            zip(config.locations[:2], snippets)
        )
    ]


def _clone_patch_mapped_snippet(
    config: Any,
    target_index: int,
    target: Any,
    default_snippet: Any | None,
    *,
    changed_patch: str | None,
) -> Any | None:
    """Prefer the unique frozen-anchor candidate over nearest same-name code."""

    contract = getattr(config, "finding_contract", None)
    baseline_targets = (
        contract.get("baseline_target_anchors")
        if isinstance(contract, dict)
        else None
    )
    if not isinstance(baseline_targets, list) or changed_patch is None:
        return default_snippet
    frozen = next(
        (
            item
            for item in baseline_targets
            if isinstance(item, dict)
            and item.get("target_index") == target_index
        ),
        None,
    )
    if frozen is None or not target.file_path.is_file():
        return default_snippet

    matches: list[Any] = []
    for snippet, parseable in extract_snippet_candidates(
        target,
        config.language,
    ):
        if parseable is not True:
            continue
        candidate = _clone_target_anchor_record(
            config,
            target_index,
            target,
            snippet,
        )
        identity = evaluate_clone_target_patch_identity(
            [frozen],
            [candidate],
            changed_patch=changed_patch,
        )
        if identity.get("ok") is True:
            matches.append(snippet)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return default_snippet


def _clone_target_patch_identity(
    config: Any,
    current_targets: list[dict[str, Any]],
    *,
    changed_patch: str | None,
) -> dict[str, Any] | None:
    contract = getattr(config, "finding_contract", None)
    if not isinstance(contract, dict) or not contract:
        return None
    baseline_targets = contract.get("baseline_target_anchors")
    baseline_targets_valid = bool(
        isinstance(baseline_targets, list)
        and len(baseline_targets) == 2
        and {
            item.get("target_index")
            for item in baseline_targets
            if isinstance(item, dict)
        }
        == {0, 1}
        and all(
            isinstance(item, dict)
            and bool(str(item.get("file") or ""))
            and isinstance(item.get("begin_line"), int)
            and int(item.get("begin_line") or 0) > 0
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(item.get("signature_sha256") or ""),
            )
            is not None
            and isinstance(item.get("body_token_count"), int)
            and not isinstance(item.get("body_token_count"), bool)
            and int(item.get("body_token_count") or 0) > 0
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(item.get("body_sha256") or ""),
            )
            is not None
            and isinstance(item.get("complete_token_count"), int)
            and not isinstance(item.get("complete_token_count"), bool)
            and int(item.get("complete_token_count") or 0) > 0
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(item.get("complete_sha256") or ""),
            )
            is not None
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(item.get("complete_first_token_sha256") or ""),
            )
            is not None
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(item.get("complete_last_token_sha256") or ""),
            )
            is not None
            and validate_ast_declaration_identity(
                item.get("declaration_identity")
            )[0]
            is not None
            for item in baseline_targets
        )
    )
    identity = evaluate_clone_target_patch_identity(
        baseline_targets if baseline_targets_valid else [],
        current_targets,
        changed_patch=changed_patch,
    )
    return identity


def _clone_related_removed_occurrence_closure(
    config: Any,
    *,
    changed_patch: str | None,
) -> dict[str, Any] | None:
    """Reject deletion of exact clone occurrences outside frozen endpoints.

    The closure consumes only the two frozen complete-declaration witnesses
    and old-side removed lines from the caller-supplied production patch.  It
    neither opens nor discovers project source.  Unchanged hunk context is not
    considered, so an untouched third occurrence remains outside this edit-
    local safety gate.
    """

    contract = getattr(config, "finding_contract", None)
    if not isinstance(contract, dict) or not contract:
        return None
    baseline = contract.get("baseline_target_anchors")
    result: dict[str, Any] = {
        "contract": CLONE_RELATED_OCCURRENCE_CLOSURE_CONTRACT,
        "ok": False,
        "reason": "",
        "removed_occurrences": [],
        "unfrozen_removed_occurrences": [],
    }
    if not isinstance(baseline, list) or len(baseline) != 2:
        result["reason"] = "baseline_target_anchors_unavailable"
        return result

    sha_pattern = re.compile(r"[0-9a-f]{64}")
    canonical_specs: dict[
        tuple[str, int, str, str, str, str, str],
        dict[str, Any],
    ] = {}
    validation_failures: list[dict[str, Any]] = []
    for item in baseline:
        if not isinstance(item, dict):
            validation_failures.append({
                "reason": "baseline_target_anchor_invalid",
            })
            continue
        target_index = item.get("target_index")
        file_name = str(item.get("file") or "")
        body_count = item.get("body_token_count")
        complete_count = item.get("complete_token_count")
        signature_sha = str(item.get("signature_sha256") or "")
        body_sha = str(item.get("body_sha256") or "")
        complete_sha = str(item.get("complete_sha256") or "")
        first_sha = str(item.get("complete_first_token_sha256") or "")
        last_sha = str(item.get("complete_last_token_sha256") or "")
        witness, witness_error = (
            validate_target_declaration_deletion_witness(
                item.get("declaration_deletion_witness")
            )
        )
        if (
            not isinstance(target_index, int)
            or isinstance(target_index, bool)
            or not file_name
            or isinstance(body_count, bool)
            or not isinstance(body_count, int)
            or body_count <= 0
            or isinstance(complete_count, bool)
            or not isinstance(complete_count, int)
            or complete_count <= 0
            or any(
                sha_pattern.fullmatch(value) is None
                for value in (
                    signature_sha,
                    body_sha,
                    complete_sha,
                    first_sha,
                    last_sha,
                )
            )
            or witness is None
        ):
            validation_failures.append({
                "target_index": target_index,
                "file": file_name,
                "reason": "baseline_complete_declaration_witness_invalid",
                "error": witness_error,
            })
            continue
        key = (
            signature_sha,
            int(body_count),
            body_sha,
            int(complete_count),
            complete_sha,
            first_sha,
            last_sha,
        )
        spec = canonical_specs.setdefault(key, {
            "signature_sha256": signature_sha,
            "body_token_count": int(body_count),
            "body_sha256": body_sha,
            "complete_token_count": int(complete_count),
            "complete_sha256": complete_sha,
            "complete_first_token_sha256": first_sha,
            "complete_last_token_sha256": last_sha,
            "frozen_spans": [],
        })
        spec["frozen_spans"].append({
            "target_index": target_index,
            "file": file_name,
            "start_line": int(witness["start_line"]),
            "end_line": int(witness["end_line"]),
        })
    if validation_failures or not canonical_specs:
        result["reason"] = "baseline_complete_declaration_witness_invalid"
        result["failures"] = validation_failures
        return result

    removed_blocks, patch_error = previous_target_removed_blocks(
        changed_patch
    )
    if patch_error:
        result["reason"] = "changed_production_patch_unavailable"
        result["error"] = patch_error
        return result

    occurrences: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for block in removed_blocks:
        file_name = str(block.get("file") or "")
        block_start = int(block.get("start_line") or 0)
        body_text = str(block.get("body_text") or "")
        if not file_name or block_start < 1 or not body_text.strip():
            continue
        located_tokens = clone_normalized_tokens_with_lines(
            body_text,
            config.language,
        )
        tokens = tuple(item[0] for item in located_tokens)
        token_hashes: dict[str, str] = {}

        def singleton_hash(token: str) -> str:
            cached = token_hashes.get(token)
            if cached is None:
                cached = _clone_token_sha256((token,))
                token_hashes[token] = cached
            return cached

        for spec in canonical_specs.values():
            token_count = int(spec["complete_token_count"])
            if len(tokens) < token_count:
                continue
            offset = 0
            while offset <= len(tokens) - token_count:
                if (
                    singleton_hash(tokens[offset])
                    != spec["complete_first_token_sha256"]
                    or singleton_hash(tokens[offset + token_count - 1])
                    != spec["complete_last_token_sha256"]
                    or _clone_token_sha256(
                        tokens[offset : offset + token_count]
                    )
                    != spec["complete_sha256"]
                ):
                    offset += 1
                    continue
                relative_lines = [
                    line
                    for _token, lines in located_tokens[
                        offset : offset + token_count
                    ]
                    for line in lines
                ]
                if not relative_lines:
                    offset += token_count
                    continue
                start_line = block_start + min(relative_lines) - 1
                end_line = block_start + max(relative_lines) - 1
                frozen_indexes = sorted(
                    int(span["target_index"])
                    for span in spec["frozen_spans"]
                    if span["file"] == file_name
                    and int(span["start_line"]) <= start_line
                    and end_line <= int(span["end_line"])
                )
                occurrence_key = (
                    file_name,
                    start_line,
                    end_line,
                    str(spec["complete_sha256"]),
                )
                occurrences[occurrence_key] = {
                    "file": file_name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "signature_sha256": spec["signature_sha256"],
                    "body_sha256": spec["body_sha256"],
                    "complete_sha256": spec["complete_sha256"],
                    "frozen_target_indexes": frozen_indexes,
                }
                # Exact complete declarations are non-overlapping units.  A
                # match consumes its full token span before looking for the
                # next removed occurrence.
                offset += token_count

    ordered_occurrences = [
        occurrences[key]
        for key in sorted(occurrences)
    ]
    unfrozen = [
        item
        for item in ordered_occurrences
        if not item["frozen_target_indexes"]
    ]
    result["removed_occurrences"] = ordered_occurrences
    result["unfrozen_removed_occurrences"] = unfrozen
    result["removed_occurrence_count"] = len(ordered_occurrences)
    result["unfrozen_removed_occurrence_count"] = len(unfrozen)
    if unfrozen:
        result["reason"] = "unfrozen_related_clone_occurrence_deleted"
        return result
    result["ok"] = True
    result["reason"] = "RELATED_REMOVED_OCCURRENCE_CLOSURE_PRESERVED"
    return result


def _clone_consolidation_contract(
    config: Any,
    current_targets: list[dict[str, Any]],
    *,
    changed_patch: str | None,
) -> dict[str, Any]:
    """Authorize one exact shared implementation for identical endpoints.

    The route is intentionally narrow: both frozen endpoints must have the
    same parser-derived declaration identity, signature, and complete token
    body.  Missing old declarations need exact deletion hunks, while the
    resulting implementation must exist exactly once either at a retained
    endpoint or as one complete declaration in a changed production hunk.
    No project source discovery is performed.
    """

    result: dict[str, Any] = {
        "contract": "same-declaration-exact-body-single-production-hunk-v2",
        "ok": False,
        "reason": "",
        "retained_target_indexes": [],
        "relocated_declarations": [],
    }
    contract = getattr(config, "finding_contract", None)
    baseline = (
        list(contract.get("baseline_target_anchors") or [])
        if isinstance(contract, dict)
        else []
    )
    if len(baseline) != 2 or not all(isinstance(item, dict) for item in baseline):
        result["reason"] = "baseline_target_anchors_unavailable"
        return result
    baseline.sort(key=lambda item: int(item.get("target_index", -1)))
    if [item.get("target_index") for item in baseline] != [0, 1]:
        result["reason"] = "baseline_target_anchors_invalid"
        return result

    frozen_identities: list[dict[str, str]] = []
    for item in baseline:
        identity, error = validate_ast_declaration_identity(
            item.get("declaration_identity")
        )
        if identity is None:
            result["reason"] = error or "baseline_declaration_identity_invalid"
            return result
        frozen_identities.append(identity)
    shared_fields = ("signature_sha256", "body_sha256", "complete_sha256")
    if frozen_identities[0] != frozen_identities[1] or any(
        not str(baseline[0].get(field) or "")
        or baseline[0].get(field) != baseline[1].get(field)
        for field in shared_fields
    ):
        result["reason"] = "clone_endpoints_are_not_one_declaration_contract"
        return result
    for count_field in (
        "body_token_count",
        "complete_token_count",
    ):
        value = baseline[0].get(count_field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value != baseline[1].get(count_field)
        ):
            result["reason"] = f"baseline_{count_field}_invalid"
            return result

    current_by_index = {
        int(item.get("target_index")): item
        for item in current_targets
        if isinstance(item.get("target_index"), int)
    }
    retained = [
        item
        for item in current_targets
        if item.get("resolved") is True
    ]
    missing_baseline = [
        item
        for item in baseline
        if current_by_index.get(int(item["target_index"]), {}).get("resolved")
        is not True
    ]
    if not missing_baseline:
        result["reason"] = "clone_endpoints_not_consolidated"
        return result
    if len(retained) > 1:
        result["reason"] = "multiple_clone_endpoints_retained"
        return result

    canonical = baseline[0]
    for item in retained:
        identity, error = validate_ast_declaration_identity(
            item.get("declaration_identity")
        )
        if (
            identity is None
            or error
            or identity != frozen_identities[0]
            or item.get("signature_sha256") != canonical.get("signature_sha256")
            or item.get("body_sha256") != canonical.get("body_sha256")
            or item.get("complete_sha256") != canonical.get("complete_sha256")
            or item.get("complete_token_count")
            != canonical.get("complete_token_count")
        ):
            result["reason"] = "retained_clone_endpoint_changed"
            return result
    if retained:
        retained_indexes = {int(item["target_index"]) for item in retained}
        retained_identity = evaluate_target_patch_identity(
            [
                item
                for item in baseline
                if int(item["target_index"]) in retained_indexes
            ],
            retained,
            changed_patch=changed_patch,
        )
        if retained_identity.get("ok") is not True:
            result["reason"] = "retained_clone_endpoint_identity_failed"
            result["failures"] = list(retained_identity.get("failures") or [])
            result["error"] = str(retained_identity.get("error") or "")
            return result

    deletion = evaluate_target_anchor_deletions(
        missing_baseline,
        current_targets,
        changed_patch=changed_patch,
    )
    if deletion.get("ok") is not True:
        result["reason"] = "clone_endpoint_deletion_unverified"
        result["failures"] = list(deletion.get("failures") or [])
        result["error"] = str(deletion.get("error") or "")
        return result

    added_blocks, patch_error = current_target_added_blocks(changed_patch)
    if patch_error:
        result["reason"] = "clone_consolidation_patch_unavailable"
        result["error"] = patch_error
        return result
    relocated: list[dict[str, Any]] = []
    for block in added_blocks:
        file_name = str(block.get("file") or "")
        block_start = int(block.get("start_line") or 0)
        body_text = str(block.get("body_text") or "")
        if not file_name or block_start < 1 or not body_text.strip():
            continue
        try:
            signatures = function_signatures_in_text(
                body_text,
                config.language,
                file_path=Path(file_name),
                start_line=block_start,
            )
        except Exception as exc:
            result["reason"] = "clone_added_declaration_parse_failed"
            result["error"] = f"{type(exc).__name__}:{exc}"
            return result
        for signature in signatures:
            identity = ast_declaration_identity(
                signature.name,
                signature.owner_qualified_name,
            )
            signature_tokens = clone_normalized_tokens(
                signature.signature_text,
                config.language,
            )
            if (
                identity != frozen_identities[0]
                or _clone_token_sha256(signature_tokens)
                != canonical.get("signature_sha256")
            ):
                continue
            complete_tokens = clone_normalized_tokens(
                signature.declaration_text,
                config.language,
            )
            if (
                len(complete_tokens) == canonical.get("complete_token_count")
                and _clone_token_sha256(complete_tokens)
                == canonical.get("complete_sha256")
            ):
                relocated.append({
                    "file": file_name,
                    "begin_line": int(
                        signature.declaration_start_line
                        or signature.start_line
                    ),
                    "end_line": signature.end_line,
                    "declaration_identity": identity,
                })

    canonical_count = len(retained) + len(relocated)
    if canonical_count != 1:
        result["reason"] = "clone_consolidation_implementation_count_invalid"
        result["implementation_count"] = canonical_count
        result["relocated_declarations"] = relocated
        return result
    result.update({
        "ok": True,
        "reason": "CLONE_ENDPOINTS_CONSOLIDATED",
        "retained_target_indexes": sorted(
            int(item["target_index"]) for item in retained
        ),
        "relocated_declarations": relocated,
        "implementation_count": canonical_count,
        "deletion_contract": deletion.get("contract"),
    })
    return result


def _code_clone(
    config: Any,
    evidence: str,
    *,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    if len(config.locations) < 2:
        return {"ok": False, "objectives": {}, "error": "clone pair requires two locations"}
    left, right = config.locations[:2]
    parseability = explicit_target_files_parseability(
        list(config.locations[:2]),
        config.language,
    )
    if parseability.get("ok") is not True:
        unresolved_targets = _clone_target_anchor_records(
            config,
            (None, None),
        )
        return {
            "ok": False,
            "detector": "tree_sitter_generic",
            "objectives": {"clone_token_count": 0.0},
            "source_file_parseability": parseability,
            "target_anchor_contract": unresolved_targets,
            "target_missing": any(
                item.get("exists") is not True
                for item in list(parseability.get("files") or [])
                if isinstance(item, dict)
            ),
            "unresolved_targets": [0, 1],
            "clone_pair_present": False,
            "finding_present": False,
            "candidate_count": 0,
            "finding_identity": _contract_identity(config) or {
                "smell": str(config.smell),
                "left_file": str(left.project_path).replace("\\", "/"),
                "left_method": str(left.method or ""),
                "right_file": str(right.project_path).replace("\\", "/"),
                "right_method": str(right.method or ""),
            },
            "error": "TARGET_SOURCE_NOT_PARSEABLE",
        }
    left_snippet = (
        extract_snippet(left, config.language) if left.file_path.is_file() else None
    )
    right_snippet = (
        extract_snippet(right, config.language) if right.file_path.is_file() else None
    )
    left_snippet = _clone_patch_mapped_snippet(
        config,
        0,
        left,
        left_snippet,
        changed_patch=changed_patch,
    )
    right_snippet = _clone_patch_mapped_snippet(
        config,
        1,
        right,
        right_snippet,
        changed_patch=changed_patch,
    )
    current_targets = _clone_target_anchor_records(
        config,
        (left_snippet, right_snippet),
    )
    targets_resolved = left_snippet is not None and right_snippet is not None
    score = 0
    if targets_resolved:
        _left_text, _right_text, score = clone_normalized_token_score(
            left_snippet.body_text,
            right_snippet.body_text,
            config.language,
        )
    threshold = nonjava_finding_threshold(
        config.language,
        "code_clone_type1",
        PRODUCT_THRESHOLDS["code_clone_type1"],
    )
    result = {
        "ok": True,
        "detector": "tree_sitter_generic",
        "objectives": {"clone_token_count": float(score)},
        "source_file_parseability": parseability,
        "target_anchor_contract": current_targets,
        "target_missing": not targets_resolved,
        "unresolved_targets": [
            item["target_index"]
            for item in current_targets
            if item.get("resolved") is not True
        ],
        "clone_pair_present": score >= threshold,
        "finding_present": score >= threshold,
        "candidate_count": 1 if score >= threshold else 0,
        "finding_identity": _contract_identity(config) or {
            "smell": str(config.smell),
            "left_file": str(left.project_path).replace("\\", "/"),
            "left_method": str(left.method or ""),
            "right_file": str(right.project_path).replace("\\", "/"),
            "right_method": str(right.method or ""),
        },
    }
    declaration_identity_errors: list[dict[str, Any]] = []
    for item in current_targets:
        _declaration_identity, declaration_error = (
            validate_ast_declaration_identity(item.get("declaration_identity"))
        )
        if _declaration_identity is None:
            declaration_identity_errors.append({
                "target_index": item.get("target_index"),
                "error": declaration_error,
            })
    result["declaration_identity_valid"] = not declaration_identity_errors
    result["declaration_identity_errors"] = declaration_identity_errors
    if declaration_identity_errors:
        result["ok"] = False
        result["error"] = "TARGET_DECLARATION_IDENTITY_UNAVAILABLE"
    identity = _clone_target_patch_identity(
        config,
        current_targets,
        changed_patch=changed_patch,
    )
    if identity is not None:
        result.update({
            "target_patch_identity_ok": identity.get("ok") is True,
            "target_patch_identity_contract": str(identity.get("contract") or ""),
            "target_patch_identity_failures": list(identity.get("failures") or []),
            "target_patch_identity_error": str(identity.get("error") or ""),
            "target_patch_identity_reanchors": list(
                identity.get("retained_endpoint_reanchors") or []
            ),
        })
        if identity.get("ok") is not True:
            result["guard_violations"] = [{
                "code": "CLONE_TARGET_DECLARATION_IDENTITY_FAILED",
                "contract": str(identity.get("contract") or ""),
                "error": str(identity.get("error") or ""),
                "failures": list(identity.get("failures") or []),
            }]
            consolidation = _clone_consolidation_contract(
                config,
                current_targets,
                changed_patch=changed_patch,
            )
            result["clone_consolidation"] = consolidation
            if consolidation.get("ok") is True:
                result.update({
                    "ok": True,
                    "error": "",
                    "target_absence_allowed": True,
                    "target_patch_identity_ok": True,
                    "target_patch_identity_contract": consolidation.get(
                        "contract"
                    ),
                    "target_patch_identity_failures": [],
                    "target_patch_identity_error": "",
                    "clone_pair_present": False,
                    "finding_present": False,
                    "candidate_count": 0,
                    "declaration_identity_authorized_by_consolidation": True,
                })
                result.pop("guard_violations", None)
    related_occurrence_closure = _clone_related_removed_occurrence_closure(
        config,
        changed_patch=changed_patch,
    )
    if related_occurrence_closure is not None:
        result["clone_related_occurrence_closure"] = (
            related_occurrence_closure
        )
        if related_occurrence_closure.get("ok") is not True:
            violations = list(result.get("guard_violations") or [])
            violations.append({
                "code": (
                    "UNFROZEN_RELATED_CLONE_OCCURRENCE_DELETED"
                    if related_occurrence_closure.get("reason")
                    == "unfrozen_related_clone_occurrence_deleted"
                    else "CLONE_RELATED_OCCURRENCE_CLOSURE_UNAVAILABLE"
                ),
                "contract": str(
                    related_occurrence_closure.get("contract") or ""
                ),
                "reason": str(
                    related_occurrence_closure.get("reason") or ""
                ),
                "error": str(
                    related_occurrence_closure.get("error") or ""
                ),
                "unfrozen_removed_occurrences": list(
                    related_occurrence_closure.get(
                        "unfrozen_removed_occurrences"
                    )
                    or []
                ),
            })
            result["guard_violations"] = violations
    return result


def _god_class(config: Any, evidence: str) -> dict[str, Any]:
    target = _target(config)
    candidate_records = extract_class_definition_candidate_records(
        target,
        str(config.language),
    )
    candidates = [
        (str(record.get("text") or ""), bool(record.get("parseable")))
        for record in candidate_records
    ]
    definitions = [text for text, parseable in candidates if parseable]
    selected = candidate_records[0] if len(candidate_records) == 1 else None
    recovery_allowed = str(config.language) in {"c", "cpp"}
    text = (
        str(selected.get("text") or "")
        if selected is not None
        and (bool(selected.get("parseable")) or recovery_allowed)
        else None
    )
    syntax_witnesses = (
        list(selected.get("syntax_issue_witnesses") or [])
        if selected is not None
        else []
    )
    metric_text = text
    metric_class_name = ""
    if text is not None and str(config.language) == "cpp":
        # The explicit target file is already caller-selected.  Read that one
        # file so Owner::method definitions outside the class body contribute
        # their real complexity; no project discovery or source scan occurs.
        try:
            metric_text = target.file_path.read_text(
                encoding="utf-8",
                errors="surrogateescape",
            )
            metric_class_name = str(
                target.class_name
                or (selected or {}).get("declared_name")
                or ""
            )
        except OSError:
            metric_text = None
    metrics = (
        nonjava_god_class_metrics(
            metric_text,
            str(config.language),
            class_name=metric_class_name,
        )
        if metric_text is not None
        else {}
    )
    metric_ready = text is not None and metric_text is not None
    profile = nonjava_god_class_product_profile(metrics)
    error = (
        ""
        if metric_ready
        else (
            "target_source_unavailable"
            if text is not None and metric_text is None
            else "target_class_not_found"
            if not candidates
            else "target_class_definition_syntax_invalid"
            if len(candidates) == 1 and not definitions
            else "target_class_definition_ambiguous"
        )
    )
    finding_present = metric_ready and profile["finding_present"] is True
    return {
        "ok": metric_ready,
        "detector": "tree_sitter_generic",
        "objectives": {name: float(value) for name, value in metrics.items()},
        "god_class_profile": profile,
        "unsupported_metrics": list(profile["unsupported_metrics"]),
        "target_missing": text is None,
        "target_match_count": len(candidates),
        "target_parseable_match_count": len(definitions),
        "parser_recovery_required": bool(syntax_witnesses),
        "target_syntax_issue_witnesses": syntax_witnesses,
        "finding_present": finding_present,
        "candidate_count": 1 if finding_present else 0,
        "finding_identity": _contract_identity(config) or _identity(
            config,
            target,
            class_name=target.class_name or "",
        ),
        "error": error,
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
