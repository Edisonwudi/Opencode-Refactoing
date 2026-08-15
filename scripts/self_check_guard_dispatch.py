#!/usr/bin/env python3
"""Verify checkpoint-only smell dispatch for every supported language."""
from __future__ import annotations

import sys
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


def _profile(smell: str, **thresholds: object) -> SimpleNamespace:
    return SimpleNamespace(guards=[{"type": smell, **thresholds}])


def _java_config(
    *,
    guards: list[dict[str, object]] | None = None,
    run_build: bool = True,
    run_tests: bool = True,
    build_command: str = "true",
    test_command: str = "true",
    sample_test_command: str = "true",
    verification_mode: str = "project_full",
) -> SimpleNamespace:
    root = Path.cwd().resolve()
    return SimpleNamespace(
        language="java",
        smell="long_method",
        profile=SimpleNamespace(
            guards=[{"type": "long_method"}] if guards is None else guards,
        ),
        defaults=DefaultsConfig(run_build=run_build, run_tests=run_tests),
        build=CommandConfig(command=build_command or None),
        test=CommandConfig(command=test_command or None),
        sample_test=CommandConfig(command=sample_test_command or None),
        project_root=root,
        dataset_root=root,
        verification_cwd=root,
        cwd=root,
        env={},
        verification_mode=verification_mode,
        verification_command_source="command",
        build_source="command",
        test_source="command",
        sample_test_source="command" if sample_test_command else "",
        sample_test_location="",
        sample_test_command=sample_test_command,
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
        "smell": "long_method",
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

    empty_sample_test = run_build_test_guard(_java_config(sample_test_command=""))
    assert empty_sample_test["success"] is True, empty_sample_test
    assert empty_sample_test["details"]["sample_test"] is None, empty_sample_test

    sample_optimized_without_test = run_build_test_guard(
        _java_config(
            test_command="",
            sample_test_command="",
            verification_mode="sample_optimized",
        )
    )
    assert sample_optimized_without_test["success"] is False
    assert "JAVA_SAMPLE_TEST_COMMAND_MISSING" in _violation_codes(
        sample_optimized_without_test
    ), sample_optimized_without_test

    project = Path.cwd().resolve()
    nonjava = SimpleNamespace(
        language="python",
        smell="long_parameter_list",
        project_root=project,
        profile=_profile("long_parameter_list", max_params=5),
        locations=[],
        defaults=DefaultsConfig(run_build=False, run_tests=False),
        build=CommandConfig(),
        test=CommandConfig(),
        sample_test=CommandConfig(),
        dataset_root=project,
        cwd=project,
        verification_cwd=project,
        env={},
        verification_mode="sample_optimized",
        verification_command_source="",
        build_source="",
        test_source="",
        sample_test_source="",
        sample_test_location="",
        sample_test_command="",
    )
    assert validate_java_strict_verification_contract(nonjava) == []

    nonjava_missing = run_smell_guards(nonjava)
    assert len(nonjava_missing) == 1 and nonjava_missing[0]["success"] is False, nonjava_missing
    assert nonjava_missing[0]["details"]["reason"] == "BASELINE_CHECKPOINT_MISSING", nonjava_missing

    nonjava_mismatch = run_smell_guards(
        nonjava,
        GuardRunContext(
            checkpoint_required=True,
            checkpoint_smell="nested_complexity",
            checkpoint={"required": True, "smell": "nested_complexity"},
        ),
    )
    assert len(nonjava_mismatch) == 1 and nonjava_mismatch[0]["success"] is False, nonjava_mismatch
    assert nonjava_mismatch[0]["details"]["reason"] == "BASELINE_CHECKPOINT_MISSING", nonjava_mismatch

    nonjava_checkpoint = {
        "required": True,
        "smell": "long_parameter_list",
        "checkpoint_id": "c000-nonjava-self-check",
        "current_metrics": {
            "ok": True,
            "candidate_count": 0,
            "finding_present": False,
        },
        "delta": {"metric_progress": True},
    }
    nonjava_resolved = run_smell_guards(
        nonjava,
        GuardRunContext(
            checkpoint_required=True,
            checkpoint_smell="long_parameter_list",
            checkpoint=nonjava_checkpoint,
        ),
    )
    # PASS is represented once: no second legacy dispatcher may append another
    # success result after the authoritative checkpoint gate.
    assert len(nonjava_resolved) == 1 and nonjava_resolved[0]["success"] is True, nonjava_resolved
    assert nonjava_resolved[0]["details"]["guard"] == "checkpoint_contract", nonjava_resolved

    unknown = SimpleNamespace(**{**vars(nonjava), "smell": "future_smell", "profile": _profile("future_smell")})
    unknown_result = run_smell_guards(unknown)
    assert len(unknown_result) == 1 and unknown_result[0]["success"] is False, unknown_result
    assert "Unknown guard type" in str(unknown_result[0]["message"]), unknown_result

    nonjava_build = run_build_test_guard(nonjava)
    assert nonjava_build["success"] is True, nonjava_build

    print(
        "guard-dispatch self-check: PASS java_fallback=0 "
        "java_invalid_config_pass=0 nonjava_checkpoint_only=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
