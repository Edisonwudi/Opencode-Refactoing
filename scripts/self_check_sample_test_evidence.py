#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.guards import _sample_test_execution_evidence  # noqa: E402


def _write_report(root: Path, class_name: str, tests: int, skipped: int = 0) -> None:
    report = root / "target" / "surefire-reports" / f"TEST-example.{class_name}.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f'<testsuite name="example.{class_name}" tests="{tests}" '
        f'failures="0" errors="0" skipped="{skipped}"></testsuite>\n',
        encoding="utf-8",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sample-test-evidence-self-check-") as tmp:
        root = Path(tmp)
        started_ns = time.time_ns()
        _write_report(root, "FirstBehaviorTest", 2)
        _write_report(root, "SecondBehaviorTest", 3, skipped=1)

        config = SimpleNamespace(
            project_root=root,
            sample_test_location=(
                "src/test/java/example/FirstBehaviorTest.java;"
                "src/test/java/example/SecondBehaviorTest.java"
            ),
        )
        evidence = _sample_test_execution_evidence(config, started_ns)
        assert evidence["success"] is True, evidence
        assert evidence["test_classes"] == [
            "FirstBehaviorTest",
            "SecondBehaviorTest",
        ]
        assert evidence["tests"] == 4
        assert evidence["skipped"] == 1
        assert all(item["success"] for item in evidence["classes"])
        print("  ok   every declared test class has fresh evidence")

        missing_config = SimpleNamespace(
            project_root=root,
            sample_test_location=(
                "src/test/java/example/FirstBehaviorTest.java;"
                "src/test/java/example/MissingBehaviorTest.java"
            ),
        )
        missing = _sample_test_execution_evidence(missing_config, started_ns)
        assert missing["success"] is False, missing
        assert missing["missing_test_classes"] == ["MissingBehaviorTest"]
        print("  ok   one missing declared class fails closed")

        skipped_started_ns = time.time_ns()
        _write_report(root, "SkippedBehaviorTest", 1, skipped=1)
        skipped_config = SimpleNamespace(
            project_root=root,
            sample_test_location="src/test/java/example/SkippedBehaviorTest.java",
        )
        skipped = _sample_test_execution_evidence(skipped_config, skipped_started_ns)
        assert skipped["success"] is False, skipped
        assert skipped["missing_test_classes"] == ["SkippedBehaviorTest"]
        print("  ok   skipped-only report fails closed")

    print("sample test evidence self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
