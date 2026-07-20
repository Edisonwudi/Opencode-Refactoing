#!/usr/bin/env python3
"""End-to-end checks for switch, mysterious-name, and dead-code checkpoints."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(
    project: Path,
    env: dict[str, str],
    command: str,
    smell: str,
    location: str,
    evidence: str,
) -> dict[str, object]:
    args = [
        sys.executable,
        str(BRIDGE),
        command,
        "--project-root",
        str(project),
        "--language",
        "java",
        "--smell",
        smell,
        "--location",
        location,
        "--smell-evidence",
        evidence,
    ]
    if command == "verify":
        args.extend(["--verification-mode", "local", "--skip-build-test"])
    result = _run(args, ROOT, env)
    if result.returncode != 0:
        raise AssertionError(f"{smell} {command}: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _case(
    smell: str,
    before: str,
    after: str,
    location: str,
    evidence: str,
    objective: str,
) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix=f"checkpoint-{smell}-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(before, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "runtime" / "python")
        for command in (["git", "init", "-q"], ["git", "add", "Fixture.java"]):
            result = _run(command, project, env)
            if result.returncode != 0:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project, env)
        if result.returncode != 0:
            raise AssertionError(result.stderr)

        baseline = _bridge(project, env, "capture-baseline", smell, location, evidence)
        before_value = float(baseline["metrics"]["objectives"][objective])
        unchanged = _bridge(project, env, "verify", smell, location, evidence)
        details = unchanged["smell_guard"]["results"][0]["details"]
        assert unchanged["status"] == "SMELL_GUARD_FAILED" and details["reason"] == "EDIT_REQUIRED", unchanged

        source.write_text(after, encoding="utf-8")
        repaired = _bridge(project, env, "verify", smell, location, evidence)
        if repaired.get("status") != "PASS":
            raise AssertionError(f"{smell} repaired source did not pass: {repaired}")
        delta = repaired["checkpoint"]["delta"]["objectives"][objective]
        after_value = float(delta["after"])
        assert after_value < before_value, delta
        return before_value, after_value


SWITCH_BEFORE = """\
class Fixture {
  int target(int value) {
    switch (value) {
      case 0: return 0;
      case 1: return 1;
      case 2: return 2;
      case 3: return 3;
      case 4: return 4;
      case 5: return 5;
      case 6: return 6;
      case 7: return 7;
      case 8: return 8;
      case 9: return 9;
      case 10: return 10;
      case 11: return 11;
      case 12: return 12;
      default: return -1;
    }
  }
}
"""
SWITCH_AFTER = """\
class Fixture {
  int target(int value) {
    return value >= 0 && value <= 12 ? value : -1;
  }
}
"""
MYSTERIOUS_BEFORE = "class Fixture {\n  void aa() {}\n}\n"
MYSTERIOUS_AFTER = "class Fixture {\n  void describe() {}\n}\n"
DEAD_BEFORE = "class Fixture {\n  private void unusedHelper() {}\n  void live() {}\n}\n"
DEAD_AFTER = "class Fixture {\n  void live() {}\n}\n"


def main() -> int:
    results = {
        "switch_statements": _case(
            "switch_statements",
            SWITCH_BEFORE,
            SWITCH_AFTER,
            "Fixture.java:method=target|line=2",
            "switch_count=1; case_count=13; density=13.00",
            "switch_case_count",
        ),
        "mysterious_name": _case(
            "mysterious_name",
            MYSTERIOUS_BEFORE,
            MYSTERIOUS_AFTER,
            "Fixture.java:method=aa|line=2",
            "kind=method; name=aa; reason=too_short",
            "target_suspicious_name_present",
        ),
        "dead_code": _case(
            "dead_code",
            DEAD_BEFORE,
            DEAD_AFTER,
            "Fixture.java:2",
            "kind=unused_private_method; method=unusedHelper(); refs=0",
            "target_declaration_present",
        ),
    }
    rendered = " ".join(f"{name}={before:g}->{after:g}" for name, (before, after) in results.items())
    print(f"checkpoint-remaining-adapters-self-check PASS unchanged_pass=0 {rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
