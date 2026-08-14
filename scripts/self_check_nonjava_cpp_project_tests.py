#!/usr/bin/env python3
"""Verify the C++ runtime overrides use upstream suites and fresh JUnit."""

from __future__ import annotations

import sys
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import load_project_overrides  # noqa: E402
from smell_core.guards import _project_test_execution_evidence  # noqa: E402


def _assert_duckdb_contract(script: str, test_script: str) -> None:
    assert "-DBUILD_UNITTESTS=ON" in script
    assert "-DENABLE_UNITTEST_CPP_TESTS=OFF" in script
    assert "-DCMAKE_BUILD_TYPE=Debug" in script
    assert "-DCXX_EXTRA_DEBUG=-g0" in script
    assert "-DENABLE_SANITIZER=FALSE" in script
    assert "-DENABLE_UBSAN=0" in script
    assert "--target duckdb" in script
    assert "--target unittest" in script
    assert '${SMELL_BUILD_JOBS:-1}' in script
    assert "cmake --build" not in test_script
    assert "build-refactoragent/test/unittest" in test_script
    assert "test/smoke_tests.list" in test_script
    assert "--reporter junit" in test_script
    assert "TEST-duckdb-smoke.xml" in test_script
    assert "--version" not in test_script


def _assert_rocksdb_contract(script: str, test_script: str) -> None:
    assert "make clean" in script
    assert "static_lib" in script
    assert "db_basic_test" in script
    assert "DEBUG_LEVEL=2" in script
    assert "LIB_MODE=static" in script
    assert "EXTRA_CXXFLAGS=-g0" in script
    assert '${SMELL_BUILD_JOBS:-1}' in script
    assert "make " not in test_script
    assert "db_basic_test" in test_script
    assert "--gtest_output=" in test_script
    assert "TEST-rocksdb-db-basic.xml" in test_script
    assert "test -f" not in test_script


def _assert_openttd_contract(script: str, test_script: str) -> None:
    assert "-DOPTION_DEDICATED=ON" in script
    assert "--target openttd_test" in script
    assert '${SMELL_BUILD_JOBS:-1}' in script
    assert "ctest" in test_script
    assert "-E '^regression_'" in test_script
    assert "--no-tests=error" in test_script
    assert "--output-junit" in test_script
    assert "TEST-openttd-unit.xml" in test_script
    assert " -h" not in test_script


def _assert_cmake_contract(script: str, test_script: str) -> None:
    assert "-DBUILD_TESTING=ON" in script
    assert "--target cmake ctest" in script
    assert '${SMELL_BUILD_JOBS:-1}' in script
    assert "ctest" in test_script
    assert "CMake\\." in test_script
    assert "ProcessorCount" not in test_script
    assert "|File|" not in test_script
    assert "--no-tests=error" in test_script
    assert "--output-junit" in test_script
    assert "TEST-cmake-selected.xml" in test_script
    assert "--version" not in test_script


def _assert_aria2_contract(script: str, test_script: str) -> None:
    assert "autoreconf -fi" in script
    assert "--disable-bittorrent" in script
    assert 'make -j"${SMELL_BUILD_JOBS:-1}"' in script
    assert "run_aria2_project_tests.py" in test_script
    assert "--build-dir build-refactoragent" in test_script
    assert "--version" not in test_script


def _assert_protobuf_contract(script: str, test_script: str) -> None:
    external_build_dir = "/tmp/refactoragent-protobuf-29.3-build"
    assert f'build_dir="{external_build_dir}"' in script
    assert 'cmake -S "${project_root}" -B "${build_dir}"' in script
    assert "-Dprotobuf_BUILD_TESTS=ON" in script
    assert "-Dprotobuf_BUILD_CONFORMANCE=OFF" in script
    assert "--target protoc lite-test upb-test --parallel 1" in script
    assert "$(nproc)" not in script
    assert "SMELL_BUILD_JOBS" not in script
    assert "${project_root}/build-refactoragent" not in script
    assert f'ctest --test-dir "{external_build_dir}"' in test_script
    assert "-R '^(lite-test|upb-test)$'" in test_script
    assert "--output-on-failure --parallel 1" in test_script
    assert "${project_root}/build-refactoragent" not in test_script


def _assert_aria2_runner(temp: Path) -> None:
    project = temp / "aria2"
    build = project / "build-refactoragent"
    test_build = build / "test"
    source_tests = project / "test"
    test_build.mkdir(parents=True)
    source_tests.mkdir()
    (source_tests / "AllTest.cc").write_text("// fixture\n", encoding="utf-8")
    (build / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    (test_build / "Makefile").write_text(
        "check:\n\t@printf '\\033[31mPASS: aria2c\\033[0m\\n'\n",
        encoding="utf-8",
    )
    started_ns = time.time_ns()
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_aria2_project_tests.py"),
            "--project-root",
            str(project),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, completed.stdout
    report = ET.parse(
        project / ".smell-test-reports" / "TEST-aria2-make-check.xml"
    ).getroot()
    assert report.attrib["tests"] == "1", report.attrib
    assert report.attrib["failures"] == "0", report.attrib
    evidence = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="cpp"),
        started_ns,
        {"output": completed.stdout},
    )
    assert evidence["success"] is True, evidence
    assert evidence["tests"] == 1, evidence


def _assert_fresh_junit_evidence(project: Path) -> None:
    reports = project / ".smell-test-reports"
    reports.mkdir(parents=True)

    started_ns = time.time_ns()
    (reports / "TEST-rocksdb-db-basic.xml").write_text(
        '<testsuites tests="2" failures="0" disabled="0" errors="0">'
        '<testsuite name="DBBasicTest" tests="2" failures="0">'
        '<testcase name="Open"/><testcase name="Write"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    evidence = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="cpp"),
        started_ns,
        {"output": ""},
    )
    assert evidence["success"] is True
    assert evidence["tests"] == 2

    for report in reports.iterdir():
        report.unlink()
    started_ns = time.time_ns()
    (reports / "TEST-rocksdb-all-disabled.xml").write_text(
        '<testsuites tests="1" failures="0" disabled="1" errors="0">'
        '<testsuite name="DBBasicTest" tests="1" failures="0" disabled="1">'
        '<testcase name="DisabledCase" status="notrun"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    evidence = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="cpp"),
        started_ns,
        {"output": ""},
    )
    assert evidence["success"] is False
    assert evidence["tests"] == 0
    assert evidence["disabled"] == 1

    for report in reports.iterdir():
        report.unlink()
    started_ns = time.time_ns()
    (reports / "TEST-duckdb-smoke.xml").write_text(
        '<testsuites name="DuckDB smoke">'
        '<testsuite name="smoke" tests="2" failures="0" errors="0">'
        '<testcase name="test/sql/select.test"/>'
        '<testcase name="test/sql/storage.test"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    evidence = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="cpp"),
        started_ns,
        {"output": ""},
    )
    assert evidence["success"] is True
    assert evidence["tests"] == 2


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cpp-project-tests-") as raw_temp:
        temp = Path(raw_temp)
        projects = temp / "projects.yaml"
        projects.write_text(
            "projects:\n"
            "  - root: /opt/projects/cpp/duckdb\n"
            "    language: cpp\n"
            "    build:\n"
            "      command: old-duckdb-build\n"
            "    test:\n"
            "      command: duckdb --version\n"
            "  - root: /opt/projects/cpp/rocksdb\n"
            "    language: cpp\n"
            "    build:\n"
            "      command: old-rocksdb-build\n"
            "    test:\n"
            "      command: test -f librocksdb.a\n"
            "  - root: /opt/projects/cpp/other\n"
            "    language: cpp\n"
            "    test:\n"
            "      command: make test\n"
            "  - root: /opt/projects/cpp/OpenTTD\n"
            "    language: cpp\n"
            "    build:\n"
            "      command: old-openttd-build\n"
            "    test:\n"
            "      command: openttd -h\n"
            "  - root: /opt/projects/cpp/CMake\n"
            "    language: cpp\n"
            "    test:\n"
            "      command: cmake --version\n"
            "  - root: /opt/projects/cpp/aria2\n"
            "    language: cpp\n"
            "    test:\n"
            "      command: aria2c --version\n"
            "  - root: /opt/projects/cpp/protobuf-29.3\n"
            "    language: cpp\n"
            "    build:\n"
            "      command: old-protobuf-build\n"
            "    test:\n"
            "      command: protoc --version\n",
            encoding="utf-8",
        )
        overrides = load_project_overrides(str(projects))
        assert len(overrides) == 7
        duckdb = next(item for item in overrides if item.root.name == "duckdb")
        rocksdb = next(item for item in overrides if item.root.name == "rocksdb")
        other = next(item for item in overrides if item.root.name == "other")
        openttd = next(item for item in overrides if item.root.name == "OpenTTD")
        cmake = next(item for item in overrides if item.root.name == "CMake")
        aria2 = next(item for item in overrides if item.root.name == "aria2")
        protobuf = next(
            item for item in overrides if item.root.name == "protobuf-29.3"
        )

        assert duckdb.build.command is None
        assert duckdb.test.command is None
        assert duckdb.shell_timeout == 1500
        _assert_duckdb_contract(duckdb.build.script or "", duckdb.test.script or "")

        assert rocksdb.build.command is None
        assert rocksdb.test.command is None
        assert rocksdb.shell_timeout == 1500
        _assert_rocksdb_contract(rocksdb.build.script or "", rocksdb.test.script or "")

        assert other.test.command == "make test"
        assert other.test.script is None
        assert openttd.build.command is None
        assert openttd.test.command is None
        assert openttd.shell_timeout == 1500
        _assert_openttd_contract(
            openttd.build.script or "", openttd.test.script or ""
        )
        assert cmake.build.command is None
        assert cmake.test.command is None
        assert cmake.shell_timeout == 1500
        _assert_cmake_contract(cmake.build.script or "", cmake.test.script or "")
        assert aria2.build.command is None
        assert aria2.test.command is None
        assert aria2.shell_timeout == 1500
        _assert_aria2_contract(aria2.build.script or "", aria2.test.script or "")
        assert protobuf.build.command is None
        assert protobuf.test.command is None
        assert protobuf.shell_timeout == 1800
        _assert_protobuf_contract(
            protobuf.build.script or "", protobuf.test.script or ""
        )
        _assert_aria2_runner(temp)
        _assert_fresh_junit_evidence(temp / "junit-fixture")

    print(
        "non-Java C++ project-test self-check: PASS "
        "cmake=8-native-ctest aria2=make-check "
        "protobuf=external-build-single-job duckdb=upstream-smoke "
        "rocksdb=db_basic_test openttd=94-unit-ctest evidence=native-junit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
