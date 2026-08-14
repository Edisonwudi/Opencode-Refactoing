#!/usr/bin/env python3
"""Run the pinned Lua portable upstream suite and emit strict JUnit evidence."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


SUCCESS_SENTINEL = "final OK !!!"


def _within(root: Path, value: Path, label: str) -> Path:
    resolved = value.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside project root: {resolved}") from exc
    return resolved


def _write_junit(report: Path, duration: float) -> None:
    suite = ET.Element(
        "testsuite",
        {
            "name": "lua-portable-upstream-suite",
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "0",
            "time": f"{duration:.6f}",
        },
    )
    ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "lua.testes",
            "name": "all.lua portable user mode",
            "time": f"{duration:.6f}",
        },
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=report.parent, prefix=".TEST-lua-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        ET.ElementTree(suite).write(handle, encoding="utf-8", xml_declaration=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--lua", default="lua")
    parser.add_argument("--suite", default="testes/all.lua")
    args = parser.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()
    lua = _within(project_root, project_root / args.lua, "Lua executable")
    suite = _within(project_root, project_root / args.suite, "Lua suite")
    if not lua.is_file() or not suite.is_file():
        print("Lua executable or upstream suite is missing", file=sys.stderr)
        return 2

    report = project_root / ".smell-test-reports" / "TEST-lua-all.xml"
    report.unlink(missing_ok=True)
    started = time.monotonic()
    completed = subprocess.run(
        [str(lua), "-e", "_U=true", suite.name],
        cwd=suite.parent,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    duration = time.monotonic() - started
    output = completed.stdout or ""
    sys.stdout.write(output)
    sentinel_count = sum(
        1 for line in output.splitlines() if line.strip() == SUCCESS_SENTINEL
    )
    if completed.returncode != 0 or sentinel_count != 1:
        print(
            "Lua upstream suite did not produce one exact success sentinel "
            f"(returncode={completed.returncode}, sentinels={sentinel_count})",
            file=sys.stderr,
        )
        return completed.returncode or 1
    _write_junit(report, duration)
    print(f"JUnit report: {report.relative_to(project_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
