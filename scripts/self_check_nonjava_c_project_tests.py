#!/usr/bin/env python3
"""Verify C delivery overrides run project suites rather than smoke commands."""

from __future__ import annotations

import sys
import shutil
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
from run_nginx_project_tests import _select_tests  # noqa: E402


ROOTS = {
    "git": "/opt/projects/c/git",
    "rrdtool": "/opt/projects/c/rrdtool",
    "curl": "/opt/projects/c/curl",
    "redis": "/opt/projects/c/redis",
    "nginx": "/opt/projects/c/nginx",
    "tmux": "/opt/projects/c/tmux",
    "libssh2": "/opt/projects/c/libssh2",
    "libuv": "/opt/projects/c/libuv",
}


def _assert_no_weak_test(script: str) -> None:
    lowered = script.casefold()
    for token in (" --version", " -v", "test -f", "[ -x"):
        assert token not in lowered, (token, script)


def _assert_native_console_evidence(project: Path) -> None:
    curl = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="c"),
        time.time_ns(),
        {
            "script": (
                'perl "/opt/projects/c/curl/tests/runtests.pl" '
                "-a -p ~flaky ~timing-dependent --min=1"
            ),
            "output": (
                "TESTDONE: 120 tests were considered during 30 seconds.\n"
                "TESTDONE: 118 tests out of 118 reported OK: 100%\n"
            ),
        },
    )
    assert curl["success"] is True, curl
    assert curl["tests"] == 118, curl

    redis = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="c"),
        time.time_ns(),
        {
            "script": "./src/redis-server test all",
            "output": (
                "========== Test Suite Summary ==========\n"
                "Test Groups: 23 passed, 0 failed, 23 total\n"
                "Tests: 945 passed, 0 failed, 945 total\n"
            ),
        },
    )
    assert redis["success"] is True, redis
    assert redis["tests"] == 945, redis

    forged = _project_test_execution_evidence(
        SimpleNamespace(project_root=project, language="c"),
        time.time_ns(),
        {"script": "./src/redis-server --version", "output": "945 passed"},
    )
    assert forged["success"] is False, forged


def _assert_project_runners(temp: Path) -> None:
    fixture = ROOT / "test-fixtures" / "nginx-tests"
    assert len(_select_tests(fixture)) == 8
    copied_fixture = temp / "nginx-tests"
    shutil.copytree(fixture, copied_fixture)
    (copied_fixture / "map.t").unlink()
    try:
        _select_tests(copied_fixture)
    except SystemExit as exc:
        assert "exactly eight tests" in str(exc), exc
    else:
        raise AssertionError("incomplete nginx test fixture was accepted")

    tmux = temp / "tmux"
    regress = tmux / "regress"
    binary = tmux / "build-refactoragent" / "tmux"
    regress.mkdir(parents=True)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    (regress / "Makefile").write_text(
        "TESTS!= echo *.sh\nall: ${TESTS}\n.SILENT:\n.SUFFIXES: .sh\n.sh:\n\tsh $*.sh\n",
        encoding="utf-8",
    )
    for name in ("one.sh", "two.sh", "utf8-test.sh"):
        (regress / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_tmux_project_tests.py"),
            "--project-root",
            str(tmux),
            "--exclude",
            "utf8-test.sh",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, completed.stdout
    report = tmux / ".smell-test-reports" / "TEST-tmux-regress.xml"
    report_root = ET.parse(report).getroot()
    assert report_root.attrib == {
        "name": "tmux-regress",
        "tests": "3",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    }, report_root.attrib

    git = temp / "git"
    git_tests = git / "t"
    git_tests.mkdir(parents=True)
    (git / "GIT-BUILD-OPTIONS").write_text("fixture\n", encoding="utf-8")
    for name in (
        "t0000-basic.sh",
        "t0060-path-utils.sh",
        "t1006-cat-file.sh",
        "t1300-config.sh",
        "t1450-fsck.sh",
        "t1500-rev-parse.sh",
        "t4202-log.sh",
        "t4211-line-log.sh",
    ):
        script = git_tests / name
        script.write_text("#!/bin/sh\necho 'ok 1 - fixture'\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    git_completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_git_project_tests.py"),
            "--project-root",
            str(git),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert git_completed.returncode == 0, git_completed.stdout
    git_report = ET.parse(
        git / ".smell-test-reports" / "TEST-git-selected.xml"
    ).getroot()
    assert git_report.attrib["tests"] == "8", git_report.attrib
    assert git_report.attrib["failures"] == "0", git_report.attrib

    rrd = temp / "rrdtool"
    rrd_build = rrd
    rrd_build.mkdir(parents=True)
    rrd_tests = rrd / "tests"
    rrd_tests.mkdir()
    rrd_test_names = (
        "modify1",
        "compat-cloexec$(EXEEXT)",
        "create-with-source-4",
        "graph3",
    )
    (rrd_tests / "Makefile").write_text(
        "TESTS = " + " ".join(rrd_test_names) + "\nall:\n\t@:\n",
        encoding="utf-8",
    )
    (rrd_build / "Makefile").write_text(
        "check:\n"
        + "".join(
            f"\t@echo 'PASS: {name.replace('$(EXEEXT)', '')}'\n"
            for name in rrd_test_names
            if name != "create-with-source-4"
        ),
        encoding="utf-8",
    )
    rrd_completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_rrdtool_project_tests.py"),
            "--project-root",
            str(rrd),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert rrd_completed.returncode == 0, rrd_completed.stdout
    rrd_report = ET.parse(
        rrd / ".smell-test-reports" / "TEST-rrdtool-make-check.xml"
    ).getroot()
    assert rrd_report.attrib["tests"] == "4", rrd_report.attrib
    assert rrd_report.attrib["skipped"] == "1", rrd_report.attrib
    (rrd_tests / "Makefile").write_text(
        "TESTS = " + " ".join(rrd_test_names) + " newly-added-upstream-test\n"
        "all:\n\t@:\n",
        encoding="utf-8",
    )
    rrd_drift = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_rrdtool_project_tests.py"),
            "--project-root",
            str(rrd),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert rrd_drift.returncode != 0, rrd_drift.stdout
    assert "did not produce a clean non-zero Automake result" in rrd_drift.stdout

    libuv = temp / "libuv"
    libuv_build = libuv / "build-refactoragent"
    libuv_build.mkdir(parents=True)
    temp.chmod(0o755)
    libuv.chmod(0o755)
    libuv_build.chmod(0o755)
    for binary in ("uv_run_tests", "uv_run_tests_a"):
        executable = libuv_build / binary
        executable.write_text(
            "#!/bin/sh\n"
            "test \"$(id -u)\" -ne 0 || { echo root-forbidden; exit 9; }\n"
            "echo ok\nexit 0\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    from run_libuv_project_tests import _resolved_child_identity
    account = SimpleNamespace(pw_uid=65534, pw_gid=65534)
    assert _resolved_child_identity(0, account) == (65534, 65534)
    assert _resolved_child_identity(501, account) is None
    try:
        _resolved_child_identity(0, SimpleNamespace(pw_uid=0, pw_gid=0))
    except SystemExit as exc:
        assert "must be non-root" in str(exc), exc
    else:
        raise AssertionError("root libuv test identity was accepted")
    libuv_completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_libuv_project_tests.py"),
            "--project-root",
            str(libuv),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert libuv_completed.returncode == 0, libuv_completed.stdout
    libuv_report = ET.parse(
        libuv / ".smell-test-reports" / "TEST-libuv-selected.xml"
    ).getroot()
    assert libuv_report.attrib["tests"] == "12", libuv_report.attrib
    assert libuv_report.attrib["failures"] == "0", libuv_report.attrib


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="c-project-tests-") as raw:
        temp = Path(raw)
        projects = temp / "projects.yaml"
        projects.write_text(
            "projects:\n"
            + "".join(
                f"  - root: {root}\n"
                "    language: c\n"
                "    test:\n"
                "      command: weak-placeholder --version\n"
                for root in ROOTS.values()
            ),
            encoding="utf-8",
        )
        overrides = load_project_overrides(str(projects))
        selected = {item.root.name: item for item in overrides}
        assert set(selected) == set(ROOTS), selected

        curl = selected["curl"]
        assert "autoreconf -fi" in (curl.build.script or "")
        assert "./configure" in (curl.build.script or "")
        assert "--with-openssl" in (curl.build.script or "")
        assert "make -j" in (curl.build.script or "")
        assert "make -C tests all" in (curl.build.script or "")
        assert "cmake" not in (curl.build.script or "").casefold()
        assert "tests/runtests.pl" in (curl.test.script or "")
        assert "--min=1" in (curl.test.script or "")
        assert "ulimit -n 1024" in (curl.test.script or "")

        git = selected["git"]
        assert "NO_GETTEXT=YesPlease" in (git.build.script or "")
        assert "run_git_project_tests.py" in (git.test.script or "")

        rrdtool = selected["rrdtool"]
        assert "autoreconf -fvi -I m4" in (rrdtool.build.script or "")
        assert "./configure" in (rrdtool.build.script or "")
        assert "build-refactoragent" not in (rrdtool.build.script or "")
        assert "make check" not in (rrdtool.build.script or "")
        assert "run_rrdtool_project_tests.py" in (rrdtool.test.script or "")

        redis = selected["redis"]
        assert "REDIS_CFLAGS=-DREDIS_TEST" in (redis.build.script or "")
        assert "redis-server test all" in (redis.test.script or "")

        libssh2 = selected["libssh2"]
        assert "-DBUILD_TESTING=ON" in (libssh2.build.script or "")
        assert "-DRUN_DOCKER_TESTS=OFF" in (libssh2.build.script or "")
        assert "-DRUN_SSHD_TESTS=OFF" in (libssh2.build.script or "")
        assert "ctest" in (libssh2.test.script or "")
        assert "--output-junit" in (libssh2.test.script or "")

        libuv = selected["libuv"]
        assert "-DLIBUV_BUILD_TESTS=ON" in (libuv.build.script or "")
        assert "run_libuv_project_tests.py" in (libuv.test.script or "")

        tmux = selected["tmux"]
        assert "run_tmux_project_tests.py" in (tmux.test.script or "")
        assert "--exclude utf8-test.sh" in (tmux.test.script or "")

        nginx = selected["nginx"]
        assert "run_nginx_project_tests.py" in (nginx.test.script or "")
        assert "nginx-tests" in (nginx.test.script or "")

        for item in selected.values():
            assert item.test.command is None
            _assert_no_weak_test(item.test.script or "")

        _assert_native_console_evidence(temp / "evidence")
        _assert_project_runners(temp)

    print(
        "non-Java C project-test self-check: PASS "
        "git/rrdtool/curl/redis/nginx/tmux/libssh2/libuv run real suites"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
