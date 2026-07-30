#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
                argparse.Namespace(verification_mode="auto"),
            )
            == "project_full"
        )
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
                    "focused_test_command",
                    "verification_mode",
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
                    "test_command": "mvn test",
                    "focused_test_command": "mvn -Dtest=FocusedTest test",
                    "verification_mode": "sample_optimized",
                }
            )
        focused_sample = runner._load_samples(focused_dataset)[0]
        assert focused_sample.test_command == "mvn -Dtest=FocusedTest test"
        assert focused_sample.raw["test_command"] == "mvn test"
        print("  ok   sample_optimized selects focused_test_command")
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
        )
        assert (
            runner._effective_verification_mode(
                strict_oracle,
                argparse.Namespace(verification_mode="auto"),
            )
            == "sample_optimized"
        )
        project_full_command = replace(
            sample,
            language="java",
            test_command="mvn test",
        )
        assert (
            runner._effective_verification_mode(
                project_full_command,
                argparse.Namespace(verification_mode="auto"),
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
                argparse.Namespace(verification_mode="auto"),
            )
        except ValueError as exc:
            assert "SAMPLE_ORACLE_TEST_FILE_MISSING" in str(exc)
        else:
            raise AssertionError("sample_optimized verification must require a pinned test file")
        missing_strict_test = replace(strict_oracle, test_location="")
        try:
            runner._effective_verification_mode(
                missing_strict_test,
                argparse.Namespace(verification_mode="auto"),
            )
        except ValueError as exc:
            assert "STRICT_ORACLE_TEST_FILE_MISSING" in str(exc)
        else:
            raise AssertionError("strict Refused Bequest Oracle must require a pinned test file")
        try:
            runner._effective_verification_mode(
                strict_oracle,
                argparse.Namespace(verification_mode="local"),
            )
        except ValueError as exc:
            assert "STRICT_ORACLE_LOCAL_FORBIDDEN" in str(exc)
        else:
            raise AssertionError("strict Refused Bequest Oracle must reject local verification")
        prompt = runner._task_prompt(
            sample,
            argparse.Namespace(idea_refactor_cli=""),
            "local",
            "java-refactor-agent",
        )
        assert "Repair this one python smell" in prompt
        assert "Repair this one Java smell" not in prompt

        args = argparse.Namespace(agent="", idea=False, opencode_bin="opencode", model="test/model")
        assert runner._select_agent(sample, args) == "smell-refactor-agent"
        command = runner._opencode_run_command(args, "smell-refactor-agent")
        assert command[command.index("--command") + 1] == "smell-refactor-run"
        rebased = _rebase_command_config(
            CommandConfig(script=f'cd "{project}"\npython -m compileall demo.py'),
            project,
            root / "execution-worktree",
        )
        assert str(root / "execution-worktree") in str(rebased.script)
        assert str(project) not in str(rebased.script)

        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_smell_dataset.py"), "--dataset", str(dataset), "--idea", "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 2, proc
        assert "IDEA_UNSUPPORTED_LANGUAGE" in proc.stderr

        direct = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_smell_dataset.py"), "--dataset", str(dataset), "--no-idea", "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert direct.returncode == 0, direct.stderr
        assert "python" in direct.stdout

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
