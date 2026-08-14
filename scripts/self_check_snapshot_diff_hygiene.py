#!/usr/bin/env python3
"""Regression checks for complete, classified exported diffs."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNTIME = ROOT / "runtime" / "python"
if str(PYTHON_RUNTIME) not in sys.path:
    sys.path.insert(0, str(PYTHON_RUNTIME))

from bridge import smell_bridge


def _run(root: Path, *args: str) -> None:
    subprocess.run(
        list(args),
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="snapshot-diff-hygiene-") as raw:
        root = Path(raw)
        _run(root, "git", "init", "-q")
        _run(root, "git", "config", "user.email", "guard@example.invalid")
        _run(root, "git", "config", "user.name", "Guard Check")
        _write(root, "src/target.c", "int target(void) { return 1; }\n")
        _write(root, "tests/target_test.c", "int test_target(void) { return 1; }\n")
        _write(root, "qa/oracle.py", "expected = 1\n")
        _write(root, "spec/protocol.md", "not a test tree\n")
        _write(root, "Makefile", "all:\n\t@true\n")
        _write(root, "src/Makefile.inc", "CSOURCES = target.c\n")
        _write(root, "docs/note.md", "baseline\n")
        _run(root, "git", "add", ".")
        _run(root, "git", "commit", "-qm", "baseline")
        baseline_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

        _write(root, "src/target.c", "int target(void) { return 2; }\n")
        _write(root, "src/new.h", "int target(void);\n")
        _write(root, "src/new\nline.c", "int newline_path(void) { return 1; }\n")
        _write(root, "tests/target_test.c", "int test_target(void) { return 2; }\n")
        _write(root, "qa/oracle.py", "expected = 2\n")
        _write(root, "spec/protocol.md", "still not a test tree\n")
        _write(root, "Makefile", "all:\n\t@echo rebuilt\n")
        _write(root, "src/Makefile.inc", "CSOURCES = target.c new.c\n")
        _write(root, "docs/note.md", "changed\n")
        _write(
            root,
            "build-refactoragent/generated.cc",
            "int generated(void) { return 2; }\n",
        )

        # Staged edits must not disappear from the exported patch.
        _run(root, "git", "add", "Makefile")
        _run(root, "git", "config", "diff.mnemonicPrefix", "true")
        _run(root, "git", "config", "diff.algorithm", "histogram")
        _run(root, "git", "config", "diff.indentHeuristic", "true")
        snapshot = smell_bridge._snapshot_project(
            root,
            declared_test_paths=["qa/oracle.py"],
            base_commit=baseline_commit,
        )
        patch = snapshot["diff"]["stdout"]
        stat = snapshot["diff_stat"]["stdout"]
        status = snapshot["status"]["stdout"]
        assert snapshot["scope"] == "full_worktree_pre_verification"
        for rendered in (patch, stat, status):
            assert "src/target.c" in rendered, rendered
            assert "build-refactoragent" not in rendered, rendered
        assert "src/new.h" in patch, patch
        assert "tests/target_test.c" in patch, patch
        assert "qa/oracle.py" in patch, patch
        assert "Makefile" in patch, patch
        assert "docs/note.md" in patch, patch
        assert "--- a/src/target.c" in patch, patch
        assert "+++ b/src/target.c" in patch, patch

        audit = snapshot["change_audit"]
        assert audit["success"] is True, audit
        assert audit["category_counts"] == {
            "production": 3,
            "test": 2,
            "build_metadata": 2,
            "other": 2,
        }, audit
        assert any(
            item["path"] == "src/Makefile.inc"
            and item["category"] == "build_metadata"
            for item in audit["changes"]
        ), audit
        assert any(
            item["path"] == "src/new\nline.c"
            for item in audit["changes"]
        ), audit
        assert audit["ignored_untracked_count"] == 1, audit

        immutable = smell_bridge._worktree_test_change_audit(
            audit,
            allow_test_changes=False,
        )
        assert immutable["success"] is False, immutable
        assert immutable["status"] == "TEST_SOURCE_MODIFIED", immutable
        assert immutable["changed"] == [
            {"path": "qa/oracle.py"},
            {"path": "tests/target_test.c"},
        ], immutable

        allowed = smell_bridge._worktree_test_change_audit(
            audit,
            allow_test_changes=True,
        )
        assert allowed["success"] is True, allowed
        assert allowed["status"] == "TEST_SOURCE_CHANGE_ALLOWED", allowed

        # A candidate-created commit must not erase the c000-relative delivery
        # patch or hide its test edits. Commit only the deliverable paths; the
        # generated build tree remains an ignored untracked artifact.
        _run(
            root,
            "git", "add",
            "src", "tests", "qa", "spec", "Makefile", "docs",
            "src/Makefile.inc",
        )
        _run(root, "git", "commit", "-qm", "candidate commit")
        committed_snapshot = smell_bridge._snapshot_project(
            root,
            declared_test_paths=["qa/oracle.py"],
            base_commit=baseline_commit,
        )
        committed_patch = committed_snapshot["diff"]["stdout"]
        committed_audit = committed_snapshot["change_audit"]
        assert committed_snapshot["base_commit"] == baseline_commit
        assert "src/target.c" in committed_patch, committed_patch
        assert "tests/target_test.c" in committed_patch, committed_patch
        assert "Makefile" in committed_patch, committed_patch
        assert committed_audit["category_counts"] == audit["category_counts"], (
            committed_audit,
            audit,
        )
        committed_immutable = smell_bridge._worktree_test_change_audit(
            committed_audit,
            allow_test_changes=False,
        )
        assert committed_immutable["status"] == "TEST_SOURCE_MODIFIED", (
            committed_immutable
        )

    print(
        "snapshot diff hygiene self-check passed: "
        "c000-relative committed/staged/unstaged patch; "
        "production/test/build metadata classified; "
        "immutable test edits fail closed"
    )


if __name__ == "__main__":
    main()
