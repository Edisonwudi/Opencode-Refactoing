#!/usr/bin/env python3
"""Run aria2's upstream Automake/CppUnit suite and emit fresh JUnit."""

from __future__ import annotations

import argparse
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def _xml_safe(value: str) -> str:
    return "".join(
        character
        if (
            character in "\t\n\r"
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
        else "\uFFFD"
        for character in value
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--build-dir", default="build-refactoragent")
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    build = (root / args.build_dir).resolve()
    if not (build / "Makefile").is_file():
        raise SystemExit("aria2 configured build directory is missing")
    test_build = build / "test"
    if not (test_build / "Makefile").is_file():
        raise SystemExit("aria2 configured CppUnit test directory is missing")
    if not (root / "test" / "AllTest.cc").is_file():
        raise SystemExit("aria2 upstream CppUnit suite is missing")

    report = root / ".smell-test-reports" / "TEST-aria2-make-check.xml"
    report.unlink(missing_ok=True)
    started = time.monotonic()
    completed = subprocess.run(
        ["make", "check", "TESTS=aria2c"],
        cwd=test_build,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    duration = time.monotonic() - started
    output = completed.stdout
    for name in ("test-suite.log", "aria2c.log"):
        diagnostic = test_build / name
        if diagnostic.is_file():
            output += f"\n=== aria2 {name} ===\n{diagnostic.read_text(encoding='utf-8', errors='replace')}"
    print(output, end="" if output.endswith("\n") else "\n")

    passed = completed.returncode == 0 and "PASS: aria2c" in output
    suite = ET.Element(
        "testsuite",
        {
            "name": "aria2-upstream-make-check",
            "tests": "1",
            "failures": "0" if passed else "1",
            "errors": "0",
            "skipped": "0",
            "time": f"{duration:.3f}",
        },
    )
    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "aria2.test",
            "name": "aria2c-cppunit-suite",
            "time": f"{duration:.3f}",
        },
    )
    safe_output = _xml_safe(output)
    ET.SubElement(case, "system-out").text = safe_output
    if not passed:
        failure = ET.SubElement(
            case,
            "failure",
            {"message": f"make check exited {completed.returncode}"},
        )
        failure.text = safe_output[-16000:]
    report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
