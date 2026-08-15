#!/usr/bin/env python3
"""Prove non-Java Guards read only caller-selected source locations."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from smell_core.checkpoint_adapters import capture_metric_snapshot  # noqa: E402
from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402
from smell_core.checkpoints import capture_baseline_finding_snapshot  # noqa: E402
from smell_core.config import load_refactor_config, resolve_run_config  # noqa: E402
from smell_core.guards import run_smell_guards  # noqa: E402
from smell_core.guards.context import GuardRunContext  # noqa: E402


GROUP = "int:start|int:end|int:retry"


def _guard_context(config, baseline, current, *, has_production_diff: bool) -> GuardRunContext:
    delta = evaluate_checkpoint_contract(
        baseline,
        current,
        has_production_diff=has_production_diff,
        smell=config.smell,
    ).to_dict()
    checkpoint = {
        "required": True,
        "smell": config.smell,
        "checkpoint_id": "c-target-scope",
        "verification_mode": config.verification_mode,
        "production_diff": has_production_diff,
        "baseline_metrics": baseline,
        "current_metrics": current,
        "delta": delta,
    }
    return GuardRunContext(
        checkpoint_required=True,
        checkpoint_smell=config.smell,
        current_metrics=current,
        metric_delta=delta,
        has_production_diff=has_production_diff,
        metric_progress=bool(delta.get("metric_progress")),
        checkpoint=checkpoint,
    )


def _source(language: str, name: str, include_group: bool = True) -> str:
    if language == "python":
        params = "start: int, end: int, retry: int" if include_group else "start: int, end: int"
        return f"def {name}({params}):\n    return start + end\n"
    params = "int start, int end, int retry" if include_group else "int start, int end"
    return f"int {name}({params}) {{ return start + end; }}\n"


def _extension(language: str) -> str:
    return {"python": ".py", "c": ".c", "cpp": ".cpp"}[language]


def _switch_source(language: str) -> str:
    if language == "python":
        return "def dispatch(x):\n    match x:\n        case 1: return 1\n        case _: return 0\n"
    return "int dispatch(int x) { switch (x) { case 1: return 1; default: return 0; } }\n"


def _nested_source(language: str) -> str:
    if language == "python":
        return (
            "def nested(x):\n"
            "    if x:\n        while x:\n            for i in range(x):\n"
            "                if i:\n                    while i:\n                        return i\n"
        )
    return (
        "int nested(int x) { if (x) { while (x) { for (int i = 0; i < x; ++i) "
        "{ if (i) { while (i) { return i; } } } } } return 0; }\n"
    )


def _repeated_local_source(language: str) -> str:
    if language == "python":
        return "def target(x):\n    if x:\n        m = 1\n    else:\n        m = 2\n    return m\n"
    return "int target(int x) { if (x) { int m = 1; } else { int m = 2; } return x; }\n"


def _mysterious_source(language: str, method: str) -> str:
    if language == "python":
        return f"def {method}(n: int):\n    return n + 1\n"
    return f"int {method}(int n) {{ return n + 1; }}\n"


def _config(
    project: Path,
    language: str,
    names: list[str],
    *,
    smell: str = "data_clumps",
    target_context: dict[str, str] | None = None,
):
    extension = _extension(language)
    locations = ";".join(
        f"{project / f'{name}{extension}'}:method={name}|line=1"
        for name in names
    )
    return resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell=smell,
        location=locations,
        cli_language=language,
        target_context=target_context if target_context is not None else {"group": GROUP},
    )


def _check_language(language: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"nonjava-target-scope-{language}-") as raw:
        project = Path(raw)
        extension = _extension(language)
        for name in ("first", "second", "third", "unlisted"):
            (project / f"{name}{extension}").write_text(
                _source(language, name),
                encoding="utf-8",
            )

        config = _config(project, language, ["first", "second", "third"])
        with patch(
            "smell_core.data_clumps.iter_function_signatures",
            side_effect=AssertionError("runtime Guard attempted project-wide discovery"),
        ):
            baseline = capture_baseline_finding_snapshot(config)
            ordinary_before = run_smell_guards(
                config,
                _guard_context(
                    config,
                    baseline,
                    baseline,
                    has_production_diff=False,
                ),
            )
        assert baseline["candidate_count"] == 1, baseline
        assert baseline["objectives"]["occurrence_count"] == 3, baseline
        assert baseline["scope_mode"] == "explicit_target_locations", baseline
        assert baseline["parsed_file_count"] == 3, baseline
        assert set(baseline["scope_files"]) == {
            f"first{extension}",
            f"second{extension}",
            f"third{extension}",
        }, baseline
        assert baseline["guard_scope"] == {
            "mode": "explicit_target_locations",
            "files": [f"first{extension}", f"second{extension}", f"third{extension}"],
            "file_count": 3,
            "source_bytes": sum((project / f"{name}{extension}").stat().st_size for name in ("first", "second", "third")),
            "source_discovery": "forbidden",
        }, baseline
        assert len(ordinary_before) == 1 and ordinary_before[0]["success"] is False, ordinary_before
        assert ordinary_before[0]["details"]["current_metrics"]["objectives"]["occurrence_count"] == 3, ordinary_before

        # The fourth matching function is intentionally outside the frozen
        # locations. Removing one listed occurrence must reduce the target
        # count to two instead of rediscovering the unlisted function.
        (project / f"third{extension}").write_text(
            _source(language, "third", include_group=False),
            encoding="utf-8",
        )
        with patch(
            "smell_core.data_clumps.iter_function_signatures",
            side_effect=AssertionError("runtime Guard attempted project-wide discovery"),
        ):
            current = capture_metric_snapshot(config, "group=forged")
            ordinary_after = run_smell_guards(
                config,
                _guard_context(
                    config,
                    baseline,
                    current,
                    has_production_diff=True,
                ),
            )
        assert current["ok"] is True, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["finding_present"] is False, current
        assert len(ordinary_after) == 1 and ordinary_after[0]["success"] is True, ordinary_after
        assert ordinary_after[0]["details"]["current_metrics"]["objectives"]["occurrence_count"] == 2, ordinary_after

        # Supplying only two locations cannot be rescued by matching functions
        # elsewhere in the project.
        two_targets = _config(project, language, ["first", "second"])
        try:
            capture_baseline_finding_snapshot(two_targets)
        except ValueError as exc:
            assert "BASELINE_FINDING_NOT_FOUND" in str(exc), exc
        else:
            raise AssertionError("two explicit occurrences unexpectedly captured a finding")


def _check_other_local_guards(language: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"nonjava-local-scope-{language}-") as raw:
        project = Path(raw)
        extension = _extension(language)
        target = project / f"target{extension}"
        target.write_text(_mysterious_source(language, "target"), encoding="utf-8")
        # This sibling would have been traversed by the old Mysterious Name
        # implementation even though it was not part of the target contract.
        (project / f"unlisted{extension}").write_text(
            _mysterious_source(language, "unlisted"),
            encoding="utf-8",
        )

        mysterious = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="mysterious_name",
            location=f"{target}:method=target|line=1",
            cli_language=language,
            target_context={"symbol_kind": "param", "symbol_name": "n"},
        )
        with patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("Mysterious Name attempted project discovery"),
        ):
            snapshot = capture_metric_snapshot(mysterious, "kind=forged")
        assert snapshot["finding_present"] is True, snapshot
        assert snapshot["guard_scope"]["files"] == [f"target{extension}"], snapshot
        assert snapshot["detector_profile"]["version"] == (
            f"nonjava-target-guard/{language}/mysterious_name/v6"
        ), snapshot
        assert snapshot["detector_profile"]["source_parseability_contract"] == (
            "selected-container-no-errors-with-frozen-target-file-recovery-v2"
        ), snapshot
        assert snapshot["detector_profile"]["same_hunk_witness_contract"] == (
            "target-old-new-lines-identifiers-same-unique-hunk-v1"
        ), snapshot
        assert snapshot["detector_profile"]["container_identity_contract"] == (
            "complete-parser-declaration-boundaries-and-sha256-v1"
        ), snapshot
        assert snapshot["detector_profile"]["container_continuity_contract"] == (
            "complete-container-cohort-old-current-target-patch-bijection-v1"
        ), snapshot

        dead = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="dead_code",
            location=f"{target}:method=target|line=1",
            cli_language=language,
        )
        with patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("Dead Code attempted project-wide reference search"),
        ):
            dead_snapshot = capture_metric_snapshot(dead, "method=forged")
            dead_guard = run_smell_guards(
                dead,
                _guard_context(
                    dead,
                    dead_snapshot,
                    dead_snapshot,
                    has_production_diff=False,
                ),
            )
        assert dead_snapshot["finding_present"] is True, dead_snapshot
        assert dead_snapshot["guard_scope"]["files"] == [f"target{extension}"], dead_snapshot
        assert len(dead_guard) == 1 and dead_guard[0]["success"] is False, dead_guard
        assert dead_guard[0]["details"]["current_metrics"]["guard_scope"]["mode"] == "explicit_target_locations", dead_guard

        switch_file = project / f"switch{extension}"
        switch_file.write_text(_switch_source(language), encoding="utf-8")
        switch = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="switch_statements",
            location=f"{switch_file}:method=dispatch|line=1",
            cli_language=language,
        )
        switch_snapshot = capture_baseline_finding_snapshot(switch)
        assert switch_snapshot["objectives"]["switch_count"] == 1, switch_snapshot
        assert switch_snapshot["guard_scope"]["files"] == [f"switch{extension}"], switch_snapshot

        nested_file = project / f"nested{extension}"
        nested_file.write_text(_nested_source(language), encoding="utf-8")
        nested = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="nested_complexity",
            location=f"{nested_file}:method=nested|line=1",
            cli_language=language,
        )
        nested_snapshot = capture_baseline_finding_snapshot(nested)
        assert nested_snapshot["objectives"]["cognitive_complexity"] == 5, nested_snapshot
        assert nested_snapshot["detector_profile"]["metric"] == "max_nesting_depth", nested_snapshot

        repeated_file = project / f"repeated{extension}"
        repeated_file.write_text(_repeated_local_source(language), encoding="utf-8")
        repeated = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="mysterious_name",
            location=f"{repeated_file}:method=target|line=1",
            cli_language=language,
            target_context={"symbol_kind": "local", "symbol_name": "m"},
        )
        try:
            capture_baseline_finding_snapshot(repeated)
        except ValueError as exc:
            assert "TARGET_AMBIGUOUS" in str(exc), exc
        else:
            raise AssertionError(
                "same kind/name at multiple declaration lines was aggregated"
            )


def main() -> int:
    for language in ("python", "c", "cpp"):
        _check_language(language)
        _check_other_local_guards(language)
    print(
        "Non-Java target-scope self-check passed: "
        "python/c/cpp explicit locations only; project discovery blocked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
