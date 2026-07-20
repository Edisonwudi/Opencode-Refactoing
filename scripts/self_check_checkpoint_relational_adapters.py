#!/usr/bin/env python3
"""End-to-end checks for Data Clumps and Type-1 Clone checkpoint adapters."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"

DATA_BEFORE = """\
class A { void target(boolean confReq, int maxTokSize, int qop) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, int qop) {} }
"""
DATA_AFTER = """\
class A { void target(boolean confReq, int maxTokSize, int qop) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, long qop) {} }
"""
CLONE_BODY = "int total = 0; for (int i = 0; i < 20; i++) { total += i; } if (total > 10) { total--; } consume(total);"
CLONE_BEFORE = f"class Fixture {{\n  void left() {{ {CLONE_BODY} }}\n  void right() {{ {CLONE_BODY} }}\n  void consume(int value) {{}}\n}}\n"
CLONE_AFTER = f"class Fixture {{\n  void left() {{ {CLONE_BODY} }}\n  void right() {{ int total = 0; consume(total); }}\n  void consume(int value) {{}}\n}}\n"


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(project: Path, env: dict[str, str], command: str, smell: str, location: str, evidence: str) -> dict:
    args = [
        sys.executable, str(BRIDGE), command,
        "--project-root", str(project), "--language", "java",
        "--smell", smell, "--location", location, "--smell-evidence", evidence,
    ]
    if command == "verify":
        args.extend(["--verification-mode", "local", "--skip-build-test"])
    result = _run(args, ROOT, env)
    if result.returncode:
        raise AssertionError(f"{smell} {command}: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _case(smell: str, before: str, after: str, location: str, evidence: str, objective: str) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix=f"checkpoint-{smell}-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(before, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "runtime" / "python")}
        for command in (["git", "init", "-q"], ["git", "add", "Fixture.java"]):
            result = _run(command, project, env)
            if result.returncode:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project, env)
        if result.returncode:
            raise AssertionError(result.stderr)
        baseline = _bridge(project, env, "capture-baseline", smell, location, evidence)
        before_value = float(baseline["metrics"]["objectives"][objective])
        unchanged = _bridge(project, env, "verify", smell, location, evidence)
        assert unchanged["smell_guard"]["results"][0]["details"]["reason"] == "EDIT_REQUIRED", unchanged
        source.write_text(after, encoding="utf-8")
        repaired = _bridge(project, env, "verify", smell, location, evidence)
        if repaired.get("status") != "PASS":
            raise AssertionError(f"{smell} repaired source did not pass: {repaired}")
        after_value = float(repaired["checkpoint"]["delta"]["objectives"][objective]["after"])
        assert after_value < before_value
        return before_value, after_value


def main() -> int:
    data = _case(
        "data_clumps", DATA_BEFORE, DATA_AFTER,
        "Fixture.java:method=target|line=1",
        "group=boolean:confreq|int:maxtoksize|int:qop; occurrences=3",
        "occurrence_count",
    )
    clone = _case(
        "code_clone_type1", CLONE_BEFORE, CLONE_AFTER,
        "Fixture.java:method=left|line=2 <-> Fixture.java:method=right|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    print(
        "checkpoint-relational-adapters-self-check PASS unchanged_pass=0 "
        f"data_clumps={data[0]:g}->{data[1]:g} code_clone_type1={clone[0]:g}->{clone[1]:g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
