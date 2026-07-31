#!/usr/bin/env python3
"""End-to-end checks for compound God Class and Refused Bequest adapters."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_adapters import capture_metric_snapshot  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402


def _method(index: int, controls: int) -> str:
    statements = "\n".join(f"    if (value > {n}) value--;" for n in range(controls))
    return f"  void method{index}(int value) {{\n{statements}\n  }}"


GOD_BEFORE = "class Candidate {\n" + "\n".join(_method(i, 2) for i in range(10)) \
    + "\n" + "\n".join("  // calibration padding" for _ in range(80)) + "\n}\n"
GOD_AFTER = "class Candidate {\n" + "\n".join(_method(i, 4) for i in range(5)) + "\n}\n"
PARENT = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
  void fourth() {}
  void fifth() {}
}
"""
REFUSED_BEFORE = PARENT + """\
class Child extends Parent {
  @Override void first() { throw new UnsupportedOperationException(); }
  @Override void second() { throw new UnsupportedOperationException(); }
}
"""
REFUSED_AFTER = PARENT + """\
class Child extends Parent {
  @Override void first() { owner.run(); }
  @Override void second() { owner.run(); }
}
"""
NULL_RETURN_BEFORE = """\
interface Packet {
  byte[] toBytes();
}
class ReadOnlyPacket implements Packet {
  public byte[] toBytes() { return null; }
}
"""
NULL_RETURN_AFTER = """\
interface Packet {
  byte[] toBytes();
}
class ReadOnlyPacket implements Packet {
  public byte[] toBytes() { return new byte[0]; }
}
"""


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


def _refused_snapshot(source_text: str, evidence: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="checkpoint-refused-snapshot-") as temp_dir:
        project = Path(temp_dir)
        (project / "Fixture.java").write_text(source_text, encoding="utf-8")
        config = SimpleNamespace(
            project_root=project,
            language="java",
            smell="refused_bequest",
            locations=[
                parse_location_descriptor(
                    "Fixture.java:method=toBytes|line=5",
                    project,
                )
            ],
        )
        return capture_metric_snapshot(config, evidence)


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
    god = _case(
        "god_class", GOD_BEFORE, GOD_AFTER, "Fixture.java:class=Candidate|line=1",
        "class=Candidate; nom=10; nof=0; wmc=20; loc=122; atfd=0", "nom",
    )
    refused = _case(
        "refused_bequest", REFUSED_BEFORE, REFUSED_AFTER, "Fixture.java:method=first|line=9",
        "parent=Parent; refactor_path=implement_contract", "refusal_score",
    )
    refused_null = _case(
        "refused_bequest",
        NULL_RETURN_BEFORE,
        NULL_RETURN_AFTER,
        "Fixture.java:method=toBytes|line=5",
        "parents=Packet; flags=returns_null; refactor_path=implement_contract",
        "rejection_signals",
    )
    evidence_free_null = _refused_snapshot(
        NULL_RETURN_BEFORE,
        "parents=Packet; refactor_path=implement_contract",
    )
    assert evidence_free_null["objectives"]["rejection_signals"] == 1, evidence_free_null
    print(
        "checkpoint-semantic-adapters-self-check PASS unchanged_pass=0 "
        f"god_class_nom={god[0]:g}->{god[1]:g} "
        f"refused_score={refused[0]:g}->{refused[1]:g} "
        f"refused_null_signals={refused_null[0]:g}->{refused_null[1]:g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
