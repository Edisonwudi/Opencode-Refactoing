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
  static final Collaborator RECEIVER = new Collaborator();
  void target() {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
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
  static final Collaborator RECEIVER = new Collaborator();
  void target() {
    RECEIVER.a();
    RECEIVER.doWork();
    RECEIVER.d();
  }
}
"""

RELOCATED_SOURCE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class Subject {
  static final Collaborator RECEIVER = new Collaborator();
  void target() {
    relocatedWork();
  }
  void relocatedWork() {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
}
"""

RESOLVED_SOURCE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class Subject {
  void target() {}
}
"""


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(root: Path, env: dict[str, str], command: str) -> dict[str, object]:
    location = "Fixture.java:method=target|line=9"
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
        if (
            repaired.get("status") != "IMPROVED"
            or repaired.get("success") is not False
            or repaired.get("accepted") is not False
            or repaired.get("progress") is not True
            or repaired.get("resolution") != "improved"
        ):
            raise AssertionError(f"metric-improving refactor was misclassified: {repaired}")
        delta = repaired["checkpoint"]["delta"]["objectives"]["expected_receiver_access"]
        if delta.get("before") != 4 or delta.get("after") != 3 or delta.get("absolute_reduction") != 1:
            raise AssertionError(f"unexpected metric delta: {delta}")
        if repaired["checkpoint"].get("best_partial_eligible") is not False:
            raise AssertionError(
                f"unchecked checkpoint became restorable: {repaired['checkpoint']}"
            )
        if repaired["checkpoint"].get("best_checkpoint") is not False:
            raise AssertionError(
                f"unchecked PASS-like checkpoint became best: {repaired['checkpoint']}"
            )

        checkpoint_root = project / ".smell-artifacts" / "checkpoints"
        manifests = sorted(checkpoint_root.glob("*/c*-verify/manifest.json"))
        if len(manifests) != 2:
            raise AssertionError(f"expected two verify checkpoints, found {manifests}")
        task_states = sorted(checkpoint_root.glob("*/task-state.json"))
        if len(task_states) != 1:
            raise AssertionError(f"expected one checkpoint state, found {task_states}")
        state = json.loads(task_states[0].read_text(encoding="utf-8"))
        if state.get("best_partial") is not None:
            raise AssertionError(f"unchecked checkpoint was retained as best partial: {state}")
        repaired_manifest_path = manifests[-1]
        repaired_manifest = json.loads(repaired_manifest_path.read_text(encoding="utf-8"))
        production_patch = repaired_manifest_path.parent / str(
            repaired_manifest.get("production_patch") or "production.patch"
        )
        if not production_patch.is_file():
            raise AssertionError(f"production-only patch is missing: {production_patch}")
        patch_text = production_patch.read_text(encoding="utf-8")
        if "Fixture.java" not in patch_text or ".smell-artifacts" in patch_text:
            raise AssertionError(f"production-only patch has the wrong scope: {patch_text[:500]}")

        source.write_text(RELOCATED_SOURCE, encoding="utf-8")
        relocated = _bridge(project, env, "verify")
        if relocated.get("status") != "SMELL_GUARD_FAILED":
            raise AssertionError(f"same-owner Feature Envy relocation unexpectedly passed: {relocated}")
        relocated_delta = relocated["checkpoint"]["delta"]
        if relocated_delta.get("reason") != "SEMANTIC_CONTRACT_REGRESSION":
            raise AssertionError(f"relocation used the wrong checkpoint reason: {relocated_delta}")
        regressions = relocated_delta.get("semantic_contract", {}).get("regressions", [])
        if not any(
            str(item).startswith("same_owner_receiver_finding_relocated:")
            for item in regressions
        ):
            raise AssertionError(f"relocated finding was not identified: {relocated_delta}")

        source.write_text(RESOLVED_SOURCE, encoding="utf-8")
        resolved = _bridge(project, env, "verify")
        if (
            resolved.get("status") != "PASS"
            or resolved.get("accepted") is not True
            or resolved["checkpoint"].get("best_checkpoint") is not False
            or resolved["checkpoint"].get("restorable") is not False
        ):
            raise AssertionError(f"unchecked resolved checkpoint became restorable: {resolved}")
        final_state = json.loads(task_states[0].read_text(encoding="utf-8"))
        if final_state.get("best") is not None or final_state.get("best_partial") is not None:
            raise AssertionError(f"unchecked checkpoint left a recovery pointer: {final_state}")
        print(
            "feature-envy-checkpoint-self-check PASS "
            "baseline=4 strict_hit=true unchanged=EDIT_REQUIRED "
            "refactored=4->3 status=IMPROVED relocated=REJECTED "
            "resolved=PASS unchecked_restorable=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
