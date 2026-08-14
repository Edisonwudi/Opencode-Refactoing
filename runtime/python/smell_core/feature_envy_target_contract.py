"""Target-local Feature Envy contract for Python, C, and C++.

The caller must select one source file, one declaration, and one receiver root.
This module never discovers source files and never reads smell evidence.  A
deleted, renamed, owner-moved, or ambiguous declaration is therefore an
unresolved target rather than a successful refactoring.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .analysis import (
    extract_snippet,
    extract_snippet_candidates,
    method_basename,
    signature_parameter_fingerprints,
)
from .feature_envy import (
    FEATURE_ENVY_RATIO_DENOMINATOR_CONTRACT,
    analyze_feature_envy_target,
)
from .target_patch_identity import (
    FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT,
    ast_declaration_identity,
    evaluate_feature_envy_target_patch_identity,
)


FEATURE_ENVY_TARGET_CONTRACT = "explicit-receiver-declaration-continuity-v4"
_NONJAVA_LANGUAGES = frozenset({"python", "c", "cpp"})
_RECEIVER_NAME_RE = re.compile(r"[A-Za-z_]\w*\Z")
_RATIO_DENOMINATOR_CAP_KEY = "baseline_member_access_count"


class FeatureEnvyTargetContractError(ValueError):
    """A caller-supplied target cannot be evaluated without widening scope."""


def explicit_receiver_name(target_context: Mapping[str, Any] | None) -> str:
    """Return the mandatory non-Java receiver root from target_context.

    ``receiver_type`` is retained as the public schema key for compatibility,
    but its non-Java value is now strictly a parser root name (for example
    ``order``), never a type name and never audit evidence.
    """
    context = target_context if isinstance(target_context, Mapping) else {}
    receiver = str(context.get("receiver_type") or "").strip()
    if not receiver:
        raise FeatureEnvyTargetContractError(
            "missing_explicit_receiver_selector"
        )
    if _RECEIVER_NAME_RE.fullmatch(receiver) is None:
        raise FeatureEnvyTargetContractError(
            "invalid_explicit_receiver_selector"
        )
    return receiver


def feature_envy_target_snapshot(
    config: Any,
    *,
    changed_patch: str | None = None,
) -> dict[str, Any]:
    """Capture or evaluate one frozen Feature Envy target.

    Baseline capture freezes parser-derived declaration identity.  Verification
    resolves that identity only among declarations in the same caller-selected
    file.  Zero or multiple matches fail closed; no other source file is read.
    """
    language = str(getattr(config, "language", "") or "").lower()
    if language not in _NONJAVA_LANGUAGES:
        return _failure("unsupported_nonjava_language")
    locations = list(getattr(config, "locations", ()) or ())
    if len(locations) != 1:
        return _failure("feature_envy_requires_one_explicit_target")
    target = locations[0]
    root = Path(getattr(config, "project_root")).expanduser().resolve()
    try:
        relative_file = target.file_path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return _failure("target_outside_project_root")
    try:
        receiver = explicit_receiver_name(getattr(config, "target_context", None))
    except FeatureEnvyTargetContractError as exc:
        return _failure(str(exc), file=relative_file)

    frozen_identity = _contract_identity(config)
    if frozen_identity:
        frozen_receiver = str(frozen_identity.get("receiver_name") or "").strip()
        if not frozen_receiver or frozen_receiver != receiver:
            return _failure(
                "frozen_receiver_selector_mismatch",
                file=relative_file,
                finding_identity=frozen_identity,
            )
        return _evaluate_frozen_target(
            config,
            target=target,
            relative_file=relative_file,
            receiver=receiver,
            frozen_identity=frozen_identity,
            changed_patch=changed_patch,
        )
    return _capture_baseline_target(
        config,
        target=target,
        relative_file=relative_file,
        receiver=receiver,
    )


def _capture_baseline_target(
    config: Any,
    *,
    target: Any,
    relative_file: str,
    receiver: str,
) -> dict[str, Any]:
    if not target.file_path.is_file():
        return _target_missing("target_file_missing", relative_file, receiver, {})
    try:
        selected = extract_snippet(target, str(config.language))
        candidates = extract_snippet_candidates(target, str(config.language))
    except Exception as exc:
        return _failure(
            "target_file_parse_failed",
            file=relative_file,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if selected is None:
        return _target_missing("target_declaration_missing", relative_file, receiver, {})
    identity = _declaration_identity(
        config,
        relative_file=relative_file,
        receiver=receiver,
        snippet=selected,
    )
    anchor_matches = [
        (snippet, parseable)
        for snippet, parseable in candidates
        if int(snippet.start_line) == int(selected.start_line)
        and _matches_identity(config, snippet, identity)
    ]
    if len(anchor_matches) != 1:
        return _identity_collision(
            relative_file, receiver, identity, len(anchor_matches)
        )
    if anchor_matches[0][1] is not True:
        return _failure(
            "target_declaration_parse_failed",
            file=relative_file,
            finding_identity=identity,
        )
    # A source line selects the intended declaration, but line identity alone
    # cannot prevent an identical declaration elsewhere in this file from
    # becoming its replacement after a delete/format patch.  Freeze only an
    # identity that is unique within the explicit target file; widening to
    # body similarity or nearest-declaration fallback would make that
    # substitution possible again.
    identity_matches = [
        snippet
        for snippet, _parseable in candidates
        if _matches_identity(config, snippet, identity)
    ]
    if len(identity_matches) != 1:
        return _identity_collision(
            relative_file, receiver, identity, len(identity_matches)
        )
    return _profile_snapshot(
        config,
        target=replace(
            target,
            method=selected.declared_name,
            line=selected.start_line,
        ),
        relative_file=relative_file,
        receiver=receiver,
        identity=identity,
    )


def _evaluate_frozen_target(
    config: Any,
    *,
    target: Any,
    relative_file: str,
    receiver: str,
    frozen_identity: dict[str, Any],
    changed_patch: str | None,
) -> dict[str, Any]:
    if str(frozen_identity.get("file") or "") != relative_file:
        return _failure(
            "frozen_target_file_mismatch",
            file=relative_file,
            finding_identity=frozen_identity,
        )
    if not target.file_path.is_file():
        return _target_missing(
            "target_file_missing", relative_file, receiver, frozen_identity
        )
    try:
        candidates = extract_snippet_candidates(target, str(config.language))
    except Exception as exc:
        return _failure(
            "target_file_parse_failed",
            file=relative_file,
            detail=f"{type(exc).__name__}: {exc}",
            finding_identity=frozen_identity,
        )
    identity_matches = [
        (snippet, parseable)
        for snippet, parseable in candidates
        if _matches_identity(config, snippet, frozen_identity)
    ]
    if not identity_matches:
        return _target_missing(
            "frozen_target_declaration_missing",
            relative_file,
            receiver,
            frozen_identity,
        )
    if len(identity_matches) != 1:
        result = _identity_collision(
            relative_file,
            receiver,
            frozen_identity,
            len(identity_matches),
        )
        result["target_patch_identity_ok"] = False
        result["target_patch_identity_failures"] = [
            {"reason": "feature_envy_current_identity_not_unique"}
        ]
        return result
    if identity_matches[0][1] is not True:
        return _failure(
            "target_declaration_parse_failed",
            file=relative_file,
            finding_identity=frozen_identity,
        )
    identity_candidates = [identity_matches[0][0]]
    if changed_patch is None:
        return _failure(
            "changed_target_patch_unavailable",
            file=relative_file,
            finding_identity=frozen_identity,
        )
    anchored_candidates: list[tuple[Any, dict[str, Any]]] = []
    for snippet in identity_candidates:
        current_anchor = _target_anchor_record(
            config,
            relative_file=relative_file,
            snippet=snippet,
        )
        anchored_candidates.append((snippet, current_anchor))
    patch_identity = evaluate_feature_envy_target_patch_identity(
        frozen_identity,
        [record for _, record in anchored_candidates],
        changed_patch=changed_patch,
    )
    if patch_identity.get("ok") is not True:
        result = _target_missing(
            "frozen_target_patch_identity_unresolved",
            relative_file,
            receiver,
            frozen_identity,
        )
        result.update({
            "target_patch_identity_ok": False,
            "target_patch_identity_contract": (
                FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT
            ),
            "target_patch_identity_failures": list(
                patch_identity.get("failures") or []
            ),
            "target_patch_identity_error": str(
                patch_identity.get("error") or ""
            ),
        })
        return result
    mapped_line = 0
    mapping_records = list(patch_identity.get("strict_target_mappings") or []) + list(
        patch_identity.get("wrapper_reanchors") or []
    )
    if len(mapping_records) == 1:
        mapped_line = int(mapping_records[0].get("current_begin_line") or 0)
    selected_matches = [
        snippet
        for snippet, record in anchored_candidates
        if int(record.get("begin_line") or 0) == mapped_line
    ]
    if len(selected_matches) != 1:
        result = _identity_collision(
            relative_file, receiver, frozen_identity, len(selected_matches)
        )
        result["target_patch_identity_failures"] = [
            {"reason": "feature_envy_selected_mapping_not_unique"}
        ]
        return result
    selected = selected_matches[0]
    snapshot = _profile_snapshot(
        config,
        target=replace(
            target,
            method=selected.declared_name,
            line=selected.start_line,
        ),
        relative_file=relative_file,
        receiver=receiver,
        identity=frozen_identity,
    )
    snapshot.update({
        "target_patch_identity_ok": True,
        "target_patch_identity_contract": str(
            patch_identity.get("contract")
            or FEATURE_ENVY_WRAPPER_REANCHOR_CONTRACT
        ),
        "target_patch_identity_failures": [],
        "wrapper_reanchors": list(patch_identity.get("wrapper_reanchors") or []),
    })
    return snapshot


def _profile_snapshot(
    config: Any,
    *,
    target: Any,
    relative_file: str,
    receiver: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    frozen_ratio_cap = identity.get(_RATIO_DENOMINATOR_CAP_KEY)
    baseline_capture = frozen_ratio_cap is None
    ratio_cap = (
        int(frozen_ratio_cap)
        if isinstance(frozen_ratio_cap, int)
        and not isinstance(frozen_ratio_cap, bool)
        and frozen_ratio_cap >= 0
        else None
    )
    try:
        profile = analyze_feature_envy_target(
            Path(config.project_root),
            language=str(config.language),
            target_file=target.file_path,
            method=target.method,
            line=target.line,
            expected_receiver=receiver,
            exact_receiver_selector=True,
            ratio_denominator_cap=ratio_cap,
        )
    except Exception as exc:
        return _failure(
            "target_analysis_failed",
            file=relative_file,
            detail=f"{type(exc).__name__}: {exc}",
            finding_identity=identity,
        )
    if not profile.get("ok"):
        return _target_missing(
            str(profile.get("error") or "target_declaration_missing"),
            relative_file,
            receiver,
            identity,
        )
    finding_identity = dict(identity)
    if ratio_cap is None:
        ratio_cap = int(profile.get("total_member_access") or 0)
        finding_identity.update({
            _RATIO_DENOMINATOR_CAP_KEY: ratio_cap,
            "ratio_denominator_contract": (
                FEATURE_ENVY_RATIO_DENOMINATOR_CONTRACT
            ),
        })
    budget = dict(profile.get("feature_envy_budget") or {})
    expected_receiver_finding = bool(
        profile.get("expected_receiver_strict_hit")
    )
    # c000 must freeze the same receiver that owns both the objective and the
    # admitted finding.  Otherwise an unrelated explicit receiver can be
    # paired with a dominant-receiver finding, leaving a zero or irrelevant
    # objective that can never describe the requested repair.  Verification
    # remains stricter: after a valid c000, any newly dominant foreign receiver
    # still keeps the target finding alive instead of authorizing relocation.
    finding_present = bool(
        expected_receiver_finding
        if baseline_capture
        else profile.get("strict_detector_hit")
    )
    return {
        **profile,
        "adapter": "feature_envy",
        "detector": "tree_sitter_generic",
        "contract": FEATURE_ENVY_TARGET_CONTRACT,
        "scope_mode": "explicit_target_file",
        "scope_files": [relative_file],
        "objectives": {
            "expected_receiver_access": int(
                profile.get("expected_receiver_access") or 0
            )
        },
        "guard_receiver_name": str(budget.get("receiver_name") or ""),
        "guard_receiver_access": int(budget.get("receiver_access") or 0),
        "guard_receiver_access_finding_min": int(
            budget.get("receiver_access_finding_min") or 0
        ),
        "guard_receiver_access_passing_max": int(
            budget.get("receiver_access_passing_max") or 0
        ),
        "guard_receiver_access_required_reduction": int(
            budget.get("receiver_access_required_reduction") or 0
        ),
        "guard_receiver_ratio": float(budget.get("receiver_ratio") or 0.0),
        "guard_receiver_ratio_finding_min": float(
            budget.get("receiver_ratio_finding_min") or 0.0
        ),
        "guard_receiver_ratio_required_access_reduction": int(
            budget.get("receiver_ratio_required_access_reduction") or 0
        ),
        "guard_required_receiver_access_reduction": int(
            budget.get("required_receiver_access_reduction") or 0
        ),
        "guard_receiver_pass_when": str(budget.get("receiver_pass_when") or ""),
        "guard_finding_when": str(budget.get("finding_when") or ""),
        "guard_pass_when": str(budget.get("pass_when") or ""),
        "selector_receiver_finding_present": expected_receiver_finding,
        # The current declaration must be free of Feature Envy at this exact
        # code location, even if a different receiver became dominant.
        "finding_present": finding_present,
        "candidate_count": 1 if finding_present else 0,
        "target_match_count": 1,
        "target_missing": False,
        "target_identity_collision": False,
        "finding_identity": finding_identity,
    }


def _declaration_identity(
    config: Any,
    *,
    relative_file: str,
    receiver: str,
    snippet: Any,
) -> dict[str, Any]:
    return {
        "smell": "feature_envy",
        "file": relative_file,
        "declared_name": str(snippet.declared_name or ""),
        "owner_qualified_name": str(snippet.owner_qualified_name or ""),
        "parameter_fingerprints": signature_parameter_fingerprints(
            str(snippet.signature_text or ""), str(config.language)
        ),
        "receiver_name": receiver,
        **_target_anchor_record(
            config,
            relative_file=relative_file,
            snippet=snippet,
        ),
    }


def _target_anchor_record(
    config: Any,
    *,
    relative_file: str,
    snippet: Any,
) -> dict[str, Any]:
    return {
        "target_index": 0,
        "resolved": True,
        "file": relative_file,
        "method": str(snippet.declared_name or ""),
        "begin_line": int(snippet.start_line),
        "declaration_identity": ast_declaration_identity(
            str(snippet.declared_name or ""),
            str(snippet.owner_qualified_name or ""),
        ),
        "parameter_fingerprints": signature_parameter_fingerprints(
            str(snippet.signature_text or ""), str(config.language)
        ),
    }


def _matches_identity(config: Any, snippet: Any, identity: Mapping[str, Any]) -> bool:
    return bool(
        str(snippet.declared_name or "")
        == str(identity.get("declared_name") or "")
        and str(snippet.owner_qualified_name or "")
        == str(identity.get("owner_qualified_name") or "")
        and signature_parameter_fingerprints(
            str(snippet.signature_text or ""), str(config.language)
        )
        == list(identity.get("parameter_fingerprints") or [])
    )


def _contract_identity(config: Any) -> dict[str, Any]:
    contract = getattr(config, "finding_contract", None)
    identity = contract.get("entity_identity") if isinstance(contract, Mapping) else None
    return dict(identity) if isinstance(identity, Mapping) else {}


def _target_missing(
    error: str,
    relative_file: str,
    receiver: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "adapter": "feature_envy",
        "detector": "tree_sitter_generic",
        "contract": FEATURE_ENVY_TARGET_CONTRACT,
        "scope_mode": "explicit_target_file",
        "scope_files": [relative_file],
        "expected_receiver_name": receiver,
        "expected_receiver_access": 0,
        "objectives": {"expected_receiver_access": 0},
        "finding_present": False,
        "candidate_count": 0,
        "target_match_count": 0,
        "target_missing": True,
        "target_absence_allowed": False,
        "target_identity_collision": False,
        "finding_identity": dict(identity),
        "error": error,
    }


def _identity_collision(
    relative_file: str,
    receiver: str,
    identity: Mapping[str, Any],
    match_count: int,
) -> dict[str, Any]:
    return {
        **_target_missing(
            "target_identity_collision", relative_file, receiver, identity
        ),
        "ok": False,
        "candidate_count": int(match_count),
        "target_match_count": int(match_count),
        "target_identity_collision": True,
    }


def _failure(error: str, **details: Any) -> dict[str, Any]:
    identity = details.pop("finding_identity", {})
    result = {
        "ok": False,
        "adapter": "feature_envy",
        "detector": "tree_sitter_generic",
        "contract": FEATURE_ENVY_TARGET_CONTRACT,
        "objectives": {},
        "finding_present": False,
        "candidate_count": 0,
        "target_match_count": 0,
        "target_missing": True,
        "target_absence_allowed": False,
        "target_identity_collision": False,
        "finding_identity": dict(identity) if isinstance(identity, Mapping) else {},
        "error": error,
    }
    result.update(details)
    return result


__all__ = [
    "FEATURE_ENVY_TARGET_CONTRACT",
    "FeatureEnvyTargetContractError",
    "explicit_receiver_name",
    "feature_envy_target_snapshot",
]
