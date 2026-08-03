#!/usr/bin/env python3
"""Regression checks for the delivery-aligned God Class semantic guard."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "runtime" / "python"))

from smell_core.java.semantic_detector import (  # noqa: E402
    GOD_CLASS_PROFILE_ID,
    god_class_product_profile,
    run_java_semantic_detector,
)


def _method(index: int, controls: int) -> str:
    statements = "\n".join(f"    if (value > {n}) value--;" for n in range(controls))
    return f"  void method{index}(int value) {{\n{statements}\n  }}"


def _finding(source: str):
    with tempfile.TemporaryDirectory(prefix="god-class-guard-") as tmp:
        root = Path(tmp)
        (root / "Candidate.java").write_text(source, encoding="utf-8")
        result = run_java_semantic_detector(root)
        assert result.ok, result.error
        return result.findings["god_class"]


def _metric(evidence: str, name: str) -> int:
    match = re.search(rf"\b{name}=(\d+)", evidence)
    assert match, evidence
    return int(match.group(1))


def main() -> int:
    # Dataset rule: NOM>=5 and WMC>=20 are mandatory, then at least two
    # candidate signals.  This fixture qualifies through NOM and LOC without
    # requiring the obsolete ATFD>=10 runtime condition.
    candidate = "class Candidate {\n" + "\n".join(_method(i, 2) for i in range(10))
    candidate += "\n" + "\n".join("  // calibration padding" for _ in range(80)) + "\n}\n"
    findings = _finding(candidate)
    assert len(findings) == 1, findings
    evidence = findings[0].evidence
    assert _metric(evidence, "nom") == 10, evidence
    assert _metric(evidence, "wmc") == 20, evidence
    assert "signals=nom,loc" in evidence, evidence
    profile = god_class_product_profile(
        {name: _metric(evidence, name) for name in ("nom", "nof", "wmc", "loc", "atfd")}
    )
    assert profile["id"] == GOD_CLASS_PROFILE_ID, profile
    assert profile["min_signals"] == 2, profile
    assert profile["triggered_signals"] == ["nom", "loc"], profile
    boundaries = {
        item["name"]: item["boundary"]
        for item in profile["signals"]
        if "boundary" in item
    }
    assert boundaries == {"nom": 10, "wmc": 30, "loc": 100, "atfd": 3}, profile
    assert profile["finding_present"] is True, profile

    # The previous runtime detector used method LOC as WMC and would report a
    # long class with trivial methods.  Dataset WMC is control-flow complexity,
    # so WMC<20 must reject it before the signal rule.
    trivial = "class Candidate {\n" + "\n".join(_method(i, 0) for i in range(12))
    trivial += "\n" + "\n".join("  // long but not complex" for _ in range(120)) + "\n}\n"
    assert _finding(trivial) == [], "trivial long class must not be a God Class"

    # A smaller post-extraction class can retain five non-trivial methods but
    # must disappear once fewer than two candidate signals remain.
    reduced = "class Candidate {\n" + "\n".join(_method(i, 4) for i in range(5)) + "\n}\n"
    assert _finding(reduced) == [], "reduced class must be below the dataset rule"

    print("god_class target-Guard profile self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
