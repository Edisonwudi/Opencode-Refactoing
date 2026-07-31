#!/usr/bin/env python3
"""End-to-end checks for the schema-v3 detector finding contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"


def _run(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "runtime" / "python")}
    return subprocess.run(
        [sys.executable, str(BRIDGE), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _bridge(project: Path, command: str, *extra: str) -> dict:
    result = _run(
        project,
        command,
        "--project-root",
        str(project),
        "--language",
        "java",
        "--smell",
        "mysterious_name",
        "--location",
        "Fixture.java:method=target|line=2",
        *extra,
    )
    payload = json.loads(result.stdout)
    payload["_returncode"] = result.returncode
    return payload


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="finding-contract-v3-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(
            "class Fixture {\n"
            "  void target() { int tmp = 1; Object obj = tmp; System.out.println(obj); }\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "add", "Fixture.java"], cwd=project, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=finding-contract-self-check",
                "-c",
                "user.email=finding-contract@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=project,
            check=True,
        )

        forged = _bridge(
            project,
            "capture-baseline",
            "--target-context-json",
            '{"symbol_name":"tmp","score":99}',
        )
        assert forged["_returncode"] != 0 and "cannot contain" in str(forged.get("error", "")).lower(), forged

        ambiguous = _bridge(project, "capture-baseline")
        assert ambiguous["_returncode"] != 0 and "TARGET_AMBIGUOUS" in str(ambiguous.get("error", "")), ambiguous

        captured = _bridge(
            project,
            "capture-baseline",
            "--target-context-json",
            '{"symbol_kind":"local","symbol_name":"tmp"}',
        )
        assert captured["_returncode"] == 0, captured
        contract = captured["finding_contract"]
        assert captured["metrics"]["candidate_count"] == 1, captured
        assert contract["entity_identity"]["symbol_name"] == "tmp", contract
        assert contract["detector_profile_hash"], contract

        unchanged_without_evidence = _bridge(
            project,
            "verify",
            "--verification-mode",
            "local",
            "--skip-build-test",
        )
        first = unchanged_without_evidence["smell_guard"]["results"][0]
        assert first["details"]["reason"] == "EDIT_REQUIRED", unchanged_without_evidence

        source.write_text(
            "\n\nclass Fixture {\n"
            "  void target() { int temporaryValue = 1; Object obj = temporaryValue; System.out.println(obj); }\n"
            "}\n",
            encoding="utf-8",
        )
        resolved = _bridge(
            project,
            "verify",
            "--verification-mode",
            "local",
            "--skip-build-test",
        )
        assert resolved.get("status") == "PASS", resolved
        assert resolved["checkpoint"]["current_metrics"]["finding_present"] is False, resolved

        baseline_path = next(project.glob(".smell-artifacts/checkpoints/*/c000-baseline/manifest.json"))
        manifest = json.loads(baseline_path.read_text(encoding="utf-8"))
        manifest["finding_contract"]["detector_profile_hash"] = "changed-profile"
        baseline_path.write_text(json.dumps(manifest), encoding="utf-8")
        mismatch = _bridge(
            project,
            "verify",
            "--verification-mode",
            "local",
            "--skip-build-test",
        )
        mismatch_result = mismatch["smell_guard"]["results"][0]
        assert mismatch_result["details"]["reason"] == "DETECTOR_PROFILE_MISMATCH", mismatch

        manifest["schema_version"] = 2
        baseline_path.write_text(json.dumps(manifest), encoding="utf-8")
        old_schema = _bridge(
            project,
            "capture-baseline",
            "--target-context-json",
            '{"symbol_kind":"local","symbol_name":"tmp"}',
        )
        assert old_schema["_returncode"] != 0 and "CHECKPOINT_SCHEMA_MISMATCH" in str(old_schema.get("error", "")), old_schema

    print(
        "finding-contract-v3-self-check PASS "
        "forged_metrics=REJECTED ambiguous=REJECTED evidence_free_tracking=PASS "
        "line_drift=PASS profile_mismatch=REJECTED schema_v2=RECAPTURE_REQUIRED"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
