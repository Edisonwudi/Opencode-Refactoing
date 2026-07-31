#!/usr/bin/env python3
"""End-to-end checks for the three clear syntactic checkpoint metrics."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java.syntactic_detector import compute_cognitive_complexity  # noqa: E402


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(project: Path, env: dict[str, str], command: str, smell: str) -> dict[str, object]:
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
        "Fixture.java:method=target|line=2",
    ]
    if command == "verify":
        args.extend(["--verification-mode", "local", "--skip-build-test"])
    result = _run(args, ROOT, env)
    if result.returncode != 0:
        raise AssertionError(f"{smell} {command}: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _long_method_source(statements: int) -> str:
    body = "\n".join(f"    consume({index});" for index in range(statements))
    return f"class Fixture {{\n  void target() {{\n{body}\n  }}\n  void consume(int value) {{}}\n}}\n"


NESTED_BEFORE = """\
class Fixture {
  void target(boolean a, boolean b, boolean c, boolean d, boolean e, boolean f) {
    if (a) { if (b) { if (c) { if (d) { if (e) { if (f) { consume(); } } } } } }
  }
  void consume() {}
}
"""
NESTED_AFTER = """\
class Fixture {
  void target(boolean a, boolean b, boolean c, boolean d, boolean e, boolean f) {
    if (a && b && c && d && e && f) { consume(); }
  }
  void consume() {}
}
"""
PARAMS_BEFORE = "class Fixture {\n  void target(int a, int b, int c, int d, int e, int f) {}\n}\n"
PARAMS_AFTER = "class Fixture {\n  void target(int a, int b, int c, int d, int e) {}\n}\n"
INTERFACE_PARAMS_BEFORE = (
    "interface Fixture {\n"
    "  void target(int a, int b, int c, int d, int e, int f);\n"
    "}\n"
)
INTERFACE_PARAMS_AFTER = (
    "interface Fixture {\n"
    "  void target(int a, int b, int c, int d, int e);\n"
    "}\n"
)
ANNOTATED_PARAMS_BEFORE = """\
class Fixture {
  @Remote(variants = Variant.both, unreliable = true)
  void target(int a, int b, int c, int d, int e, int f) {}
}
"""
ANNOTATED_PARAMS_AFTER = ANNOTATED_PARAMS_BEFORE.replace(
    ", int e, int f",
    ", int e",
)
PMD_ELSE_CHAIN_BODY = """\
if (value == null || value.equals(null)) {
  writer.write("null");
} else if (value instanceof JSONObject) {
  consume(value);
} else if (value instanceof JSONArray) {
  consume(value);
} else if (value instanceof Map) {
  consume(value);
} else if (value instanceof Collection) {
  consume(value);
} else if (value.getClass().isArray()) {
  consume(value);
} else if (value instanceof Number) {
  consume(value);
} else if (value instanceof Boolean) {
  consume(value);
} else if (value instanceof JSONString) {
  try {
    Object output = convert(value);
    writer.write(output != null ? output.toString() : quote(value.toString()));
  } catch (Exception error) {
    throw new IllegalStateException(error);
  }
} else {
  quote(value.toString(), writer);
}
"""


def _case(smell: str, before: str, after: str, objective: str) -> tuple[float, float]:
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

        baseline = _bridge(project, env, "capture-baseline", smell)
        before_value = float((baseline["metrics"]["objectives"])[objective])
        unchanged = _bridge(project, env, "verify", smell)
        details = unchanged["smell_guard"]["results"][0]["details"]
        assert unchanged["status"] == "SMELL_GUARD_FAILED" and details["reason"] == "EDIT_REQUIRED", unchanged

        source.write_text(after, encoding="utf-8")
        repaired = _bridge(project, env, "verify", smell)
        if repaired.get("status") != "PASS":
            raise AssertionError(f"{smell} repaired source did not pass: {repaired}")
        delta = repaired["checkpoint"]["delta"]["objectives"][objective]
        after_value = float(delta["after"])
        assert after_value < before_value, delta
        return before_value, after_value


def _missing_baseline_fails_closed() -> None:
    """A threshold-clean original source must not PASS without its checkpoint."""
    with tempfile.TemporaryDirectory(prefix="checkpoint-missing-baseline-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        # Five parameters is accepted by the ordinary max_params=5 detector.
        # Before the fail-closed fix, verify therefore returned PASS when c000
        # was absent even though this is still the untouched original source.
        source.write_text(PARAMS_AFTER, encoding="utf-8")
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

        verified = _bridge(project, env, "verify", "long_parameter_list")
        details = verified["smell_guard"]["results"][0]["details"]
        assert verified["status"] == "SMELL_GUARD_FAILED", verified
        assert verified["checkpoint"]["reason"] == "baseline_checkpoint_missing", verified
        assert details["reason"] == "BASELINE_CHECKPOINT_MISSING", verified


def main() -> int:
    pmd_score = compute_cognitive_complexity(PMD_ELSE_CHAIN_BODY, "writeValue")
    assert pmd_score == 29, f"PMD else-chain metric drifted: {pmd_score} != 29"
    _missing_baseline_fails_closed()
    results = {
        "long_method": _case("long_method", _long_method_source(65), _long_method_source(2), "ast_ncss"),
        "nested_complexity": _case("nested_complexity", NESTED_BEFORE, NESTED_AFTER, "cognitive_complexity"),
        "long_parameter_list": _case("long_parameter_list", PARAMS_BEFORE, PARAMS_AFTER, "parameter_count"),
        "long_parameter_list_interface": _case(
            "long_parameter_list",
            INTERFACE_PARAMS_BEFORE,
            INTERFACE_PARAMS_AFTER,
            "parameter_count",
        ),
        "long_parameter_list_annotated": _case(
            "long_parameter_list",
            ANNOTATED_PARAMS_BEFORE,
            ANNOTATED_PARAMS_AFTER,
            "parameter_count",
        ),
    }
    rendered = " ".join(f"{name}={before:g}->{after:g}" for name, (before, after) in results.items())
    print(
        "checkpoint-syntactic-adapters-self-check PASS "
        f"unchanged_pass=0 missing_baseline_pass=0 pmd_else_chain={pmd_score} {rendered}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
