#!/usr/bin/env python3
"""Checkpoint patches must round-trip non-UTF-8 source bytes byte-exactly.

Regression guard for the POCO long_parameter_list failure where a Latin-1
byte (0xED) inside a C++ source crashed the strict UTF-8 decoding of
``git diff --binary`` during checkpoint capture.  The fix decodes git output
with ``errors="surrogateescape"`` and persists patches with the same policy,
so undecodable bytes survive the text round-trip unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"


def run_bridge(project: Path, *args: str) -> dict:
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


def git(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr!r}")
    return result


def build_source(comment: bytes, body_lines: list[bytes]) -> bytes:
    lines = [
        b"/* demo translation unit for checkpoint encoding checks */",
        b"int target(int a, int b) {",
        b"    int value = 0;",
        b"    " + comment,
        *body_lines,
        b"    return value;",
        b"}",
        b"",
    ]
    return b"\n".join(lines)


def production_patch_path(project: Path, checkpoint_id: str) -> Path:
    matches = sorted(
        project.glob(f".smell-artifacts/checkpoints/*/{checkpoint_id}-verify/production.patch")
    )
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one production.patch for {checkpoint_id}: {matches}")
    return matches[0]


def assert_patch_restores(project: Path, target: Path, patch_path: Path) -> None:
    """A fresh clone of the baseline commit plus the patch == current bytes."""
    with tempfile.TemporaryDirectory(prefix="checkpoint-restore-") as raw:
        restore = Path(raw) / "restore"
        subprocess.run(
            ["git", "clone", "-q", str(project), str(restore)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        probe = git(restore, "apply", "--check", str(patch_path), check=False)
        assert probe.returncode == 0, f"git apply --check failed: {probe.stderr!r}"
        git(restore, "apply", str(patch_path))
        restored = (restore / target.name).read_bytes()
        assert restored == target.read_bytes(), "patched clone diverges from the working tree bytes"


def run_scenario(name: str, comment: bytes, *, expect_non_utf8: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=f"checkpoint-{name}-") as raw:
        project = Path(raw)
        target = project / "legacy.c"
        baseline_body = [f"    value += {index};".encode("ascii") for index in range(65)]
        target.write_bytes(build_source(comment + b" original", baseline_body))
        git(project, "init", "-q")
        git(project, "config", "user.email", "self-check@example.invalid")
        git(project, "config", "user.name", "Self Check")
        git(project, "add", "legacy.c")
        git(project, "commit", "-qm", "baseline")
        baseline_commit = git(project, "rev-parse", "HEAD").stdout.decode("ascii").strip()

        common = (
            "--project-root", str(project),
            "--language", "c",
            "--smell", "long_method",
            "--location", f"{target}:method=target|line=2",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        assert baseline["metrics"]["objectives"]["ast_ncss"] > 60, baseline

        # An unchanged tree must never pass the checkpoint contract.
        unchanged = run_bridge(project, "verify", *common, "--skip-build-test")
        assert unchanged["success"] is False, unchanged
        assert unchanged["checkpoint"]["delta"]["has_production_diff"] is False, unchanged
        assert unchanged["checkpoint"]["delta"]["reason"] == "EDIT_REQUIRED", unchanged

        # Shrink the function and touch the non-UTF-8 comment line so the raw
        # bytes are forced into the diff as removed/added lines.
        target.write_bytes(build_source(comment + b" trimmed", [b"    value = a + b;"]))
        reduced = run_bridge(project, "verify", *common, "--skip-build-test")
        assert "error" not in reduced, f"bridge wrapped an exception (UnicodeDecodeError regression?): {reduced}"
        assert reduced["success"] is True, reduced
        delta = reduced["checkpoint"]["delta"]
        assert delta["has_production_diff"] is True, reduced
        assert delta["metric_progress"] is True, reduced
        objectives = delta["objectives"]["ast_ncss"]
        assert objectives["before"] > 60 and objectives["after"] < 10, objectives

        checkpoint_id = str(reduced["checkpoint"]["checkpoint_id"])
        patch_path = production_patch_path(project, checkpoint_id)
        patch_bytes = patch_path.read_bytes()
        raw_diff = git(project, "diff", "--binary", baseline_commit, "--", "legacy.c").stdout
        assert patch_bytes == raw_diff.rstrip(b"\n") + b"\n", "stored patch is not byte-identical to git output"
        if expect_non_utf8:
            assert b"\xed" in patch_bytes, "non-UTF-8 byte did not survive into the patch"
            try:
                patch_bytes.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                raise AssertionError("expected the patch to remain non-UTF-8 decodable")
        else:
            patch_bytes.decode("utf-8")  # plain UTF-8 projects must stay strict-decodable
        assert_patch_restores(project, target, patch_path)
        print(f"  scenario {name}: checkpoint={checkpoint_id} patch_bytes={len(patch_bytes)} restore=OK")


def main() -> int:
    print("Non-UTF-8 checkpoint patch self-check")
    # 0xED is the exact byte that crashed the POCO checkpoint (Latin-1 'í').
    run_scenario("nonutf8", b"/* legacy Latin-1 comment: caf\xed na\xefve */", expect_non_utf8=True)
    run_scenario("utf8", "/* regular UTF-8 comment: café naïve */".encode("utf-8"), expect_non_utf8=False)
    print("Non-UTF-8 checkpoint patch self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
