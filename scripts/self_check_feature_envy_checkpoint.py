#!/usr/bin/env python3
"""End-to-end self-check for Feature Envy baseline/checkpoint gating."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"

BASELINE_SOURCE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class Subject {
  void target(Collaborator receiver) {
    receiver.a();
    receiver.b();
    receiver.c();
    receiver.d();
  }
}
"""

REFACTORED_SOURCE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
  void doWork() {
    b();
    c();
  }
}
class Subject {
  void target(Collaborator receiver) {
    receiver.a();
    receiver.doWork();
    receiver.d();
  }
}
"""


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(root: Path, env: dict[str, str], command: str) -> dict[str, object]:
    location = "Fixture.java:method=target|line=8"
    args = [
        sys.executable,
        str(BRIDGE),
        command,
        "--project-root",
        str(root),
        "--language",
        "java",
        "--smell",
        "feature_envy",
        "--location",
        location,
        "--smell-evidence",
        "source=self-check; envied_type=Collaborator; label_status=review_candidate_not_gold",
    ]
    if command == "verify":
        args.extend(["--verification-mode", "local", "--skip-build-test"])
    result = _run(args, ROOT, env)
    if result.returncode != 0:
        raise AssertionError(f"bridge {command} failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="feature-envy-checkpoint-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(BASELINE_SOURCE, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "runtime" / "python")
        for command in (["git", "init", "-q"], ["git", "add", "Fixture.java"]):
            result = _run(command, project, env)
            if result.returncode != 0:
                raise AssertionError(result.stderr)
        result = _run(
            [
                "git",
                "-c",
                "user.name=checkpoint-self-check",
                "-c",
                "user.email=checkpoint@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            project,
            env,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

        baseline = _bridge(project, env, "capture-baseline")
        metrics = baseline.get("metrics") or {}
        if metrics.get("expected_receiver_access") != 4 or metrics.get("strict_detector_hit") is not True:
            raise AssertionError(f"unexpected weak baseline metrics: {metrics}")

        unchanged = _bridge(project, env, "verify")
        if unchanged.get("status") != "SMELL_GUARD_FAILED":
            raise AssertionError(f"unchanged baseline unexpectedly passed: {unchanged}")
        unchanged_details = unchanged["smell_guard"]["results"][0]["details"]
        if unchanged_details.get("reason") != "EDIT_REQUIRED":
            raise AssertionError(f"missing EDIT_REQUIRED result: {unchanged_details}")

        source.write_text(REFACTORED_SOURCE, encoding="utf-8")
        repaired = _bridge(project, env, "verify")
        if repaired.get("status") != "PASS" or repaired.get("success") is not True:
            raise AssertionError(f"metric-improving refactor did not pass: {repaired}")
        delta = repaired["checkpoint"]["delta"]["expected_receiver_access"]
        if delta.get("before") != 4 or delta.get("after") != 3 or delta.get("required_reduction") != 1:
            raise AssertionError(f"unexpected metric delta: {delta}")

        checkpoint_root = project / ".smell-artifacts" / "checkpoints"
        manifests = sorted(checkpoint_root.glob("*/c*-verify/manifest.json"))
        if len(manifests) != 2:
            raise AssertionError(f"expected two verify checkpoints, found {manifests}")
        print(
            "feature-envy-checkpoint-self-check PASS "
            "baseline=4 strict_hit=true unchanged=EDIT_REQUIRED refactored=4->3 required=1"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
