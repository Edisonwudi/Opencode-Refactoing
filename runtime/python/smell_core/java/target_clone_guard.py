"""Explicit-scope Type-1 clone predicate for the lightweight Java Guard.

The caller supplies exactly two frozen endpoints plus optional changed/new
``analysis_files``.  This module never discovers project files and never runs a
project smell detector.  A finding is one exact, contiguous token window of at
least ``MIN_CLONE_TOKENS`` inside the two target methods.  This mirrors CPD's
method-local duplicate contract without requiring the complete method bodies to
be equal, and emits only a bounded witness instead of a clone catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..location import LocationTarget, parse_location_descriptor
from . import clone_closure
from .catalog_identity import (
    stable_java_method_signature,
    stable_method_record_identity,
    stable_method_record_signature,
)
from . import semantic_detector


MIN_CLONE_TOKENS = 30
PREDICATE_ID = "java-target/code-clone-type1/exact-window-v2"
PROFILE_ID = "java-target-clone-guard/v2"

_SHINGLE_WIDTH = 5
_SKETCH_SIZE = 64
_NEAR_TOKEN_RATIO = 0.80
_NEAR_SKETCH_OVERLAP = 0.80
_SCOPE_PREVIEW_LIMIT = 16
_RANGE_PREVIEW_LIMIT = 16
_COPY_PREVIEW_LIMIT = 8


TargetCloneResult = dict[str, Any]


@dataclass(frozen=True)
class _Endpoint:
    file_path: Path
    relative_file: str
    class_name: str
    method: str
    line: int | None


def capture_code_clone_type1(
    project_root: Path | str,
    locations: Sequence[Any],
    analysis_files: Sequence[str | Path] | None = None,
) -> TargetCloneResult:
    """Freeze one real exact-clone pair from an explicit source scope."""
    result = _evaluate_scope(
        project_root,
        locations,
        analysis_files=analysis_files,
        baseline_identity=None,
    )
    if result.get("ok") is not True:
        return result
    if (
        int(result.get("target_match_count") or 0) != 1
        or result.get("target_smell_present") is not True
    ):
        result["ok"] = False
        result["witness"]["error"] = "BASELINE_FINDING_NOT_FOUND"
    return result


def evaluate_code_clone_type1(
    project_root: Path | str,
    locations: Sequence[Any],
    baseline_identity: Mapping[str, Any],
    analysis_files: Sequence[str | Path] | None = None,
    changed_line_ranges: Mapping[
        str | Path,
        Sequence[Sequence[int]],
    ] | None = None,
) -> TargetCloneResult:
    """Verify a frozen clone pair and scan only explicit changed/new methods.

    ``changed_line_ranges`` maps a project-relative or absolute Java path to
    inclusive ``[start_line, end_line]`` ranges in the *current* source.  When
    supplied, non-endpoint methods participate in anti-copy only when their
    current declaration intersects one of those ranges.  This prevents an
    unrelated edit in a file from admitting a pre-existing similar method.
    """
    error = _baseline_identity_error(baseline_identity)
    if error:
        return _input_error(error)
    return _evaluate_scope(
        project_root,
        locations,
        analysis_files=analysis_files,
        baseline_identity=baseline_identity,
        changed_line_ranges=changed_line_ranges,
    )


# Short aliases for callers that dispatch by guard type rather than smell name.
capture_target_clone = capture_code_clone_type1
evaluate_target_clone = evaluate_code_clone_type1


def _evaluate_scope(
    project_root: Path | str,
    locations: Sequence[Any],
    *,
    analysis_files: Sequence[str | Path] | None,
    baseline_identity: Mapping[str, Any] | None,
    changed_line_ranges: Mapping[
        str | Path,
        Sequence[Sequence[int]],
    ] | None = None,
) -> TargetCloneResult:
    root = Path(project_root).expanduser().resolve()
    try:
        if len(locations) != 2:
            raise ValueError("CLONE_GUARD_REQUIRES_TWO_LOCATIONS")
        requested = [
            _coerce_endpoint(root, locations[0]),
            _coerce_endpoint(root, locations[1]),
        ]
        scope_paths = _explicit_scope_paths(
            root,
            requested,
            analysis_files or (),
        )
        normalized_changed_ranges = _normalize_changed_line_ranges(
            root,
            changed_line_ranges,
        )
        methods = []
        if scope_paths:
            model = semantic_detector.build_scoped_project_model(
                root,
                scope_paths,
            )
            methods = list(model.methods)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return _input_error(str(exc))

    frozen_endpoints = (
        list(baseline_identity.get("endpoints") or [])
        if isinstance(baseline_identity, Mapping)
        else []
    )
    anchors = [
        _endpoint_anchor(
            requested[index],
            frozen_endpoints[index] if len(frozen_endpoints) == 2 else None,
        )
        for index in range(2)
    ]
    endpoint_matches = [
        _locate_methods(methods, anchor)
        for anchor in anchors
    ]
    pairs = [
        (left, right)
        for left in endpoint_matches[0]
        for right in endpoint_matches[1]
        if stable_method_record_identity(left) != stable_method_record_identity(right)
    ]
    pair_count = len(pairs)
    selection = _selection(pair_count)
    scope_witness = _scope_witness(root, scope_paths)
    if pair_count > 1:
        return _result(
            ok=False,
            target_match_count=pair_count,
            target_smell_present=False,
            target_missing=False,
            objectives={},
            entity_identity=dict(baseline_identity or {}),
            witness={
                "predicate_id": PREDICATE_ID,
                "selection": selection,
                "scope": scope_witness,
                "endpoint_match_counts": [
                    len(endpoint_matches[0]),
                    len(endpoint_matches[1]),
                ],
                "error": "TARGET_AMBIGUOUS",
            },
            guard_violations=[{"code": "TARGET_AMBIGUOUS"}],
        )

    pair_present = False
    pair_token_count = 0
    pair_fingerprint = ""
    pair_window: dict[str, Any] = {}
    current_endpoint_witness: list[dict[str, Any]] = []
    profiles: dict[str, dict[str, Any]] = {}
    for method in methods:
        profile = _target_method_profile(method)
        profiles[stable_method_record_identity(method)] = profile
    if pair_count == 1:
        left, right = pairs[0]
        left_profile = profiles.get(stable_method_record_identity(left), {})
        right_profile = profiles.get(stable_method_record_identity(right), {})
        pair_window = _pair_clone_window(left_profile, right_profile)
        pair_token_count = int(pair_window.get("token_count") or 0)
        pair_fingerprint = str(pair_window.get("fingerprint") or "")
        pair_present = bool(pair_fingerprint and pair_token_count >= MIN_CLONE_TOKENS)
        current_endpoint_witness = [
            _bounded_method_profile(left, left_profile),
            _bounded_method_profile(right, right_profile),
        ]

    if baseline_identity is None:
        identity = (
            _capture_identity(pairs[0], profiles, pair_window)
            if pair_count == 1 and pair_present
            else {}
        )
        return _result(
            ok=pair_count <= 1,
            target_match_count=pair_count,
            target_smell_present=pair_present,
            target_missing=pair_count == 0,
            objectives={"clone_token_count": float(pair_token_count if pair_present else 0)},
            entity_identity=identity,
            witness={
                "predicate_id": PREDICATE_ID,
                "selection": selection,
                "minimum_token_count": MIN_CLONE_TOKENS,
                "scope": scope_witness,
                "endpoint_match_counts": [
                    len(endpoint_matches[0]),
                    len(endpoint_matches[1]),
                ],
                "endpoints": current_endpoint_witness,
                "pair_fingerprint": pair_fingerprint,
                "pair_token_count": pair_token_count if pair_present else 0,
                "pair_window_kind": str(pair_window.get("kind") or ""),
                "pair_window_offsets": _pair_window_offsets(pair_window),
            },
            guard_violations=[],
        )

    baseline_fingerprint = str(baseline_identity.get("pair_fingerprint") or "")
    baseline_window_token_count = int(
        baseline_identity.get("clone_window_token_count") or 0
    )
    baseline_window_anchor = str(
        baseline_identity.get("clone_window_anchor") or ""
    )
    baseline_sketch = {
        str(item)
        for item in baseline_identity.get("token_sketch", [])
        if str(item)
    }
    baseline_like = _baseline_like_methods(
        methods,
        profiles,
        baseline_fingerprint=baseline_fingerprint,
        baseline_window_token_count=baseline_window_token_count,
        baseline_window_anchor=baseline_window_anchor,
        baseline_sketch=baseline_sketch,
        endpoint_method_keys={
            stable_method_record_identity(method)
            for matches in endpoint_matches
            for method in matches
        },
        changed_line_ranges=normalized_changed_ranges,
    )
    violations: list[dict[str, Any]] = []
    if pair_present:
        violations.append({
            "code": "BASELINE_CLONE_PAIR_STILL_PRESENT",
            "pair_token_count": pair_token_count,
        })
    if len(baseline_like) >= 2:
        violations.append({
            "code": "BASELINE_CLONE_RELOCATED_OR_PERTURBED",
            "matching_method_count": len(baseline_like),
            "exact_match_count": sum(
                1 for item in baseline_like if item["match_kind"] == "exact"
            ),
            "methods": baseline_like[:_COPY_PREVIEW_LIMIT],
            "preview_truncated": len(baseline_like) > _COPY_PREVIEW_LIMIT,
        })
    return _result(
        ok=True,
        target_match_count=pair_count,
        target_smell_present=pair_present,
        target_missing=pair_count == 0,
        objectives={
            "clone_token_count": float(pair_token_count if pair_present else 0),
            "baseline_like_copy_count": float(len(baseline_like)),
        },
        entity_identity=dict(baseline_identity),
        witness={
            "predicate_id": PREDICATE_ID,
            "selection": selection,
            "minimum_token_count": MIN_CLONE_TOKENS,
            "scope": scope_witness,
            "endpoint_match_counts": [
                len(endpoint_matches[0]),
                len(endpoint_matches[1]),
            ],
            "endpoints": current_endpoint_witness,
            "current_pair_fingerprint": pair_fingerprint,
            "current_pair_token_count": pair_token_count if pair_present else 0,
            "current_pair_window_kind": str(pair_window.get("kind") or ""),
            "current_pair_window_offsets": _pair_window_offsets(pair_window),
            "baseline_copy_scan": {
                "diff_filter": _changed_range_witness(
                    normalized_changed_ranges
                ),
                "matching_method_count": len(baseline_like),
                "exact_match_count": sum(
                    1 for item in baseline_like if item["match_kind"] == "exact"
                ),
                "near_match_count": sum(
                    1 for item in baseline_like if item["match_kind"] == "near"
                ),
                "method_preview": baseline_like[:_COPY_PREVIEW_LIMIT],
                "preview_truncated": len(baseline_like) > _COPY_PREVIEW_LIMIT,
            },
        },
        guard_violations=violations,
    )


def _capture_identity(
    pair: tuple[Any, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    pair_window: Mapping[str, Any],
) -> dict[str, Any]:
    left, right = pair
    left_profile = profiles[stable_method_record_identity(left)]
    right_profile = profiles[stable_method_record_identity(right)]
    window_tokens = [str(item) for item in pair_window.get("tokens", [])]
    pair_fingerprint = str(pair_window.get("fingerprint") or "")
    pair_token_count = int(pair_window.get("token_count") or 0)
    if (
        len(pair_fingerprint) != 64
        or pair_token_count < MIN_CLONE_TOKENS
        or len(window_tokens) != pair_token_count
    ):
        return {}
    return {
        "smell": "code_clone_type1",
        "profile_id": PROFILE_ID,
        "minimum_token_count": MIN_CLONE_TOKENS,
        "pair_fingerprint": pair_fingerprint,
        "pair_token_count": pair_token_count,
        "clone_window_token_count": len(window_tokens),
        "clone_window_kind": str(pair_window.get("kind") or ""),
        "clone_window_anchor": _token_window_anchor(window_tokens),
        "token_sketch": _token_sketch(window_tokens),
        "endpoints": [
            _endpoint_identity(left, left_profile, pair_fingerprint),
            _endpoint_identity(right, right_profile, pair_fingerprint),
        ],
    }


def _endpoint_identity(
    method: Any,
    profile: Mapping[str, Any],
    pair_fingerprint: str,
) -> dict[str, Any]:
    return {
        "kind": "method",
        "file": str(method.file).replace("\\", "/").lstrip("/"),
        "class": str(method.owner_qualified_name or method.class_name or ""),
        "method": stable_method_record_signature(method),
        "method_identity": stable_method_record_identity(method),
        "normalized_clone_fingerprint": pair_fingerprint,
        "token_count": int(profile.get("token_count") or 0),
    }


def _bounded_method_profile(method: Any, profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "method": stable_method_record_identity(method),
        "fingerprint": str(profile.get("fingerprint") or ""),
        "token_count": int(profile.get("token_count") or 0),
        "body_token_count": len(profile.get("body_tokens") or []),
        "method_token_count": len(profile.get("method_tokens") or []),
        "thin_forwarder": bool(profile.get("thin_forwarder")),
    }


def _target_method_profile(method: Any) -> dict[str, Any]:
    """Build the two bounded token streams used by the target predicate."""
    return dict(
        clone_closure._body_profile(method, include_method_tokens=True)
    )


def _profile_clone_streams(
    profile: Mapping[str, Any],
) -> list[tuple[str, list[str]]]:
    """Return body-first CPD streams; thin delegates are resolved adapters."""
    if bool(profile.get("thin_forwarder")):
        return []
    body = [str(item) for item in profile.get("body_tokens", [])]
    method = [
        str(item) for item in profile.get("method_tokens", [])
    ]
    streams: list[tuple[str, list[str]]] = []
    if body:
        streams.append(("body", body))
    if method and method != body:
        streams.append(("method", method))
    return streams


def _pair_clone_window(
    left_profile: Mapping[str, Any],
    right_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Select one deterministic exact window from the two target methods.

    Body windows are preferred because they remain stable when a legitimate
    shared helper changes modifiers or its method name.  For short bodies, the
    same predicate is applied to the complete method token stream because
    the CPD-sized duplicate may span the signature and body; it never widens
    beyond the two target methods.
    """
    left_streams = dict(_profile_clone_streams(left_profile))
    right_streams = dict(_profile_clone_streams(right_profile))
    for kind in ("body", "method"):
        match = _first_exact_token_window(
            left_streams.get(kind, []),
            right_streams.get(kind, []),
            minimum=MIN_CLONE_TOKENS,
        )
        if match:
            match["kind"] = kind
            return match
    return {}


def _first_exact_token_window(
    left: Sequence[str],
    right: Sequence[str],
    *,
    minimum: int,
) -> dict[str, Any]:
    """Find an exact contiguous window in linear bounded-method space.

    A tuple index implements the same exact-token predicate as CPD without a
    project catalog or quadratic dynamic-programming matrix.  Once the first
    deterministic minimum window is selected, it is extended to its maximal
    local run for a useful progress metric.
    """
    if minimum <= 0 or len(left) < minimum or len(right) < minimum:
        return {}
    left_starts: dict[tuple[str, ...], int] = {}
    for start in range(0, len(left) - minimum + 1):
        left_starts.setdefault(tuple(left[start : start + minimum]), start)
    selected: tuple[int, int] | None = None
    for right_start in range(0, len(right) - minimum + 1):
        left_start = left_starts.get(
            tuple(right[right_start : right_start + minimum])
        )
        if left_start is None:
            continue
        candidate = (left_start, right_start)
        if selected is None or candidate < selected:
            selected = candidate
    if selected is None:
        return {}
    left_start, right_start = selected
    while (
        left_start > 0
        and right_start > 0
        and left[left_start - 1] == right[right_start - 1]
    ):
        left_start -= 1
        right_start -= 1
    left_end = left_start + minimum
    right_end = right_start + minimum
    while (
        left_end < len(left)
        and right_end < len(right)
        and left[left_end] == right[right_end]
    ):
        left_end += 1
        right_end += 1
    tokens = [str(item) for item in left[left_start:left_end]]
    return {
        "tokens": tokens,
        "token_count": len(tokens),
        "fingerprint": _token_fingerprint(tokens),
        "left_start": left_start,
        "right_start": right_start,
    }


def _pair_window_offsets(value: Mapping[str, Any]) -> dict[str, int]:
    if not value:
        return {}
    return {
        "left_start": int(value.get("left_start") or 0),
        "right_start": int(value.get("right_start") or 0),
    }


def _baseline_like_methods(
    methods: Sequence[Any],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    baseline_fingerprint: str,
    baseline_window_token_count: int,
    baseline_window_anchor: str,
    baseline_sketch: set[str],
    endpoint_method_keys: set[str],
    changed_line_ranges: Mapping[str, Sequence[tuple[int, int]]] | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for method in methods:
        key = stable_method_record_identity(method)
        if (
            changed_line_ranges is not None
            and key not in endpoint_method_keys
            and not _method_intersects_changed_ranges(
                method,
                changed_line_ranges,
            )
        ):
            continue
        profile = profiles.get(key, {})
        if bool(profile.get("thin_forwarder")):
            continue
        streams = _profile_clone_streams(profile)
        if not streams:
            continue
        match_kind = ""
        similarity = 0.0
        if any(
            _contains_token_window_fingerprint(
                tokens,
                baseline_window_token_count,
                baseline_fingerprint,
                baseline_window_anchor,
            )
            for _, tokens in streams
        ):
            match_kind = "exact"
            similarity = 1.0
        elif baseline_window_token_count and baseline_sketch:
            similarity = max(
                (
                    _baseline_sketch_coverage(tokens, baseline_sketch)
                    if len(tokens)
                    >= baseline_window_token_count * _NEAR_TOKEN_RATIO
                    else 0.0
                )
                for _, tokens in streams
            )
            if similarity >= _NEAR_SKETCH_OVERLAP:
                match_kind = "near"
        if match_kind:
            matches.append({
                "method": key,
                "match_kind": match_kind,
                "similarity": round(float(similarity), 6),
                "token_count": int(profile.get("token_count") or 0),
                "body_token_count": len(profile.get("body_tokens") or []),
            })
    return sorted(matches, key=lambda item: (item["method"], item["match_kind"]))


def _normalize_changed_line_ranges(
    root: Path,
    value: Mapping[str | Path, Sequence[Sequence[int]]] | None,
) -> dict[str, tuple[tuple[int, int], ...]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("CHANGED_LINE_RANGES_MUST_BE_A_MAPPING")
    normalized: dict[str, tuple[tuple[int, int], ...]] = {}
    for raw_path, raw_ranges in value.items():
        path = Path(str(raw_path)).expanduser()
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        relative = _relative_java_path(root, path)
        if isinstance(raw_ranges, (str, bytes)) or not isinstance(
            raw_ranges,
            Sequence,
        ):
            raise ValueError(f"CHANGED_LINE_RANGES_INVALID:{relative}")
        ranges: list[tuple[int, int]] = []
        for raw_range in raw_ranges:
            if (
                isinstance(raw_range, (str, bytes))
                or not isinstance(raw_range, Sequence)
                or len(raw_range) != 2
            ):
                raise ValueError(f"CHANGED_LINE_RANGE_INVALID:{relative}")
            start = int(raw_range[0])
            end = int(raw_range[1])
            if start <= 0 or end < start:
                raise ValueError(f"CHANGED_LINE_RANGE_INVALID:{relative}")
            ranges.append((start, end))
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        normalized[relative] = tuple(merged)
    return normalized


def _method_intersects_changed_ranges(
    method: Any,
    changed_line_ranges: Mapping[str, Sequence[tuple[int, int]]],
) -> bool:
    relative = str(method.file).replace("\\", "/").lstrip("/")
    begin = int(method.begin_line or 0)
    end = int(method.end_line or begin)
    return any(
        begin <= changed_end and changed_start <= end
        for changed_start, changed_end in changed_line_ranges.get(relative, ())
    )


def _changed_range_witness(
    value: Mapping[str, Sequence[tuple[int, int]]] | None,
) -> dict[str, Any]:
    if value is None:
        return {
            "enabled": False,
            "file_count": 0,
            "range_count": 0,
            "files": [],
            "preview_truncated": False,
        }
    files = sorted(value)
    return {
        "enabled": True,
        "file_count": len(files),
        "range_count": sum(len(value[path]) for path in files),
        "files": [
            {
                "file": path,
                "range_count": len(value[path]),
                "ranges": [
                    list(item)
                    for item in value[path][:_RANGE_PREVIEW_LIMIT]
                ],
                "ranges_truncated": len(value[path]) > _RANGE_PREVIEW_LIMIT,
            }
            for path in files[:_SCOPE_PREVIEW_LIMIT]
        ],
        "preview_truncated": len(files) > _SCOPE_PREVIEW_LIMIT,
    }


def _token_sketch(tokens: Sequence[str]) -> list[str]:
    return sorted(_token_shingle_hashes(tokens))[:_SKETCH_SIZE]


def _token_fingerprint(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    return hashlib.sha256(
        "\x1f".join(tokens).encode("utf-8")
    ).hexdigest()


def _token_shingle_hashes(tokens: Sequence[str]) -> set[str]:
    if not tokens:
        return set()
    width = min(_SHINGLE_WIDTH, len(tokens))
    return {
        hashlib.sha256(
            "\x1f".join(tokens[index : index + width]).encode("utf-8")
        ).hexdigest()[:16]
        for index in range(0, len(tokens) - width + 1)
    }


def _token_window_anchor(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    width = min(_SHINGLE_WIDTH, len(tokens))
    return hashlib.sha256(
        "\x1f".join(tokens[:width]).encode("utf-8")
    ).hexdigest()[:16]


def _contains_token_window_fingerprint(
    tokens: Sequence[str],
    window_token_count: int,
    fingerprint: str,
    anchor: str,
) -> bool:
    if (
        window_token_count <= 0
        or len(tokens) < window_token_count
        or len(fingerprint) != 64
        or not anchor
    ):
        return False
    anchor_width = min(_SHINGLE_WIDTH, window_token_count)
    for start in range(0, len(tokens) - window_token_count + 1):
        current_anchor = hashlib.sha256(
            "\x1f".join(tokens[start : start + anchor_width]).encode("utf-8")
        ).hexdigest()[:16]
        if current_anchor != anchor:
            continue
        if (
            _token_fingerprint(tokens[start : start + window_token_count])
            == fingerprint
        ):
            return True
    return False


def _baseline_sketch_coverage(
    tokens: Sequence[str],
    baseline_sketch: set[str],
) -> float:
    if not tokens or not baseline_sketch:
        return 0.0
    current = _token_shingle_hashes(tokens)
    return len(current.intersection(baseline_sketch)) / len(baseline_sketch)


def _locate_methods(methods: Sequence[Any], anchor: _Endpoint) -> list[Any]:
    method_text = str(anchor.method or "").strip()
    method_name = method_text.split("(", 1)[0].strip().rsplit(".", 1)[-1]
    exact_signature = (
        stable_java_method_signature(
            method_text,
            preserve_source_qualification=True,
        )
        if "(" in method_text and ")" in method_text
        else ""
    )
    candidates = [
        method
        for method in methods
        if str(method.file).replace("\\", "/").lstrip("/") == anchor.relative_file
        and (
            not anchor.class_name
            or _same_owner(method, anchor.class_name)
        )
        and (not method_name or method.method_name == method_name)
    ]
    if exact_signature:
        return [
            method for method in candidates
            if stable_method_record_signature(method) == exact_signature
        ]
    if anchor.line:
        candidates = [
            method for method in candidates
            if method.begin_line <= int(anchor.line) <= method.end_line
        ]
    elif not method_name:
        return []
    return sorted(candidates, key=stable_method_record_identity)


def _same_owner(method: Any, expected: str) -> bool:
    if "." in expected:
        return str(method.owner_qualified_name or "") == expected
    return (
        str(method.class_name or "").rsplit(".", 1)[-1]
        == expected.rsplit(".", 1)[-1]
    )


def _endpoint_anchor(
    requested: _Endpoint,
    frozen: Any,
) -> _Endpoint:
    if not isinstance(frozen, Mapping):
        return requested
    frozen_file = str(frozen.get("file") or requested.relative_file)
    return _Endpoint(
        file_path=requested.file_path,
        relative_file=frozen_file.replace("\\", "/").lstrip("/"),
        class_name=str(frozen.get("class") or requested.class_name or ""),
        method=str(frozen.get("method") or requested.method or ""),
        line=None,
    )


def _coerce_endpoint(root: Path, location: Any) -> _Endpoint:
    if isinstance(location, LocationTarget):
        parsed: Any = location
    elif isinstance(location, str):
        parsed = parse_location_descriptor(location, root)
    elif isinstance(location, Mapping):
        parsed = location
    elif hasattr(location, "file_path"):
        parsed = location
    else:
        raise TypeError("clone location must be a descriptor, mapping, or LocationTarget")

    def read(name: str, default: Any = None) -> Any:
        if isinstance(parsed, Mapping):
            return parsed.get(name, default)
        return getattr(parsed, name, default)

    raw_path = read("file_path") or read("project_path") or read("file")
    if raw_path is None:
        raise ValueError("clone location does not contain a file")
    path = Path(str(raw_path)).expanduser()
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    relative = _relative_java_path(root, path)
    raw_line = read("line")
    line = int(raw_line) if raw_line not in (None, "") else None
    if line is not None and line <= 0:
        raise ValueError("clone endpoint line must be positive")
    return _Endpoint(
        file_path=path,
        relative_file=relative,
        class_name=str(read("class_name") or read("class") or "").strip(),
        method=str(read("method") or "").strip(),
        line=line,
    )


def _explicit_scope_paths(
    root: Path,
    endpoints: Sequence[_Endpoint],
    analysis_files: Iterable[str | Path],
) -> list[Path]:
    paths = {endpoint.file_path for endpoint in endpoints}
    for item in analysis_files:
        raw = Path(str(item)).expanduser()
        path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        _relative_java_path(root, path)
        paths.add(path)
    # Deleted endpoint/analysis paths carry absence information but cannot be
    # parsed. Their omission is explicit and never triggers project discovery.
    return sorted(path for path in paths if path.is_file())


def _relative_java_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"SCOPED_SOURCE_OUTSIDE_PROJECT:{path}") from exc
    if path.suffix.casefold() != ".java":
        raise ValueError(f"SCOPED_SOURCE_NOT_JAVA:{relative.as_posix()}")
    return relative.as_posix()


def _scope_witness(root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    relative = sorted(path.relative_to(root).as_posix() for path in paths)
    digest = hashlib.sha256("\n".join(relative).encode("utf-8")).hexdigest()
    return {
        "file_count": len(relative),
        "files": relative[:_SCOPE_PREVIEW_LIMIT],
        "files_sha256": digest,
        "preview_truncated": len(relative) > _SCOPE_PREVIEW_LIMIT,
    }


def _baseline_identity_error(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        return "BASELINE_CLONE_IDENTITY_REQUIRED"
    if str(value.get("profile_id") or "") != PROFILE_ID:
        return "BASELINE_CLONE_PROFILE_MISMATCH"
    fingerprint = str(value.get("pair_fingerprint") or "")
    pair_token_count = int(value.get("pair_token_count") or 0)
    window_token_count = int(value.get("clone_window_token_count") or 0)
    window_kind = str(value.get("clone_window_kind") or "")
    window_anchor = str(value.get("clone_window_anchor") or "")
    endpoints = value.get("endpoints")
    sketch = value.get("token_sketch")
    if len(fingerprint) != 64:
        return "BASELINE_CLONE_FINGERPRINT_INVALID"
    if not isinstance(endpoints, list) or len(endpoints) != 2:
        return "BASELINE_CLONE_ENDPOINTS_INVALID"
    if (
        pair_token_count < MIN_CLONE_TOKENS
        or window_token_count != pair_token_count
    ):
        return "BASELINE_CLONE_TOKEN_COUNT_INVALID"
    if window_kind not in {"body", "method"}:
        return "BASELINE_CLONE_WINDOW_KIND_INVALID"
    if len(window_anchor) != 16:
        return "BASELINE_CLONE_WINDOW_ANCHOR_INVALID"
    if not isinstance(sketch, list) or not sketch:
        return "BASELINE_CLONE_SKETCH_INVALID"
    return ""


def _selection(count: int) -> str:
    if count == 1:
        return "MATCHED"
    return "NOT_FOUND" if count == 0 else "AMBIGUOUS"


def _result(
    *,
    ok: bool,
    target_match_count: int,
    target_smell_present: bool,
    target_missing: bool,
    objectives: Mapping[str, float],
    entity_identity: Mapping[str, Any],
    witness: Mapping[str, Any],
    guard_violations: Sequence[Mapping[str, Any]],
) -> TargetCloneResult:
    return {
        "ok": bool(ok),
        "target_match_count": int(target_match_count),
        "target_smell_present": bool(target_smell_present),
        "target_missing": bool(target_missing),
        "objectives": dict(objectives),
        "entity_identity": dict(entity_identity),
        "witness": dict(witness),
        "guard_violations": [dict(item) for item in guard_violations],
    }


def _input_error(error: str) -> TargetCloneResult:
    return _result(
        ok=False,
        target_match_count=0,
        target_smell_present=False,
        target_missing=True,
        objectives={},
        entity_identity={},
        witness={
            "predicate_id": PREDICATE_ID,
            "selection": "ERROR",
            "scope": {
                "file_count": 0,
                "files": [],
                "files_sha256": hashlib.sha256(b"").hexdigest(),
                "preview_truncated": False,
            },
            "error": str(error),
        },
        guard_violations=[{"code": str(error)}],
    )
