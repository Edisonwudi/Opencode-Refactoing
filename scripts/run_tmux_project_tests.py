#!/usr/bin/env python3
"""Run the pinned tmux repository's BSD-make regression list portably."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TEMP_PATH = re.compile(r"/(?:tmp|var/tmp)/[^\s'\"]+")


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


def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = 5.0) -> str:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return ""
    try:
        output, _ = process.communicate(timeout=grace_seconds)
        return str(output or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        return str(output or "")


def _cleanup_tmux_servers(binary: Path, env: dict[str, str]) -> None:
    for label in ("test", "test2"):
        try:
            subprocess.run(
                [str(binary), f"-L{label}", "kill-server"],
                env=env,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            continue


def _normalized_failure_line(line: str) -> str:
    text = _ANSI_ESCAPE.sub("", str(line or ""))
    text = _TEMP_PATH.sub("<tmp>", text)
    return " ".join(text.split())


def _diagnostic_fingerprint(returncode: int, output: str) -> str:
    lines = [
        normalized
        for line in str(output or "").splitlines()
        if (normalized := _normalized_failure_line(line))
    ]
    diagnostic = [
        line
        for line in lines
        if line == "TIMEOUT"
        or line.startswith("[FAIL]")
        or re.search(r"(?:failed|error|unexpected|no such file)", line, re.IGNORECASE)
    ]
    selected = (diagnostic or lines)[:3]
    if "TIMEOUT" in diagnostic and "TIMEOUT" not in selected:
        selected = ["TIMEOUT", *selected[:2]]
    detail = " | ".join(selected)
    return f"exit={returncode}" + (f" | {detail}" if detail else "")


def _exit_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--timeout-per-test", type=int, default=600)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    if args.timeout_per_test <= 0:
        raise SystemExit("tmux per-test timeout must be positive")

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

    base_env = dict(os.environ)
    base_env["TEST_TMUX"] = str(binary)
    temporary_parent = base_env.get("TMPDIR") or None
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)
    results: list[tuple[str, float, int, str]] = []
    for test in tests:
        with tempfile.TemporaryDirectory(
            prefix=f"tmux-regress-{test.stem}-", dir=temporary_parent
        ) as raw_tmp:
            env = dict(base_env)
            env["TMPDIR"] = raw_tmp
            env["TMUX_TMPDIR"] = raw_tmp
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
                output = _terminate_process_group(process)
                returncode = 124
                output = str(output or "") + "\nTIMEOUT\n"
            finally:
                _terminate_process_group(process, grace_seconds=1.0)
                _cleanup_tmux_servers(binary, env)
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
    for name, _, returncode, output in results:
        if returncode == 0:
            continue
        print(
            "TMUX_FAIL_CASE "
            + json.dumps(
                {
                    "diagnostic_fingerprint": _diagnostic_fingerprint(
                        returncode, output
                    ),
                    "exit_code": returncode,
                    "test": name,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
