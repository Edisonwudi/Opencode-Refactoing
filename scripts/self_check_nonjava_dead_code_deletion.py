#!/usr/bin/env python3
"""Regression checks for exact, target-local non-Java Dead Code deletion."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_adapters import (  # noqa: E402
    authorize_dead_code_target_absence,
)
from smell_core.checkpoint_contract import (  # noqa: E402
    checkpoint_gate_result,
    evaluate_checkpoint_contract,
)
from smell_core.checkpoints import (  # noqa: E402
    capture_checkpoint_baseline,
    checkpoint_location,
    finalize_checkpoint,
    prepare_checkpoint,
)
from smell_core.config import (  # noqa: E402
    load_refactor_config,
    resolve_run_config,
)
from smell_core.guards import run_build_test_guard, run_smell_guards  # noqa: E402
from smell_core.guards.context import GuardRunContext  # noqa: E402


@dataclass(frozen=True)
class Fixture:
    language: str
    filename: str
    baseline: str
    exact_deletion: str
    renamed_target: str
    parse_failure: str


FIXTURES = (
    Fixture(
        language="python",
        filename="fixture.py",
        baseline=(
            "def target():\n"
            "    value = 1\n"
            "    return value\n"
            "\n"
            "def keep():\n"
            "    return 2\n"
        ),
        exact_deletion="def keep():\n    return 2\n",
        renamed_target=(
            "def renamed():\n"
            "    value = 1\n"
            "    return value\n"
            "\n"
            "def keep():\n"
            "    return 2\n"
        ),
        parse_failure="def target(:\n    return 1\n",
    ),
    Fixture(
        language="c",
        filename="fixture.c",
        baseline=(
            "static int target(void) {\n"
            "    int value = 1;\n"
            "    return value;\n"
            "}\n"
            "\n"
            "int keep(void) {\n"
            "    return 2;\n"
            "}\n"
        ),
        exact_deletion="int keep(void) {\n    return 2;\n}\n",
        renamed_target=(
            "static int renamed(void) {\n"
            "    int value = 1;\n"
            "    return value;\n"
            "}\n"
            "\n"
            "int keep(void) {\n"
            "    return 2;\n"
            "}\n"
        ),
        parse_failure="static int target( {\n    return 1;\n}\n",
    ),
    Fixture(
        language="cpp",
        filename="fixture.cpp",
        baseline=(
            "static int target() {\n"
            "    int value = 1;\n"
            "    return value;\n"
            "}\n"
            "\n"
            "int keep() {\n"
            "    return 2;\n"
            "}\n"
        ),
        exact_deletion="int keep() {\n    return 2;\n}\n",
        renamed_target=(
            "static int renamed() {\n"
            "    int value = 1;\n"
            "    return value;\n"
            "}\n"
            "\n"
            "int keep() {\n"
            "    return 2;\n"
            "}\n"
        ),
        parse_failure="static int target( {\n    return 1;\n}\n",
    ),
)


def _git(project: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


def _forbidden_rglob(*_args: object, **_kwargs: object) -> Iterator[Path]:
    raise AssertionError("Dead Code Guard attempted project-wide Path.rglob discovery")


@contextmanager
def _bounded_guard(checkpoint_root: Path) -> Iterator[None]:
    with (
        patch.dict(
            os.environ,
            {"SMELL_CHECKPOINT_ROOT": str(checkpoint_root)},
            clear=False,
        ),
        patch.object(Path, "rglob", _forbidden_rglob),
    ):
        yield


def _create_case(
    workspace: Path,
    fixture: Fixture,
    case_name: str,
    *,
    verification_mode: str = "project_full",
    baseline_source: str | None = None,
):
    project = workspace / f"{fixture.language}-{case_name}"
    project.mkdir()
    source_path = project / fixture.filename
    source_path.write_text(
        fixture.baseline if baseline_source is None else baseline_source,
        encoding="utf-8",
    )
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "self-check@example.invalid")
    _git(project, "config", "user.name", "Dead Code Self Check")
    _git(project, "add", fixture.filename)
    _git(project, "commit", "-qm", "baseline")
    config = resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell="dead_code",
        location=f"{fixture.filename}:method=target|line=1",
        cli_language=fixture.language,
        verification_mode=verification_mode,
    )
    assert config.verification_mode == verification_mode, config.verification_mode
    return project, source_path, config, workspace / f"checkpoints-{fixture.language}-{case_name}"


def _capture_then_mutate(
    workspace: Path,
    fixture: Fixture,
    case_name: str,
    mutation: Callable[[Path], None],
    *,
    verification_mode: str = "project_full",
    baseline_source: str | None = None,
):
    project, source_path, config, checkpoint_root = _create_case(
        workspace,
        fixture,
        case_name,
        verification_mode=verification_mode,
        baseline_source=baseline_source,
    )
    with _bounded_guard(checkpoint_root):
        baseline = capture_checkpoint_baseline(config, "self-check baseline")
        mutation(source_path)
        checkpoint = prepare_checkpoint(config, "self-check current")
    assert checkpoint.get("required") is True, checkpoint
    return project, config, checkpoint_root, baseline, checkpoint


def _wrong_old_line_patch(fixture: Fixture, witness: dict[str, object]) -> str:
    start = int(witness["start_line"])
    end = int(witness["end_line"])
    old_lines = fixture.baseline.encode("utf-8").splitlines()[start - 1 : end]
    wrong_start = start + 20
    rendered = "\n".join(f"-{line.decode('utf-8')}" for line in old_lines)
    return (
        f"diff --git a/{fixture.filename} b/{fixture.filename}\n"
        f"--- a/{fixture.filename}\n"
        f"+++ b/{fixture.filename}\n"
        f"@@ -{wrong_start},{len(old_lines)} +{wrong_start},0 @@\n"
        f"{rendered}\n"
    )


def _check_exact_deletion(workspace: Path, fixture: Fixture) -> None:
    project, config, checkpoint_root, baseline, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "exact",
        lambda path: path.write_text(fixture.exact_deletion, encoding="utf-8"),
    )
    current = checkpoint["current_metrics"]
    absence = current.get("target_absence_evidence") or {}
    assert current.get("target_missing") is True, checkpoint
    assert current.get("target_absence_allowed") is True, checkpoint
    assert absence.get("allowed") is True, checkpoint
    assert absence.get("reason") == "DEAD_CODE_EXACT_TARGET_DELETED", checkpoint
    assert absence.get("target_file_exists") is True, checkpoint
    assert absence.get("target_file_parseable") is True, checkpoint
    assert checkpoint["delta"]["reason"] == "METRIC_PROGRESS", checkpoint
    assert checkpoint["delta"]["metric_progress"] is True, checkpoint
    assert checkpoint["production_diff"] is True, checkpoint
    assert checkpoint_gate_result("dead_code", checkpoint) is None, checkpoint

    # Structural success only unlocks the later behavior gate; prepare never
    # turns a checkpoint into an accepted result by itself.
    assert checkpoint["accepted"] is False, checkpoint
    assert checkpoint["best_checkpoint"] is False, checkpoint
    assert "build_test_success" not in checkpoint, checkpoint

    with _bounded_guard(checkpoint_root):
        unproven_guard = run_smell_guards(config)
        proven_guard = run_smell_guards(
            config,
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="dead_code",
                current_metrics=current,
                metric_delta=checkpoint["delta"],
                has_production_diff=True,
                metric_progress=True,
                checkpoint=checkpoint,
            ),
        )
    assert unproven_guard and all(
        outcome.get("success") is False for outcome in unproven_guard
    ), unproven_guard
    assert proven_guard and all(
        outcome.get("success") is True for outcome in proven_guard
    ), proven_guard
    assert any(
        (outcome.get("details") or {}).get("target_absence_allowed") is True
        for outcome in proven_guard
    ), proven_guard

    witness = baseline["metrics"].get("declaration_witness") or {}
    assert witness.get("schema_version") == 2, baseline
    assert witness.get("body_token_normalization") == "clone-normalized-tokens-v1", baseline
    assert int(witness.get("body_token_count") or 0) > 0, baseline
    assert len(str(witness.get("body_token_sha256") or "")) == 64, baseline
    with _bounded_guard(checkpoint_root):
        wrong_position = authorize_dead_code_target_absence(
            config,
            baseline["metrics"],
            current,
            production_patch=_wrong_old_line_patch(fixture, witness),
            changed_production_source_files=[fixture.filename],
        )
    wrong_evidence = wrong_position.get("target_absence_evidence") or {}
    assert wrong_position.get("target_absence_allowed") is False, wrong_position
    assert (
        wrong_evidence.get("reason")
        == "DEAD_CODE_EXACT_DELETION_EVIDENCE_MISSING"
    ), wrong_position
    assert wrong_evidence.get("missing_old_line") == witness.get("start_line"), wrong_position

    if fixture.language == "python":
        # Even a superficially PASS/resolved payload cannot bypass a failed
        # project_full build/test gate after structural deletion succeeds.
        with _bounded_guard(checkpoint_root):
            finalized = finalize_checkpoint(
                project,
                "dead_code",
                checkpoint_location(config),
                checkpoint["checkpoint_id"],
                {
                    "status": "PASS",
                    "resolution": "resolved",
                    "success": True,
                    "accepted": True,
                    "progress": True,
                    "smell_guard": {"success": True},
                    "build_test_guard": {"success": False},
                },
            )
        assert finalized is not None, finalized
        assert finalized["build_test_success"] is False, finalized
        assert finalized["accepted"] is False, finalized
        assert finalized["best_checkpoint"] is False, finalized
        assert finalized["restorable"] is False, finalized


def _check_exact_deletion_with_blank_addition(
    workspace: Path,
    fixture: Fixture,
) -> None:
    project, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "exact-with-blank",
        lambda path: path.write_text(
            "\n\n" + fixture.exact_deletion,
            encoding="utf-8",
        ),
    )
    diff = subprocess.run(
        ["git", "diff", "--", fixture.filename],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    assert "\n+\n" in diff, diff
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_missing") is True, checkpoint
    assert current.get("target_absence_allowed") is True, checkpoint
    assert evidence.get("reason") == "DEAD_CODE_EXACT_TARGET_DELETED", checkpoint
    assert checkpoint["delta"]["reason"] == "METRIC_PROGRESS", checkpoint


def _check_file_deletion(workspace: Path, fixture: Fixture) -> None:
    _, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "file-missing",
        lambda path: path.unlink(),
    )
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_file_exists") is False, checkpoint
    assert current.get("target_absence_allowed") is False, checkpoint
    assert evidence.get("reason") == "DEAD_CODE_TARGET_FILE_MISSING", checkpoint
    assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint


def _check_parse_failure(workspace: Path, fixture: Fixture) -> None:
    _, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "parse-failure",
        lambda path: path.write_text(fixture.parse_failure, encoding="utf-8"),
    )
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_file_exists") is True, checkpoint
    assert current.get("target_file_parseable") is False, checkpoint
    assert current.get("target_absence_allowed") is False, checkpoint
    if fixture.language == "python":
        assert current.get("ok") is False, checkpoint
        assert evidence.get("reason") == "DEAD_CODE_CURRENT_TARGET_UNAVAILABLE", checkpoint
        assert checkpoint["delta"]["reason"] == "CURRENT_DETECTOR_UNAVAILABLE", checkpoint
    else:
        assert current.get("ok") is True, checkpoint
        assert evidence.get("reason") == (
            "DEAD_CODE_TARGET_FILE_SYNTAX_REGRESSION"
        ), checkpoint
        assert checkpoint["delta"]["semantic_contract"]["regressions"] == [
            "TARGET_SYNTAX_RECOVERY_REGRESSION"
        ], checkpoint
        assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint


def _check_frozen_parser_recovery(workspace: Path, fixture: Fixture) -> None:
    if fixture.language not in {"c", "cpp"}:
        return
    macro_tail = (
        "\n#define UNUSED(value) value\n"
        "int macro_keep(int UNUSED(*value)) { return *value; }\n"
    )
    baseline_source = fixture.baseline + macro_tail
    current_source = fixture.exact_deletion + macro_tail
    _, _, _, baseline, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "frozen-parser-recovery",
        lambda path: path.write_text(current_source, encoding="utf-8"),
        baseline_source=baseline_source,
    )
    assert baseline["metrics"]["parser_recovery_required"] is True, baseline
    assert baseline["metrics"]["target_syntax_issue_witnesses"], baseline
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current["target_file_parseable"] is False, checkpoint
    assert current["target_absence_allowed"] is True, checkpoint
    assert evidence["reason"] == "DEAD_CODE_EXACT_TARGET_DELETED", checkpoint
    assert checkpoint["delta"]["reason"] == "METRIC_PROGRESS", checkpoint

    _, _, _, _, regressed = _capture_then_mutate(
        workspace,
        fixture,
        "new-parser-error",
        lambda path: path.write_text(
            current_source + "\nint newly_broken( {\n",
            encoding="utf-8",
        ),
        baseline_source=baseline_source,
    )
    regressed_current = regressed["current_metrics"]
    regressed_evidence = regressed_current.get("target_absence_evidence") or {}
    assert regressed_current["target_absence_allowed"] is False, regressed
    assert regressed_evidence["reason"] == (
        "DEAD_CODE_TARGET_FILE_SYNTAX_REGRESSION"
    ), regressed
    assert regressed["delta"]["reason"] == "TARGET_NOT_LOCATED", regressed
    assert regressed["delta"]["semantic_contract"]["regressions"] == [
        "TARGET_SYNTAX_RECOVERY_REGRESSION"
    ], regressed


def _check_partial_rename(workspace: Path, fixture: Fixture) -> None:
    _, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "partial-rename",
        lambda path: path.write_text(fixture.renamed_target, encoding="utf-8"),
    )
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_missing") is True, checkpoint
    assert current.get("target_file_parseable") is True, checkpoint
    assert current.get("target_absence_allowed") is False, checkpoint
    assert evidence.get("reason") == "DEAD_CODE_REPLACEMENT_IS_NOT_DELETION", checkpoint
    assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint


def _check_separate_hunk_rename(workspace: Path, fixture: Fixture) -> None:
    comment = "#" if fixture.language == "python" else "//"
    stable_context = "\n" + "".join(
        f"{comment} stable relocation separator {index}\n"
        for index in range(16)
    )
    keep_marker = "\ndef keep" if fixture.language == "python" else "\nint keep"
    renamed_only = fixture.renamed_target.split(keep_marker, 1)[0].rstrip() + "\n"
    baseline_source = fixture.baseline + stable_context
    current_source = fixture.exact_deletion + stable_context + "\n" + renamed_only
    project, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "separate-hunk-rename",
        lambda path: path.write_text(current_source, encoding="utf-8"),
        baseline_source=baseline_source,
    )
    patch_text = subprocess.run(
        ["git", "diff", "HEAD", "--", fixture.filename],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    assert sum(line.startswith("@@") for line in patch_text.splitlines()) >= 2, patch_text
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_missing") is True, checkpoint
    assert current.get("target_file_parseable") is True, checkpoint
    assert current.get("target_absence_allowed") is False, checkpoint
    assert evidence.get("reason") == "DEAD_CODE_RELOCATION_NOT_DELETION", checkpoint
    assert int(evidence.get("relocation_added_block") or 0) >= 1, checkpoint
    assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint


def _check_cross_hunk_function_relocation(
    workspace: Path,
    fixture: Fixture,
) -> None:
    keep_marker = "\ndef keep" if fixture.language == "python" else "\nint keep"
    target_only = fixture.baseline.split(keep_marker, 1)[0].rstrip() + "\n\n"
    if fixture.language == "python":
        stable_context = "".join(
            f"    # stable function separator {index}\n"
            for index in range(16)
        )
        baseline_keep = "def keep():\n" + stable_context + "    pass\n"
        relocated_keep = (
            "def keep():\n"
            "    value = 1\n"
            + stable_context
            + "    return value\n"
        )
    else:
        stable_context = "".join(
            f"    // stable function separator {index}\n"
            for index in range(16)
        )
        baseline_keep = (
            "int keep(void) {\n" + stable_context + "    return 0;\n}\n"
        )
        relocated_keep = (
            "int keep(void) {\n"
            "    int value = 1;\n"
            + stable_context
            + "    return value;\n}\n"
        )
    project, _, _, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "cross-hunk-function-relocation",
        lambda path: path.write_text(relocated_keep, encoding="utf-8"),
        baseline_source=target_only + baseline_keep,
    )
    patch_text = subprocess.run(
        ["git", "diff", "HEAD", "--", fixture.filename],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    assert sum(line.startswith("@@") for line in patch_text.splitlines()) >= 2, patch_text
    current = checkpoint["current_metrics"]
    evidence = current.get("target_absence_evidence") or {}
    assert current.get("target_missing") is True, checkpoint
    assert current.get("target_absence_allowed") is False, checkpoint
    assert evidence.get("reason") == "DEAD_CODE_RELOCATION_NOT_DELETION", checkpoint
    assert int(evidence.get("relocation_function_start_line") or 0) >= 1, checkpoint
    assert int(evidence.get("relocation_added_line") or 0) >= 1, checkpoint
    assert "relocation_added_block" not in evidence, checkpoint
    assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint


def _check_sample_optimized_rejected(workspace: Path, fixture: Fixture) -> None:
    project, config, checkpoint_root, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "sample-optimized",
        lambda path: path.write_text(fixture.exact_deletion, encoding="utf-8"),
        verification_mode="sample_optimized",
    )
    current = checkpoint["current_metrics"]
    absence = current.get("target_absence_evidence") or {}
    assert current["target_missing"] is True, checkpoint
    assert current["target_absence_allowed"] is False, checkpoint
    assert absence.get("allowed") is False, checkpoint
    assert absence.get("reason") == "DEAD_CODE_PROJECT_FULL_REQUIRED", checkpoint
    assert checkpoint["verification_mode"] == "sample_optimized", checkpoint
    assert checkpoint["delta"]["reason"] == "TARGET_NOT_LOCATED", checkpoint
    assert checkpoint["delta"]["metric_progress"] is False, checkpoint
    assert checkpoint_gate_result("dead_code", checkpoint) is not None, checkpoint
    with _bounded_guard(checkpoint_root):
        guard_results = run_smell_guards(
            config,
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="dead_code",
                current_metrics=current,
                metric_delta=checkpoint["delta"],
                has_production_diff=True,
                metric_progress=False,
                checkpoint=checkpoint,
            ),
        )
    assert guard_results and any(
        outcome.get("success") is False for outcome in guard_results
    ), guard_results
    with _bounded_guard(checkpoint_root):
        finalized = finalize_checkpoint(
            project,
            "dead_code",
            checkpoint_location(config),
            checkpoint["checkpoint_id"],
            {
                "status": "PASS",
                "resolution": "resolved",
                "success": True,
                "accepted": True,
                "progress": True,
                "smell_guard": {"success": True},
                "build_test_guard": {"success": True},
            },
        )
    assert finalized is not None, finalized
    assert finalized["build_test_success"] is True, finalized
    assert finalized["accepted"] is False, finalized
    assert finalized["best_checkpoint"] is False, finalized
    assert finalized["restorable"] is False, finalized


def _check_bridge_skip_build_required(workspace: Path, fixture: Fixture) -> None:
    project, _, _, artifact_root = _create_case(
        workspace,
        fixture,
        "bridge-skip-build",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(BRIDGE),
            "verify",
            "--project-root",
            str(project),
            "--language",
            fixture.language,
            "--smell",
            "dead_code",
            "--location",
            f"{fixture.filename}:method=target|line=1",
            "--verification-mode",
            "project_full",
            "--skip-build-test",
            "--artifact-root",
            str(artifact_root),
            "--output-detail",
            "audit",
        ],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "BUILD_TEST_REQUIRED", payload
    assert payload["success"] is False, payload
    assert payload["accepted"] is False, payload


def _check_project_full_fresh_test_required(
    workspace: Path,
    fixture: Fixture,
) -> None:
    project, config, checkpoint_root, _, checkpoint = _capture_then_mutate(
        workspace,
        fixture,
        "project-full-fresh-test",
        lambda path: path.write_text(fixture.exact_deletion, encoding="utf-8"),
    )
    assert checkpoint["current_metrics"]["target_absence_allowed"] is True, checkpoint
    tests_disabled = replace(
        config,
        defaults=replace(config.defaults, run_tests=False),
    )
    disabled_result = run_build_test_guard(
        tests_disabled,
        require_test_execution=True,
    )
    assert disabled_result["success"] is False, disabled_result
    assert disabled_result["reason"] == "TEST_EXECUTION_DISABLED", disabled_result
    assert disabled_result["details"]["test"]["status"] == "test_not_executed", disabled_result
    result = subprocess.run(
        [
            sys.executable,
            str(BRIDGE),
            "verify",
            "--project-root",
            str(project),
            "--language",
            fixture.language,
            "--smell",
            "dead_code",
            "--location",
            f"{fixture.filename}:method=target|line=1",
            "--verification-mode",
            "project_full",
            "--no-snapshot",
            "--artifact-root",
            str(workspace / f"bridge-artifacts-{fixture.language}"),
            "--output-detail",
            "audit",
        ],
        cwd=project,
        env={**os.environ, "SMELL_CHECKPOINT_ROOT": str(checkpoint_root)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "TEST_EVIDENCE_MISSING", payload
    assert payload["smell_guard"]["success"] is True, payload
    assert payload["build_test_guard"]["success"] is False, payload
    test_result = payload["build_test_guard"]["details"]["test"]
    assert test_result["status"] == "test_not_executed", payload
    assert payload["failure_pack"]["failure_category"] == "TEST_EVIDENCE_MISSING", payload
    assert payload["failure_pack"]["retryable"] is False, payload
    full_test_result = json.loads(
        Path(payload["artifacts"]["test_result"]).read_text(encoding="utf-8")
    )
    assert full_test_result["execution_evidence"]["success"] is False, payload
    assert payload["success"] is False, payload
    assert payload["accepted"] is False, payload
    assert payload["checkpoint"]["accepted"] is False, payload


def _check_contract_ordering() -> None:
    evaluation = evaluate_checkpoint_contract(
        {"ok": True, "objectives": {"target_declaration_present": 1}},
        {
            "ok": True,
            "objectives": {"target_declaration_present": 0},
            "candidate_count": 0,
            "target_missing": True,
            "target_absence_allowed": True,
        },
        has_production_diff=False,
        smell="dead_code",
        changed_production_source_files=(),
    )
    assert evaluation.reason == "EDIT_REQUIRED", evaluation
    assert evaluation.metric_progress is False, evaluation


def main() -> int:
    _check_contract_ordering()
    with tempfile.TemporaryDirectory(prefix="nonjava-dead-code-deletion-") as raw:
        workspace = Path(raw)
        for fixture in FIXTURES:
            _check_exact_deletion(workspace, fixture)
            _check_exact_deletion_with_blank_addition(workspace, fixture)
            _check_file_deletion(workspace, fixture)
            _check_parse_failure(workspace, fixture)
            _check_frozen_parser_recovery(workspace, fixture)
            _check_partial_rename(workspace, fixture)
            _check_separate_hunk_rename(workspace, fixture)
            _check_cross_hunk_function_relocation(workspace, fixture)
            _check_sample_optimized_rejected(workspace, fixture)
            _check_bridge_skip_build_required(workspace, fixture)
            _check_project_full_fresh_test_required(workspace, fixture)
    print(
        "Non-Java Dead Code deletion self-check passed: "
        "python/c/cpp exact deletion accepted structurally; "
        "missing file, parse failure, adjacent/separate/cross-hunk relocation, wrong old position, "
        "failed final build gate, sample_optimized authorization, and skipped "
        "project_full build/fresh-test execution (disabled or missing command) rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
