#!/usr/bin/env python3
"""Focused invariance checks for the Java Feature Envy detector."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.java.semantic_detector import run_java_semantic_detector  # noqa: E402


FIXTURE = """\
class Collaborator {
  void a() {} void b() {} void c() {} void d() {}
}
class Other {
  void a() {} void b() {} void c() {}
}
class Subject {
  void direct(Collaborator receiver) {
    receiver.a();
    receiver.b();
    receiver.c();
    receiver.d();
  }

  void aliased(Collaborator receiver) {
    Collaborator local = receiver;
    local.a();
    local.b();
    local.c();
    local.d();
  }

  void enhancedLoop(java.util.List<Collaborator> receivers) {
    for (Collaborator receiver : receivers) {
      receiver.a();
      receiver.b();
      receiver.c();
      receiver.d();
    }
  }

  void splitAcrossTypes(Collaborator first, Other second) {
    first.a();
    first.b();
    first.c();
    second.a();
    second.b();
    second.c();
  }

  void dominantType(Collaborator first, Other second) {
    first.a();
    first.b();
    first.c();
    first.d();
    second.a();
    second.b();
  }

}
"""


def _metrics(evidence: str) -> tuple[int, int, str]:
    foreign = re.search(r"\bforeign_access=(\d+)", evidence)
    total = re.search(r"\btotal_access=(\d+)", evidence)
    dominant = re.search(r"\bdominant_foreign_type=([^;]+)", evidence)
    if not foreign or not total or not dominant:
        raise AssertionError(f"Incomplete Feature Envy evidence: {evidence}")
    return int(foreign.group(1)), int(total.group(1)), dominant.group(1)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="feature-envy-self-check-") as temp_dir:
        root = Path(temp_dir)
        (root / "Fixture.java").write_text(FIXTURE, encoding="utf-8")
        result = run_java_semantic_detector(root)
        if not result.ok:
            raise AssertionError(result.error)
        findings = {
            finding.method.split("(", 1)[0]: finding
            for finding in result.findings["feature_envy"]
            if finding.class_name == "Subject"
        }

        for method in ("direct", "aliased", "enhancedLoop", "dominantType"):
            if method not in findings:
                raise AssertionError(f"Expected Feature Envy finding for {method}")
        if "splitAcrossTypes" in findings:
            raise AssertionError("Foreign accesses split across types must not form one synthetic envy target")

        direct = _metrics(findings["direct"].evidence)
        aliased = _metrics(findings["aliased"].evidence)
        enhanced = _metrics(findings["enhancedLoop"].evidence)
        dominant = _metrics(findings["dominantType"].evidence)
        if direct != aliased:
            raise AssertionError(f"Local alias changed Feature Envy metrics: {direct} != {aliased}")
        if enhanced[:2] != (4, 4):
            raise AssertionError(f"Enhanced-for receiver accesses were not counted consistently: {enhanced}")
        if dominant[:2] != (4, 6):
            raise AssertionError(f"Dominant receiver metric is incorrect: {dominant}")
        if not direct[2].endswith("Collaborator"):
            raise AssertionError(f"Unexpected dominant receiver type: {direct[2]}")

        print(
            "feature-envy-self-check PASS "
            f"alias={direct[0]}/{direct[1]} enhanced={enhanced[0]}/{enhanced[1]} "
            f"dominant={dominant[0]}/{dominant[1]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
