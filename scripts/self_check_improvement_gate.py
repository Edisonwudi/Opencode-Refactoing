#!/usr/bin/env python3
"""Regression checks for the contract improvement outcome in bridge verify.

Semantics under test: a real production diff that reduces any valid target
metric vs baseline is recorded as IMPROVED, never PASS, while the
product detector still reports the smell. Without a diff or metric
reduction the ordinary failure semantics remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(BRIDGE.parent))

from smell_bridge import _requires_structural_resolution  # noqa: E402


def _method(index: int, controls: int) -> str:
    statements = "\n".join(f"    if (value > {n}) value--;" for n in range(controls))
    return f"  void method{index}(int value) {{\n{statements}\n  }}"


def _class_source(methods: int, controls: int, padding: int) -> str:
    body = "\n".join(_method(i, controls) for i in range(methods))
    pad = "\n".join("  // padding" for _ in range(padding))
    return f"class Smelly {{\n{body}\n{pad}\n}}\n"


def _bridge(
    project: Path,
    subcommand: str,
    *,
    smell: str = "god_class",
    location: str = "Smelly.java:1",
    evidence: str = "nom=12;wmc=36;loc=110;atfd=0;class=Smelly",
    target_context: dict[str, str] | None = None,
) -> dict:
    cmd = [
        sys.executable,
        str(BRIDGE),
        subcommand,
        "--project-root",
        str(project),
        "--smell",
        smell,
        "--location",
        location,
        "--smell-evidence",
        evidence,
    ]
    if target_context:
        cmd += ["--target-context-json", json.dumps(target_context, sort_keys=True)]
    if subcommand == "verify":
        cmd += ["--skip-build-test", "--no-snapshot"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bridge failed: {proc.stderr[-500:]}"
    return json.loads(proc.stdout)


REFUSED_BASELINE = """\
interface Capability {
  Object target();
}
class ExistingSibling implements Capability {
  public Object target() { throw new UnsupportedOperationException(); }
}
class Subject implements Capability {
  public Object target() { throw new UnsupportedOperationException(); }
  public Object supported() { return new Object(); }
}
"""

REFUSED_RESOLVED = """\
interface Capability {
  Object target();
}
class ExistingSibling implements Capability {
  public Object target() { throw new UnsupportedOperationException(); }
}
class Subject {
  public Object supported() { return new Object(); }
}
"""

REFUSED_RELOCATED = REFUSED_RESOLVED + """\
class CompatibilityShell implements Capability {
  public Object target() { throw new UnsupportedOperationException(); }
}
"""


def _refused_bequest_baseline_delta_case() -> None:
    with tempfile.TemporaryDirectory(prefix="improvement-gate-refused-") as tmp:
        root = Path(tmp)
        source = root / "Fixture.java"
        source.write_text(REFUSED_BASELINE, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"],
            cwd=root,
            check=True,
        )
        common = {
            "smell": "refused_bequest",
            "location": "Fixture.java:method=target|line=8",
            "evidence": "",
            "target_context": {"target_class": "Subject", "parent": "Capability"},
        }
        baseline = _bridge(root, "capture-baseline", **common)
        assert baseline.get("success") is True, baseline

        # A rejecting sibling that was already present at c000 is not a
        # relocation of the target finding.
        source.write_text(REFUSED_RESOLVED, encoding="utf-8")
        resolved = _bridge(root, "verify", **common)
        assert resolved.get("status") == "PASS", resolved

        # A newly introduced equivalent rejecting override is a structural
        # failure. Even though the target metric fell from 1 to 0, it must not
        # be reported as IMPROVED.
        source.write_text(REFUSED_RELOCATED, encoding="utf-8")
        relocated = _bridge(root, "verify", **common)
        assert relocated.get("status") == "SMELL_GUARD_FAILED", relocated
        assert relocated.get("resolution") == "unresolved", relocated
        results = (relocated.get("smell_guard") or {}).get("results") or []
        assert any("moved to another type" in str(item.get("message") or "") for item in results), results


def main() -> int:
    assert _requires_structural_resolution(
        "refused_bequest",
        "parents=Parent; structural_expectation=capability_split",
    )
    assert _requires_structural_resolution(
        "refused_bequest",
        "parents=Parent; structural_expectation=rejecting_override_removed",
    )
    assert _requires_structural_resolution(
        "refused_bequest",
        "parents=Parent",
    )
    assert _requires_structural_resolution("code_clone_type1", "")
    assert not _requires_structural_resolution(
        "god_class",
        "structural_expectation=capability_split",
    )

    with tempfile.TemporaryDirectory(prefix="improvement-gate-") as tmp:
        root = Path(tmp)
        (root / "Smelly.java").write_text(_class_source(12, 3, 60), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"],
            cwd=root,
            check=True,
        )

        baseline = _bridge(root, "capture-baseline")
        assert baseline.get("captured") is True or baseline.get("success") is True, baseline

        # 1) No edit: detector still reports, no diff -> must stay a failure.
        first = _bridge(root, "verify")
        assert first["success"] is False, first
        assert first["status"] == "SMELL_GUARD_FAILED", first["status"]
        assert first.get("resolution") == "unresolved", first.get("resolution")

        # 2) Improvement edit: drop two methods and ten padding lines. All of
        # nom/wmc/loc shrink but the detector must still report the class.
        (root / "Smelly.java").write_text(_class_source(10, 3, 50), encoding="utf-8")
        second = _bridge(root, "verify")
        guard = second.get("smell_guard") or {}
        assert guard.get("success") is False, "detector should still report the class"
        assert second["success"] is False, second
        assert second["accepted"] is False, second
        assert second["progress"] is True, second
        assert second["status"] == "IMPROVED", second["status"]
        assert second.get("resolution") == "improved", second.get("resolution")
        checkpoint = second.get("checkpoint") or {}
        assert checkpoint.get("accepted") is False, checkpoint
        assert checkpoint.get("best_partial_eligible") is False, checkpoint

        # 3) Cosmetic-only edit on the ORIGINAL baseline class (comment change
        # only): production diff exists but every metric is back at baseline,
        # so there is no reduction and the improvement gate must NOT pass.
        (root / "Smelly.java").write_text(_class_source(12, 3, 60).replace("// padding", "// pad"), encoding="utf-8")
        third = _bridge(root, "verify")
        assert third["success"] is False, third
        assert third["status"] == "SMELL_GUARD_FAILED", third["status"]

    _refused_bequest_baseline_delta_case()

    print("improvement-gate self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
