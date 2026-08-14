#!/usr/bin/env python3
"""Run the repository-pinned subset of the official nginx test suite."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def _select_tests(root: Path) -> list[Path]:
    tests = sorted(root.glob("*.t"))
    if len(tests) != 8:
        raise SystemExit("nginx-tests fixture must contain exactly eight tests")
    if not (root / "lib" / "Test" / "Nginx.pm").is_file():
        raise SystemExit("nginx-tests Perl helper missing")
    return tests


def _write_report(
    path: Path,
    tests: list[Path],
    assertions: int,
    elapsed: float,
    output: str,
    ok: bool,
) -> None:
    suite = ET.Element(
        "testsuite",
        name="nginx-tests-pinned",
        tests=str(assertions if ok else len(tests)),
        failures="0" if ok else "1",
        errors="0",
        skipped="0",
        time=f"{elapsed:.3f}",
    )
    for index, test in enumerate(tests):
        case = ET.SubElement(suite, "testcase", name=test.name)
        if not ok and index == 0:
            failure = ET.SubElement(case, "failure", message="prove failed")
            failure.text = output[-20000:]
    stdout = ET.SubElement(suite, "system-out")
    stdout.text = output[-20000:]
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--fixture-root", required=True)
    args = parser.parse_args()

    project = Path(args.project_root).resolve()
    fixture = Path(args.fixture_root).resolve()
    binary = project / "objs" / "nginx"
    if not binary.is_file():
        raise SystemExit("fresh nginx binary missing")
    selected_tests = _select_tests(fixture)
    env = dict(os.environ)
    env["TEST_NGINX_BINARY"] = str(binary)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="nginx-tests-") as raw_work:
        os.chmod(raw_work, 0o755)
        work = Path(raw_work) / "suite"
        shutil.copytree(fixture, work)
        for directory in [work, *[path for path in work.rglob("*") if path.is_dir()]]:
            directory.chmod(0o755)
        for regular_file in [path for path in work.rglob("*") if path.is_file()]:
            regular_file.chmod(0o644)
        preexec_fn = None
        if os.geteuid() == 0:
            account = pwd.getpwnam("smell")
            for path in [Path(raw_work), work, *work.rglob("*")]:
                os.chown(path, account.pw_uid, account.pw_gid)

            def _drop_privileges() -> None:
                os.setgroups([])
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)

            preexec_fn = _drop_privileges
        tests = [work / path.name for path in selected_tests]
        completed = subprocess.run(
            ["prove", "-I", str(work / "lib"), *[str(path) for path in tests]],
            cwd=work,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            preexec_fn=preexec_fn,
        )
        output = completed.stdout or ""
    elapsed = time.monotonic() - started
    summary = re.search(r"^Files=(\d+),\s+Tests=(\d+),", output, re.MULTILINE)
    assertions = int(summary.group(2)) if summary else 0
    ok = bool(
        completed.returncode == 0
        and "Result: PASS" in output
        and summary
        and int(summary.group(1)) == len(selected_tests)
        and assertions > 0
    )
    report = project / ".smell-test-reports" / "TEST-nginx-pinned.xml"
    _write_report(report, selected_tests, assertions, elapsed, output, ok)
    print(output, end="")
    return 0 if ok else completed.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
