#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.guards import _sample_test_execution_evidence  # noqa: E402


def _write_report(root: Path, class_name: str, tests: int, skipped: int = 0) -> None:
    report = root / "target" / "surefire-reports" / f"TEST-example.{class_name}.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f'<testsuite name="example.{class_name}" tests="{tests}" '
        f'failures="0" errors="0" skipped="{skipped}"></testsuite>\n',
        encoding="utf-8",
    )


def _command_result(command: str, output: str) -> dict[str, object]:
    return {
        "command": command,
        "script": "",
        "output": output,
        "returncode": 0,
        "success": True,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sample-test-evidence-self-check-") as tmp:
        root = Path(tmp)
        started_ns = time.time_ns()
        _write_report(root, "FirstBehaviorTest", 2)
        _write_report(root, "SecondBehaviorTest", 3, skipped=1)

        config = SimpleNamespace(
            project_root=root,
            sample_test_location=(
                "src/test/java/example/FirstBehaviorTest.java;"
                "src/test/java/example/SecondBehaviorTest.java"
            ),
        )
        evidence = _sample_test_execution_evidence(config, started_ns)
        assert evidence["success"] is True, evidence
        assert evidence["test_classes"] == [
            "FirstBehaviorTest",
            "SecondBehaviorTest",
        ]
        assert evidence["tests"] == 4
        assert evidence["skipped"] == 1
        assert all(item["success"] for item in evidence["classes"])
        print("  ok   every declared test class has fresh evidence")

        missing_config = SimpleNamespace(
            project_root=root,
            sample_test_location=(
                "src/test/java/example/FirstBehaviorTest.java;"
                "src/test/java/example/MissingBehaviorTest.java"
            ),
        )
        missing = _sample_test_execution_evidence(missing_config, started_ns)
        assert missing["success"] is True, missing
        assert missing["missing_test_classes"] == ["MissingBehaviorTest"]
        assert missing["executed_test_classes"] == ["FirstBehaviorTest"]
        print("  ok   support file without a report is recorded")

        all_missing_config = SimpleNamespace(
            project_root=root,
            sample_test_location=(
                "src/test/java/example/MissingBehaviorTest.java;"
                "src/test/java/example/OtherMissingBehaviorTest.java"
            ),
        )
        all_missing = _sample_test_execution_evidence(all_missing_config, started_ns)
        assert all_missing["success"] is False, all_missing
        assert all_missing["executed_test_classes"] == []
        assert all_missing["missing_test_classes"] == [
            "MissingBehaviorTest",
            "OtherMissingBehaviorTest",
        ]
        print("  ok   no declared class executed fails closed")

        skipped_started_ns = time.time_ns()
        _write_report(root, "SkippedBehaviorTest", 1, skipped=1)
        skipped_config = SimpleNamespace(
            project_root=root,
            sample_test_location="src/test/java/example/SkippedBehaviorTest.java",
        )
        skipped = _sample_test_execution_evidence(skipped_config, skipped_started_ns)
        assert skipped["success"] is False, skipped
        assert skipped["missing_test_classes"] == ["SkippedBehaviorTest"]
        print("  ok   skipped-only report fails closed")

        nested_started_ns = time.time_ns()
        _write_report(root, "NestedBehaviorTest$Case", 3)
        nested_config = SimpleNamespace(
            project_root=root,
            sample_test_location="src/test/java/example/NestedBehaviorTest.java",
        )
        nested = _sample_test_execution_evidence(nested_config, nested_started_ns)
        assert nested["success"] is True, nested
        assert nested["tests"] == 3
        assert nested["classes"][0]["evidence_mode"] == "xml"
        print("  ok   nested test suites count as execution of the declared class")

        console_config = SimpleNamespace(
            project_root=root,
            sample_test_location="src/test/java/example/ConsoleBehaviorTest.java",
        )
        console = _sample_test_execution_evidence(
            console_config,
            time.time_ns(),
            _command_result(
                "java -cp target/test-classes org.junit.runner.JUnitCore "
                "example.ConsoleBehaviorTest",
                "JUnit version 4.13.2\n....\nOK (4 tests)\n",
            ),
        )
        assert console["success"] is True, console
        assert console["tests"] == 4
        assert console["classes"][0]["evidence_mode"] == "junit_console"
        print("  ok   explicit JUnitCore output is accepted without XML")

        runner_console = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=root,
                sample_test_location="src/test/java/example/RunnerBehaviorTest.java",
            ),
            time.time_ns(),
            _command_result(
                "mvn -Dtest=RunnerBehaviorTest test",
                "Tests run: 5, Failures: 0, Errors: 0, Skipped: 0, "
                "Time elapsed: 0.01 s -- in "
                "example.RunnerBehaviorTest$NestedCase\nBUILD SUCCESS\n",
            ),
        )
        assert runner_console["success"] is True, runner_console
        assert runner_console["tests"] == 5
        assert (
            runner_console["classes"][0]["evidence_mode"]
            == "test_runner_console"
        )
        print("  ok   nested Maven console suites count as declared execution")

        main_class = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=root,
                sample_test_location="src/test/java/example/MainBehaviorTest.java",
            ),
            time.time_ns(),
            _command_result(
                "java -cp target/test-classes example.MainBehaviorTest",
                "",
            ),
        )
        assert main_class["success"] is True, main_class
        assert main_class["classes"][0]["evidence_mode"] == "java_main_exit_zero"
        print("  ok   an explicitly invoked test main class is accepted")

        unrelated = _sample_test_execution_evidence(
            console_config,
            time.time_ns(),
            _command_result(
                "mvn -Dtest=OtherBehaviorTest test",
                "Tests run: 1, Failures: 0\nBUILD SUCCESS\n",
            ),
        )
        assert unrelated["success"] is False, unrelated
        print("  ok   unrelated successful tests still fail closed")

    print("sample test evidence self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
