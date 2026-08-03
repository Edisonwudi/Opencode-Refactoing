#!/usr/bin/env python3
"""Self-check the target-file-only Java guard predicates."""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java import semantic_detector, syntactic_detector  # noqa: E402
from smell_core.java import target_guard_predicates as predicates  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402


def _source(*, resolved: bool) -> str:
    if resolved:
        long_body = "    consume(1);"
        nested_body = "    if (a && b && c && d && e && f) { consume(1); }"
        switch_body = "    if (value == 1) { consume(value); }"
        name_body = "    int result = 1; consume(result);"
    else:
        long_body = "\n".join(
            f"    consume({index});" for index in range(59)
        )
        nested_body = (
            "    if (a) { if (b) { if (c) { if (d) { if (e) { "
            "if (f) { consume(1); } } } } } }"
        )
        switch_body = (
            "    switch (value) { case 1: consume(value); break; "
            "default: break; }"
        )
        name_body = "    int tmp = 1; consume(tmp);"
    return f"""\
class Fixture {{
  void longTarget() {{
{long_body}
  }}
  void nested(boolean a, boolean b, boolean c, boolean d, boolean e, boolean f) {{
{nested_body}
  }}
  void dispatch(int value) {{
{switch_body}
  }}
  void names() {{
{name_body}
  }}
  void overloaded(int value) {{ if (value > 0) {{ consume(value); }} }}
  void overloaded(String value) {{ if (value != null) {{ consume(1); }} }}
  void consume(int value) {{}}
}}
"""


def _forbidden(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("a project detector was called by a target predicate")


def _assert_schema(result: dict[str, object]) -> None:
    assert {
        "ok",
        "target_match_count",
        "target_smell_present",
        "target_missing",
        "objectives",
        "entity_identity",
        "witness",
    } == set(result), result
    witness = result["witness"]
    assert isinstance(witness, dict), witness
    assert witness["parsed_file_count"] in {0, 1}, witness
    assert len(witness["source_scope"]) <= 1, witness


def _run() -> None:
    module_source = inspect.getsource(predicates)
    assert "run_java_" not in module_source
    assert ".rglob(" not in module_source

    with tempfile.TemporaryDirectory(prefix="target-guard-predicates-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(_source(resolved=False), encoding="utf-8")
        # This file must be irrelevant: target predicates receive exactly one
        # source file and do not enumerate project Java files.
        (project / "Unrelated.java").write_text(
            "class Unrelated { this is deliberately invalid Java }\n",
            encoding="utf-8",
        )

        locations = {
            "long_method": parse_location_descriptor(
                "Fixture.java:method=longTarget()",
                project,
            ),
            "nested_complexity": parse_location_descriptor(
                "Fixture.java:method=nested(boolean a, boolean b, boolean c, boolean d, boolean e, boolean f)",
                project,
            ),
            "switch_statements": parse_location_descriptor(
                "Fixture.java:method=dispatch(int value)",
                project,
            ),
            "mysterious_name": parse_location_descriptor(
                "Fixture.java:method=names()",
                project,
            ),
        }
        selectors = {
            "long_method": {"target_class": "Fixture"},
            "nested_complexity": {"target_class": "Fixture"},
            "switch_statements": {"target_class": "Fixture"},
            "mysterious_name": {
                "target_class": "Fixture",
                "container_method": "names()",
                "symbol_kind": "local",
                "symbol_name": "tmp",
            },
        }

        with (
            patch.object(
                syntactic_detector,
                "run_java_syntactic_detector",
                _forbidden,
            ),
            patch.object(
                semantic_detector,
                "run_java_semantic_detector",
                _forbidden,
            ),
        ):
            captured: dict[str, dict[str, object]] = {}
            for smell in locations:
                result = predicates.capture_target_guard_predicate(
                    smell,
                    project,
                    locations[smell],
                    selectors[smell],
                )
                _assert_schema(result)
                assert result["ok"] is True, (smell, result)
                assert result["target_match_count"] == 1, (smell, result)
                assert result["target_smell_present"] is True, (smell, result)
                assert result["target_missing"] is False, (smell, result)
                assert result["witness"]["source_scope"] == ["Fixture.java"]
                captured[smell] = result

            assert captured["long_method"]["objectives"] == {"ast_ncss": 60.0}
            assert captured["nested_complexity"]["objectives"] == {
                "cognitive_complexity": 21.0
            }
            assert captured["switch_statements"]["objectives"]["switch_count"] == 1.0
            assert captured["mysterious_name"]["entity_identity"]["symbol_name"] == "tmp"

            ambiguous = predicates.evaluate_nested_complexity(
                project,
                parse_location_descriptor(
                    "Fixture.java:method=overloaded",
                    project,
                ),
                {"target_class": "Fixture"},
            )
            _assert_schema(ambiguous)
            assert ambiguous["ok"] is False, ambiguous
            assert ambiguous["target_match_count"] == 2, ambiguous
            assert ambiguous["target_missing"] is False, ambiguous
            assert ambiguous["witness"]["error"] == "TARGET_AMBIGUOUS", ambiguous

            source.write_text(_source(resolved=True), encoding="utf-8")
            for smell in locations:
                result = predicates.evaluate_target_guard_predicate(
                    smell,
                    project,
                    locations[smell],
                    captured[smell]["entity_identity"],
                )
                _assert_schema(result)
                assert result["ok"] is True, (smell, result)
                assert result["target_smell_present"] is False, (smell, result)
                if smell == "mysterious_name":
                    assert result["target_match_count"] == 0, result
                    assert result["target_missing"] is True, result
                else:
                    assert result["target_match_count"] == 1, result
                    assert result["target_missing"] is False, result

            rejected_capture = predicates.capture_long_method(
                project,
                locations["long_method"],
                captured["long_method"]["entity_identity"],
            )
            assert rejected_capture["ok"] is False, rejected_capture
            assert (
                rejected_capture["witness"]["error"]
                == "BASELINE_FINDING_NOT_FOUND"
            ), rejected_capture

            unsupported = predicates.evaluate_target_guard_predicate(
                "god_class",
                project,
                locations["long_method"],
            )
            _assert_schema(unsupported)
            assert unsupported["ok"] is False, unsupported


if __name__ == "__main__":
    _run()
    print("target guard predicate self-check passed")
