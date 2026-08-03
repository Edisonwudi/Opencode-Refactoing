#!/usr/bin/env python3
"""Self-check the bounded Refused Bequest ancestor relation scope."""

from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java import target_relation_scope as relation_scope  # noqa: E402


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _git(project: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(project),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)


def _expect_error(code: str, callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except relation_scope.TargetRelationScopeError as exc:
        assert exc.code == code, exc.violation()
    else:
        raise AssertionError(f"expected {code}")


def main() -> int:
    module_source = inspect.getsource(relation_scope)
    assert ".rglob(" not in module_source
    assert "_build_project_model" not in module_source
    assert "run_java_semantic_detector" not in module_source

    with tempfile.TemporaryDirectory(prefix="target-relation-scope-") as raw:
        project = Path(raw)
        child = _write(
            project,
            "src/main/java/app/Child.java",
            """\
package app;
class Child extends AbstractCapability {
  @Override public void execute() { throw new UnsupportedOperationException(); }
}
""",
        )
        abstract_parent = _write(
            project,
            "src/main/java/app/AbstractCapability.java",
            """\
package app;
import api.Capability;
abstract class AbstractCapability implements Capability {
}
""",
        )
        right_contract = _write(
            project,
            "src/main/java/api/Capability.java",
            """\
package api;
public interface Capability { void execute(); }
""",
        )
        wrong_contract = _write(
            project,
            "src/main/java/trap/Capability.java",
            """\
package trap;
public interface Capability { void execute(); }
""",
        )

        chosen_child = _write(
            project,
            "src/main/java/chosen/ChosenChild.java",
            """\
package chosen;
import right.Parent;
class ChosenChild extends Parent {
  @Override public void execute() { throw new UnsupportedOperationException(); }
}
""",
        )
        right_parent = _write(
            project,
            "src/main/java/right/Parent.java",
            """\
package right;
public class Parent { public void execute() {} }
""",
        )
        wrong_parent = _write(
            project,
            "src/main/java/wrong/Parent.java",
            """\
package wrong;
public class Parent { public void execute() {} }
            """,
        )
        for index in range(30):
            _write(
                project,
                f"src/main/java/decoy{index:02d}/Parent.java",
                f"package decoy{index:02d}; "
                "public class Parent { public void execute() {} }\n",
            )

        ambiguous_child = _write(
            project,
            "src/main/java/ambiguous/AmbiguousChild.java",
            """\
package ambiguous;
import left.*;
import rightcap.*;
class AmbiguousChild implements Capability {
  @Override public void execute() { throw new UnsupportedOperationException(); }
}
""",
        )
        left_contract = _write(
            project,
            "src/main/java/left/Capability.java",
            "package left; public interface Capability { void execute(); }\n",
        )
        other_right_contract = _write(
            project,
            "src/main/java/rightcap/Capability.java",
            "package rightcap; public interface Capability { void execute(); }\n",
        )

        for index in range(200):
            _write(
                project,
                f"src/main/java/noise/Noise{index:03d}.java",
                f"package noise; class Noise{index:03d} {{ int value() {{ return {index}; }} }}\n",
            )

        _git(project, "init", "-q")
        _git(project, "add", ".")
        _git(
            project,
            "-c",
            "user.name=relation-scope-self-check",
            "-c",
            "user.email=relation-scope@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

        original_read_bytes = Path.read_bytes
        original_build_model = relation_scope.semantic.build_scoped_project_model
        java_reads: list[Path] = []
        semantic_scopes: list[tuple[str, ...]] = []

        def tracked_read_bytes(path: Path) -> bytes:
            if path.suffix.casefold() == ".java":
                java_reads.append(path.resolve())
            return original_read_bytes(path)

        def tracked_build_model(
            root: Path,
            files: object,
            classpath: str = "",
        ) -> object:
            frozen = tuple(sorted(str(item) for item in files))  # type: ignore[arg-type]
            semantic_scopes.append(frozen)
            return original_build_model(root, frozen, classpath)

        Path.read_bytes = tracked_read_bytes
        relation_scope.semantic.build_scoped_project_model = tracked_build_model
        try:
            semantic_scopes.clear()
            transitive = relation_scope.resolve_refused_bequest_relation_scope(
                project,
                [child],
                "src/main/java/app/Child.java:method=execute()|line=3",
                {"target_class": "app.Child", "parent": "api.Capability"},
            )
            assert transitive.files == (
                "src/main/java/api/Capability.java",
                "src/main/java/app/AbstractCapability.java",
                "src/main/java/app/Child.java",
            ), transitive.witness()
            assert transitive.ancestors == (
                "api.Capability",
                "app.AbstractCapability",
            ), transitive.witness()
            assert [
                (edge.child, edge.parent, edge.depth)
                for edge in transitive.edges
            ] == [
                ("app.Child", "app.AbstractCapability", 1),
                ("app.AbstractCapability", "api.Capability", 2),
            ], transitive.witness()
            assert transitive.resolved_reported_parent == "api.Capability"
            assert not any(
                wrong_contract.relative_to(project).as_posix() in scope
                for scope in semantic_scopes
            ), semantic_scopes

            java_reads.clear()
            semantic_scopes.clear()
            disambiguated = relation_scope.resolve_refused_bequest_relation_scope(
                project,
                [chosen_child],
                "src/main/java/chosen/ChosenChild.java:method=execute()|line=3",
                {"target_class": "chosen.ChosenChild", "parent": "right.Parent"},
            )
            assert disambiguated.ancestors == ("right.Parent",), disambiguated.witness()
            assert right_parent.relative_to(project).as_posix() in disambiguated.files
            assert wrong_parent.relative_to(project).as_posix() not in disambiguated.files
            assert not any(
                wrong_parent.relative_to(project).as_posix() in scope
                for scope in semantic_scopes
            ), semantic_scopes

            _expect_error(
                "ANCESTOR_TYPE_AMBIGUOUS",
                lambda: relation_scope.resolve_refused_bequest_relation_scope(
                    project,
                    [ambiguous_child],
                    "src/main/java/ambiguous/AmbiguousChild.java:method=execute()|line=5",
                    {"target_class": "ambiguous.AmbiguousChild"},
                ),
            )
            _expect_error(
                "RELATION_HOP_LIMIT_EXCEEDED",
                lambda: relation_scope.resolve_refused_bequest_relation_scope(
                    project,
                    [child],
                    "src/main/java/app/Child.java:method=execute()|line=3",
                    {"target_class": "app.Child"},
                    max_hops=1,
                ),
            )
            _expect_error(
                "RELATION_SCOPE_TOO_LARGE",
                lambda: relation_scope.resolve_refused_bequest_relation_scope(
                    project,
                    [child],
                    "src/main/java/app/Child.java:method=execute()|line=3",
                    {"target_class": "app.Child"},
                    max_bytes=16,
                ),
            )
        finally:
            Path.read_bytes = original_read_bytes
            relation_scope.semantic.build_scoped_project_model = original_build_model

        assert not any(path.name.startswith("Noise") for path in java_reads), java_reads

    print(
        "target relation scope self-check PASS "
        "transitive_ancestor=2 package_import_disambiguation=exact "
        "same_name_decoys=31 ambiguous=fail_closed unrelated_java_not_read=200"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
