#!/usr/bin/env python3
"""Run libuv's pinned native cases and emit fresh JUnit evidence."""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable


BINARIES = ("uv_run_tests", "uv_run_tests_a")
TESTS = (
    "getaddrinfo_basic",
    "getaddrinfo_basic_sync",
    "random_async",
    "threadpool_cancel_random",
    "tcp_connect_error_after_write",
    "idna_toascii",
)


def _resolved_child_identity(effective_uid: int, account: Any) -> tuple[int, int] | None:
    """Select one fixed non-root identity only when the container runs as root."""
    if effective_uid != 0:
        return None
    uid = int(account.pw_uid)
    gid = int(account.pw_gid)
    if uid == 0 or gid == 0:
        raise SystemExit("libuv test identity must be non-root")
    return uid, gid


def _demote(uid: int, gid: int) -> Callable[[], None]:
    def apply() -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    return apply


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--build-dir", default="build-refactoragent")
    parser.add_argument("--run-as-user", default="nobody")
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    build = (root / args.build_dir).resolve()
    missing = [name for name in BINARIES if not (build / name).is_file()]
    if missing:
        raise SystemExit(f"libuv native test binaries are missing: {missing}")
    report = root / ".smell-test-reports" / "TEST-libuv-selected.xml"
    report.unlink(missing_ok=True)
    account = pwd.getpwnam(args.run_as_user)
    child_identity = _resolved_child_identity(os.geteuid(), account)
    child_setup = _demote(*child_identity) if child_identity is not None else None
    child_uid = child_identity[0] if child_identity is not None else os.geteuid()
    if child_uid == 0:
        raise SystemExit("libuv native tests may not run as root")
    print(f"libuv native tests execute as uid={child_uid}")

    results: list[dict[str, object]] = []
    for binary in BINARIES:
        for test_name in TESTS:
            started = time.monotonic()
            completed = subprocess.run(
                [str(build / binary), test_name],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                preexec_fn=child_setup,
            )
            duration = time.monotonic() - started
            print(f"=== libuv {binary} {test_name} rc={completed.returncode} ===")
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
            results.append(
                {
                    "binary": binary,
                    "name": test_name,
                    "returncode": completed.returncode,
                    "duration": duration,
                    "output": completed.stdout,
                }
            )
            if completed.returncode != 0:
                break
        if results and results[-1]["returncode"] != 0:
            break

    failures = sum(int(item["returncode"] != 0) for item in results)
    suite = ET.Element(
        "testsuite",
        {
            "name": "libuv-native-selected",
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
                "classname": f"libuv.{item['binary']}",
                "name": str(item["name"]),
                "time": f"{float(item['duration']):.3f}",
            },
        )
        ET.SubElement(case, "system-out").text = str(item["output"])
        if int(item["returncode"]) != 0:
            failure = ET.SubElement(
                case,
                "failure",
                {"message": f"native test exited {item['returncode']}"},
            )
            failure.text = str(item["output"])[-16000:]
    report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)

    expected = len(BINARIES) * len(TESTS)
    return 0 if len(results) == expected and failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
