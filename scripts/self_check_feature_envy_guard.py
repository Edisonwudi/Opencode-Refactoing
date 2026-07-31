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
  private Collaborator collaborator;
  private Other other;

  void direct() {
    collaborator.a();
    collaborator.b();
    collaborator.c();
    collaborator.d();
  }

  void aliased() {
    Collaborator local = collaborator;
    local.a();
    local.b();
    local.c();
    local.d();
  }

  void transitivelyAliased() {
    Collaborator first = this.collaborator;
    Collaborator second = first;
    second.a();
    second.b();
    second.c();
    second.d();
  }

  void reassignedAlias() {
    Collaborator local = collaborator;
    local = new Collaborator();
    local.a();
    local.b();
    local.c();
    local.d();
  }

  void dominantField() {
    collaborator.a();
    collaborator.b();
    collaborator.c();
    collaborator.d();
    other.a();
    other.b();
  }

  void selfBalanced() {
    collaborator.a();
    collaborator.b();
    collaborator.c();
    collaborator.d();
    helperA();
    helperB();
    helperC();
  }

  void explicitSelfBalanced() {
    collaborator.a();
    collaborator.b();
    collaborator.c();
    collaborator.d();
    this.helperA();
    this.helperB();
    this.helperC();
  }

  void helperA() {}
  void helperB() {}
  void helperC() {}
}
"""


def _metrics(evidence: str) -> tuple[int, int, str, str]:
    envy = re.search(r"\benvy_access=(\d+)", evidence)
    self_access = re.search(r"\bself_access=(\d+)", evidence)
    envied_type = re.search(r"\benvied_type=([^;]+)", evidence)
    envied_field = re.search(r"\benvied_field=([^;]+)", evidence)
    if not envy or not self_access or not envied_type or not envied_field:
        raise AssertionError(f"Incomplete Feature Envy evidence: {evidence}")
    return (
        int(envy.group(1)),
        int(self_access.group(1)),
        envied_type.group(1),
        envied_field.group(1),
    )


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

        for method in ("direct", "aliased", "transitivelyAliased", "dominantField"):
            if method not in findings:
                raise AssertionError(f"Expected Feature Envy finding for {method}")
        if "reassignedAlias" in findings:
            raise AssertionError("A reassigned local must not retain stale field provenance")
        if "selfBalanced" in findings:
            raise AssertionError("Same-class calls must offset the envy-access metric")
        if "explicitSelfBalanced" in findings:
            raise AssertionError("this-qualified self calls must equal unqualified self calls")

        direct = _metrics(findings["direct"].evidence)
        aliased = _metrics(findings["aliased"].evidence)
        transitive = _metrics(findings["transitivelyAliased"].evidence)
        dominant = _metrics(findings["dominantField"].evidence)
        if direct[:2] != (4, 0):
            raise AssertionError(f"Direct field metric is incorrect: {direct}")
        if aliased[:2] != direct[:2] or transitive[:2] != direct[:2]:
            raise AssertionError(
                f"Stable aliases must preserve field provenance: direct={direct} "
                f"alias={aliased} transitive={transitive}"
            )
        if dominant[:2] != (4, 0):
            raise AssertionError(f"Dominant receiver metric is incorrect: {dominant}")
        if not direct[2].endswith("Collaborator") or direct[3] != "collaborator":
            raise AssertionError(f"Unexpected dominant field/type: {direct}")
        if dominant[3] != "collaborator":
            raise AssertionError(f"Strongest field was not selected deterministically: {dominant}")

        print(
            "feature-envy-self-check PASS "
            f"direct={direct[0]}/{direct[1]} alias={aliased[0]}/{aliased[1]} "
            f"dominant={dominant[3]}:{dominant[0]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
