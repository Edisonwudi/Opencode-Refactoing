#!/usr/bin/env python3
"""Exercise the bounded non-Java Data Clumps checkpoint closure."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from smell_core.checkpoint_adapters import capture_metric_snapshot  # noqa: E402
from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402
from smell_core.checkpoints import (  # noqa: E402
    capture_checkpoint_baseline,
    prepare_checkpoint,
)
from smell_core.config import load_refactor_config, resolve_run_config  # noqa: E402
from smell_core.guards import run_smell_guards  # noqa: E402
from smell_core.target_patch_identity import (  # noqa: E402
    ast_declaration_identity,
    evaluate_data_clump_target_patch_identity,
)


GROUP = "int:end|int:retry|int:start"
CPP_CONSTRUCTOR_GROUP = (
    "statsconst&:_stats|bool:_printinfomessages|std::ostream&:_stream"
)


def _legacy_function(name: str, factor: int) -> str:
    return (
        f"def {name}(start: int, end: int, retry: int):\n"
        f"    {name}_total = start * {factor} + end * {factor + 2}\n"
        "    if retry > 0:\n"
        f"        return {name}_total + retry\n"
        f"    return {name}_total - retry\n"
    )


def _baseline_source() -> str:
    return "\n\n".join(
        _legacy_function(name, factor)
        for name, factor in (
            ("alpha", 17),
            ("beta", 23),
            ("gamma", 31),
            ("delta", 41),
        )
    ) + "\n"


def _holder_function(name: str, factor: int) -> str:
    return (
        f"def {name}(bounds: Bounds):\n"
        f"    {name}_total = bounds.start * {factor} + bounds.end * {factor + 2}\n"
        "    if bounds.retry > 0:\n"
        f"        return {name}_total + bounds.retry\n"
        f"    return {name}_total - bounds.retry\n"
    )


def _derived_function(name: str, factor: int) -> str:
    return (
        f"def {name}(start: int, end: int):\n"
        "    retry = max(0, end - start)\n"
        f"    {name}_total = start * {factor} + end * {factor + 2}\n"
        "    if retry > 0:\n"
        f"        return {name}_total + retry\n"
        f"    return {name}_total - retry\n"
    )


def _source_with_replacements(
    first: str,
    second: str | None = None,
    *,
    suffix: str = "",
) -> str:
    pieces = [
        first,
        second if second is not None else _legacy_function("beta", 23),
        _legacy_function("gamma", 31),
        _legacy_function("delta", 41),
    ]
    return "\n\n".join(pieces) + "\n" + suffix


def _copied_alpha(name: str) -> str:
    return (
        f"def {name}(start, end, retry):\n"
        "    alpha_total = start * 17 + end * 19\n"
        "    if retry > 0:\n"
        "        return alpha_total + retry\n"
        "    return alpha_total - retry\n"
    )


def _copied_alpha_typed(name: str) -> str:
    return (
        f"def {name}(start: int, end: int, retry: int):\n"
        "    alpha_total = start * 17 + end * 19\n"
        "    if retry > 0:\n"
        "        return alpha_total + retry\n"
        "    return alpha_total - retry\n"
    )


def _different_group_helper(name: str) -> str:
    return (
        f"def {name}(start: int, end: int, retry: int):\n"
        "    values = [start, end, retry]\n"
        "    return max(values) - min(values)\n"
    )


def _typed_alpha(annotation: str) -> str:
    return (
        f"def alpha(start: {annotation}, end: {annotation}, retry: {annotation}):\n"
        "    alpha_total = start * 17 + end * 19\n"
        "    if retry > 0:\n"
        "        return alpha_total + retry\n"
        "    return alpha_total - retry\n"
    )


def _existing_alpha_copy(*, added_comment: bool = False) -> str:
    comment = "    # unrelated current edit\n" if added_comment else ""
    return (
        "def existing_copy(payload):\n"
        "    start, end, retry = payload\n"
        "    alpha_total = start * 17 + end * 19\n"
        f"{comment}"
        "    if retry > 0:\n"
        "        return alpha_total + retry\n"
        "    return alpha_total - retry\n"
    )


def _class_owned_source(owner: str, *, holder_alpha: bool = False) -> str:
    functions = "\n\n".join([
        (
            "def alpha(self, bounds: Bounds):\n"
            "    alpha_total = bounds.start * 17 + bounds.end * 19\n"
            "    if bounds.retry > 0:\n"
            "        return alpha_total + bounds.retry\n"
            "    return alpha_total - bounds.retry\n"
            if holder_alpha
            else (
                "def alpha(self, start: int, end: int, retry: int):\n"
                "    alpha_total = start * 17 + end * 19\n"
                "    if retry > 0:\n"
                "        return alpha_total + retry\n"
                "    return alpha_total - retry\n"
            )
        ),
        (
            "def beta(self, start: int, end: int, retry: int):\n"
            "    beta_total = start * 23 + end * 25\n"
            "    if retry > 0:\n"
            "        return beta_total + retry\n"
            "    return beta_total - retry\n"
        ),
        (
            "def gamma(self, start: int, end: int, retry: int):\n"
            "    gamma_total = start * 31 + end * 33\n"
            "    if retry > 0:\n"
            "        return gamma_total + retry\n"
            "    return gamma_total - retry\n"
        ),
    ])
    indented = "\n".join(
        f"    {line}" if line else ""
        for line in functions.splitlines()
    )
    return f"class {owner}:\n{indented}\n"


def _run(project: Path, *args: str) -> str:
    result = subprocess.run(
        [*args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr}"
        )
    return result.stdout


def _config(project: Path):
    target = project / "targets.py"
    return _python_config_for_names(
        project,
        target,
        ("alpha", "beta", "gamma", "delta"),
    )


def _python_config_for_names(
    project: Path,
    target: Path,
    names: tuple[str, ...],
):
    locations = ";".join(
        f"{target}:method={name}|line=1"
        for name in names
    )
    return resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell="data_clumps",
        location=locations,
        cli_language="python",
        target_context={"group": GROUP},
    )


def _three_target_fixture(
    *,
    extra_source: str = "",
) -> tuple[tempfile.TemporaryDirectory[str], Path, object, dict]:
    temporary = tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-three-target-"
    )
    project = Path(temporary.name)
    target = project / "targets.py"
    source = "\n\n".join(
        _legacy_function(name, factor)
        for name, factor in (
            ("alpha", 17),
            ("beta", 23),
            ("gamma", 31),
        )
    ) + "\n"
    if extra_source:
        source += "\n" + extra_source.rstrip() + "\n"
    target.write_text(source, encoding="utf-8")
    _run(project, "git", "init", "-q")
    _run(project, "git", "config", "user.email", "self-check@example.invalid")
    _run(project, "git", "config", "user.name", "Self Check")
    _run(project, "git", "add", "targets.py")
    _run(project, "git", "commit", "-qm", "baseline")
    config = _python_config_for_names(
        project,
        target,
        ("alpha", "beta", "gamma"),
    )
    baseline = capture_checkpoint_baseline(config)
    metrics = baseline["metrics"]
    assert metrics["objectives"]["occurrence_count"] == 3, metrics
    assert metrics["inline_copy_contract_available"] is True, metrics
    return temporary, project, config, baseline


def _fixture() -> tuple[tempfile.TemporaryDirectory[str], Path, object, dict]:
    temporary = tempfile.TemporaryDirectory(prefix="nonjava-data-clumps-checkpoint-")
    project = Path(temporary.name)
    (project / "targets.py").write_text(_baseline_source(), encoding="utf-8")
    (project / "unlisted.py").write_text("def untouched():\n    return 0\n", encoding="utf-8")
    _run(project, "git", "init", "-q")
    _run(project, "git", "config", "user.email", "self-check@example.invalid")
    _run(project, "git", "config", "user.name", "Self Check")
    _run(project, "git", "add", "targets.py", "unlisted.py")
    _run(project, "git", "commit", "-qm", "baseline")
    config = _config(project)
    baseline = capture_checkpoint_baseline(config)
    metrics = baseline["metrics"]
    assert metrics["objectives"]["occurrence_count"] == 4, metrics
    assert len(metrics["occurrence_contract"]) == 4, metrics
    assert all(item["parameter_slots"] for item in metrics["occurrence_contract"]), metrics
    assert metrics["inline_copy_contract_available"] is True, metrics
    return temporary, project, config, baseline


def _check_boundary_still_finding() -> None:
    temporary, project, config, _ = _fixture()
    try:
        (project / "targets.py").write_text(
            _source_with_replacements(_holder_function("alpha", 17)),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["finding_present"] is True, current
        assert current["objectives"]["occurrence_count"] == 3, current
        assert current["continuity_ok"] is True, current
        assert current["continuity_occurrence_count"] == 3, current
        assert current["inline_copy_analysis_ok"] is True, current
    finally:
        temporary.cleanup()


def _check_resolved_holder_route() -> None:
    temporary, project, config, _ = _fixture()
    try:
        holder = (
            "class Bounds:\n"
            "    def __init__(self, start: int, end: int, retry: int):\n"
            "        self.start = start\n"
            "        self.end = end\n"
            "        self.retry = retry\n\n"
        )
        (project / "targets.py").write_text(
            _source_with_replacements(
                _holder_function("alpha", 17),
                _holder_function("beta", 23),
                suffix="\n" + holder,
            ),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["finding_present"] is False, current
        assert current["continuity_occurrence_count"] == 2, current
        assert current["inline_copy_expansions"] == [], current
        assert checkpoint["delta"]["semantic_contract_preserved"] is True, checkpoint
        assert checkpoint["delta"]["metric_progress"] is True, checkpoint
    finally:
        temporary.cleanup()


def _check_non_holder_and_target_hunk_scope() -> None:
    temporary, project, config, _ = _fixture()
    try:
        (project / "targets.py").write_text(
            _source_with_replacements(
                _derived_function("alpha", 17),
                _derived_function("beta", 23),
            ),
            encoding="utf-8",
        )
        # These two copies are a deliberate scope trap. unlisted.py is changed
        # production source, but it is not one of config.locations' files.
        (project / "unlisted.py").write_text(
            _copied_alpha("outside_one") + "\n" + _copied_alpha("outside_two"),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert set(checkpoint["changed_production_source_files"]) == {
            "targets.py",
            "unlisted.py",
        }, checkpoint
        assert current["finding_present"] is False, current
        assert current["continuity_occurrence_count"] == 2, current
        assert current["inline_copy_expansions"] == [], current
        assert checkpoint["delta"]["semantic_contract_preserved"] is True, checkpoint
    finally:
        temporary.cleanup()


def _check_inline_copy_expansion_and_fail_closed_patch() -> None:
    temporary, project, config, baseline = _fixture()
    try:
        copied = "\n" + _copied_alpha("copied_one") + "\n" + _copied_alpha("copied_two")
        (project / "targets.py").write_text(
            _source_with_replacements(
                _holder_function("alpha", 17),
                _holder_function("beta", 23),
                suffix=copied,
            ),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["finding_present"] is False, current
        assert current["continuity_occurrence_count"] == 2, current
        assert current["inline_copy_analysis_ok"] is True, current
        assert current["inline_copy_expansions"], current
        assert any(
            item["source_method"] == "alpha"
            and item["baseline_occurrences"] == 1
            and item["current_occurrences"] >= 2
            for item in current["inline_copy_expansions"]
        ), current
        assert checkpoint["delta"]["semantic_contract_preserved"] is False, checkpoint
        assert any(
            str(item).startswith("inlined_body_window_relocated:targets.py#alpha:1->")
            for item in checkpoint["delta"]["semantic_contract"]["regressions"]
        ), checkpoint

        config.finding_contract = baseline["finding_contract"]
        missing_patch = capture_metric_snapshot(config, "", changed_patch=None)
        assert missing_patch["inline_copy_analysis_ok"] is False, missing_patch
        malformed_patch = capture_metric_snapshot(
            config,
            "",
            changed_patch="@@ malformed explicit target hunk @@\n",
        )
        assert malformed_patch["inline_copy_analysis_ok"] is False, malformed_patch
        assert malformed_patch["checkpoint_contract_error"] == (
            "changed_target_hunk_parse_failed"
        ), malformed_patch
        invalid_format = capture_metric_snapshot(
            config,
            "",
            changed_patch="not a target unified diff\n",
        )
        assert invalid_format["target_patch_identity_ok"] is False, invalid_format
        assert invalid_format["checkpoint_contract_error"] == (
            "changed_target_patch_format_invalid"
        ), invalid_format
    finally:
        temporary.cleanup()


def _c_family_function(
    name: str,
    factor: int,
    *,
    mode: str = "legacy",
    language: str,
) -> str:
    if mode == "holder":
        parameter = "Bounds bounds" if language == "c" else "const Bounds& bounds"
        start, end, retry = "bounds.start", "bounds.end", "bounds.retry"
    elif mode == "renamed":
        parameter = "int low, int high, int attempts"
        start, end, retry = "low", "high", "attempts"
    else:
        parameter = "int start, int end, int retry"
        start, end, retry = "start", "end", "retry"
    return (
        f"int {name}({parameter}) {{\n"
        f"  int {name}_total = {start} * {factor} + {end} * {factor + 2};\n"
        f"  if ({retry} > 0) return {name}_total + {retry};\n"
        f"  return {name}_total - {retry};\n"
        "}\n"
    )


def _c_family_source(
    language: str,
    *,
    alpha_mode: str = "legacy",
    beta_mode: str = "legacy",
) -> str:
    prefix = (
        "typedef struct Bounds { int start; int end; int retry; } Bounds;\n\n"
        if language == "c"
        else "struct Bounds { int start; int end; int retry; };\n\n"
    )
    return prefix + "\n".join(
        _c_family_function(name, factor, mode=mode, language=language)
        for name, factor, mode in (
            ("alpha", 17, alpha_mode),
            ("beta", 23, beta_mode),
            ("gamma", 31, "legacy"),
            ("delta", 41, "legacy"),
        )
    )


def _check_c_family_continuity(language: str) -> None:
    with tempfile.TemporaryDirectory(
        prefix=f"nonjava-data-clumps-{language}-"
    ) as raw:
        project = Path(raw)
        extension = ".c" if language == "c" else ".cpp"
        target = project / f"targets{extension}"
        target.write_text(_c_family_source(language), encoding="utf-8")
        locations = ";".join(
            f"{target}:method={name}|line=1"
            for name in ("alpha", "beta", "gamma", "delta")
        )
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language=language,
            target_context={"group": GROUP},
        )
        baseline = capture_metric_snapshot(config, "")
        assert baseline["finding_present"] is True, baseline
        assert baseline["objectives"]["occurrence_count"] == 4, baseline
        assert baseline["inline_copy_contract_available"] is True, baseline
        config.finding_contract = {
            "entity_identity": baseline["finding_identity"],
            "baseline_occurrence_contract": baseline["occurrence_contract"],
        }

        target.write_text(
            _c_family_source(
                language,
                alpha_mode="holder",
                beta_mode="holder",
            ),
            encoding="utf-8",
        )
        holder = capture_metric_snapshot(config, "", changed_patch="")
        assert holder["target_missing"] is False, holder
        assert holder["finding_present"] is False, holder
        assert holder["objectives"]["occurrence_count"] == 2, holder
        assert holder["continuity_ok"] is True, holder
        assert holder["continuity_occurrence_count"] == 2, holder
        holder_delta = evaluate_checkpoint_contract(
            baseline,
            holder,
            has_production_diff=True,
            smell="data_clumps",
        )
        assert holder_delta.semantic_contract_preserved is True, holder_delta

        target.write_text(
            _c_family_source(
                language,
                alpha_mode="renamed",
                beta_mode="renamed",
            ),
            encoding="utf-8",
        )
        renamed = capture_metric_snapshot(config, "", changed_patch="")
        assert renamed["objectives"]["occurrence_count"] == 2, renamed
        assert renamed["finding_present"] is False, renamed
        assert renamed["continuity_ok"] is True, renamed
        assert renamed["continuity_occurrence_count"] == 4, renamed
        assert all(
            item.get("match_mode") in {
                "exact_frozen_group",
                "frozen_parameter_slot_name_or_type",
            }
            for item in renamed["continuity_occurrences"]
        ), renamed
        renamed_delta = evaluate_checkpoint_contract(
            baseline,
            renamed,
            has_production_diff=True,
            smell="data_clumps",
        )
        assert renamed_delta.semantic_contract_preserved is False, renamed_delta
        assert any(
            str(item).startswith("parameter_group_remains:")
            for item in renamed_delta.semantic_contract_delta["regressions"]
        ), renamed_delta


def _check_unresolved_target_fails_closed() -> None:
    temporary, project, config, _ = _fixture()
    try:
        (project / "targets.py").unlink()
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["target_missing"] is True, current
        assert current["objectives"]["occurrence_count"] == 0, current
        assert current["continuity_ok"] is False, current
        assert checkpoint["delta"]["metric_progress"] is False, checkpoint
        assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
        ordinary = run_smell_guards(config)
        assert len(ordinary) == 1 and ordinary[0]["success"] is False, ordinary
        assert ordinary[0]["details"]["target_missing"] is True, ordinary
    finally:
        temporary.cleanup()


def _check_parse_failure_fails_closed() -> None:
    temporary, _, config, baseline = _fixture()
    try:
        config.finding_contract = baseline["finding_contract"]
        with patch(
            "smell_core.data_clumps.extract_snippet_candidates",
            side_effect=RuntimeError("synthetic parse failure"),
        ):
            snapshot = capture_metric_snapshot(config, "", changed_patch="")
            ordinary = run_smell_guards(config)
        assert snapshot["ok"] is False, snapshot
        assert snapshot["target_missing"] is True, snapshot
        assert snapshot["continuity_ok"] is False, snapshot
        assert len(ordinary) == 1 and ordinary[0]["success"] is False, ordinary
        assert "evaluation unavailable" in ordinary[0]["message"], ordinary
    finally:
        temporary.cleanup()


def _shared_function(name: str, parameter: str) -> str:
    return (
        f"def {name}({parameter}):\n"
        "    total = start * 17 + end * 19\n"
        "    if retry > 0:\n"
        "        return total + retry\n"
        "    return total - retry\n"
    )


def _check_shared_non_unique_windows() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-shared-window-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.py"
        target.write_text(
            "\n\n".join(
                _shared_function(name, "start: int, end: int, retry: int")
                for name in ("alpha", "beta", "gamma", "delta")
            ) + "\n",
            encoding="utf-8",
        )
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.py")
        _run(project, "git", "commit", "-qm", "baseline")
        locations = ";".join(
            f"{target}:method={name}|line=1"
            for name in ("alpha", "beta", "gamma", "delta")
        )
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language="python",
            target_context={"group": GROUP},
        )
        baseline = capture_checkpoint_baseline(config)
        contracts = baseline["metrics"]["occurrence_contract"]
        assert len(contracts) == 4, contracts
        assert all(item["body_windows"] for item in contracts), contracts
        assert all(
            int(window["baseline_occurrences"]) == 4
            for item in contracts
            for window in item["body_windows"]
        ), contracts

        migrated = (
            "class Bounds:\n"
            "    pass\n\n"
            "def alpha(bounds: Bounds):\n"
            "    return bounds\n\n"
            "def beta(bounds: Bounds):\n"
            "    return bounds\n\n"
        )
        retained = "\n\n".join(
            _shared_function(name, "start: int, end: int, retry: int")
            for name in ("gamma", "delta")
        )
        copied = "\n\n".join(
            _shared_function(name, "start, end, retry")
            for name in ("copied_one", "copied_two", "copied_three")
        )
        target.write_text(
            migrated + retained + "\n\n" + copied + "\n",
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["finding_present"] is False, current
        assert current["inline_copy_contract_available"] is True, current
        assert any(
            item["baseline_occurrences"] == 4
            and item["current_occurrences"] == 5
            for item in current["inline_copy_expansions"]
        ), current
        assert checkpoint["delta"]["semantic_contract_preserved"] is False, checkpoint


def _check_short_body_baseline_rejected() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-short-window-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.py"
        target.write_text(
            "\n\n".join(
                (
                    f"def {name}(start: int, end: int, retry: int):\n"
                    "    return start\n"
                )
                for name in ("alpha", "beta", "gamma", "delta")
            ) + "\n",
            encoding="utf-8",
        )
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.py")
        _run(project, "git", "commit", "-qm", "baseline")
        config = _config_for_target(project, target)
        snapshot = capture_metric_snapshot(config, "")
        assert snapshot["finding_present"] is True, snapshot
        assert snapshot["inline_copy_contract_available"] is False, snapshot
        assert snapshot["inline_copy_analysis_ok"] is False, snapshot
        assert snapshot["ok"] is False, snapshot
        try:
            capture_checkpoint_baseline(config)
        except ValueError as exc:
            assert "baseline_body_window_contract_unavailable" in str(exc), exc
        else:
            raise AssertionError("short-body Data Clumps baseline was captured")


def _check_empty_cpp_body_has_explicit_no_copy_witness() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-empty-cpp-body-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.cpp"
        source = "\n\n".join(
            f"struct {name} {{\n"
            f"  {name}(int start, int end, int retry) {{}}\n"
            "};"
            for name in ("Alpha", "Beta", "Gamma", "Delta")
        ) + "\n"
        target.write_text(source, encoding="utf-8")
        locations = ";".join(
            f"{target}:method={name}|line=1"
            for name in ("Alpha", "Beta", "Gamma", "Delta")
        )
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language="cpp",
            target_context={"group": GROUP},
        )
        snapshot = capture_metric_snapshot(config, "")
        assert snapshot["ok"] is True, snapshot
        assert snapshot["finding_present"] is True, snapshot
        assert snapshot["inline_copy_contract_available"] is True, snapshot
        assert all(
            record["body_windows"] == []
            and record["body_copy_not_applicable"]
            == "empty_function_body"
            for record in snapshot["occurrence_contract"]
        ), snapshot


def _config_for_target(project: Path, target: Path):
    locations = ";".join(
        f"{target}:method={name}|line=1"
        for name in ("alpha", "beta", "gamma", "delta")
    )
    return resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell="data_clumps",
        location=locations,
        cli_language="python",
        target_context={"group": GROUP},
    )


def _check_old_contract_without_witness_fails_closed() -> None:
    temporary, _, config, baseline = _fixture()
    try:
        config.finding_contract = {
            "entity_identity": baseline["metrics"]["finding_identity"],
        }
        current = capture_metric_snapshot(config, "", changed_patch="")
        assert current["continuity_ok"] is False, current
        assert current["inline_copy_contract_available"] is False, current
        assert current["inline_copy_analysis_ok"] is False, current
        assert current["checkpoint_contract_error"] == (
            "baseline_occurrence_contract_unavailable"
        ), current
        profile = current["detector_profile"]
        assert profile["continuity_contract"] == (
            "frozen-parameter-slot-name-or-type-v2"
        ), profile
        assert "source-relocation" in profile["inline_copy_contract"], profile
        assert profile["changed_hunk_group_contract"] == (
            "added-target-hunk-signatures-v1"
        ), profile
    finally:
        temporary.cleanup()


def _cpp_overload_source(*, include_second: bool) -> str:
    functions = [
        (
            "int compute(int start, int end, int retry) {\n"
            "  int total = start * 17 + end * 19;\n"
            "  return retry > 0 ? total + retry : total - retry;\n"
            "}\n"
        ),
    ]
    if include_second:
        functions.append(
            "int compute(int start, int end, int retry, int mode) {\n"
            "  int total = start * 23 + end * 29 + mode;\n"
            "  return retry > 0 ? total + retry : total - retry;\n"
            "}\n"
        )
    functions.extend([
        (
            "int gamma(int start, int end, int retry) {\n"
            "  int total = start * 31 + end * 37;\n"
            "  return retry > 0 ? total + retry : total - retry;\n"
            "}\n"
        ),
        (
            "int delta(int start, int end, int retry) {\n"
            "  int total = start * 41 + end * 43;\n"
            "  return retry > 0 ? total + retry : total - retry;\n"
            "}\n"
        ),
    ])
    return "\n".join(functions)


def _cpp_constructor_definition(
    owner: str,
    *,
    holder: bool,
) -> str:
    signature = (
        f"{owner}::{owner}(PrinterContext const& ctx)"
        if holder
        else (
            f"{owner}::{owner}(std::ostream& _stream, Stats const& _stats, "
            "bool _printInfoMessages)"
        )
    )
    stream = "ctx.stream" if holder else "_stream"
    stats = "ctx.stats" if holder else "_stats"
    print_info = "ctx.printInfoMessages" if holder else "_printInfoMessages"
    return (
        ("// parameter-object constructor\n" if holder else "")
        + f"{signature} {{\n"
        f"  int total = {stats}.value + ({print_info} ? 7 : 3);\n"
        f"  {stream}.write(total);\n"
        f"  if ({print_info}) {{\n"
        f"    {stream}.write({stats}.value + total);\n"
        "  }\n"
        "}\n"
    )


def _cpp_constructor_source(*, holder: bool) -> str:
    owners = ("FirstPrinter", "SecondPrinter", "ThirdPrinter", "FourthPrinter")
    declarations = "\n".join(
        (
            f"struct {owner} {{\n"
            + (
                f"  {owner}(PrinterContext const& ctx);\n"
                if holder
                else (
                    f"  {owner}(std::ostream& _stream, Stats const& _stats, "
                    "bool _printInfoMessages);\n"
                )
            )
            + "};"
        )
        for owner in owners
    )
    holder_prelude = (
        "struct PrinterContext {\n"
        "  std::ostream& stream;\n"
        "  Stats const& stats;\n"
        "  bool printInfoMessages;\n"
        "};\n"
        "PrinterContext makePrinterContext(\n"
        "    std::ostream& _stream, Stats const& _stats, bool _printInfoMessages) {\n"
        "  return {_stream, _stats, _printInfoMessages};\n"
        "}\n"
        if holder
        else ""
    )
    return (
        "namespace std { struct ostream { void write(int); }; }\n"
        "struct Stats { int value; };\n"
        + holder_prelude
        + declarations
        + "\n\n"
        + "\n\n".join(
            _cpp_constructor_definition(owner, holder=holder)
            for owner in owners
        )
        + "\n"
    )


def _cpp_constructor_config(project: Path, target: Path, source: str):
    owners = ("FirstPrinter", "SecondPrinter", "ThirdPrinter", "FourthPrinter")
    locations = []
    lines = source.splitlines()
    for owner in owners:
        line_number = next(
            index
            for index, line in enumerate(lines, start=1)
            if line.startswith(f"{owner}::{owner}(")
        )
        locations.append(f"{target}:method={owner}|line={line_number}")
    return resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell="data_clumps",
        location=";".join(locations),
        cli_language="cpp",
        target_context={"group": CPP_CONSTRUCTOR_GROUP},
    )


def _check_type_only_mutation_continuity() -> None:
    temporary, project, config, _ = _three_target_fixture()
    try:
        (project / "targets.py").write_text(
            "\n\n".join([
                _typed_alpha("str"),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
            ]) + "\n",
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["target_patch_identity_ok"] is True, current
        assert current["finding_present"] is False, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["continuity_occurrence_count"] == 3, current
        alpha = next(
            item
            for item in current["continuity_occurrences"]
            if item["method"] == "alpha"
        )
        assert alpha["match_mode"] == (
            "frozen_parameter_slot_name_or_type"
        ), alpha
        assert checkpoint["delta"]["semantic_contract_preserved"] is False, checkpoint
        assert any(
            str(item).startswith("parameter_group_remains:")
            for item in checkpoint["delta"]["semantic_contract"]["regressions"]
        ), checkpoint
    finally:
        temporary.cleanup()


def _check_one_for_one_helper_relocation() -> None:
    temporary, project, config, _ = _three_target_fixture()
    try:
        (project / "targets.py").write_text(
            "\n\n".join([
                _holder_function("alpha", 17),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
                _copied_alpha_typed("alpha_helper"),
            ]) + "\n",
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["target_patch_identity_ok"] is True, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["continuity_occurrence_count"] == 3, current
        assert any(
            item.get("method") == "alpha_helper"
            and item.get("match_mode") == "added_target_hunk_frozen_group"
            for item in current["continuity_occurrences"]
        ), current
        relocation = next(
            item
            for item in current["inline_copy_expansions"]
            if item["source_method"] == "alpha"
        )
        assert relocation["reason"] == "source_window_relocated", relocation
        assert relocation["baseline_occurrences"] == 1, relocation
        assert relocation["current_occurrences"] == 1, relocation
        assert checkpoint["delta"]["semantic_contract_preserved"] is False, checkpoint
        assert any(
            str(item).startswith(
                "inlined_body_window_relocated:targets.py#alpha:1->1"
            )
            for item in checkpoint["delta"]["semantic_contract"]["regressions"]
        ), checkpoint
    finally:
        temporary.cleanup()


def _check_added_helper_group_without_body_copy() -> None:
    temporary, project, config, _ = _three_target_fixture()
    try:
        (project / "targets.py").write_text(
            "\n\n".join([
                _holder_function("alpha", 17),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
                _different_group_helper("alpha_helper"),
            ]) + "\n",
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["target_patch_identity_ok"] is True, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["continuity_occurrence_count"] == 3, current
        assert any(
            item.get("method") == "alpha_helper"
            and item.get("match_mode") == "added_target_hunk_frozen_group"
            for item in current["continuity_occurrences"]
        ), current
        assert current["inline_copy_expansions"] == [], current
        assert checkpoint["delta"]["semantic_contract_preserved"] is False, checkpoint
    finally:
        temporary.cleanup()


def _check_hunk_context_is_not_new_copy() -> None:
    temporary, project, config, _ = _three_target_fixture(
        extra_source=_existing_alpha_copy(),
    )
    try:
        (project / "targets.py").write_text(
            "\n\n".join([
                _holder_function("alpha", 17),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
                _existing_alpha_copy(added_comment=True),
            ]) + "\n",
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["target_patch_identity_ok"] is True, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["continuity_occurrence_count"] == 2, current
        assert current["inline_copy_expansions"] == [], current
        assert checkpoint["delta"]["semantic_contract_preserved"] is True, checkpoint
    finally:
        temporary.cleanup()


def _check_same_name_decoys_do_not_replace_frozen_target() -> None:
    scenarios = (
        (
            "existing-unlisted-decoy",
            _holder_function("alpha", 53),
            "\n\n".join([
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
                _holder_function("alpha", 53),
            ]) + "\n",
        ),
        (
            "distant-added-decoy",
            "",
            "\n\n".join([
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
                _holder_function("alpha", 59),
            ]) + "\n",
        ),
        (
            "same-replacement-block-decoy",
            "",
            "\n\n".join([
                _holder_function("renamed_alpha", 61),
                _holder_function("alpha", 67),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
            ]) + "\n",
        ),
    )
    for label, baseline_extra, current_source in scenarios:
        temporary, project, config, _ = _three_target_fixture(
            extra_source=baseline_extra,
        )
        try:
            (project / "targets.py").write_text(
                current_source,
                encoding="utf-8",
            )
            checkpoint = prepare_checkpoint(config, "")
            current = checkpoint["current_metrics"]
            assert current["finding_present"] is False, (label, current)
            assert current["objectives"]["occurrence_count"] == 2, (label, current)
            assert current["target_patch_identity_ok"] is False, (label, current)
            assert current["target_missing"] is True, (label, current)
            assert current["target_patch_identity_failures"], (label, current)
            assert checkpoint["delta"]["metric_progress"] is False, (
                label,
                checkpoint,
            )
            assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", (
                label,
                checkpoint,
            )
            ordinary = run_smell_guards(config)
            assert len(ordinary) == 1 and ordinary[0]["success"] is False, (
                label,
                ordinary,
            )
        finally:
            temporary.cleanup()


def _check_malformed_current_fails_closed(language: str) -> None:
    with tempfile.TemporaryDirectory(
        prefix=f"nonjava-data-clumps-malformed-{language}-"
    ) as raw:
        project = Path(raw)
        extension = ".py" if language == "python" else ".c" if language == "c" else ".cpp"
        target = project / f"targets{extension}"
        if language == "python":
            baseline_source = "\n\n".join([
                _legacy_function("alpha", 17),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
            ]) + "\n"
            malformed_source = baseline_source.replace(
                _legacy_function("alpha", 17),
                "def alpha(start: int, end: int, retry: int):\n"
                "    value = (start +\n"
                "    return value\n",
                1,
            )
        else:
            baseline_source = _c_family_source(language)
            malformed_source = baseline_source.replace(
                _c_family_function(
                    "alpha",
                    17,
                    mode="legacy",
                    language=language,
                ),
                "int alpha(int start, int end, int retry) {\n"
                "  return start + ;\n"
                "}\n",
                1,
            )
        target.write_text(baseline_source, encoding="utf-8")
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", target.name)
        _run(project, "git", "commit", "-qm", "baseline")
        locations = ";".join(
            f"{target}:method={name}|line=1"
            for name in ("alpha", "beta", "gamma")
        )
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language=language,
            target_context={"group": GROUP},
        )
        baseline = capture_checkpoint_baseline(config)
        assert baseline["metrics"]["finding_present"] is True, baseline
        target.write_text(
            malformed_source,
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["ok"] is False, current
        assert current["target_missing"] is True, current
        assert "target parse failed" in current["error"], current
        assert checkpoint["delta"]["metric_progress"] is False, checkpoint
        assert checkpoint["delta"]["reason"] == (
            "CURRENT_DETECTOR_UNAVAILABLE"
        ), checkpoint
        ordinary = run_smell_guards(config)
        assert len(ordinary) == 1 and ordinary[0]["success"] is False, ordinary


def _check_target_external_parse_error_is_ignored() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-cpp-target-local-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.cpp"
        target.write_text(
            _c_family_source("cpp")
            + "\nint unrelated_broken( {\n  return 0;\n}\n",
            encoding="utf-8",
        )
        locations = ";".join(
            f"{target}:method={name}|line=1"
            for name in ("alpha", "beta", "gamma", "delta")
        )
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language="cpp",
            target_context={"group": GROUP},
        )
        snapshot = capture_metric_snapshot(config, "")
        assert snapshot["ok"] is True, snapshot
        assert snapshot["finding_present"] is True, snapshot
        assert snapshot["objectives"]["occurrence_count"] == 4, snapshot
        assert snapshot["scope_files"] == ["targets.cpp"], snapshot


def _check_ast_owner_change_fails_closed() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-owner-identity-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.py"
        target.write_text(
            _class_owned_source("OwnerA"),
            encoding="utf-8",
        )
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.py")
        _run(project, "git", "commit", "-qm", "baseline")
        config = _python_config_for_names(
            project,
            target,
            ("alpha", "beta", "gamma"),
        )
        baseline = capture_checkpoint_baseline(config)
        occurrence_contract = baseline["metrics"]["occurrence_contract"]
        assert all(
            item["declaration_identity"]["declared_name"]
            in {"alpha", "beta", "gamma"}
            and item["declaration_identity"]["owner_qualified_name"]
            == "OwnerA"
            for item in occurrence_contract
        ), occurrence_contract
        target.write_text(
            _class_owned_source("OwnerB", holder_alpha=True),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["finding_present"] is False, current
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["target_patch_identity_ok"] is False, current
        assert current["target_missing"] is True, current
        assert any(
            item.get("reason") == "target_declaration_identity_changed"
            and item.get("baseline_declaration_identity", {}).get(
                "owner_qualified_name"
            ) == "OwnerA"
            and item.get("current_declaration_identity", {}).get(
                "owner_qualified_name"
            ) == "OwnerB"
            for item in current["target_patch_identity_failures"]
        ), current
        assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
        ordinary = run_smell_guards(config)
        assert len(ordinary) == 1 and ordinary[0]["success"] is False, ordinary


def _check_frozen_patch_anchor_beats_nearest_same_name() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-frozen-anchor-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.py"
        prefix_before_insertion = (
            "class DecoyError:\n"
            "    def __init__(self):\n"
            "        pass\n\n"
            "# pad 1\n"
        )
        prefix_after_insertion = (
            "# pad 2\n# pad 3\n# pad 4\n"
            "# frozen target follows\n"
        )
        target_function = (
            "class Target:\n"
            "    def __init__(self, start: int, end: int, retry: int):\n"
            "        self.total = start * 17 + end * 19\n"
            "        if retry > 0:\n"
            "            self.total += retry\n"
            "        self.total -= retry\n"
        )
        tail = "\n" + "\n\n".join([
            _legacy_function("beta", 23),
            _legacy_function("gamma", 31),
        ]) + "\n"
        baseline_source = (
            prefix_before_insertion
            + prefix_after_insertion
            + target_function
            + tail
        )
        target.write_text(baseline_source, encoding="utf-8")
        target_line = next(
            line_number
            for line_number, line in enumerate(
                baseline_source.splitlines(),
                start=1,
            )
            if line.strip().startswith("def __init__(self, start:")
        )
        locations = ";".join([
            f"{target}:method=__init__|line={target_line}",
            f"{target}:method=beta|line=1",
            f"{target}:method=gamma|line=1",
        ])
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language="python",
            target_context={"group": GROUP},
        )
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.py")
        _run(project, "git", "commit", "-qm", "baseline")
        baseline = capture_checkpoint_baseline(config)
        assert baseline["metrics"]["objectives"]["occurrence_count"] == 3, baseline

        holder = (
            "class Bounds:\n"
            "    def __init__(self, start: int, end: int, retry: int):\n"
            "        self.start = start\n"
            "        self.end = end\n"
            "        self.retry = retry\n\n"
            "# holder pad 1\n# holder pad 2\n"
            "# holder pad 3\n# holder pad 4\n"
        )
        migrated_target = target_function.replace(
            "def __init__(self, start: int, end: int, retry: int):",
            "def __init__(self, bounds: Bounds):",
        ).replace("start", "bounds.start").replace(
            "end",
            "bounds.end",
        ).replace("retry", "bounds.retry")
        target.write_text(
            prefix_before_insertion
            + holder
            + prefix_after_insertion
            + migrated_target
            + tail,
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["objectives"]["occurrence_count"] == 2, current
        assert current["target_patch_identity_ok"] is True, current
        assert current["target_patch_identity_failures"] == [], current
        assert current["target_missing"] is False, current
        assert current["continuity_ok"] is True, current
        assert checkpoint["delta"]["metric_progress"] is False, checkpoint
        assert checkpoint["delta"]["reason"] == (
            "SEMANTIC_CONTRACT_REGRESSION"
        ), checkpoint


def _check_invalid_ast_identity_baseline_rejected() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-invalid-owner-witness-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.py"
        target.write_text(
            "\n\n".join([
                _legacy_function("alpha", 17),
                _legacy_function("beta", 23),
                _legacy_function("gamma", 31),
            ]) + "\n",
            encoding="utf-8",
        )
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.py")
        _run(project, "git", "commit", "-qm", "baseline")
        config = _python_config_for_names(
            project,
            target,
            ("alpha", "beta", "gamma"),
        )
        invalid_identity = {
            "contract": "ast-declared-name-and-owner-v1",
            "declared_name": "",
            "owner_qualified_name": "",
        }
        with patch(
            "smell_core.data_clumps.ast_declaration_identity",
            return_value=invalid_identity,
        ):
            snapshot = capture_metric_snapshot(config, "")
            assert snapshot["ok"] is False, snapshot
            assert snapshot["error"] == (
                "baseline_declaration_identity_contract_unavailable"
            ), snapshot
            try:
                capture_checkpoint_baseline(config)
            except ValueError as exc:
                assert "baseline_declaration_identity_contract_unavailable" in str(exc), exc
            else:
                raise AssertionError("invalid AST identity baseline was captured")


def _check_overload_target_identity_collision() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-overload-collision-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.cpp"
        baseline_source = _cpp_overload_source(include_second=True)
        target.write_text(baseline_source, encoding="utf-8")
        starts: dict[str, list[int]] = {}
        for line_number, line in enumerate(baseline_source.splitlines(), start=1):
            if line.startswith("int compute("):
                starts.setdefault("compute", []).append(line_number)
            elif line.startswith("int gamma("):
                starts.setdefault("gamma", []).append(line_number)
            elif line.startswith("int delta("):
                starts.setdefault("delta", []).append(line_number)
        locations = ";".join([
            f"{target}:method=compute|line={starts['compute'][0]}",
            f"{target}:method=compute|line={starts['compute'][1]}",
            f"{target}:method=gamma|line={starts['gamma'][0]}",
            f"{target}:method=delta|line={starts['delta'][0]}",
        ])
        config = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[],
            project_root=str(project),
            smell="data_clumps",
            location=locations,
            cli_language="cpp",
            target_context={"group": GROUP},
        )
        baseline = capture_metric_snapshot(config, "")
        assert baseline["ok"] is True, baseline
        assert baseline["objectives"]["occurrence_count"] == 4, baseline
        config.finding_contract = {
            "entity_identity": baseline["finding_identity"],
            "baseline_occurrence_contract": baseline["occurrence_contract"],
        }
        target.write_text(
            _cpp_overload_source(include_second=False),
            encoding="utf-8",
        )
        current = capture_metric_snapshot(config, "", changed_patch="")
        assert current["ok"] is False, current
        assert current["target_missing"] is True, current
        assert current["target_identity_collision"] is True, current
        assert current["error"] == "target_identity_collision", current
        assert current["target_identity_collisions"][0]["target_indexes"] == [0, 1], current
        ordinary = run_smell_guards(config)
        assert len(ordinary) == 1 and ordinary[0]["success"] is False, ordinary
        assert ordinary[0]["details"]["target_identity_collision"] is True, ordinary


def _check_cpp_constructor_signature_reanchor() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-constructor-reanchor-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.cpp"
        baseline_source = _cpp_constructor_source(holder=False)
        target.write_text(baseline_source, encoding="utf-8")
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.cpp")
        _run(project, "git", "commit", "-qm", "baseline")
        config = _cpp_constructor_config(project, target, baseline_source)
        baseline = capture_checkpoint_baseline(config)
        assert baseline["metrics"]["objectives"]["occurrence_count"] == 4, baseline
        assert [
            item["declaration_identity"]["owner_qualified_name"]
            for item in baseline["metrics"]["occurrence_contract"]
        ] == [
            "FirstPrinter",
            "SecondPrinter",
            "ThirdPrinter",
            "FourthPrinter",
        ], baseline

        target.write_text(
            _cpp_constructor_source(holder=True),
            encoding="utf-8",
        )
        checkpoint = prepare_checkpoint(config, "")
        current = checkpoint["current_metrics"]
        assert current["objectives"]["occurrence_count"] == 0, current
        assert current["target_patch_identity_ok"] is True, current
        assert current["target_patch_identity_failures"] == [], current
        assert current["target_missing"] is False, current
        assert current["continuity_ok"] is True, current
        assert current["continuity_occurrence_count"] == 1, current
        assert len(current["constructor_signature_reanchors"]) == 4, current
        assert checkpoint["delta"]["semantic_contract_preserved"] is True, checkpoint
        assert checkpoint["delta"]["metric_progress"] is True, checkpoint


def _check_cpp_constructor_reanchor_fail_closed_boundaries() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nonjava-data-clumps-constructor-reanchor-negative-"
    ) as raw:
        project = Path(raw)
        target = project / "targets.cpp"
        baseline_source = _cpp_constructor_source(holder=False)
        target.write_text(baseline_source, encoding="utf-8")
        _run(project, "git", "init", "-q")
        _run(project, "git", "config", "user.email", "self-check@example.invalid")
        _run(project, "git", "config", "user.name", "Self Check")
        _run(project, "git", "add", "targets.cpp")
        _run(project, "git", "commit", "-qm", "baseline")
        config = _cpp_constructor_config(project, target, baseline_source)
        capture_checkpoint_baseline(config)
        migrated = _cpp_constructor_source(holder=True)

        first = _cpp_constructor_definition("FirstPrinter", holder=True)
        target.write_text(
            migrated.replace(first, first + "\n" + first, 1),
            encoding="utf-8",
        )
        multiple = prepare_checkpoint(config, "")["current_metrics"]
        assert multiple["target_missing"] is True, multiple
        assert multiple["target_identity_collision"] is True, multiple

        target.write_text(
            migrated.replace(
                "FirstPrinter::FirstPrinter(PrinterContext const& ctx)",
                "OtherPrinter::FirstPrinter(PrinterContext const& ctx)",
                1,
            ),
            encoding="utf-8",
        )
        owner_changed = prepare_checkpoint(config, "")["current_metrics"]
        assert owner_changed["target_patch_identity_ok"] is False, owner_changed
        assert owner_changed["target_missing"] is True, owner_changed
        assert any(
            item.get("reason") == (
                "constructor_signature_reanchor_owner_or_name_changed"
            )
            for item in owner_changed["target_patch_identity_failures"]
        ), owner_changed

        baseline_first = _cpp_constructor_definition(
            "FirstPrinter",
            holder=False,
        )
        without_first = baseline_source.replace(
            baseline_first + "\n\n",
            "",
            1,
        )
        target.write_text(
            without_first
            + "\n"
            + "\n".join(f"// distant padding {index}" for index in range(24))
            + "\n"
            + first,
            encoding="utf-8",
        )
        cross_hunk = prepare_checkpoint(config, "")["current_metrics"]
        assert cross_hunk["target_patch_identity_ok"] is False, cross_hunk
        assert cross_hunk["target_missing"] is True, cross_hunk
        assert any(
            item.get("reason") == (
                "constructor_signature_reanchor_not_same_unique_hunk"
            )
            for item in cross_hunk["target_patch_identity_failures"]
        ), cross_hunk

    identity = ast_declaration_identity("Printer", "demo::Printer")
    patch = (
        "diff --git a/targets.cpp b/targets.cpp\n"
        "--- a/targets.cpp\n"
        "+++ b/targets.cpp\n"
        "@@ -1,3 +1,2 @@\n"
        "-Printer::Printer(Stream& s, Stats const& st, bool p) {}\n"
        "-Printer::Printer(Stream& s, Stats const& st, bool p, int mode) {}\n"
        "+Printer::Printer(PrinterContext const& ctx) {}\n"
        " int stable = 0;\n"
    )
    baseline_targets = [
        {
            "target_index": index,
            "file": "targets.cpp",
            "method": "Printer",
            "begin_line": index + 1,
            "declaration_identity": identity,
        }
        for index in range(2)
    ]
    current_targets = [
        {
            "target_index": index,
            "file": "targets.cpp",
            "method": "Printer",
            "resolved": True,
            "begin_line": 1,
            "declaration_identity": identity,
        }
        for index in range(2)
    ]
    collision = evaluate_data_clump_target_patch_identity(
        baseline_targets,
        current_targets,
        changed_patch=patch,
        language="cpp",
    )
    assert collision["ok"] is False, collision
    assert any(
        item.get("reason") == (
            "constructor_signature_reanchor_not_one_to_one"
        )
        for item in collision["failures"]
    ), collision


def main() -> int:
    _check_boundary_still_finding()
    _check_resolved_holder_route()
    _check_non_holder_and_target_hunk_scope()
    _check_inline_copy_expansion_and_fail_closed_patch()
    _check_c_family_continuity("c")
    _check_c_family_continuity("cpp")
    _check_unresolved_target_fails_closed()
    _check_parse_failure_fails_closed()
    _check_shared_non_unique_windows()
    _check_short_body_baseline_rejected()
    _check_empty_cpp_body_has_explicit_no_copy_witness()
    _check_old_contract_without_witness_fails_closed()
    _check_type_only_mutation_continuity()
    _check_one_for_one_helper_relocation()
    _check_added_helper_group_without_body_copy()
    _check_hunk_context_is_not_new_copy()
    _check_same_name_decoys_do_not_replace_frozen_target()
    _check_malformed_current_fails_closed("python")
    _check_malformed_current_fails_closed("c")
    _check_malformed_current_fails_closed("cpp")
    _check_target_external_parse_error_is_ignored()
    _check_ast_owner_change_fails_closed()
    _check_frozen_patch_anchor_beats_nearest_same_name()
    _check_invalid_ast_identity_baseline_rejected()
    _check_overload_target_identity_collision()
    _check_cpp_constructor_signature_reanchor()
    _check_cpp_constructor_reanchor_fail_closed_boundaries()
    print(
        "Non-Java Data Clumps checkpoint self-check passed: resolved, boundary, "
        "non-holder, target-only hunks, inline-copy rejection, fail-closed patch, "
        "C/C++ holder and rename continuity, unresolved/parse rejection"
        ", shared-window counts, short/legacy baseline rejection"
        ", explicit empty-body no-copy witness"
        ", type-only continuity, helper relocation/group rejection"
        ", context-line exclusion, same-name decoy rejection"
        ", Python/C/C++ malformed-file rejection"
        ", C++ target-external parse-error isolation"
        ", AST owner identity/baseline witness rejection"
        ", frozen patch anchor over nearest same-name decoy"
        ", overload collision rejection"
        ", same-hunk C++ constructor signature reanchor"
        ", constructor multi-candidate/cross-hunk/owner/bijection rejection"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
