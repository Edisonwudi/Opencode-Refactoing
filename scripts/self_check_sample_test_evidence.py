#!/usr/bin/env python3
from __future__ import annotations

import csv
from collections import Counter
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.guards import (  # noqa: E402
    _force_fresh_gradle_test_execution,
    _project_test_execution_evidence,
    _run_command_config,
    _sample_test_execution_evidence,
    run_build_test_guard,
)
from smell_core.config import CommandConfig  # noqa: E402


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


def _check_delivery_dataset_contracts() -> None:
    """Keep every Java row on one explicit, fallback-free verification path."""
    checked = 0
    rows_seen = 0
    modes: Counter[str] = Counter()
    protected_rows: dict[tuple[str, str], tuple[str, str]] = {}
    schema_root = ROOT / "dataset" / "java" / "delivery_schema"
    for path in sorted(schema_root.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            legacy = {
                "test_location",
                "focused_test_command",
                "focused_test_command_source",
            }.intersection(reader.fieldnames or [])
            assert not legacy, f"{path.name} retains legacy columns: {sorted(legacy)}"
            for row in reader:
                rows_seen += 1
                test_file = str(row.get("test_file") or "").strip()
                command = str(row.get("test_command") or "").strip()
                mode = str(row.get("verification_mode") or "").strip()
                modes[mode] += 1
                assert mode in {"sample_optimized", "project_full"}, (
                    f"{path.name}:{row.get('sample_id')} lacks an explicit final mode"
                )
                if mode != "sample_optimized":
                    continue
                assert test_file and command, (
                    f"{path.name}:{row.get('sample_id')} has an incomplete sample oracle"
                )
                if path.name not in {"feature_envy.csv", "data_clumps.csv"}:
                    continue
                declared_classes = [
                    Path(part.strip()).stem
                    for part in test_file.split(";")
                    if part.strip()
                ]
                missing = [name for name in declared_classes if name not in command]
                assert not missing, (
                    f"{path.name}:{row.get('sample_id')} command does not execute declared "
                    f"test class(es): {', '.join(missing)}"
                )
                checked += 1
                key = (path.name, str(row.get("sample_id") or "").strip())
                if key in {
                    ("feature_envy.csv", "6"),
                    ("data_clumps.csv", "10"),
                }:
                    protected_rows[key] = (test_file, command)
    assert rows_seen == 751, rows_seen
    assert checked == 120, checked
    assert modes == Counter({"sample_optimized": 711, "project_full": 40}), modes
    envy_test_file, envy_command = protected_rows[("feature_envy.csv", "6")]
    assert envy_test_file.split(";") == [
        "kerby-kerb/kerb-kdc-test/src/test/java/org/apache/kerby/kerberos/kerb/server/CacheFileTest.java",
        "kerby-kerb/kerb-server/src/test/java/org/apache/kerby/kerberos/kerb/server/request/TgsRequestCoverageBoostTest.java",
    ]
    assert "CacheFileTest,TgsRequestCoverageBoostTest" in envy_command
    clumps_test_file, clumps_command = protected_rows[("data_clumps.csv", "10")]
    assert clumps_test_file.split(";") == [
        "h2/src/test/org/h2/test/unit/TestGeometryScope_ESTest.java",
        "h2/src/test/org/h2/test/unit/TestGeometryUtils.java",
    ]
    assert "org.h2.test.unit.TestGeometryScope_ESTest" in clumps_command
    assert "org.h2.test.unit.TestGeometryUtils" in clumps_command
    print("  ok   751 rows use explicit modes; 120 envy/clumps commands match test_file")
    print("  ok   feature_envy#6 and data_clumps#10 retain both behavior tests")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sample-test-evidence-self-check-") as tmp:
        root = Path(tmp)

        fake_gradle = root / "gradlew"
        fake_gradle.write_text(
            """#!/bin/sh
set -eu
state=.fake-gradle-test-output
report=build/test-results/test/TEST-example.FreshBehaviorTest.xml
case " $* " in
  *" :demo:cleanTest "*)
    rm -f "$state" "$report"
    ;;
esac
if [ -f "$state" ]; then
  echo ":demo:test UP-TO-DATE"
  exit 0
fi
mkdir -p "$(dirname "$report")"
printf '%s\n' '<testsuite name="example.FreshBehaviorTest" tests="2" failures="0" errors="0" skipped="0"></testsuite>' > "$report"
# The fixture owns this timestamp.  A WSL/Docker mount may round the write
# mtime below Python's nanosecond test start even though the report is new.
"$SELF_CHECK_PYTHON" -c 'import os, sys, time; stamp = time.time_ns() + 2_000_000_000; os.utime(sys.argv[1], ns=(stamp, stamp))' "$report"
: > "$state"
echo ":demo:test"
""",
            encoding="utf-8",
        )
        fake_gradle.chmod(0o755)
        (root / ".fake-gradle-test-output").write_text("cached\n", encoding="utf-8")

        stale_started_ns = time.time_ns()
        stale_result = _run_command_config(
            CommandConfig(command="./gradlew :demo:test --no-build-cache"),
            cwd=root,
            env={},
            label="test",
            project_root=root,
        )
        assert stale_result["success"] is True, stale_result
        assert "UP-TO-DATE" in stale_result["output"], stale_result
        stale_evidence = _project_test_execution_evidence(
            SimpleNamespace(project_root=root),
            stale_started_ns,
            stale_result,
        )
        assert stale_evidence["success"] is False, stale_evidence

        fresh_guard = run_build_test_guard(
            SimpleNamespace(
                language="python",
                project_root=root,
                dataset_root=root,
                cwd=root,
                env={"SELF_CHECK_PYTHON": sys.executable},
                defaults=SimpleNamespace(run_build=False, run_tests=True),
                build=CommandConfig(),
                test=CommandConfig(
                    command="./gradlew :demo:test --no-build-cache"
                ),
                build_source="",
                test_source="projects.yaml",
                verification_mode="project_full",
                sample_test_location="",
                sample_test_command="",
            ),
            require_test_execution=True,
        )
        assert fresh_guard["success"] is True, fresh_guard
        fresh_result = fresh_guard["details"]["test"]
        assert fresh_result["success"] is True, fresh_result
        assert "./gradlew :demo:cleanTest :demo:test" in fresh_result["command"]
        fresh_evidence = fresh_result["execution_evidence"]
        assert fresh_evidence["success"] is True, fresh_evidence
        assert fresh_evidence["tests"] == 2, fresh_evidence
        (root / "build/test-results/test/TEST-example.FreshBehaviorTest.xml").unlink()
        print("  ok   fresh Gradle verification cleans the named test task")

        multi_gradle = (
            "./gradlew --offline :common:test --tests Example \\\n"
            "  --no-build-cache\n"
            "./gradlew --offline :stirling-pdf:test --tests Other"
        )
        normalized = _force_fresh_gradle_test_execution(multi_gradle)
        assert ":common:cleanTest" in normalized, normalized
        assert ":stirling-pdf:cleanTest" in normalized, normalized
        assert _force_fresh_gradle_test_execution(normalized) == normalized
        assert _force_fresh_gradle_test_execution("mvn test") == "mvn test"
        print("  ok   every Gradle invocation is normalized once; Maven is unchanged")

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

        project_zero = _project_test_execution_evidence(
            SimpleNamespace(project_root=root),
            time.time_ns(),
            _command_result("mvn test", "BUILD SUCCESS\n"),
        )
        assert project_zero["success"] is False, project_zero
        print("  ok   project-full exit zero with no executed test fails closed")

        project_started_ns = time.time_ns()
        _write_report(root, "ProjectFullBehaviorTest", 2)
        project_executed = _project_test_execution_evidence(
            SimpleNamespace(project_root=root),
            project_started_ns,
            _command_result("mvn test", "BUILD SUCCESS\n"),
        )
        assert project_executed["success"] is True, project_executed
        assert project_executed["tests"] == 2, project_executed
        print("  ok   project-full requires fresh non-zero test execution")

    _check_delivery_dataset_contracts()

    print("sample test evidence self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
