#!/usr/bin/env python3
"""Verify C delivery overrides run project suites rather than smoke commands."""

from __future__ import annotations

import os
import signal
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
from run_rrdtool_project_tests import (  # noqa: E402
    _libdbi_build_contract,
    _run_libdbi_attribute_probe,
)


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

LIBDBI_PROBE_FIXTURE = r"""
static long rrd_fetch_dbi_long(dbi_result result, int idx) {
  long value = DNAN;
  unsigned int attr = dbi_result_get_field_attribs_idx(result, idx);
  unsigned int type = dbi_result_get_field_type_idx(result, idx);
  if (dbi_result_field_is_null_idx(result, idx)) { return DNAN; }
  switch (type) {
    case DBI_TYPE_INTEGER:
      if        (attr & DBI_INTEGER_SIZE1) { value = dbi_result_get_char_idx(result, idx);
      } else if (attr & DBI_INTEGER_SIZE2) { value = dbi_result_get_short_idx(result, idx);
      } else if (attr & DBI_INTEGER_SIZE3) { value = dbi_result_get_int_idx(result, idx);
      } else if (attr & DBI_INTEGER_SIZE4) { value = dbi_result_get_int_idx(result, idx);
      } else if (attr & DBI_INTEGER_SIZE8) { value = dbi_result_get_longlong_idx(result, idx);
      }
      break;
    case DBI_TYPE_DECIMAL:
      if        (attr & DBI_DECIMAL_SIZE4) { value = floor(dbi_result_get_float_idx(result, idx));
      } else if (attr & DBI_DECIMAL_SIZE8) { value = floor(dbi_result_get_double_idx(result, idx));
      }
      break;
    default:
      break;
  }
  return value;
}
""".strip()


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
        (regress / name).write_text(
            "#!/bin/sh\nprintf 'TMPDIR=%s\\n' \"$TMPDIR\"\nexit 0\n",
            encoding="utf-8",
        )
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
    tmux_tmpdirs = {
        (case.findtext("system-out") or "").strip()
        for case in report_root.findall("testcase")
        if case.attrib["name"] in {"one.sh", "two.sh"}
    }
    assert len(tmux_tmpdirs) == 2, tmux_tmpdirs
    assert all(value.startswith("TMPDIR=") for value in tmux_tmpdirs), tmux_tmpdirs

    timeout_tmux = temp / "tmux-timeout"
    timeout_regress = timeout_tmux / "regress"
    timeout_binary = timeout_tmux / "build-refactoragent" / "tmux"
    timeout_regress.mkdir(parents=True)
    timeout_binary.parent.mkdir(parents=True)
    timeout_binary.write_text(
        "#!/bin/sh\n"
        "if [ \"${2:-}\" = kill-server ] && [ -n \"${TMUX_CLEANUP_LOG:-}\" ]; then\n"
        "  printf '%s\\n' \"${1:-}\" >> \"$TMUX_CLEANUP_LOG\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    timeout_binary.chmod(0o755)
    (timeout_regress / "Makefile").write_text(
        "TESTS!= echo *.sh\nall: ${TESTS}\n.SILENT:\n.SUFFIXES: .sh\n.sh:\n\tsh $*.sh\n",
        encoding="utf-8",
    )
    (timeout_regress / "hang.sh").write_text(
        "#!/bin/sh\nsleep 300 &\necho $! > child.pid\nwait\n",
        encoding="utf-8",
    )
    (timeout_regress / "next.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cleanup_log = timeout_tmux / "cleanup.log"
    timeout_env = {**os.environ, "TMUX_CLEANUP_LOG": str(cleanup_log)}
    timed_out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_tmux_project_tests.py"),
            "--project-root",
            str(timeout_tmux),
            "--timeout-per-test",
            "1",
        ],
        check=False,
        env=timeout_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert timed_out.returncode == 1, timed_out.stdout
    assert "FAIL hang.sh: exit 124" in timed_out.stdout, timed_out.stdout
    assert "TMUX_FAIL_CASE " in timed_out.stdout, timed_out.stdout
    assert '"test":"hang.sh"' in timed_out.stdout, timed_out.stdout
    assert "TIMEOUT" in timed_out.stdout, timed_out.stdout
    timeout_report = ET.parse(
        timeout_tmux / ".smell-test-reports" / "TEST-tmux-regress.xml"
    ).getroot()
    assert timeout_report.attrib["tests"] == "2", timeout_report.attrib
    assert timeout_report.attrib["failures"] == "1", timeout_report.attrib
    child_pid = int((timeout_regress / "child.pid").read_text(encoding="utf-8"))
    child_probe = subprocess.run(
        ["kill", "-0", str(child_pid)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert child_probe.returncode != 0, f"timed-out tmux child survived: {child_pid}"
    assert cleanup_log.read_text(encoding="utf-8").splitlines() == [
        "-Ltest",
        "-Ltest2",
        "-Ltest",
        "-Ltest2",
    ]

    (timeout_regress / "child.pid").unlink()
    cleanup_log.unlink()
    interrupted = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_tmux_project_tests.py"),
            "--project-root",
            str(timeout_tmux),
            "--timeout-per-test",
            "60",
        ],
        env=timeout_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    child_path = timeout_regress / "child.pid"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not child_path.is_file():
        time.sleep(0.02)
    assert child_path.is_file(), "external-deadline tmux child did not start"
    interrupted_child_pid = int(child_path.read_text(encoding="utf-8"))
    os.kill(interrupted.pid, signal.SIGTERM)
    interrupted_output, _ = interrupted.communicate(timeout=5)
    assert interrupted.returncode == 128 + signal.SIGTERM, interrupted_output
    interrupted_child_probe = subprocess.run(
        ["kill", "-0", str(interrupted_child_pid)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert interrupted_child_probe.returncode != 0, (
        f"externally interrupted tmux child survived: {interrupted_child_pid}"
    )
    assert cleanup_log.read_text(encoding="utf-8").splitlines() == [
        "-Ltest",
        "-Ltest2",
    ]

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
    rrd_src = rrd / "src"
    rrd_objects = rrd_src / ".libs"
    rrd_objects.mkdir(parents=True)
    (rrd_src / "rrd_config.h").write_text("#define HAVE_LIBDBI 1\n", encoding="utf-8")
    (rrd_src / "Makefile").write_text(
        "am__append_1 = rrd_fetch_libdbi.c\n", encoding="utf-8"
    )
    (rrd_objects / "librrd_la-rrd_fetch_libdbi.o").write_bytes(b"compiled-object")
    probe_include = rrd / "probe-include" / "dbi"
    probe_include.mkdir(parents=True)
    (probe_include / "dbi.h").write_text(
        "typedef struct probe_dbi_result *dbi_result;\n"
        "#define DBI_TYPE_INTEGER 1u\n"
        "#define DBI_TYPE_DECIMAL 2u\n"
        "#define DBI_TYPE_STRING 3u\n"
        "#define DBI_TYPE_BINARY 4u\n"
        "#define DBI_TYPE_DATETIME 5u\n"
        "#define DBI_INTEGER_UNSIGNED 1u\n"
        "#define DBI_INTEGER_SIZE1 2u\n"
        "#define DBI_INTEGER_SIZE2 4u\n"
        "#define DBI_INTEGER_SIZE3 8u\n"
        "#define DBI_INTEGER_SIZE4 16u\n"
        "#define DBI_INTEGER_SIZE8 32u\n"
        "#define DBI_DECIMAL_UNSIGNED 1u\n"
        "#define DBI_DECIMAL_SIZE4 2u\n"
        "#define DBI_DECIMAL_SIZE8 4u\n",
        encoding="utf-8",
    )
    (rrd_src / "rrd_fetch_libdbi.c").write_text(
        LIBDBI_PROBE_FIXTURE + "\n", encoding="utf-8"
    )
    assert _libdbi_build_contract(rrd_build) == ""
    assert _run_libdbi_attribute_probe(
        rrd_build, include_dir=probe_include.parent
    ).returncode == 0
    (rrd_src / "rrd_fetch_libdbi.c").write_text(
        LIBDBI_PROBE_FIXTURE.replace("attr & DBI_INTEGER", "attr == DBI_INTEGER") + "\n",
        encoding="utf-8",
    )
    assert _run_libdbi_attribute_probe(
        rrd_build, include_dir=probe_include.parent
    ).returncode != 0
    (rrd_src / "rrd_fetch_libdbi.c").write_text(
        LIBDBI_PROBE_FIXTURE.replace("attr & DBI_DECIMAL", "attr == DBI_DECIMAL")
        + "\n",
        encoding="utf-8",
    )
    assert _run_libdbi_attribute_probe(
        rrd_build, include_dir=probe_include.parent
    ).returncode != 0
    (rrd_src / "rrd_fetch_libdbi.c").write_text(
        LIBDBI_PROBE_FIXTURE + "\n", encoding="utf-8"
    )
    rrd_env = dict(os.environ)
    rrd_env["CPATH"] = str(probe_include.parent)
    rrd_completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_rrdtool_project_tests.py"),
            "--project-root",
            str(rrd),
        ],
        check=False,
        env=rrd_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert rrd_completed.returncode == 0, rrd_completed.stdout
    rrd_report = ET.parse(
        rrd / ".smell-test-reports" / "TEST-rrdtool-make-check.xml"
    ).getroot()
    assert rrd_report.attrib["tests"] == "6", rrd_report.attrib
    assert rrd_report.attrib["skipped"] == "1", rrd_report.attrib
    rrd_report_path = rrd / ".smell-test-reports" / "TEST-rrdtool-make-check.xml"
    rrd_report_path.unlink()
    rrd_focused = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_rrdtool_project_tests.py"),
            "--project-root",
            str(rrd),
            "--focused-libdbi-probe",
        ],
        check=False,
        env=rrd_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert rrd_focused.returncode == 0, rrd_focused.stdout
    assert "rrdtool focused libDBI probe: PASS" in rrd_focused.stdout
    assert not rrd_report_path.exists(), rrd_report_path
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
        env=rrd_env,
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
    c_dependency_layer = (
        ROOT / "docker" / "c-refactor-delivery" / "Dockerfile.project-test-dependencies"
    )
    dependency_text = c_dependency_layer.read_text(encoding="utf-8")
    assert (
        "opencode-smell-c-refactor-env:0.1.1-amd64-delivery-20260721"
        in dependency_text
    )
    assert "libdbi-dev=0.9.0-6.1build1" in dependency_text
    assert "ccache=4.9.1-1" in dependency_text

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
        assert "--focused-libdbi-probe" in (
            rrdtool.focused_preflight.script or ""
        )
        assert "autoreconf -fvi -I m4" in (rrdtool.build.script or "")
        assert "./configure" in (rrdtool.build.script or "")
        assert "--enable-libdbi" in (rrdtool.build.script or "")
        assert "With libDBI: yes" in (rrdtool.build.script or "")
        assert "librrd_la-rrd_fetch_libdbi.o" in (rrdtool.build.script or "")
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
        assert "run_tmux_project_tests.py" not in (
            tmux.focused_preflight.script or ""
        )
        assert "run_tmux_project_tests.py" in (tmux.test.script or "")
        assert "--exclude utf8-test.sh" in (tmux.test.script or "")
        assert "--timeout-per-test 180" in (tmux.test.script or "")

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
