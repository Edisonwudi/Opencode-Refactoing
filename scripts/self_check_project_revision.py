#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.project_revision import ProjectRevisionError, verify_test_oracle  # noqa: E402


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _expect_error(status: str, fn) -> None:
    try:
        fn()
    except ProjectRevisionError as exc:
        assert exc.status == status, (exc.status, status)
    else:
        raise AssertionError(f"expected {status}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="project-revision-self-check-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run(repo, "init", "-q")
        _run(repo, "config", "user.email", "self-check@example.invalid")
        _run(repo, "config", "user.name", "Self Check")
        test_file = repo / "src" / "ExampleTest.java"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("class ExampleTest {}\n", encoding="utf-8")
        _run(repo, "add", ".")
        _run(repo, "commit", "-qm", "fixture")

        expected = hashlib.sha256(test_file.read_bytes()).hexdigest()
        result = verify_test_oracle(repo, "src/ExampleTest.java", expected)
        assert result["test_oracle_alignment"] == "ALIGNED"
        assert result["actual_test_oracle_sha256"] == expected
        print("  ok   aligned oracle")

        _expect_error(
            "TEST_ORACLE_MISMATCH",
            lambda: verify_test_oracle(repo, "src/ExampleTest.java", "0" * 64),
        )
        print("  ok   mismatched oracle")

        _expect_error(
            "TEST_ORACLE_FILE_MISSING",
            lambda: verify_test_oracle(repo, "src/MissingTest.java", expected),
        )
        print("  ok   missing oracle file")

        _expect_error(
            "TEST_ORACLE_SCHEMA_INVALID",
            lambda: verify_test_oracle(repo, "src/ExampleTest.java", "not-a-sha256"),
        )
        print("  ok   invalid oracle hash")

        skipped = verify_test_oracle(repo, "", "")
        assert skipped["test_oracle_alignment"] == "NOT_DECLARED"
        print("  ok   undeclared oracle")
        _expect_error(
            "TEST_ORACLE_SCHEMA_INVALID",
            lambda: verify_test_oracle(repo, "src/ExampleTest.java", ""),
        )
        print("  ok   file without oracle hash")
        _expect_error(
            "TEST_ORACLE_SCHEMA_INVALID",
            lambda: verify_test_oracle(repo, "", expected),
        )
        print("  ok   oracle hash without file")

    print("project revision self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
