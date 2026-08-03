#!/usr/bin/env python3
"""Verify Java checkpoint-only dispatch and the retained non-Java path."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import CommandConfig, DefaultsConfig  # noqa: E402
from smell_core.guards import (  # noqa: E402
    run_build_test_guard,
    run_smell_guards,
    validate_java_strict_verification_contract,
)
from smell_core.guards.context import GuardRunContext  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402


def _profile(smell: str, **thresholds: object) -> SimpleNamespace:
    return SimpleNamespace(guards=[{"type": smell, **thresholds}])


def _java_config(
    *,
    guards: list[dict[str, object]] | None = None,
    run_build: bool = True,
    run_tests: bool = True,
    build_command: str = "true",
    test_command: str = "true",
) -> SimpleNamespace:
    return SimpleNamespace(
        language="java",
        smell="long_method",
        profile=SimpleNamespace(
            guards=[{"type": "long_method"}] if guards is None else guards,
        ),
        defaults=DefaultsConfig(run_build=run_build, run_tests=run_tests),
        build=CommandConfig(command=build_command or None),
        test=CommandConfig(command=test_command or None),
        verification_mode="project_full",
        build_source="self_check",
        test_source="self_check",
        sample_test_location="",
        sample_test_command="",
    )


def _violation_codes(result: dict[str, object]) -> set[str]:
    details = result.get("details")
    assert isinstance(details, dict), result
    violations = details.get("violations")
    assert isinstance(violations, list), result
    return {
        str(item.get("code"))
        for item in violations
        if isinstance(item, dict)
    }


def main() -> int:
    java = _java_config()

    missing = run_smell_guards(java)
    assert len(missing) == 1 and missing[0]["success"] is False, missing
    assert missing[0]["details"]["reason"] == "BASELINE_CHECKPOINT_MISSING", missing

    mismatch = run_smell_guards(
        java,
        GuardRunContext(
            checkpoint_required=True,
            checkpoint_smell="nested_complexity",
            checkpoint={"required": True},
        ),
    )
    assert len(mismatch) == 1 and mismatch[0]["success"] is False, mismatch
    assert mismatch[0]["details"]["reason"] == "BASELINE_CHECKPOINT_MISSING", mismatch

    checkpoint = {
        "required": True,
        "checkpoint_id": "c000-self-check",
        "finding_contract": {"detector_id": "java-product/long_method/v4"},
        "current_metrics": {
            "ok": True,
            "candidate_count": 0,
            "finding_present": False,
        },
        "delta": {"metric_progress": True},
    }
    resolved = run_smell_guards(
        java,
        GuardRunContext(
            checkpoint_required=True,
            checkpoint_smell="long_method",
            checkpoint=checkpoint,
        ),
    )
    assert len(resolved) == 1 and resolved[0]["success"] is True, resolved
    assert resolved[0]["details"]["guard"] == "checkpoint_contract", resolved

    no_guards = _java_config(guards=[])
    guard_failure = run_smell_guards(no_guards)
    assert guard_failure[0]["success"] is False, guard_failure
    assert "JAVA_GUARD_COUNT_INVALID" in _violation_codes(guard_failure[0]), guard_failure

    wrong_guard = _java_config(guards=[{"type": "nested_complexity"}])
    mismatch_failure = run_smell_guards(wrong_guard)
    assert mismatch_failure[0]["success"] is False, mismatch_failure
    assert "JAVA_GUARD_SMELL_MISMATCH" in _violation_codes(mismatch_failure[0]), mismatch_failure

    build_disabled = run_build_test_guard(_java_config(run_build=False))
    assert build_disabled["success"] is False, build_disabled
    assert "JAVA_BUILD_DISABLED" in _violation_codes(build_disabled), build_disabled

    tests_disabled = run_build_test_guard(_java_config(run_tests=False))
    assert tests_disabled["success"] is False, tests_disabled
    assert "JAVA_TESTS_DISABLED" in _violation_codes(tests_disabled), tests_disabled

    empty_build = run_build_test_guard(_java_config(build_command=""))
    assert empty_build["success"] is False, empty_build
    assert "JAVA_BUILD_COMMAND_MISSING" in _violation_codes(empty_build), empty_build

    empty_test = run_build_test_guard(_java_config(test_command=""))
    assert empty_test["success"] is False, empty_test
    assert "JAVA_TEST_COMMAND_MISSING" in _violation_codes(empty_test), empty_test

    with tempfile.TemporaryDirectory(prefix="nonjava-guard-dispatch-") as temp_dir:
        project = Path(temp_dir)
        source = project / "sample.py"
        source.write_text("def target(a, b):\n    return a + b\n", encoding="utf-8")
        target = parse_location_descriptor("sample.py:method=target|line=1", project)
        nonjava = SimpleNamespace(
            language="python",
            smell="long_parameter_list",
            project_root=project,
            profile=_profile("long_parameter_list", max_params=5),
            locations=[target],
            defaults=DefaultsConfig(run_build=False, run_tests=False),
            build=CommandConfig(),
            test=CommandConfig(),
            verification_mode="local",
            build_source="",
            test_source="",
            sample_test_location="",
            sample_test_command="",
        )
        assert validate_java_strict_verification_contract(nonjava) == []
        generic = run_smell_guards(nonjava)
        assert len(generic) == 1 and generic[0]["success"] is True, generic
        assert generic[0]["details"]["param_count"] == 2, generic
        nonjava_build = run_build_test_guard(nonjava)
        assert nonjava_build["success"] is True, nonjava_build

    print(
        "guard-dispatch self-check: PASS java_fallback=0 "
        "java_invalid_config_pass=0 nonjava_generic=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
