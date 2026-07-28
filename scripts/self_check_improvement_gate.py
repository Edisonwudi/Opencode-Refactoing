#!/usr/bin/env python3
"""Regression checks for the contract improvement gate in bridge verify.

Semantics under test: a real production diff that reduces any valid target
metric vs baseline is an accepted improvement (PASS, resolution="improved")
even when the strict detector still reports the smell. Without a diff or
without metric reduction the old failure semantics must be unchanged.
Refused Bequest rows with an explicit structural expectation are stricter:
metric improvement is progress-only and cannot become final acceptance.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(BRIDGE.parent))

from smell_bridge import _requires_strict_smell_resolution  # noqa: E402


def _method(index: int, controls: int) -> str:
    statements = "\n".join(f"    if (value > {n}) value--;" for n in range(controls))
    return f"  void method{index}(int value) {{\n{statements}\n  }}"


def _class_source(methods: int, controls: int, padding: int) -> str:
    body = "\n".join(_method(i, controls) for i in range(methods))
    pad = "\n".join("  // padding" for _ in range(padding))
    return f"class Smelly {{\n{body}\n{pad}\n}}\n"


def _bridge(project: Path, subcommand: str) -> dict:
    cmd = [
        sys.executable,
        str(BRIDGE),
        subcommand,
        "--project-root",
        str(project),
        "--smell",
        "god_class",
        "--location",
        "Smelly.java:1",
        "--smell-evidence",
        "nom=12;wmc=36;loc=110;atfd=0;class=Smelly",
    ]
    if subcommand == "verify":
        cmd += ["--skip-build-test", "--no-snapshot"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bridge failed: {proc.stderr[-500:]}"
    import json

    return json.loads(proc.stdout)


def main() -> int:
    assert _requires_strict_smell_resolution(
        "refused_bequest",
        "parents=Parent; structural_expectation=capability_split",
    )
    assert _requires_strict_smell_resolution(
        "refused_bequest",
        "parents=Parent; structural_expectation=rejecting_override_removed",
    )
    assert not _requires_strict_smell_resolution(
        "refused_bequest",
        "parents=Parent",
    )
    assert not _requires_strict_smell_resolution(
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
        assert first.get("resolution", "") == "", first.get("resolution")

        # 2) Improvement edit: drop two methods and ten padding lines. All of
        # nom/wmc/loc shrink but the detector must still report the class.
        (root / "Smelly.java").write_text(_class_source(10, 3, 50), encoding="utf-8")
        second = _bridge(root, "verify")
        guard = second.get("smell_guard") or {}
        assert guard.get("success") is False, "detector should still report the class"
        assert second["success"] is True, second
        assert second["status"] == "PASS", second["status"]
        assert second.get("resolution") == "improved", second.get("resolution")
        checkpoint = second.get("checkpoint") or {}
        assert checkpoint.get("accepted") is True, checkpoint

        # 3) Cosmetic-only edit on the ORIGINAL baseline class (comment change
        # only): production diff exists but every metric is back at baseline,
        # so there is no reduction and the improvement gate must NOT pass.
        (root / "Smelly.java").write_text(_class_source(12, 3, 60).replace("// padding", "// pad"), encoding="utf-8")
        third = _bridge(root, "verify")
        assert third["success"] is False, third
        assert third["status"] == "SMELL_GUARD_FAILED", third["status"]

    print("improvement-gate self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
