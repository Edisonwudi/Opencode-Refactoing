#!/usr/bin/env python3
"""Self-check for the target-bounded, rename-aware Guard file scope."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from smell_core.guard_scope import (  # noqa: E402
    GuardScopeError,
    build_changed_line_ranges,
    build_guard_verification_scope,
    read_baseline_bytes,
    read_current_bytes,
    validate_guard_analysis_scope,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    return result.stdout.decode("utf-8", errors="surrogateescape").strip()


def _write(root: Path, relative: str, payload: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _expect_status(status: str, callback) -> None:
    try:
        callback()
    except GuardScopeError as exc:
        assert exc.status == status, (exc.status, status, exc.details)
    else:
        raise AssertionError(f"expected GuardScopeError({status})")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="guard-scope-") as temporary:
        repository = Path(temporary) / "repository"
        project = repository / "module"
        project.mkdir(parents=True)
        _git(repository, "init", "-q")
        _git(repository, "config", "user.name", "Guard Scope Self Check")
        _git(repository, "config", "user.email", "guard-scope@example.invalid")

        _write(project, "pom.xml", "<project/>\n")
        anchor = _write(
            project,
            "src/main/java/p/Anchor.java",
            "package p; class Anchor { int value() { return 1; } }\n",
        )
        _write(
            project,
            "src/main/java/p/OldName.java",
            "package p; class OldName { int value() { return 2; } }\n",
        )
        _write(
            project,
            "src/main/java/p/Deleted.java",
            "package p; class Deleted {}\n",
        )
        stable = _write(
            project,
            "src/main/java/p/Stable.java",
            b"package p; class Stable { /* \xff */ }\n",
        )
        test_source = _write(
            project,
            "src/test/java/p/AnchorTest.java",
            "package p; class AnchorTest {}\n",
        )
        generated = _write(
            project,
            "build/generated/p/Generated.java",
            "package p; class Generated {}\n",
        )
        oracle = _write(project, "dataset/oracle.csv", "id,label\n1,oracle\n")
        _git(repository, "add", ".")
        _git(repository, "commit", "-qm", "baseline")
        baseline = _git(repository, "rev-parse", "HEAD")

        anchor.write_text(
            "package p; class Anchor { int value() { return 3; } }\n",
            encoding="utf-8",
        )
        _git(
            project,
            "mv",
            "src/main/java/p/OldName.java",
            "src/main/java/p/NewName.java",
        )
        (project / "src/main/java/p/Deleted.java").unlink()
        test_source.write_text("package p; class AnchorTest { int changed; }\n")
        generated.write_text("package p; class Generated { int changed; }\n")
        oracle.write_text("id,label\n1,changed-oracle\n", encoding="utf-8")
        _write(
            project,
            "src/main/java/p/Added.java",
            "package p; class Added {}\n",
        )
        _write(
            project,
            "src/test/java/p/AddedTest.java",
            "package p; class AddedTest {}\n",
        )

        scope = build_guard_verification_scope(
            project,
            baseline,
            (
                stable,
                "src/main/java/p/OldName.java",
            ),
        )

        changed = set(scope.changed_files)
        assert "src/main/java/p/OldName.java" in changed, scope
        assert "src/main/java/p/NewName.java" in changed, scope
        assert "src/main/java/p/Added.java" in changed, scope
        assert "src/test/java/p/AnchorTest.java" in changed, scope
        assert "src/test/java/p/AddedTest.java" in changed, scope
        assert "build/generated/p/Generated.java" in changed, scope
        assert "dataset/oracle.csv" in changed, scope

        expected_production = {
            "src/main/java/p/Added.java",
            "src/main/java/p/Anchor.java",
            "src/main/java/p/Deleted.java",
            "src/main/java/p/NewName.java",
            "src/main/java/p/OldName.java",
        }
        assert set(scope.changed_production_files) == expected_production, scope
        assert set(scope.target_files) == {
            "src/main/java/p/OldName.java",
            "src/main/java/p/Stable.java",
        }, scope
        assert set(scope.analysis_files) == set(scope.target_files)
        assert "dataset/oracle.csv" not in scope.analysis_files
        assert "src/test/java/p/AnchorTest.java" not in scope.analysis_files
        assert "build/generated/p/Generated.java" not in scope.analysis_files
        assert scope.changed_line_ranges == (), scope
        changed_ranges = set(
            build_changed_line_ranges(
                project,
                scope.baseline_commit,
                scope.changed_production_files,
            )
        )
        assert ("src/main/java/p/Anchor.java", 1, 1) in changed_ranges, scope
        assert ("src/main/java/p/Added.java", 1, 1) in changed_ranges, scope
        assert ("src/main/java/p/NewName.java", 1, 1) in changed_ranges, scope
        assert not any(
            path == "src/main/java/p/Deleted.java"
            for path, _start, _end in changed_ranges
        ), scope

        baseline_anchor = read_baseline_bytes(
            project, baseline, "src/main/java/p/Anchor.java"
        )
        current_anchor = read_current_bytes(project, "src/main/java/p/Anchor.java")
        assert baseline_anchor is not None and b"return 1" in baseline_anchor
        assert current_anchor is not None and b"return 3" in current_anchor

        assert read_baseline_bytes(
            project, baseline, "src/main/java/p/OldName.java"
        ) is not None
        assert read_current_bytes(project, "src/main/java/p/OldName.java") is None
        assert read_baseline_bytes(
            project, baseline, "src/main/java/p/NewName.java"
        ) is None
        assert read_current_bytes(
            project, "src/main/java/p/NewName.java"
        ) == b"package p; class OldName { int value() { return 2; } }\n"
        assert read_baseline_bytes(
            project, baseline, "src/main/java/p/Deleted.java"
        ) is not None
        assert read_current_bytes(project, "src/main/java/p/Deleted.java") is None
        assert read_baseline_bytes(
            project, baseline, "src/main/java/p/Added.java"
        ) is None
        assert read_current_bytes(project, "src/main/java/p/Added.java") is not None
        assert read_baseline_bytes(
            project, baseline, "src/main/java/p/Stable.java"
        ) == stable.read_bytes()

        _expect_status(
            "TARGET_NOT_PRODUCTION_JAVA",
            lambda: build_guard_verification_scope(
                project,
                baseline,
                ("src/test/java/p/AnchorTest.java",),
            ),
        )
        _expect_status(
            "INVALID_RELATIVE_PATH",
            lambda: read_current_bytes(project, "../outside.java"),
        )
        _expect_status(
            "BASELINE_COMMIT_INVALID",
            lambda: build_guard_verification_scope(
                project,
                "not-a-commit",
                ("src/main/java/p/Stable.java",),
            ),
        )
        for index in range(65):
            _write(
                project,
                f"src/main/java/broad/Broad{index:02d}.java",
                f"package broad; class Broad{index:02d} {{}}\n",
            )
        broad_files = tuple(
            f"src/main/java/broad/Broad{index:02d}.java"
            for index in range(65)
        )
        noisy_scope = build_guard_verification_scope(
            project,
            baseline,
            ("src/main/java/p/Stable.java",),
        )
        assert set(broad_files).issubset(noisy_scope.changed_production_files)
        assert noisy_scope.analysis_files == ("src/main/java/p/Stable.java",)
        _expect_status(
            "GUARD_SCOPE_TOO_LARGE",
            lambda: validate_guard_analysis_scope(project, broad_files),
        )

    print(
        "guard-scope-self-check PASS "
        "rename=old+new production=java-only analysis=target-only "
        "changed-noise=metadata-only changed-lines=on-demand "
        "baseline/current=exact-bytes dataset=unused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
