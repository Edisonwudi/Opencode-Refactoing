#!/usr/bin/env python3
"""Run RRDtool's upstream Automake suite and emit fresh JUnit evidence."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


RESULT_LINE = re.compile(r"^(PASS|SKIP|XFAIL|FAIL|ERROR):\s+(.+?)\s*$")
EXCLUDED = {
    "create-with-source-4": "requires the external dc calculator absent from the fixed delivery image",
}


def _normalize_test_name(name: str) -> str:
    return name.removesuffix("$(EXEEXT)").removesuffix(".exe")


def _declared_tests(build: Path) -> tuple[str, ...]:
    tests_dir = build / "tests"
    if not (tests_dir / "Makefile").is_file():
        raise SystemExit("RRDtool configured tests Makefile is missing")
    completed = subprocess.run(
        ["make", "--no-print-directory", "-C", str(tests_dir), "-pn"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "RRDtool Automake TESTS query failed: " + completed.stderr[-4000:]
        )
    matches = re.findall(r"^TESTS\s*=\s*(.+)$", completed.stdout, flags=re.MULTILINE)
    if len(matches) != 1:
        raise SystemExit(f"expected one RRDtool Automake TESTS declaration, got {len(matches)}")
    tests = tuple(_normalize_test_name(item) for item in shlex.split(matches[0]))
    if not tests or len(tests) != len(set(tests)):
        raise SystemExit("RRDtool Automake TESTS declaration is empty or ambiguous")
    unresolved = [item for item in tests if "$" in item or "(" in item or ")" in item]
    if unresolved:
        raise SystemExit(f"RRDtool Automake TESTS contains unresolved entries: {unresolved}")
    missing_exclusions = sorted(set(EXCLUDED) - set(tests))
    if missing_exclusions:
        raise SystemExit(
            f"RRDtool explicit exclusions are absent from this revision: {missing_exclusions}"
        )
    return tests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--build-dir", default=".")
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    build = (root / args.build_dir).resolve()
    if not (build / "Makefile").is_file():
        raise SystemExit("RRDtool configured build directory is missing")
    tests = _declared_tests(build)
    report = root / ".smell-test-reports" / "TEST-rrdtool-make-check.xml"
    report.unlink(missing_ok=True)

    started = time.monotonic()
    completed = subprocess.run(
        ["make", "check", f"TESTS={' '.join(name for name in tests if name not in EXCLUDED)}"],
        cwd=build,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    duration = time.monotonic() - started
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")

    seen: set[tuple[str, str]] = set()
    outcomes: list[tuple[str, str]] = []
    for raw in completed.stdout.splitlines():
        match = RESULT_LINE.fullmatch(raw.strip())
        if not match:
            continue
        item = (match.group(1), _normalize_test_name(match.group(2)))
        if item not in seen:
            seen.add(item)
            outcomes.append(item)
    executed = [item for item in outcomes if item[0] in {"PASS", "FAIL", "ERROR"}]
    failures = [item for item in outcomes if item[0] in {"FAIL", "ERROR"}]
    skipped = [item for item in outcomes if item[0] in {"SKIP", "XFAIL"}]
    for name, reason in EXCLUDED.items():
        outcomes.append(("SKIP", name))
        skipped.append(("SKIP", name))

    suite = ET.Element(
        "testsuite",
        {
            "name": "rrdtool-make-check",
            "tests": str(len(outcomes)),
            "failures": str(len(failures)),
            "errors": "0",
            "skipped": str(len(skipped)),
            "time": f"{duration:.3f}",
        },
    )
    for status, name in outcomes:
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "rrdtool.tests", "name": name},
        )
        if status in {"SKIP", "XFAIL"}:
            ET.SubElement(case, "skipped", {"message": EXCLUDED.get(name, status)})
        elif status in {"FAIL", "ERROR"}:
            failure = ET.SubElement(case, "failure", {"message": status})
            failure.text = completed.stdout[-16000:]
    report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)

    valid = (
        completed.returncode == 0
        and bool(executed)
        and not failures
        and any(status == "PASS" for status, _ in outcomes)
        and {name for status, name in outcomes if status == "PASS"}
        == {name for name in tests if name not in EXCLUDED}
    )
    if not valid:
        print(
            "RRDtool suite did not produce a clean non-zero Automake result: "
            f"rc={completed.returncode} outcomes={len(outcomes)} failures={len(failures)}"
        )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
