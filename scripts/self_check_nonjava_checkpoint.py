#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"


def run(project: Path, *args: str) -> dict:
    bridge_args = [*args, "--output-detail", "audit"]
    result = subprocess.run(
        [sys.executable, str(BRIDGE), *bridge_args, "--config", str(CONFIG)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def source(lines: int) -> str:
    body = "\n".join(f"    value += {index}" for index in range(lines))
    return f"def target(a, b, c, d, e, f):\n    value = 0\n{body}\n    return value\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nonjava-checkpoint-") as raw:
        project = Path(raw)
        target = project / "demo.py"
        target.write_text(source(65), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.email", "self-check@example.invalid"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Self Check"], cwd=project, check=True)
        subprocess.run(["git", "add", "demo.py"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=project, check=True)
        baseline_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        common = (
            "--project-root", str(project),
            "--language", "python",
            "--smell", "long_method",
            "--location", f"{target}:method=target|line=1",
        )
        baseline = run(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        unchanged = run(project, "verify", *common, "--skip-build-test")
        assert unchanged["success"] is False, unchanged
        assert unchanged["checkpoint"]["delta"]["has_production_diff"] is False, unchanged
        target.write_text(
            source(2)
            + "\ndef unrelated_helper(a, b, c, d, e, f, g):\n"
            + "    return a + b + c + d + e + f + g\n",
            encoding="utf-8",
        )
        reduced = run(project, "verify", *common, "--skip-build-test")
        assert reduced["success"] is True, reduced
        assert reduced["status"] == "PASS", reduced
        assert (
            "cross_smell_regression"
            not in reduced["checkpoint"]["current_metrics"]
        ), reduced
        assert reduced["checkpoint"]["baseline_project_commit"] == baseline_commit, reduced
        assert reduced["snapshot"]["base_commit"] == baseline_commit, reduced
        delta = reduced["checkpoint"]["delta"]
        assert delta["has_production_diff"] is True and delta["metric_progress"] is True, reduced

    print("Non-Java checkpoint self-check passed: unchanged=0 reduced=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
