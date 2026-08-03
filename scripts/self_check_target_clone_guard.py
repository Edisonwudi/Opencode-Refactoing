#!/usr/bin/env python3
"""Self-check the explicit-scope Type-1 clone Guard."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java import semantic_detector  # noqa: E402
from smell_core.java import target_clone_guard as guard  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402


def _body(*, changed_literal: bool = False) -> str:
    changed = 2 if changed_literal else 1
    statements = [
        "    int total = value;",
        f"    total += {changed};",
        "    total += 2;",
        "    total += 3;",
        "    total += 4;",
        "    total += 5;",
        "    total += 6;",
        "    total += 7;",
        "    return total;",
    ]
    return "\n".join(statements)


def _class(name: str, body: str, *, static: bool = False) -> str:
    modifier = "static " if static else ""
    return (
        f"class {name} {{\n"
        f"  {modifier}int work(int value) {{\n{body}\n  }}\n"
        "}\n"
    )


def _delegate(name: str, target: str) -> str:
    return (
        f"class {name} {{\n"
        f"  int work(int value) {{ return {target}.work(value); }}\n"
        "}\n"
    )


def _class_with_unrelated_field(name: str, body: str) -> str:
    return (
        f"class {name} {{\n"
        "  static int unrelatedMarker = 1;\n"
        f"  static int work(int value) {{\n{body}\n  }}\n"
        "}\n"
    )


def _partial_clone_class(name: str, parent_type: str, child_type: str) -> str:
    return (
        f"class {name} {{\n"
        "  void insert(Node newChild, int index) {\n"
        f"    {parent_type} oldParent = ({parent_type}) newChild.getParent();\n"
        "    if (oldParent != null) {\n"
        "      oldParent.remove(newChild);\n"
        "    }\n"
        "    newChild.setParent(this);\n"
        f"    children.add(index, ({child_type}) newChild);\n"
        "  }\n"
        "}\n"
    )


def _short_clone_class(name: str) -> str:
    return (
        f"class {name} {{\n"
        "  int tiny(int a, int b, int c, int d, int e, int f) {\n"
        "    return a + b + c + d + e + f;\n"
        "  }\n"
        "}\n"
    )


def _structural_near_class(name: str, variable: str, operator: str, base: int) -> str:
    statements = "\n".join(
        f"    {variable} {operator}= {base + index};"
        for index in range(10)
    )
    return (
        f"class {name} {{\n"
        "  int calculate(int value) {\n"
        f"    int {variable} = value;\n"
        f"{statements}\n"
        f"    return {variable};\n"
        "  }\n"
        "}\n"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _schema(result: dict[str, object]) -> None:
    assert set(result) == {
        "ok",
        "target_match_count",
        "target_smell_present",
        "target_missing",
        "objectives",
        "entity_identity",
        "witness",
        "guard_violations",
    }, result
    assert len(json.dumps(result, sort_keys=True)) < 64 * 1024, result
    serialized = json.dumps(result, sort_keys=True)
    assert "body_tokens" not in serialized, serialized[:1000]
    assert "clone_catalog" not in serialized, serialized[:1000]


def _forbidden(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("clone Guard invoked discovery or a project detector")


def _run() -> None:
    module_source = inspect.getsource(guard)
    assert "clone_closure" not in module_source
    assert "run_java_" not in module_source
    assert "_list_java_files" not in module_source
    assert "_build_project_model" not in module_source
    assert ".rglob(" not in module_source

    with tempfile.TemporaryDirectory(prefix="target-clone-guard-") as temp_dir:
        project = Path(temp_dir)
        left = project / "Left.java"
        right = project / "Right.java"
        shared_a = project / "SharedA.java"
        shared_b = project / "SharedB.java"
        _write(left, _class("Left", _body()))
        _write(right, _class("Right", _body()))
        for index in range(400):
            _write(
                project / "noise" / f"Noise{index:04d}.java",
                f"class Noise{index:04d} {{ invalid java tokens here }}\n",
            )

        locations = [
            parse_location_descriptor("Left.java:method=work(int value)", project),
            parse_location_descriptor("Right.java:method=work(int value)", project),
        ]
        original_read_bytes = Path.read_bytes
        java_reads: list[Path] = []

        def tracked_read_bytes(path: Path) -> bytes:
            if path.suffix.casefold() == ".java":
                java_reads.append(path.resolve())
            return original_read_bytes(path)

        forbidden_names = (
            "run_java_semantic_detector",
            "_build_project_model",
            "_list_java_files",
            "_detect_feature_envy",
            "_detect_refused_bequest",
            "_detect_data_clumps",
            "_detect_god_class",
            "_detect_dead_code",
        )
        patches = [
            patch.object(semantic_detector, name, _forbidden)
            for name in forbidden_names
        ]
        for active in patches:
            active.start()
        Path.read_bytes = tracked_read_bytes
        try:
            captured = guard.capture_code_clone_type1(project, locations)
            _schema(captured)
            assert captured["ok"] is True, captured
            assert captured["target_match_count"] == 1, captured
            assert captured["target_smell_present"] is True, captured
            assert captured["objectives"]["clone_token_count"] >= 30, captured
            identity = captured["entity_identity"]
            assert len(identity["endpoints"]) == 2, identity
            assert len(identity["pair_fingerprint"]) == 64, identity
            assert 0 < len(identity["token_sketch"]) <= 64, identity
            assert set(java_reads) == {left.resolve(), right.resolve()}, java_reads

            stale_identity = dict(identity)
            stale_identity["profile_id"] = "java-target-clone-guard/v1"
            stale = guard.evaluate_code_clone_type1(
                project,
                locations,
                stale_identity,
            )
            _schema(stale)
            assert stale["ok"] is False, stale
            assert (
                stale["witness"]["error"]
                == "BASELINE_CLONE_PROFILE_MISMATCH"
            ), stale

            # Legal deduplication: both endpoints become thin delegates to one
            # surviving implementation. One baseline-like implementation is
            # allowed; two copies are not.
            java_reads.clear()
            _write(left, _delegate("Left", "SharedA"))
            _write(right, _delegate("Right", "SharedA"))
            _write(shared_a, _class("SharedA", _body(), static=True))
            resolved = guard.evaluate_code_clone_type1(
                project,
                locations,
                identity,
                analysis_files=[shared_a],
            )
            _schema(resolved)
            assert resolved["ok"] is True, resolved
            assert resolved["target_match_count"] == 1, resolved
            assert resolved["target_smell_present"] is False, resolved
            assert resolved["guard_violations"] == [], resolved
            assert resolved["objectives"]["baseline_like_copy_count"] == 1.0, resolved
            assert set(java_reads) == {
                left.resolve(),
                right.resolve(),
                shared_a.resolve(),
            }, java_reads

            # SharedB already contains similar code, but this diff touches only
            # a field outside work(). It must not be admitted as a relocated
            # carrier merely because its file appears in analysis_files.
            _write(shared_b, _class_with_unrelated_field("SharedB", _body()))
            outside_only = guard.evaluate_code_clone_type1(
                project,
                locations,
                identity,
                analysis_files=[shared_a, shared_b],
                changed_line_ranges={
                    shared_a: [(2, 20)],
                    shared_b: [(2, 2)],
                },
            )
            _schema(outside_only)
            assert outside_only["guard_violations"] == [], outside_only
            assert (
                outside_only["objectives"]["baseline_like_copy_count"] == 1.0
            ), outside_only
            diff_filter = outside_only["witness"]["baseline_copy_scan"]["diff_filter"]
            assert diff_filter == {
                "enabled": True,
                "file_count": 2,
                "range_count": 2,
                "files": [
                    {
                        "file": "SharedA.java",
                        "range_count": 1,
                        "ranges": [[2, 20]],
                        "ranges_truncated": False,
                    },
                    {
                        "file": "SharedB.java",
                        "range_count": 1,
                        "ranges": [[2, 2]],
                        "ranges_truncated": False,
                    },
                ],
                "preview_truncated": False,
            }, diff_filter

            # The same pre-existing method becomes an anti-copy candidate when
            # the real current-source diff intersects its declaration.
            method_changed = guard.evaluate_code_clone_type1(
                project,
                locations,
                identity,
                analysis_files=[shared_a, shared_b],
                changed_line_ranges={
                    shared_a: [(2, 20)],
                    shared_b: [(3, 20)],
                },
            )
            _schema(method_changed)
            codes = {item["code"] for item in method_changed["guard_violations"]}
            assert "BASELINE_CLONE_RELOCATED_OR_PERTURBED" in codes, method_changed
            assert (
                method_changed["objectives"]["baseline_like_copy_count"] == 2.0
            ), method_changed

            # A second exact copy in an explicitly changed file is relocation.
            java_reads.clear()
            _write(right, _delegate("Right", "SharedB"))
            _write(shared_b, _class("SharedB", _body(), static=True))
            relocated = guard.evaluate_code_clone_type1(
                project,
                locations,
                identity,
                analysis_files=[shared_a, shared_b],
            )
            _schema(relocated)
            codes = {item["code"] for item in relocated["guard_violations"]}
            assert "BASELINE_CLONE_RELOCATED_OR_PERTURBED" in codes, relocated
            assert relocated["objectives"]["baseline_like_copy_count"] == 2.0, relocated

            # One-token perturbation must not evade the bounded fingerprint
            # witness even though the exact SHA-256 changes.
            _write(shared_b, _class("SharedB", _body(changed_literal=True), static=True))
            perturbed = guard.evaluate_code_clone_type1(
                project,
                locations,
                identity,
                analysis_files=[shared_a, shared_b],
            )
            _schema(perturbed)
            codes = {item["code"] for item in perturbed["guard_violations"]}
            assert "BASELINE_CLONE_RELOCATED_OR_PERTURBED" in codes, perturbed
            scan = perturbed["witness"]["baseline_copy_scan"]
            assert scan["exact_match_count"] == 1, scan
            assert scan["near_match_count"] == 1, scan

            # CPD-style Type-1 findings are exact contiguous windows inside
            # the two selected methods. The complete method bodies need not be
            # equal when a sufficiently long exact middle remains.
            partial_left = project / "PartialLeft.java"
            partial_right = project / "PartialRight.java"
            _write(
                partial_left,
                _partial_clone_class("PartialLeft", "ParentA", "ChildA"),
            )
            _write(
                partial_right,
                _partial_clone_class("PartialRight", "ParentB", "ChildB"),
            )
            partial_locations = [
                parse_location_descriptor(
                    "PartialLeft.java:method=insert(Node newChild, int index)",
                    project,
                ),
                parse_location_descriptor(
                    "PartialRight.java:method=insert(Node newChild, int index)",
                    project,
                ),
            ]
            partial = guard.capture_code_clone_type1(project, partial_locations)
            _schema(partial)
            assert partial["ok"] is True, partial
            assert partial["target_smell_present"] is True, partial
            assert partial["objectives"]["clone_token_count"] >= 30, partial
            assert partial["entity_identity"]["clone_window_kind"] == "body", partial

            # Short bodies may form a CPD-sized exact window only when their
            # method declaration and body are considered together.
            short_left = project / "ShortLeft.java"
            short_right = project / "ShortRight.java"
            _write(short_left, _short_clone_class("ShortLeft"))
            _write(short_right, _short_clone_class("ShortRight"))
            short_locations = [
                parse_location_descriptor(
                    "ShortLeft.java:method=tiny(int a, int b, int c, int d, int e, int f)",
                    project,
                ),
                parse_location_descriptor(
                    "ShortRight.java:method=tiny(int a, int b, int c, int d, int e, int f)",
                    project,
                ),
            ]
            short = guard.capture_code_clone_type1(project, short_locations)
            _schema(short)
            assert short["target_smell_present"] is True, short
            assert short["entity_identity"]["clone_window_kind"] == "method", short

            # Identifier/literal similarity alone is Type-2, not an exact
            # Type-1 finding; the Guard must not normalize it into a PASS-able
            # baseline.
            near_left = project / "NearLeft.java"
            near_right = project / "NearRight.java"
            _write(near_left, _structural_near_class("NearLeft", "total", "+", 1))
            _write(near_right, _structural_near_class("NearRight", "result", "-", 21))
            near_locations = [
                parse_location_descriptor(
                    "NearLeft.java:method=calculate(int value)", project
                ),
                parse_location_descriptor(
                    "NearRight.java:method=calculate(int value)", project
                ),
            ]
            near = guard.capture_code_clone_type1(project, near_locations)
            _schema(near)
            assert near["ok"] is False, near
            assert near["target_smell_present"] is False, near
            assert near["witness"]["error"] == "BASELINE_FINDING_NOT_FOUND", near

            # None of the 400 unrelated Java files may be opened.
            assert not any("Noise" in path.name for path in java_reads), java_reads
        finally:
            Path.read_bytes = original_read_bytes
            for active in reversed(patches):
                active.stop()


if __name__ == "__main__":
    _run()
    print("target clone guard self-check passed")
