#!/usr/bin/env python3
"""Run a fixed upstream Git test group and emit fresh JUnit evidence."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


TESTS = (
    "t0000-basic.sh",
    "t0060-path-utils.sh",
    "t1006-cat-file.sh",
    "t1300-config.sh",
    "t1450-fsck.sh",
    "t1500-rev-parse.sh",
    "t4202-log.sh",
    "t4211-line-log.sh",
)


def _write_report(report: Path, results: list[dict[str, object]]) -> None:
    failures = sum(int(item["returncode"] != 0) for item in results)
    suite = ET.Element(
        "testsuite",
        {
            "name": "git-upstream-selected",
            "tests": str(len(results)),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
            "time": f"{sum(float(item['duration']) for item in results):.3f}",
        },
    )
    for item in results:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "git.t",
                "name": str(item["name"]),
                "time": f"{float(item['duration']):.3f}",
            },
        )
        output = str(item["output"])
        ET.SubElement(case, "system-out").text = output
        if int(item["returncode"]) != 0:
            failure = ET.SubElement(
                case,
                "failure",
                {"message": f"{item['name']} exited {item['returncode']}"},
            )
            failure.text = output[-16000:]
    report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    test_dir = root / "t"
    build_options = root / "GIT-BUILD-OPTIONS"
    if not build_options.is_file():
        raise SystemExit("Git build evidence is missing: GIT-BUILD-OPTIONS")
    missing = [name for name in TESTS if not (test_dir / name).is_file()]
    if missing:
        raise SystemExit(f"Git upstream test scripts are missing: {missing}")

    report = root / ".smell-test-reports" / "TEST-git-selected.xml"
    report.unlink(missing_ok=True)
    results: list[dict[str, object]] = []
    for name in TESTS:
        started = time.monotonic()
        completed = subprocess.run(
            ["/bin/sh", name],
            cwd=test_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        duration = time.monotonic() - started
        print(f"=== Git upstream test {name} rc={completed.returncode} ===")
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
        results.append(
            {
                "name": name,
                "returncode": completed.returncode,
                "duration": duration,
                "output": completed.stdout,
            }
        )
        if completed.returncode != 0:
            break

    _write_report(report, results)
    return 0 if len(results) == len(TESTS) and all(item["returncode"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
