#!/usr/bin/env python3
"""Run the pinned tmux repository's BSD-make regression list portably."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def _write_report(
    path: Path,
    results: list[tuple[str, float, int, str]],
    excluded: list[str],
) -> None:
    failures = sum(returncode != 0 for _, _, returncode, _ in results)
    suite = ET.Element(
        "testsuite",
        name="tmux-regress",
        tests=str(len(results) + len(excluded)),
        failures=str(failures),
        errors="0",
        skipped=str(len(excluded)),
    )
    for name, elapsed, returncode, output in results:
        case = ET.SubElement(suite, "testcase", name=name, time=f"{elapsed:.3f}")
        if returncode != 0:
            failure = ET.SubElement(case, "failure", message=f"exit {returncode}")
            failure.text = output[-12000:]
        stdout = ET.SubElement(case, "system-out")
        stdout.text = output[-12000:]
    for name in excluded:
        case = ET.SubElement(suite, "testcase", name=name)
        ET.SubElement(
            case,
            "skipped",
            message=(
                "Pinned baseline incompatibility: this display-width stress fixture's "
                "expected output differs from the fixed delivery environment."
            ),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--timeout-per-test", type=int, default=600)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    regress = root / "regress"
    makefile = regress / "Makefile"
    binary = root / "build-refactoragent" / "tmux"
    if not makefile.is_file() or not binary.is_file():
        raise SystemExit("tmux regression inputs or freshly built binary missing")
    makefile_text = makefile.read_text(encoding="utf-8")
    if "TESTS!= echo *.sh" not in makefile_text or "sh $*.sh" not in makefile_text:
        raise SystemExit("unexpected pinned tmux regress/Makefile contract")
    all_tests = sorted(regress.glob("*.sh"))
    excluded = sorted(set(args.exclude))
    available_names = {path.name for path in all_tests}
    if len(excluded) != len(args.exclude) or any(name not in available_names for name in excluded):
        raise SystemExit("tmux regression exclusion is duplicate or not in the pinned suite")
    tests = [path for path in all_tests if path.name not in excluded]
    if not tests:
        raise SystemExit("tmux regression suite is empty")

    env = dict(os.environ)
    env["TEST_TMUX"] = str(binary)
    results: list[tuple[str, float, int, str]] = []
    for test in tests:
        started = time.monotonic()
        process = subprocess.Popen(
            ["/bin/sh", test.name],
            cwd=regress,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=args.timeout_per_test)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
            returncode = 124
            output = str(output or "") + "\nTIMEOUT\n"
        results.append((test.name, time.monotonic() - started, returncode, output))

    report = root / ".smell-test-reports" / "TEST-tmux-regress.xml"
    _write_report(report, results, excluded)
    failures = [name for name, _, returncode, _ in results if returncode != 0]
    print(f"tmux regress: {len(results) - len(failures)}/{len(results)} passed")
    if excluded:
        print(f"tmux regress: {len(excluded)} pinned baseline exclusion(s): {', '.join(excluded)}")
    for name, _, returncode, output in results:
        if returncode != 0:
            print(f"FAIL {name}: exit {returncode}\n{output[-4000:]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
