"""Java Guard v5 orchestration.

The caller supplies the smell and target context.  This module never discovers
smells or source files: it evaluates one smell predicate against the frozen
target and the explicit verification scope prepared by ``checkpoints.py``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .target_guard_predicates import (
    capture_target_guard_predicate,
    evaluate_target_guard_predicate,
)
from .source_layout import standard_test_root
from ..analysis import method_basename
from ..guard_scope import (
    GuardScopeError,
    build_changed_line_ranges,
    validate_guard_analysis_scope,
)


_LOCAL_PREDICATE_SMELLS = frozenset(
    {
        "long_method",
        "nested_complexity",
        "switch_statements",
        "mysterious_name",
    }
)


def capture_java_target_guard(config: Any) -> dict[str, Any]:
    """Capture exactly one real target smell for c000."""
    return _run_java_target_guard(config, capture=True)


def evaluate_java_target_guard(config: Any) -> dict[str, Any]:
    """Re-evaluate the frozen target and its explicit changed-file scope."""
    return _run_java_target_guard(config, capture=False)


def _run_java_target_guard(config: Any, *, capture: bool) -> dict[str, Any]:
    smell = str(config.smell)
    locations = list(getattr(config, "locations", ()) or ())
    if not locations:
        return _error(smell, "TARGET_CONTEXT_INCOMPLETE")
    selector = dict(getattr(config, "target_context", {}) or {})
    contract = dict(getattr(config, "guard_contract", {}) or {})
    frozen_identity = contract.get("entity_identity")
    if not capture and isinstance(frozen_identity, Mapping):
        # Frozen identity is authoritative; live target_context remains only a
        # selector and cannot replace it.
        selector = {**selector, **dict(frozen_identity)}

    if smell in _LOCAL_PREDICATE_SMELLS:
        predicate = (
            capture_target_guard_predicate
            if capture
            else evaluate_target_guard_predicate
        )
        result = predicate(
            smell,
            Path(config.project_root),
            locations[0],
            selector,
        )
    else:
        result = _dispatch_scoped_guard(
            smell,
            config=config,
            locations=locations,
            selector=selector,
            capture=capture,
        )
    return _normalize(smell, result)


def _dispatch_scoped_guard(
    smell: str,
    *,
    config: Any,
    locations: list[Any],
    selector: dict[str, Any],
    capture: bool,
) -> dict[str, Any]:
    scope = getattr(config, "guard_scope", None)
    root = Path(config.project_root)
    # ``analysis_files`` is deliberately target-only.  The Git diff is
    # metadata from which an individual smell Guard may derive an exact,
    # bounded relation scope; it is never an eager parse scope.
    target_files = _existing_analysis_files(
        root,
        tuple(getattr(scope, "analysis_files", ()) or ()),
    )
    changed_files = _existing_analysis_files(
        root,
        tuple(getattr(scope, "changed_production_files", ()) or ()),
    )
    if smell in {"long_parameter_list", "data_clumps"}:
        from .target_relational_guards import (
            evaluate_data_clumps_guard,
            evaluate_long_parameter_list_guard,
        )
        if smell == "long_parameter_list":
            relation_files = target_files
            if not capture:
                relation_files = tuple(
                    sorted(
                        set(target_files).union(
                            _lpl_changed_candidate_files(
                                root,
                                changed_files,
                                selector,
                            )
                        )
                    )
                )
            _enforce_explicit_scope_budget(root, relation_files)
            return evaluate_long_parameter_list_guard(
                root,
                locations[0],
                selector,
                analysis_files=relation_files,
            )
        # Re-run the same exact three-member relation query at capture and
        # verification.  Reusing only baseline files would allow a clump to be
        # copied to a newly changed file without being observed.
        source_files = _data_clump_candidate_files(
            root,
            str(selector.get("group") or ""),
        )
        return evaluate_data_clumps_guard(
            root,
            locations[0],
            selector,
            analysis_files=target_files,
            source_files=source_files,
        )
    if smell == "code_clone_type1":
        from .target_clone_guard import (
            capture_code_clone_type1,
            evaluate_code_clone_type1,
        )
        clone_files = target_files
        changed_ranges: dict[str, list[tuple[int, int]]] | None = None
        if not capture:
            clone_files = tuple(sorted(set(target_files).union(changed_files)))
            _enforce_explicit_scope_budget(root, clone_files)
            changed_ranges = _changed_range_map(root, scope, changed_files)
        if capture:
            return capture_code_clone_type1(
                root,
                locations,
                analysis_files=clone_files,
            )
        return evaluate_code_clone_type1(
            root,
            locations,
            selector,
            analysis_files=clone_files,
            changed_line_ranges=changed_ranges,
        )
    if smell in {"feature_envy", "god_class", "refused_bequest", "dead_code"}:
        from .target_semantic_guards import (
            capture_target_semantic_guard,
            evaluate_target_semantic_guard,
        )
        explicit_files = target_files
        # Semantic selector admission remains the caller's frozen
        # ``target_context``.  ``selector`` also contains the captured entity
        # identity during verify, whose fields are intentionally not accepted
        # as mutable target-context inputs.
        semantic_selector = dict(getattr(config, "target_context", {}) or {})
        changed_line_ranges: dict[str, list[tuple[int, int]]] = {}
        relation_witness: dict[str, Any] = {}
        contract = dict(getattr(config, "guard_contract", {}) or {})
        location = str(getattr(locations[0], "raw", locations[0]))
        relation_state = _frozen_relation_state(contract)

        # FE and RB often have enough information in the target file itself.
        # Capture that exact target first and expand source ancestry only when
        # the method declaration exists but the smell predicate cannot yet
        # produce a finding.  Optional/unrelated parents must not make an
        # already measurable target unavailable.
        if capture and smell in {"feature_envy", "refused_bequest"}:
            target_only = capture_target_semantic_guard(
                smell,
                root,
                location,
                semantic_selector,
                explicit_files,
            )
            if _semantic_capture_is_sufficient(target_only):
                _attach_relation_witness(
                    target_only,
                    _target_only_relation_witness(explicit_files),
                )
                return target_only
            if not _semantic_capture_needs_relation(target_only):
                return target_only

        use_relation_scope = (
            smell in {"feature_envy", "refused_bequest"}
            and (capture or relation_state != "target_only_sufficient")
        )
        if smell == "feature_envy" and use_relation_scope:
            explicit_files, relation_witness = _feature_envy_target_files(
                root,
                explicit_files,
                locations[0],
                selector,
                capture=capture,
            )
        if smell == "feature_envy" and not capture:
            # Feature Envy is the deliberate exception to target-file-only
            # parsing: every method actually touched by the refactoring diff
            # is checked for relocation.  Unchanged methods in those files are
            # filtered by ``changed_line_ranges`` in the semantic Guard.
            explicit_files = tuple(
                sorted(set(explicit_files).union(changed_files))
            )
            _enforce_explicit_scope_budget(root, explicit_files)
            changed_line_ranges = _changed_range_map(root, scope, changed_files)
        if smell == "refused_bequest" and use_relation_scope:
            explicit_files, relation_witness = _refused_bequest_relation_files(
                root,
                explicit_files,
                locations[0],
                selector,
                capture=capture,
            )
        if (
            smell in {"feature_envy", "refused_bequest"}
            and not capture
            and not use_relation_scope
        ):
            relation_witness = _target_only_relation_witness(explicit_files)
        if capture:
            captured = capture_target_semantic_guard(
                smell,
                root,
                location,
                semantic_selector,
                explicit_files,
            )
            if relation_witness:
                _attach_relation_witness(captured, relation_witness)
            return captured
        evaluated = evaluate_target_semantic_guard(
            smell,
            root,
            location,
            semantic_selector,
            explicit_files,
            {
                "ok": True,
                "entity_identity": dict(contract.get("entity_identity") or {}),
                "witness": contract.get("witness") or [],
            },
            changed_line_ranges=changed_line_ranges,
        )
        if relation_witness:
            _attach_relation_witness(evaluated, relation_witness)
        return evaluated
    return _error(smell, "UNSUPPORTED_GUARD")


def _normalize(smell: str, value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    witness_value = result.get("witness")
    witness_rule_id = ""
    if isinstance(witness_value, Mapping):
        witness_rule_id = str(witness_value.get("predicate_id") or "")
    elif isinstance(witness_value, list) and witness_value:
        first = witness_value[0]
        if isinstance(first, Mapping):
            witness_rule_id = str(first.get("predicate_id") or "")
    rule_id = str(
        result.get("guard_rule_id")
        or witness_rule_id
        or f"java-target-guard/{smell}/v5"
    )
    match_count = int(result.get("target_match_count") or 0)
    present = result.get("target_smell_present") is True
    identity = result.get("entity_identity")
    if not isinstance(identity, dict):
        identity = {}
    violations = result.get("guard_violations")
    if not isinstance(violations, list):
        violations = []
    # Presence is the primary Guard verdict and is what distinguishes
    # IMPROVED from RESOLVED.  It is not a structural regression.
    violations = [
        item
        for item in violations
        if not (
            item == "TARGET_SMELL_REMAINS"
            or isinstance(item, Mapping)
            and str(item.get("code") or "") == "TARGET_SMELL_REMAINS"
        )
    ]
    target_missing = result.get("target_missing") is True
    absence_allowed = result.get("target_absence_allowed") is True or (
        target_missing
        and smell
        in {
            "long_parameter_list",
            "feature_envy",
            "data_clumps",
            "code_clone_type1",
            "refused_bequest",
            "mysterious_name",
            "dead_code",
        }
        and not violations
    )
    return {
        **result,
        "adapter": smell,
        "guard_rule_id": rule_id,
        "target_match_count": match_count,
        "target_smell_present": present,
        "target_missing": target_missing,
        "target_absence_allowed": absence_allowed,
        "entity_identity": identity,
        "witness": result.get("witness") or {},
        "guard_violations": violations,
        # Temporary wire aliases keep the non-Java bridge/result consumer
        # stable.  They are not a detector fallback and are not frozen in c000.
        "detector": rule_id,
        "candidate_count": match_count,
        "finding_present": present,
        "finding_identity": identity,
    }


def _error(smell: str, status: str) -> dict[str, Any]:
    return {
        "ok": False,
        "guard_rule_id": f"java-target-guard/{smell}/v5",
        "target_match_count": 0,
        "target_smell_present": False,
        "target_missing": False,
        "objectives": {},
        "entity_identity": {},
        "witness": {"error": status},
        "guard_violations": [],
        "error": status,
    }


def _existing_analysis_files(
    project_root: Path,
    analysis_files: tuple[str, ...],
) -> tuple[str, ...]:
    root = project_root.expanduser().resolve()
    return tuple(
        relative
        for relative in analysis_files
        if (root / relative).is_file()
    )


def _changed_range_map(
    project_root: Path,
    scope: Any,
    changed_files: tuple[str, ...],
) -> dict[str, list[tuple[int, int]]]:
    """Return current diff ranges for one smell-selected changed scope."""
    frozen = tuple(getattr(scope, "changed_line_ranges", ()) or ())
    if frozen:
        ranges = frozen
    else:
        baseline_commit = str(getattr(scope, "baseline_commit", "") or "")
        ranges = (
            build_changed_line_ranges(
                project_root,
                baseline_commit,
                changed_files,
            )
            if baseline_commit and changed_files
            else ()
        )
    result: dict[str, list[tuple[int, int]]] = {}
    selected = set(changed_files)
    for path, start, end in ranges:
        relative = str(path)
        if relative not in selected:
            continue
        result.setdefault(relative, []).append((int(start), int(end)))
    return result


def _lpl_changed_candidate_files(
    project_root: Path,
    changed_files: tuple[str, ...],
    selector: Mapping[str, Any],
) -> tuple[str, ...]:
    """Select only changed files that may contain the frozen LPL method.

    This is a streaming exact-name query over Git's already known diff paths,
    not a Java source scan.  The relational Guard subsequently performs the
    strict owner/signature check on the bounded result.
    """
    method = (
        method_basename(
            str(selector.get("method") or selector.get("container_method") or "")
        )
        or ""
    ).strip()
    if not method:
        return ()
    candidates = tuple(
        path
        for path in changed_files
        if _file_contains_method_declaration(project_root / path, method)
    )
    _enforce_explicit_scope_budget(project_root, candidates)
    return candidates


def _file_contains_method_declaration(path: Path, method: str) -> bool:
    """Stream one explicit changed file looking for ``method(``.

    The overlap keeps identifiers split at chunk boundaries observable while
    avoiding loading a large changed source into memory merely to decide
    whether the target Guard should parse it.
    """
    needle = method.encode("utf-8", errors="strict")
    if not needle:
        return False
    overlap = max(64, len(needle) + 16)
    tail = b""
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return False
                data = tail + chunk
                start = 0
                while True:
                    index = data.find(needle, start)
                    if index < 0:
                        break
                    before = data[index - 1 : index] if index else b""
                    after = data[index + len(needle) :]
                    if (
                        not before
                        or not (
                            before.isalnum()
                            or before in {b"_", b"$"}
                        )
                    ):
                        stripped = after.lstrip()
                        if stripped.startswith(b"("):
                            return True
                    start = index + 1
                tail = data[-overlap:]
    except OSError as exc:
        raise GuardScopeError(
            "LPL_CHANGED_SCOPE_QUERY_FAILED",
            f"Cannot query an explicit changed Java source: {exc}",
            path=str(path),
        ) from exc


def _data_clump_candidate_files(project_root: Path, group: str) -> tuple[str, ...]:
    """Use exact Git text queries to select Data Clumps candidate files.

    This is an exact target relation query, not smell discovery. Candidate
    count is not an AST-memory budget: the relational Guard parses each
    returned file independently and never builds one common project model.
    """
    stems = tuple(
        sorted(
            {
                member.rsplit(":", 1)[-1].strip()
                for member in str(group).split("|")
                if ":" in member and member.rsplit(":", 1)[-1].strip()
            }
        )
    )
    if len(stems) < 3:
        return ()
    matches: set[str] | None = None
    for stem in stems:
        result = subprocess.run(
            [
                "git",
                "grep",
                "--untracked",
                "-l",
                "-z",
                "--fixed-strings",
                "-e",
                stem,
                "--",
                "*.java",
            ],
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise GuardScopeError(
                "DATA_CLUMPS_SCOPE_QUERY_FAILED",
                "Git could not evaluate the exact Data Clumps relation query",
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        current = {
            item.decode("utf-8", errors="surrogateescape")
            for item in result.stdout.split(b"\0")
            if item
        }
        matches = current if matches is None else matches.intersection(current)
    files = tuple(
        sorted(
            path
            for path in (matches or ())
            if standard_test_root(path) is None
            and _is_bounded_production_java_path(path)
        )
    )
    return files


def _refused_bequest_relation_files(
    project_root: Path,
    explicit_files: tuple[str, ...],
    location: Any,
    selector: Mapping[str, Any],
    *,
    capture: bool,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Resolve only the target's exact source ancestor chain.

    Removing the target class or its inheritance relation is a legitimate
    current state for Refused Bequest.  Those two verify-only states collapse
    to the target base so the semantic predicate can confirm absence.  Query,
    ambiguity and parse failures remain fail-closed.
    """
    from .target_relation_scope import (
        TargetRelationScopeError,
        resolve_refused_bequest_relation_scope,
    )

    if not explicit_files and not capture:
        return (), {"relation_state": "target_file_absent"}
    try:
        relation = resolve_refused_bequest_relation_scope(
            project_root,
            explicit_files,
            location,
            selector,
        )
    except TargetRelationScopeError as exc:
        if not capture and exc.code in {
            "ANCESTOR_RELATION_NOT_FOUND",
            "TARGET_CLASS_NOT_FOUND",
        }:
            return explicit_files, {
                "relation_state": "current_relation_absent",
                "reason": exc.code,
            }
        raise GuardScopeError(exc.code, exc.message, **exc.details) from exc
    files = tuple(relation.files)
    _enforce_explicit_scope_budget(project_root, files)
    return files, {
        **relation.witness(),
        "relation_state": "expanded",
    }


def _feature_envy_target_files(
    project_root: Path,
    explicit_files: tuple[str, ...],
    location: Any,
    selector: Mapping[str, Any],
    *,
    capture: bool,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Resolve only inheritance files needed by the target FE predicate."""
    from .target_feature_envy_scope import (
        FeatureEnvyScopeError,
        resolve_feature_envy_scope,
    )

    if not explicit_files and not capture:
        return (), {"relation_state": "target_file_absent"}
    try:
        relation = resolve_feature_envy_scope(
            project_root,
            explicit_files,
            location,
            selector,
        )
    except FeatureEnvyScopeError as exc:
        if not capture and exc.code == "TARGET_CLASS_NOT_FOUND":
            return explicit_files, {
                "relation_state": "current_target_absent",
                "reason": exc.code,
            }
        raise GuardScopeError(exc.code, exc.message, **exc.details) from exc
    files = tuple(relation.files)
    _enforce_explicit_scope_budget(project_root, files)
    return files, {
        **relation.witness(),
        "relation_state": "expanded",
    }


def _attach_relation_witness(
    result: dict[str, Any],
    relation_witness: Mapping[str, Any],
) -> None:
    """Attach one bounded relation trace without changing the Guard verdict."""
    witness = result.get("witness")
    if isinstance(witness, list) and witness and isinstance(witness[0], dict):
        witness[0]["relation_scope"] = dict(relation_witness)
        return
    if isinstance(witness, dict):
        witness["relation_scope"] = dict(relation_witness)
        return
    result["witness"] = {"relation_scope": dict(relation_witness)}


def _semantic_capture_is_sufficient(result: Mapping[str, Any]) -> bool:
    identity = result.get("entity_identity")
    objectives = result.get("objectives")
    return bool(
        result.get("ok") is True
        and int(result.get("target_match_count") or 0) == 1
        and result.get("target_smell_present") is True
        and isinstance(identity, Mapping)
        and bool(identity)
        and isinstance(objectives, Mapping)
        and bool(objectives)
    )


def _semantic_capture_needs_relation(result: Mapping[str, Any]) -> bool:
    violations = {
        str(item.get("code") if isinstance(item, Mapping) else item)
        for item in (result.get("guard_violations") or [])
    }
    return bool(
        int(result.get("target_match_count") or 0) == 0
        and result.get("target_missing") is False
        and "TARGET_FINDING_NOT_FOUND" in violations
        and "ANALYSIS_FAILED" not in violations
    )


def _target_only_relation_witness(
    explicit_files: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "relation_state": "target_only_sufficient",
        "scope_files": list(explicit_files),
    }


def _frozen_relation_state(contract: Mapping[str, Any]) -> str:
    witness = contract.get("witness")
    items: list[Mapping[str, Any]] = []
    if isinstance(witness, Mapping):
        items.append(witness)
    elif isinstance(witness, (list, tuple)):
        items.extend(item for item in witness if isinstance(item, Mapping))
    for item in items:
        relation = item.get("relation_scope")
        if isinstance(relation, Mapping) and relation.get("relation_state"):
            return str(relation["relation_state"])
    return ""


def _is_bounded_production_java_path(path: str) -> bool:
    excluded = {
        ".git",
        ".gradle",
        ".idea",
        ".smell-artifacts",
        "build",
        "dataset",
        "datasets",
        "dist",
        "node_modules",
        "out",
        "target",
    }
    candidate = PurePosixPath(str(path))
    return candidate.suffix.casefold() == ".java" and not any(
        part.casefold() in excluded for part in candidate.parts[:-1]
    )


def _enforce_explicit_scope_budget(
    project_root: Path,
    files: tuple[str, ...],
) -> None:
    validate_guard_analysis_scope(project_root, files)
