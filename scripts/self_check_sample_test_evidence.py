#!/usr/bin/env python3
from __future__ import annotations

import csv
from collections import Counter
import hashlib
import json
import os
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
from smell_core.checkpoints import capture_verification_contract  # noqa: E402
from smell_core.java_test_evidence import (  # noqa: E402
    JAVA_TEST_EVIDENCE_ADAPTER_ID,
    prepare_java_sample_test_command,
)


def _write_report(
    root: Path,
    class_name: str,
    tests: int,
    skipped: int = 0,
    failures: int = 0,
    nested: bool = False,
) -> None:
    report = root / "target" / "surefire-reports" / f"TEST-example.{class_name}.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    failure = (
        '<testcase name="fails"><failure message="boom"/></testcase>'
        if failures
        else ""
    )
    suite = (
        f'<testsuite name="example.{class_name}" tests="{tests}" '
        f'failures="{failures}" errors="0" skipped="{skipped}">'
        f"{failure}</testsuite>"
    )
    report.write_text(
        f"<testsuites>{suite}</testsuites>\n" if nested else f"{suite}\n",
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


def _assert_evidence_fields(
    case_name: str,
    evidence: dict[str, object],
    expected: dict[str, object],
) -> None:
    for field, expected_value in expected.items():
        actual_value = evidence[field]
        if callable(expected_value):
            assert expected_value(actual_value), (
                f"{case_name}: {field} predicate failed: {evidence}"
            )
        else:
            matches = (
                actual_value is expected_value
                if isinstance(expected_value, bool)
                else actual_value == expected_value
            )
            assert matches, (
                f"{case_name}: expected {field}={expected_value!r}, "
                f"got {actual_value!r}: {evidence}"
            )


def _check_sample_evidence_cases(root: Path, cases: tuple[tuple, ...]) -> None:
    for case_name, started_ns, class_names, expected in cases:
        location = ";".join(
            f"src/test/java/example/{class_name}.java"
            for class_name in class_names
        )
        evidence = _sample_test_execution_evidence(
            SimpleNamespace(
                project_root=root,
                sample_test_location=location,
            ),
            started_ns,
        )
        _assert_evidence_fields(case_name, evidence, expected)
        print(f"  ok   {case_name}")


def _check_project_evidence_cases(root: Path, cases: tuple[tuple, ...]) -> None:
    for case in cases:
        case_name, language, command, output, expected, *report = case
        case_root = root
        started_ns = time.time_ns()
        if report:
            case_root = root / "project-evidence-cases" / case_name
            _write_report(case_root, *report[0])
        evidence = _project_test_execution_evidence(
            SimpleNamespace(project_root=case_root, language=language),
            started_ns,
            _command_result(command, output),
        )
        _assert_evidence_fields(case_name, evidence, expected)


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
                assert command, (
                    f"{path.name}:{row.get('sample_id')} lacks the mandatory sample test command"
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
    print(
        "  ok   751 rows have explicit modes and sample commands; "
        "120 envy/clumps commands match test_file"
    )
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
                defaults=SimpleNamespace(
                    shell_timeout=600,
                    run_build=False,
                    run_tests=True,
                ),
                build=CommandConfig(),
                sample_test=CommandConfig(),
                verification_command_source="project_manifest",
                test=CommandConfig(
                    command="./gradlew :demo:test --no-build-cache"
                ),
                build_source="",
                test_source="project_manifest",
                sample_test_source="",
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

        two_stage = root / "two-stage-test.sh"
        two_stage.write_text(
            """#!/bin/sh
set -eu
phase="$1"
printf '%s\n' "$phase" >> two-stage-phases.log
if [ "$phase" = sample ]; then
  report=target/surefire-reports/TEST-example.DeclaredBehaviorTest.xml
  mkdir -p "$(dirname "$report")"
  printf '%s\n' '<testsuite name="example.DeclaredBehaviorTest" tests="2" failures="0" errors="0" skipped="0"></testsuite>' > "$report"
  "$SELF_CHECK_PYTHON" -c 'import os, sys, time; stamp = time.time_ns() + 2_000_000_000; os.utime(sys.argv[1], ns=(stamp, stamp))' "$report"
fi
""",
            encoding="utf-8",
        )
        two_stage.chmod(0o755)
        two_stage_config = SimpleNamespace(
            language="java",
            smell="long_method",
            profile=SimpleNamespace(guards=[{"type": "long_method"}]),
            project_root=root,
            dataset_root=root,
            build_root=root,
            cwd=root,
            env={"SELF_CHECK_PYTHON": sys.executable},
            defaults=SimpleNamespace(
                shell_timeout=600,
                run_build=True,
                run_tests=True,
            ),
            build=CommandConfig(command="true"),
            test=CommandConfig(command="./two-stage-test.sh project"),
            sample_test=CommandConfig(command="./two-stage-test.sh sample"),
            verification_command_source="project_manifest",
            build_source="project_manifest",
            test_source="project_manifest",
            sample_test_source="dataset",
            verification_mode="project_full",
            sample_test_location=(
                "src/test/java/example/DeclaredBehaviorTest.java"
            ),
            sample_test_command="./two-stage-test.sh sample",
        )
        two_stage_guard = run_build_test_guard(two_stage_config)
        assert two_stage_guard["success"] is True, two_stage_guard
        assert (root / "two-stage-phases.log").read_text(encoding="utf-8") == (
            "project\nsample\n"
        )
        sample_result = two_stage_guard["details"]["sample_test"]
        assert sample_result["success"] is True, sample_result
        assert sample_result["execution_evidence"]["test_classes"] == [
            "DeclaredBehaviorTest"
        ]
        verification_contract = capture_verification_contract(two_stage_config)
        assert verification_contract["contract_version"] == 5, verification_contract
        assert verification_contract["verification_command_source"] == (
            "project_manifest"
        )
        assert verification_contract["verification_cwd"] == str(root.resolve())
        assert verification_contract["test"]["configured_command"] == (
            "./two-stage-test.sh project"
        )
        assert verification_contract["sample_test"]["configured_command"] == (
            "./two-stage-test.sh sample"
        )
        assert verification_contract["sample_test"]["source"] == "dataset"
        assert verification_contract["sample_test"]["cwd"] == str(root.resolve())
        alternate_dataset_root = root / "dataset-root"
        alternate_dataset_root.mkdir()
        alternate_dataset_config = SimpleNamespace(
            **{
                **vars(two_stage_config),
                "dataset_root": alternate_dataset_root,
            }
        )
        alternate_contract = capture_verification_contract(
            alternate_dataset_config
        )
        assert alternate_contract["test"]["cwd"] == str(root.resolve())
        assert alternate_contract["sample_test"]["cwd"] == str(
            alternate_dataset_root.resolve()
        )
        optimized_contract = capture_verification_contract(
            SimpleNamespace(
                **{
                    **vars(alternate_dataset_config),
                    "verification_mode": "sample_optimized",
                    "test": alternate_dataset_config.sample_test,
                    "test_source": "dataset",
                }
            )
        )
        assert optimized_contract["test"]["cwd"] == str(
            alternate_dataset_root.resolve()
        )
        command_sample_contract = capture_verification_contract(
            SimpleNamespace(
                **{
                    **vars(alternate_dataset_config),
                    "sample_test_source": "command",
                }
            )
        )
        assert command_sample_contract["sample_test"]["cwd"] == str(
            root.resolve()
        )
        (root / "target/surefire-reports/TEST-example.DeclaredBehaviorTest.xml").unlink()
        print("  ok   project_full freezes and executes project plus sample tests")

        legacy_source = root / "src/test/java/example/LegacyMainBehaviorTest.java"
        legacy_source.parent.mkdir(parents=True, exist_ok=True)
        legacy_source.write_text(
            """package example;
public final class LegacyMainBehaviorTest {
    public static void main(String[] args) {
        if (args.length != 0) throw new AssertionError("unexpected args");
    }
}
""",
            encoding="utf-8",
        )
        legacy_command = (
            "mkdir -p target/test-classes && "
            "javac -d target/test-classes "
            "src/test/java/example/LegacyMainBehaviorTest.java && "
            "java -cp target/test-classes example.LegacyMainBehaviorTest"
        )
        legacy_config = SimpleNamespace(
            language="java",
            smell="long_method",
            profile=SimpleNamespace(guards=[{"type": "long_method"}]),
            project_root=root,
            dataset_root=root,
            build_root=root,
            cwd=root,
            env={"SELF_CHECK_PYTHON": sys.executable},
            defaults=SimpleNamespace(
                shell_timeout=600,
                run_build=True,
                run_tests=True,
            ),
            build=CommandConfig(command="true"),
            test=CommandConfig(command="true"),
            sample_test=CommandConfig(command=legacy_command),
            verification_command_source="project_manifest",
            build_source="project_manifest",
            test_source="project_manifest",
            sample_test_source="dataset",
            verification_mode="project_full",
            sample_test_location=(
                "src/test/java/example/LegacyMainBehaviorTest.java"
            ),
            sample_test_command=legacy_command,
        )
        adapted_command, adapter = prepare_java_sample_test_command(legacy_config)
        assert adapter["selected"] is True, adapter
        assert adapter["adapter_id"] == JAVA_TEST_EVIDENCE_ADAPTER_ID, adapter
        assert adapter["execution_modes"] == ["main"], adapter
        assert adapter["evidence_kinds"] == {
            "example.LegacyMainBehaviorTest": "declared_main_attestation"
        }, adapter
        assert "java_test_attestation_runner.py" in str(adapted_command.command)
        assert "DeclaredJavaTestReportAdapter" not in str(adapted_command.command)
        legacy_guard = run_build_test_guard(legacy_config)
        assert legacy_guard["success"] is True, legacy_guard
        legacy_result = legacy_guard["details"]["sample_test"]
        assert legacy_result["evidence_adapter"]["selected"] is True
        assert legacy_result["execution_evidence"]["executed_test_classes"] == [
            "LegacyMainBehaviorTest"
        ]
        legacy_execution = legacy_result["execution_evidence"]
        assert legacy_execution["tests"] == 0, legacy_execution
        assert legacy_execution["executions"] == 1, legacy_execution
        assert legacy_execution["classes"][0]["evidence_mode"] == (
            "declared_main_attestation"
        )
        attestation_path = (
            root
            / ".smell-artifacts/test-attestations"
            / "ATTEST-example.LegacyMainBehaviorTest.json"
        )
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        assert attestation["cwd"] == str(root.resolve()), attestation
        assert attestation["uid"] == os.getuid(), attestation
        assert attestation["euid"] == os.geteuid(), attestation
        assert attestation["source_sha256"] == hashlib.sha256(
            legacy_source.read_bytes()
        ).hexdigest(), attestation
        assert attestation["contract_command_sha256"] == hashlib.sha256(
            legacy_command.strip().encode("utf-8")
        ).hexdigest(), attestation
        legacy_contract = capture_verification_contract(legacy_config)
        assert legacy_contract["sample_test"]["evidence_adapter"]["selected"] is True
        assert legacy_contract["contract_version"] == 5, legacy_contract
        print("  ok   direct Java main keeps its JVM boundary and emits an attestation")

        system_exit_source = (
            root / "src/test/java/example/SystemExitMainBehaviorTest.java"
        )
        system_exit_source.write_text(
            """package example;
public final class SystemExitMainBehaviorTest {
    public static void main(String[] args) {
        System.exit(0);
    }
}
""",
            encoding="utf-8",
        )
        system_exit_command = (
            "javac -d target/test-classes "
            "src/test/java/example/SystemExitMainBehaviorTest.java && "
            "java -cp target/test-classes example.SystemExitMainBehaviorTest"
        )
        system_exit_config = SimpleNamespace(
            **{
                **vars(legacy_config),
                "sample_test": CommandConfig(command=system_exit_command),
                "sample_test_location": (
                    "src/test/java/example/SystemExitMainBehaviorTest.java"
                ),
                "sample_test_command": system_exit_command,
            }
        )
        system_exit_guard = run_build_test_guard(system_exit_config)
        assert system_exit_guard["success"] is True, system_exit_guard
        system_exit_execution = system_exit_guard["details"]["sample_test"][
            "execution_evidence"
        ]
        assert system_exit_execution["executions"] == 1, system_exit_execution
        system_exit_attestation = (
            root
            / ".smell-artifacts/test-attestations"
            / "ATTEST-example.SystemExitMainBehaviorTest.json"
        )

        tampered = json.loads(system_exit_attestation.read_text(encoding="utf-8"))
        tamper_started_ns = time.time_ns()
        tampered["uid"] = -1
        tampered["started_ns"] = tamper_started_ns + 1
        tampered["ended_ns"] = tamper_started_ns + 2
        system_exit_attestation.write_text(
            json.dumps(tampered, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rejected_uid = _sample_test_execution_evidence(
            system_exit_config,
            tamper_started_ns,
        )
        assert rejected_uid["success"] is False, rejected_uid
        assert rejected_uid["invalid_attestations"][0]["reason"] == (
            "process_identity_mismatch"
        ), rejected_uid

        tampered["uid"] = os.getuid()
        tampered["cwd"] = str(root.parent.resolve())
        tampered["started_ns"] = time.time_ns() + 1
        tampered["ended_ns"] = int(tampered["started_ns"]) + 1
        cwd_started_ns = int(tampered["started_ns"]) - 1
        system_exit_attestation.write_text(
            json.dumps(tampered, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rejected_cwd = _sample_test_execution_evidence(
            system_exit_config,
            cwd_started_ns,
        )
        assert rejected_cwd["success"] is False, rejected_cwd
        assert rejected_cwd["invalid_attestations"][0]["reason"] == (
            "cwd_outside_project"
        ), rejected_cwd
        print("  ok   System.exit(0), uid, and cwd preserve the subprocess contract")

        failing_exit_source = (
            root / "src/test/java/example/SystemExitFailureMainBehaviorTest.java"
        )
        failing_exit_source.write_text(
            """package example;
public final class SystemExitFailureMainBehaviorTest {
    public static void main(String[] args) {
        System.exit(7);
    }
}
""",
            encoding="utf-8",
        )
        failing_exit_command = (
            "javac -d target/test-classes "
            "src/test/java/example/SystemExitFailureMainBehaviorTest.java && "
            "java -cp target/test-classes example.SystemExitFailureMainBehaviorTest"
        )
        failing_exit_config = SimpleNamespace(
            **{
                **vars(legacy_config),
                "sample_test": CommandConfig(command=failing_exit_command),
                "sample_test_location": (
                    "src/test/java/example/SystemExitFailureMainBehaviorTest.java"
                ),
                "sample_test_command": failing_exit_command,
            }
        )
        failing_exit_guard = run_build_test_guard(failing_exit_config)
        assert failing_exit_guard["success"] is False, failing_exit_guard
        assert failing_exit_guard["details"]["sample_test"]["returncode"] == 7
        assert not (
            root
            / ".smell-artifacts/test-attestations"
            / "ATTEST-example.SystemExitFailureMainBehaviorTest.json"
        ).exists()
        print("  ok   a failing direct main returns its exact exit code and no attestation")

        junit_one = root / "src/test/java/example/FirstJUnitBehaviorTest.java"
        junit_two = root / "src/test/java/example/SecondJUnitBehaviorTest.java"
        for source in (junit_one, junit_two):
            source.write_text(
                f"package example; public final class {source.stem} {{}}\n",
                encoding="utf-8",
            )
        junit_config = SimpleNamespace(
            language="java",
            project_root=root,
            dataset_root=root,
            sample_test_location=(
                "src/test/java/example/FirstJUnitBehaviorTest.java;"
                "src/test/java/example/SecondJUnitBehaviorTest.java"
            ),
            sample_test=CommandConfig(
                command=(
                    "java -cp target/test-classes:deps.jar "
                    "org.junit.runner.JUnitCore "
                    "example.FirstJUnitBehaviorTest "
                    "example.SecondJUnitBehaviorTest"
                )
            ),
        )
        junit_command, junit_adapter = prepare_java_sample_test_command(junit_config)
        assert junit_adapter["selected"] is True, junit_adapter
        assert junit_adapter["execution_modes"] == ["junit4"], junit_adapter
        assert junit_adapter["evidence_kinds"] == {
            "example.FirstJUnitBehaviorTest": "junit4_xml",
            "example.SecondJUnitBehaviorTest": "junit4_xml",
        }, junit_adapter
        assert "--mode junit4" in str(junit_command.command)
        unmatched_config = SimpleNamespace(
            language="java",
            project_root=root,
            dataset_root=root,
            sample_test_location=(
                "src/test/java/example/FirstJUnitBehaviorTest.java"
            ),
            sample_test=CommandConfig(command="java -cp target/test-classes example.OtherTest"),
        )
        unmatched_command, unmatched_adapter = prepare_java_sample_test_command(
            unmatched_config
        )
        assert unmatched_adapter["selected"] is False, unmatched_adapter
        assert unmatched_command.command == unmatched_config.sample_test.command
        print("  ok   JUnitCore adapts only when every declared class matches")

        mixed_junit = root / "src/test/java/example/MixedJUnitBehaviorTest.java"
        mixed_main = root / "src/test/java/example/MixedMainBehaviorTest.java"
        mixed_junit.write_text(
            "package example; public final class MixedJUnitBehaviorTest {}\n",
            encoding="utf-8",
        )
        mixed_main.write_text(
            """package example;
public final class MixedMainBehaviorTest {
    public static void main(String[] args) {}
}
""",
            encoding="utf-8",
        )
        mixed_config = SimpleNamespace(
            language="java",
            project_root=root,
            dataset_root=root,
            sample_test_location=(
                "src/test/java/example/MixedJUnitBehaviorTest.java;"
                "src/test/java/example/MixedMainBehaviorTest.java"
            ),
            sample_test=CommandConfig(
                command=(
                    "java -cp target/test-classes:deps.jar "
                    "org.junit.runner.JUnitCore example.MixedJUnitBehaviorTest && "
                    "java -cp target/test-classes example.MixedMainBehaviorTest"
                )
            ),
        )
        mixed_command, mixed_adapter = prepare_java_sample_test_command(mixed_config)
        assert mixed_adapter["selected"] is True, mixed_adapter
        assert mixed_adapter["execution_modes"] == ["junit4", "main"], mixed_adapter
        assert mixed_adapter["evidence_kinds"] == {
            "example.MixedJUnitBehaviorTest": "junit4_xml",
            "example.MixedMainBehaviorTest": "declared_main_attestation",
        }, mixed_adapter
        assert "--mode junit4" in str(mixed_command.command)
        assert "java_test_attestation_runner.py" in str(mixed_command.command)
        print("  ok   mixed JUnitCore and direct-main commands keep distinct evidence")

        fake_junit_root = root / "fake-junit/org/junit/runner"
        fake_junit_root.mkdir(parents=True, exist_ok=True)
        (fake_junit_root / "JUnitCore.java").write_text(
            """package org.junit.runner;
public final class JUnitCore {
    public static Result runClasses(Class<?>... classes) { return new Result(); }
}
""",
            encoding="utf-8",
        )
        (fake_junit_root / "Result.java").write_text(
            """package org.junit.runner;
import java.util.Collections;
import java.util.List;
public final class Result {
    public int getRunCount() { return 0; }
    public int getIgnoreCount() { return 1; }
    public int getFailureCount() { return 0; }
    public List<Object> getFailures() { return Collections.emptyList(); }
}
""",
            encoding="utf-8",
        )
        ignored_junit_source = (
            root / "src/test/java/example/IgnoredJUnitBehaviorTest.java"
        )
        ignored_junit_source.write_text(
            "package example; public final class IgnoredJUnitBehaviorTest {}\n",
            encoding="utf-8",
        )
        ignored_junit_command = (
            "javac -d target/test-classes "
            "fake-junit/org/junit/runner/JUnitCore.java "
            "fake-junit/org/junit/runner/Result.java "
            "src/test/java/example/IgnoredJUnitBehaviorTest.java && "
            "java -cp target/test-classes org.junit.runner.JUnitCore "
            "example.IgnoredJUnitBehaviorTest"
        )
        ignored_junit_config = SimpleNamespace(
            **{
                **vars(legacy_config),
                "sample_test": CommandConfig(command=ignored_junit_command),
                "sample_test_location": (
                    "src/test/java/example/IgnoredJUnitBehaviorTest.java"
                ),
                "sample_test_command": ignored_junit_command,
            }
        )
        ignored_started_ns = time.time_ns()
        ignored_junit_guard = run_build_test_guard(ignored_junit_config)
        assert ignored_junit_guard["success"] is False, ignored_junit_guard
        ignored_report = (
            root
            / ".smell-artifacts/test-reports"
            / "TEST-example.IgnoredJUnitBehaviorTest.xml"
        )
        ignored_xml = ignored_report.read_text(encoding="utf-8")
        assert 'tests="1"' in ignored_xml, ignored_xml
        assert 'skipped="1"' in ignored_xml, ignored_xml
        ignored_evidence = _sample_test_execution_evidence(
            ignored_junit_config,
            ignored_started_ns,
        )
        assert ignored_evidence["success"] is False, ignored_evidence
        assert ignored_evidence["tests"] == 0, ignored_evidence
        print("  ok   JUnit ignored-only execution reports real counts and fails closed")

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
        skipped_started_ns = time.time_ns()
        _write_report(root, "SkippedBehaviorTest", 1, skipped=1)
        nested_started_ns = time.time_ns()
        _write_report(root, "NestedBehaviorTest$Case", 3)
        empty_started_ns = time.time_ns()

        _check_sample_evidence_cases(
            root,
            (
                (
                    "every declared test class has fresh evidence", started_ns,
                    ("FirstBehaviorTest", "SecondBehaviorTest"),
                    {
                        "success": True,
                        "test_classes": ["FirstBehaviorTest", "SecondBehaviorTest"],
                        "tests": 4,
                        "skipped": 1,
                        "classes": lambda classes: all(
                            item["success"] for item in classes
                        ),
                    },
                ),
                (
                    "every declared test class requires fresh execution evidence", started_ns,
                    ("FirstBehaviorTest", "MissingBehaviorTest"),
                    {
                        "success": False,
                        "missing_test_classes": ["MissingBehaviorTest"],
                        "executed_test_classes": ["FirstBehaviorTest"],
                    },
                ),
                (
                    "no declared class executed fails closed", started_ns,
                    ("MissingBehaviorTest", "OtherMissingBehaviorTest"),
                    {
                        "success": False,
                        "executed_test_classes": [],
                        "missing_test_classes": ["MissingBehaviorTest", "OtherMissingBehaviorTest"],
                    },
                ),
                (
                    "skipped-only report fails closed", skipped_started_ns,
                    ("SkippedBehaviorTest",),
                    {"success": False, "missing_test_classes": ["SkippedBehaviorTest"]},
                ),
                (
                    "nested test suites count as execution of the declared class", nested_started_ns,
                    ("NestedBehaviorTest",),
                    {
                        "success": True,
                        "tests": 3,
                        "classes": lambda classes: classes[0]["evidence_mode"] == "xml",
                    },
                ),
                (
                    "JUnit console output without a fresh XML report fails closed", empty_started_ns,
                    ("ConsoleBehaviorTest",),
                    {"success": False, "tests": 0},
                ),
                (
                    "Maven console output without a fresh XML report fails closed", empty_started_ns,
                    ("RunnerBehaviorTest",),
                    {"success": False, "tests": 0},
                ),
                (
                    "direct Java exit zero without XML or attestation fails closed", empty_started_ns,
                    ("MainBehaviorTest",),
                    {"success": False, "tests": 0},
                ),
                (
                    "unrelated successful tests still fail closed", empty_started_ns,
                    ("ConsoleBehaviorTest",),
                    {"success": False},
                ),
            ),
        )

        _check_project_evidence_cases(
            root,
            (
                ("java_zero_tests", "java", "mvn test", "BUILD SUCCESS\n", {"success": False}),
            ),
        )
        print("  ok   project-full exit zero with no executed test fails closed")

        _check_project_evidence_cases(
            root,
            (
                ("forged_junit_summary", "java", "printf", "OK (1 test)\n", {"success": False}),
                ("real_junit_summary", "java", "java org.junit.runner.JUnitCore ExampleTest",
                 "JUnit version 4.13.2\n.\nOK (1 test)\n",
                 {"success": True, "junit_console_tests": 1}),
                ("forged_maven_summary", "java", "printf",
                 "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n", {"success": False}),
                ("real_maven_summary", "java", "mvn test",
                 "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n",
                 {"success": True, "maven_console_tests": 1}),
            ),
        )
        print("  ok   console summaries are bound to their real test runners")

        masked_failure_marker = root / "masked-build-failure"
        masked_failure = _run_command_config(
            CommandConfig(
                script=(
                    "false\n"
                    f"printf reached > {masked_failure_marker}\n"
                )
            ),
            cwd=root,
            env={},
            label="build",
            project_root=root,
        )
        assert masked_failure["success"] is False, masked_failure
        assert not masked_failure_marker.exists(), masked_failure
        print("  ok   project scripts stop at the first failed command")

        _check_project_evidence_cases(
            root,
            (
                ("java_fresh_report", "java", "mvn test", "BUILD SUCCESS\n",
                 {"success": True, "tests": 2}, ("ProjectFullBehaviorTest", 2)),
            ),
        )
        print("  ok   project-full requires fresh non-zero test execution")

        _check_project_evidence_cases(
            root,
            (
                (
                    "java_failed_report", "java", "mvn test", "BUILD SUCCESS\n",
                    {
                        "success": False,
                        "failed_reports": [
                            "target/surefire-reports/"
                            "TEST-example.ProjectFullFailureTest.xml"
                        ],
                    },
                    ("ProjectFullFailureTest", 1, 0, 1),
                ),
                (
                    "java_nested_failed_report", "java", "mvn test", "BUILD SUCCESS\n",
                    {
                        "success": False,
                        "failed_reports": [
                            "target/surefire-reports/"
                            "TEST-example.NestedProjectFailureTest.xml"
                        ],
                    },
                    ("NestedProjectFailureTest", 1, 0, 1, True),
                ),
            ),
        )
        print("  ok   fresh JUnit failures override a zero command return code")

        _check_project_evidence_cases(
            root,
            (
                ("pytest_passed", "python", "python -m pytest -q tests/unit",
                 ".................. [100%]\n18 passed, 1 warning in 1.44s\n",
                 {"success": True, "tests": 18, "pytest_console_tests": 18}),
                ("pytest_elapsed_suffix", "python", "python -m pytest -q tests/unit",
                 "528 passed, 12 skipped, 10 warnings in 93.27s (0:01:33)\n",
                 {"success": True, "tests": 528, "pytest_console_tests": 528}),
                ("pytest_collection_only", "python", "python -m pytest --collect-only",
                 "collected 18 items\n18 tests collected in 0.12s\n", {"success": False}),
                (
                    "unittest_passed", "python", "python -m unittest -v test_fixture.py",
                    "test_one (test_fixture.Case.test_one) ... ok\n"
                    "test_two (test_fixture.Case.test_two) ... ok\n\n"
                    "----------------------------------------------------------------------\n"
                    "Ran 2 tests in 0.001s\n\nOK\n",
                    {"success": True, "unittest_console_tests": 2},
                ),
                ("unittest_all_skipped", "python", "python -m unittest -v test_fixture.py",
                 "Ran 2 tests in 0.001s\n\nOK (skipped=2)\n", {"success": False}),
            ),
        )
        print(
            "  ok   Python accepts completed pytest/unittest summaries, "
            "not collection or all-skipped output"
        )

        _check_project_evidence_cases(
            root,
            (
                ("ctest_c_passed", "c", "ctest --output-on-failure",
                 "1/1 Test #1: cjson_test .................   Passed    0.01 sec\n"
                 "100% tests passed, 0 tests failed out of 1\n",
                 {"success": True, "tests": 1, "ctest_console_tests": 1}),
                ("ctest_c_empty", "c", "ctest --output-on-failure",
                 "No tests were found!!!\n", {"success": False}),
            ),
        )
        print("  ok   C accepts detailed ctest passes, not a zero-test exit")

        _check_project_evidence_cases(
            root,
            (
                ("ctest_cpp_passed", "cpp", "ctest --output-on-failure",
                 "1/2 Test #1: parser_test ................   Passed    0.02 sec\n"
                 "2/2 Test #2: emitter_test ...............   Passed    0.03 sec\n"
                 "100% tests passed, 0 tests failed out of 2\n",
                 {"success": True, "tests": 2, "ctest_console_tests": 2}),
                ("ctest_cpp_empty", "cpp", "ctest --output-on-failure",
                 "100% tests passed, 0 tests failed out of 0\n", {"success": False}),
            ),
        )
        print("  ok   C++ accepts ctest cases plus summary, not an empty summary")

    _check_delivery_dataset_contracts()

    print("sample test evidence self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
