#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as runner  # noqa: E402
from smell_core.config import CommandConfig, _rebase_command_config  # noqa: E402
from smell_core.guards import _sample_test_execution_evidence  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="multilanguage-runner-") as raw:
        root = Path(raw)
        project = root / "project"
        project.mkdir()
        source = project / "demo.py"
        source.write_text("def f():\n    return 1\n", encoding="utf-8")
        dataset = root / "python.csv"
        with dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["sample_id", "language", "smell_type", "project_name", "project_path", "location"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "sample_id": "1",
                    "language": "python",
                    "smell_type": "long_method",
                    "project_name": "demo",
                    "project_path": str(project),
                    "location": f"{source}:method=f|line=1",
                }
            )

        sample = runner._load_samples(dataset)[0]
        assert (
            runner._effective_verification_mode(
                sample,
                argparse.Namespace(verification_mode="project_full"),
            )
            == "project_full"
        )

        feature_envy_dataset = root / "python-feature-envy.csv"
        feature_envy_fields = [
            "sample_id",
            "language",
            "smell_type",
            "project_name",
            "project_path",
            "location",
            "target_context_json",
        ]
        feature_envy_row = {
            "sample_id": "fe-1",
            "language": "python",
            "smell_type": "feature_envy",
            "project_name": "demo",
            "project_path": str(project),
            "location": f"{source}:method=f|line=1",
            "target_context_json": "",
        }
        with feature_envy_dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=feature_envy_fields,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerow(feature_envy_row)
        try:
            runner._load_samples(feature_envy_dataset)
        except ValueError as exc:
            assert "non-Java feature_envy rows require" in str(exc), exc
        else:
            raise AssertionError(
                "non-Java feature_envy rows without receiver context must fail closed"
            )
        feature_envy_row["target_context_json"] = json.dumps(
            {"receiver_type": "order"},
            separators=(",", ":"),
        )
        with feature_envy_dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=feature_envy_fields,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerow(feature_envy_row)
        feature_envy_sample = runner._load_samples(feature_envy_dataset)[0]
        assert feature_envy_sample.target_context == {"receiver_type": "order"}
        print("  ok   non-Java feature_envy requires explicit receiver context")

        focused_dataset = root / "focused.csv"
        with focused_dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "sample_id",
                    "language",
                    "smell_type",
                    "project_name",
                    "project_path",
                    "location",
                    "test_file",
                    "test_command",
                    "verification_mode",
                    "build_command",
                    "project_test_command",
                    "verification_cwd",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "sample_id": "2",
                    "language": "java",
                    "smell_type": "feature_envy",
                    "project_name": "demo",
                    "project_path": str(project),
                    "location": f"{source}:method=f|line=1",
                    "test_file": "FocusedTest.java",
                    "test_command": "mvn -Dtest=FocusedTest test",
                    "verification_mode": "sample_optimized",
                    "build_command": "./mvnw -q -DskipTests package",
                    "project_test_command": "./mvnw -q test",
                    "verification_cwd": ".",
                }
            )
        focused_sample = runner._load_samples(focused_dataset)[0]
        assert focused_sample.test_command == "mvn -Dtest=FocusedTest test"
        assert focused_sample.test_location == "FocusedTest.java"
        assert focused_sample.raw["test_command"] == "mvn -Dtest=FocusedTest test"
        assert focused_sample.build_command == "./mvnw -q -DskipTests package"
        assert focused_sample.project_test_command == "./mvnw -q test"
        assert focused_sample.verification_cwd == "."
        assert focused_sample.verification_command_source == "dataset"
        print("  ok   sample_optimized uses materialized test_command and test_file")

        no_cli_spec = argparse.Namespace(
            build_command=None,
            project_test_command=None,
            verification_cwd=None,
        )
        resolved_dataset_spec = runner._resolve_verification_command_specs(
            [focused_sample], no_cli_spec
        )[0]
        assert resolved_dataset_spec.verification_command_source == "dataset"
        matching_cli_spec = argparse.Namespace(
            build_command=focused_sample.build_command,
            project_test_command=focused_sample.project_test_command,
            verification_cwd="",
        )
        resolved_cli_spec = runner._resolve_verification_command_specs(
            [focused_sample], matching_cli_spec
        )[0]
        assert resolved_cli_spec.verification_command_source == "cli"
        assert resolved_cli_spec.verification_cwd == "."
        bridge_args: list[str] = []
        runner._append_verification_command_args(bridge_args, resolved_cli_spec)
        assert bridge_args[bridge_args.index("--build-command") + 1] == (
            focused_sample.build_command
        )
        assert bridge_args[bridge_args.index("--project-test-command") + 1] == (
            focused_sample.project_test_command
        )
        assert bridge_args[bridge_args.index("--verification-cwd") + 1] == "."
        assert bridge_args[
            bridge_args.index("--verification-command-source") + 1
        ] == "cli"
        assert bridge_args[bridge_args.index("--sample-test-source") + 1] == (
            "dataset"
        )
        try:
            runner._resolve_verification_command_specs(
                [focused_sample],
                argparse.Namespace(
                    build_command="false",
                    project_test_command="false",
                    verification_cwd=".",
                ),
            )
        except ValueError as exc:
            assert "VERIFICATION_COMMAND_SOURCE_CONFLICT" in str(exc), exc
        else:
            raise AssertionError("CLI and dataset project specs must not silently override")
        try:
            runner._resolve_verification_command_specs(
                [focused_sample],
                argparse.Namespace(
                    build_command="true",
                    project_test_command=None,
                    verification_cwd=None,
                ),
            )
        except ValueError as exc:
            assert "CLI_VERIFICATION_COMMAND_PAIR_REQUIRED" in str(exc), exc
        else:
            raise AssertionError("partial CLI project verification spec must fail")
        conflicting_row = replace(
            focused_sample,
            sample_id="3",
            project_test_command="./mvnw -q verify",
            raw={**focused_sample.raw, "test_commit": "different-test-provenance"},
        )
        try:
            runner._resolve_verification_command_specs(
                [focused_sample, conflicting_row], no_cli_spec
            )
        except ValueError as exc:
            assert "PROJECT_VERIFICATION_SPEC_CONFLICT" in str(exc), exc
        else:
            raise AssertionError("one project revision must have one project spec")

        partial_dataset = root / "partial-verification.csv"
        with partial_dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "sample_id",
                    "language",
                    "smell_type",
                    "project_name",
                    "project_path",
                    "location",
                    "verification_cwd",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "sample_id": "partial",
                    "language": "python",
                    "smell_type": "long_method",
                    "project_name": "demo",
                    "project_path": str(project),
                    "location": f"{source}:method=f|line=1",
                    "verification_cwd": ".",
                }
            )
        try:
            runner._load_samples(partial_dataset)
        except ValueError as exc:
            assert "DATASET_VERIFICATION_COMMAND_PAIR_REQUIRED" in str(exc), exc
        else:
            raise AssertionError("dataset cwd without a command pair must fail")
        auth_args = SimpleNamespace(
            dry_run=False,
            checkout_only=False,
            model="minimax/MiniMax-M2.7",
            opencode_api_key="",
            opencode_api_key_env="README_SMOKE_MISSING_KEY",
            opencode_auth_json="disabled",
        )
        try:
            runner._validate_model_auth(auth_args)
        except ValueError as exc:
            assert "MODEL_AUTH_MISSING" in str(exc)
        else:
            raise AssertionError("missing model auth must fail before a run starts")
        auth_args.dry_run = True
        runner._validate_model_auth(auth_args)
        print("  ok   missing model auth fails before run artifacts are created")
        strict_oracle = replace(
            sample,
            language="java",
            smell="refused_bequest",
            evidence="parents=Parent; structural_expectation=capability_split",
            test_location="src/test/java/ExampleTest.java",
            test_command="mvn test",
            verification_mode="sample_optimized",
        )
        assert (
            runner._effective_verification_mode(
                strict_oracle,
                argparse.Namespace(verification_mode="project_full"),
            )
            == "project_full"
        )
        project_full_command = replace(
            sample,
            language="java",
            test_command="mvn test",
        )
        assert (
            runner._effective_verification_mode(
                project_full_command,
                argparse.Namespace(verification_mode="project_full"),
            )
            == "project_full"
        )
        invalid_sample_oracle = replace(
            project_full_command,
            verification_mode="sample_optimized",
        )
        try:
            runner._effective_verification_mode(
                invalid_sample_oracle,
                argparse.Namespace(verification_mode=None),
            )
        except ValueError as exc:
            assert "SAMPLE_ORACLE_TEST_FILE_MISSING" in str(exc)
        else:
            raise AssertionError("sample_optimized verification must require a pinned test file")
        missing_strict_test = replace(strict_oracle, test_location="")
        try:
            runner._effective_verification_mode(
                missing_strict_test,
                argparse.Namespace(verification_mode=None),
            )
        except ValueError as exc:
            assert "SAMPLE_ORACLE_TEST_FILE_MISSING" in str(exc)
        else:
            raise AssertionError("materialized sample_optimized mode must fail closed")
        try:
            runner._effective_verification_mode(
                strict_oracle,
                argparse.Namespace(verification_mode="local"),
            )
        except ValueError as exc:
            assert "Unsupported verification mode" in str(exc)
        else:
            raise AssertionError("legacy local mode must be rejected")
        assert runner.build_parser().parse_args(
            ["--dataset", str(dataset)]
        ).verification_mode is None
        prompt = runner._task_prompt(sample)
        assert "Repair this one python smell" in prompt
        assert "Repair this one Java smell" not in prompt
        assert "IDEA preference" not in prompt

        args = argparse.Namespace(agent="", opencode_bin="opencode", model="test/model")
        assert runner._select_agent(sample, args) == "smell-refactor-agent"
        assert runner._select_agent(focused_sample, args) == "java-refactor-agent"
        command = runner._opencode_run_command(args, "smell-refactor-agent")
        assert command[command.index("--command") + 1] == "smell-refactor-run"
        rebased = _rebase_command_config(
            CommandConfig(script=f'cd "{project}"\npython -m compileall demo.py'),
            project,
            root / "execution-worktree",
        )
        assert str(root / "execution-worktree") in str(rebased.script)
        assert str(project) not in str(rebased.script)

        for removed_args in (
            ["--idea"],
            ["--no-idea"],
            ["--idea-refactor-cli", "/tmp/idea-refactor"],
            ["--agent", "java-refactor-agent-idea"],
        ):
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "run_smell_dataset.py"),
                    "--dataset",
                    str(dataset),
                    *removed_args,
                    "--dry-run",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert rejected.returncode == 2, rejected
            assert "unrecognized arguments" in rejected.stderr or "invalid choice" in rejected.stderr

        direct = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_smell_dataset.py"), "--dataset", str(dataset), "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert direct.returncode == 0, direct.stderr
        assert "python" in direct.stdout
        assert '"verification_mode": "project_full"' in direct.stdout
        assert '"verification_modes": [' in direct.stdout

        forced_project_full = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_smell_dataset.py"),
                "--dataset",
                str(dataset),
                "--allow-test-changes",
                "--dry-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert forced_project_full.returncode == 0, forced_project_full.stderr
        assert '"verification_mode_override": null' in forced_project_full.stdout
        assert (
            '"verification_mode_forced_by_test_changes": true'
            in forced_project_full.stdout
        )

        direct_cli_spec = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_smell_dataset.py"),
                "--dataset",
                str(dataset),
                "--build-command",
                "python -m compileall .",
                "--project-test-command",
                "python -m unittest",
                "--verification-cwd",
                ".",
                "--dry-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert direct_cli_spec.returncode == 0, direct_cli_spec.stderr
        assert '"source": "cli"' in direct_cli_spec.stdout
        partial_cli_spec = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_smell_dataset.py"),
                "--dataset",
                str(dataset),
                "--build-command",
                "true",
                "--dry-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert partial_cli_spec.returncode == 2, partial_cli_spec
        assert "CLI_VERIFICATION_COMMAND_PAIR_REQUIRED" in partial_cli_spec.stderr

        report_dir = project / "build" / "test-results" / "test"
        report_dir.mkdir(parents=True)
        started_ns = time.time_ns()
        report = report_dir / "TEST-example.PinnedBehaviorTest.xml"
        report.write_text(
            '<testsuite name="example.PinnedBehaviorTest" tests="2" failures="0"/>',
            encoding="utf-8",
        )
        execution = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=project,
                sample_test_location="src/test/java/example/PinnedBehaviorTest.java",
            ),
            started_ns,
        )
        assert execution["success"] is True
        assert execution["tests"] == 2
        missing_execution = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=project,
                sample_test_location="src/test/java/example/MissingBehaviorTest.java",
            ),
            started_ns,
        )
        assert missing_execution["success"] is False
        skipped_report = report_dir / "TEST-example.SkippedBehaviorTest.xml"
        skipped_report.write_text(
            '<testsuite name="example.SkippedBehaviorTest" tests="1" skipped="1"/>',
            encoding="utf-8",
        )
        skipped_execution = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=project,
                sample_test_location="src/test/java/example/SkippedBehaviorTest.java",
            ),
            started_ns,
        )
        assert skipped_execution["success"] is False

    print("Multilanguage runner self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
