"""Run one declared Java ``main`` test unchanged and attest its execution.

This module is intentionally a process wrapper, not an in-process Java
adapter.  The declared class remains the main class of its own JVM, so
``System.exit``, stack inspection, system properties, cwd, uid, and the
application class loader keep their original semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


ATTESTATION_SCHEMA = "declared-java-test-attestation/v1"
ATTESTATION_ADAPTER_ID = "declared-java-test-evidence/v2"
_CLASS_RE = re.compile(r"^[A-Za-z_$][\w.$]*$")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _argv_sha256(argv: Sequence[str]) -> str:
    encoded = json.dumps(
        list(argv),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_report_name(class_name: str) -> str:
    return "ATTEST-" + re.sub(r"[^A-Za-z0-9_.-]", "_", class_name) + ".json"


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError as exc:
        raise ValueError("declared main runner requires '--' before the Java argv") from exc
    parser = argparse.ArgumentParser(prog="java_test_attestation_runner")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--declared-class", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--contract-command-sha256", required=True)
    parsed = parser.parse_args(values[:separator])
    return parsed, values[separator + 1 :]


def _validate_java_argv(command: Sequence[str], declared_class: str) -> None:
    if not _CLASS_RE.fullmatch(declared_class):
        raise ValueError(f"invalid declared Java class: {declared_class}")
    if len(command) != 4 or command[1] not in {"-cp", "-classpath"}:
        raise ValueError("declared main runner accepts only 'java -cp CLASSPATH CLASS'")
    if Path(command[0]).name != "java":
        raise ValueError("declared main runner command is not a Java launcher")
    if command[3] != declared_class:
        raise ValueError(
            f"declared main class mismatch: expected {declared_class}, got {command[3]}"
        )


def run(argv: Sequence[str]) -> int:
    parsed, command = _parse_args(argv)
    _validate_java_argv(command, parsed.declared_class)
    source = Path(parsed.source).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"declared Java test source is missing: {source}")
    command_hash = str(parsed.contract_command_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", command_hash):
        raise ValueError("invalid frozen sample-test command hash")

    started_ns = time.time_ns()
    completed = subprocess.run(command, check=False)
    ended_ns = time.time_ns()
    if completed.returncode != 0:
        return int(completed.returncode)

    report_dir = Path(parsed.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ATTESTATION_SCHEMA,
        "adapter_id": ATTESTATION_ADAPTER_ID,
        "evidence_kind": "declared_main",
        "declared_class": parsed.declared_class,
        "source_sha256": _sha256_file(source),
        "contract_command_sha256": command_hash,
        "argv_sha256": _argv_sha256(command),
        "cwd": str(Path.cwd().resolve()),
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "started_ns": started_ns,
        "ended_ns": ended_ns,
        "returncode": 0,
        "executions": 1,
    }
    report = report_dir / _safe_report_name(parsed.declared_class)
    temporary = report.with_name(f".{report.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report)
    print(
        "DECLARED_MAIN_ATTESTATION "
        f"class={parsed.declared_class} executions=1 report={report}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except (OSError, ValueError) as exc:
        print(f"DECLARED_MAIN_ATTESTATION_ERROR: {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
