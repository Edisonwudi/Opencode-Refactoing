#!/usr/bin/env python3
"""Self-check the conditional exact Feature Envy target scope."""

from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java import semantic_detector as semantic  # noqa: E402
from smell_core.java import target_feature_envy_scope as feature_scope  # noqa: E402


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
    except feature_scope.FeatureEnvyScopeError as exc:
        assert exc.code == code, exc.violation()
    else:
        raise AssertionError(f"expected {code}")


def main() -> int:
    module_source = inspect.getsource(feature_scope)
    assert ".rglob(" not in module_source
    assert "_build_project_model" not in module_source
    assert "run_java_semantic_detector" not in module_source
    assert feature_scope.DEFAULT_MAX_SCOPE_FILES == 32
    assert feature_scope.DEFAULT_MAX_SCOPE_BYTES == 8 * 1024 * 1024

    with tempfile.TemporaryDirectory(prefix="target-feature-envy-scope-") as raw:
        project = Path(raw)
        receiver = _write(
            project,
            "src/main/java/foreign/Receiver.java",
            "package foreign; public class Receiver { "
            "public int a(){return 1;} public int b(){return 2;} "
            "public int c(){return 3;} }\n",
        )
        direct = _write(
            project,
            "src/main/java/app/DirectTarget.java",
            """\
package app;
import foreign.Receiver;
class DirectTarget {
  private Receiver receiver;
  int calculate() { return receiver.a() + receiver.b() + receiver.c(); }
}
""",
        )
        inherited = _write(
            project,
            "src/main/java/app/InheritedTarget.java",
            """\
package app;
import support.Base;
class InheritedTarget extends Base {
  int calculate() { return receiver.a() + receiver.b() + receiver.c(); }
}
""",
        )
        base = _write(
            project,
            "src/main/java/support/Base.java",
            """\
package support;
import foreign.Receiver;
public class Base { protected Receiver receiver; }
""",
        )
        private_base = _write(
            project,
            "src/main/java/support/PrivateBase.java",
            """\
package support;
import foreign.Receiver;
public class PrivateBase { private Receiver receiver; }
""",
        )
        private_target = _write(
            project,
            "src/main/java/app/PrivateTarget.java",
            """\
package app;
import support.PrivateBase;
class PrivateTarget extends PrivateBase {
  int calculate() { return receiver.a() + receiver.b() + receiver.c(); }
}
""",
        )
        shadow_target = _write(
            project,
            "src/main/java/app/ShadowTarget.java",
            """\
package app;
import support.Base;
class ShadowTarget extends Base {
  private LocalReceiver receiver;
  int calculate() { return receiver.a() + receiver.b() + receiver.c(); }
  static class LocalReceiver {
    int a() { return 1; } int b() { return 2; } int c() { return 3; }
  }
}
""",
        )

        chosen = _write(
            project,
            "src/main/java/chosen/ChosenTarget.java",
            """\
package chosen;
import right.Parent;
class ChosenTarget extends Parent { int calculate() { return 1; } }
""",
        )
        right_parent = _write(
            project,
            "src/main/java/right/Parent.java",
            "package right; public class Parent {}\n",
        )
        wrong_parent = _write(
            project,
            "src/main/java/wrong/Parent.java",
            "package wrong; public class Parent {}\n",
        )

        ambiguous = _write(
            project,
            "src/main/java/ambiguous/AmbiguousTarget.java",
            """\
package ambiguous;
import left.*;
import rightcap.*;
class AmbiguousTarget extends Parent { int calculate() { return 1; } }
""",
        )
        _write(
            project,
            "src/main/java/left/Parent.java",
            "package left; public class Parent {}\n",
        )
        _write(
            project,
            "src/main/java/rightcap/Parent.java",
            "package rightcap; public class Parent {}\n",
        )
        for index in range(100):
            _write(
                project,
                f"src/main/java/noise/Noise{index:03d}.java",
                f"package noise; class Noise{index:03d} {{ int n = {index}; }}\n",
            )

        _git(project, "init", "-q")
        _git(project, "add", ".")
        _git(
            project,
            "-c",
            "user.name=feature-envy-scope-self-check",
            "-c",
            "user.email=feature-envy-scope@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

        original_read_bytes = Path.read_bytes
        java_reads: list[Path] = []

        def tracked_read_bytes(path: Path) -> bytes:
            if path.suffix.casefold() == ".java":
                java_reads.append(path.resolve())
            return original_read_bytes(path)

        Path.read_bytes = tracked_read_bytes
        try:
            target_only = feature_scope.resolve_feature_envy_scope(
                project,
                [direct],
                "src/main/java/app/DirectTarget.java:method=calculate()|line=5",
                {
                    "target_class": "app.DirectTarget",
                    "receiver_type": "foreign.Receiver",
                    "receiver": "receiver",
                },
            )
            assert target_only.files == (
                "src/main/java/app/DirectTarget.java",
            ), target_only.witness()
            assert not target_only.expanded_for_inheritance
            assert receiver.resolve() not in java_reads, java_reads

            direct_model = semantic.build_scoped_project_model(
                project,
                target_only.files,
            )
            direct_findings = semantic.evaluate_scoped_guard_findings(
                direct_model,
                "feature_envy",
            )
            assert len(direct_findings) == 1, direct_findings

            java_reads.clear()
            inherited_scope = feature_scope.resolve_feature_envy_scope(
                project,
                [inherited],
                "src/main/java/app/InheritedTarget.java:method=calculate()|line=4",
                {"target_class": "app.InheritedTarget"},
            )
            assert inherited_scope.files == (
                "src/main/java/app/InheritedTarget.java",
                "src/main/java/support/Base.java",
            ), inherited_scope.witness()
            assert inherited_scope.ancestors == (
                "support.Base",
            ), inherited_scope.witness()
            assert inherited_scope.expanded_for_inheritance
            assert receiver.resolve() not in java_reads, java_reads

            inherited_model = semantic.build_scoped_project_model(
                project,
                inherited_scope.files,
            )
            inherited_findings = semantic.evaluate_scoped_guard_findings(
                inherited_model,
                "feature_envy",
            )
            inherited_profile_hit = any(
                finding.class_name == "InheritedTarget"
                for finding in inherited_findings
            )
            assert inherited_profile_hit, inherited_findings

            private_scope = feature_scope.resolve_feature_envy_scope(
                project,
                [private_target],
                "src/main/java/app/PrivateTarget.java:method=calculate()|line=4",
                {"target_class": "app.PrivateTarget"},
            )
            assert private_base.relative_to(project).as_posix() in private_scope.files
            private_model = semantic.build_scoped_project_model(
                project,
                private_scope.files,
            )
            private_findings = semantic.evaluate_scoped_guard_findings(
                private_model,
                "feature_envy",
            )
            assert not any(
                finding.class_name == "PrivateTarget"
                for finding in private_findings
            ), private_findings

            shadow_scope = feature_scope.resolve_feature_envy_scope(
                project,
                [shadow_target],
                "src/main/java/app/ShadowTarget.java:method=calculate()|line=5",
                {"target_class": "app.ShadowTarget"},
            )
            shadow_model = semantic.build_scoped_project_model(
                project,
                shadow_scope.files,
            )
            shadow_findings = [
                finding
                for finding in semantic.evaluate_scoped_guard_findings(
                    shadow_model,
                    "feature_envy",
                )
                if finding.class_name == "ShadowTarget"
            ]
            assert len(shadow_findings) == 1, shadow_findings
            assert str(shadow_findings[0].attributes["envied_type"]).endswith(
                ".LocalReceiver"
            ), shadow_findings[0]

            java_reads.clear()
            disambiguated = feature_scope.resolve_feature_envy_scope(
                project,
                [chosen],
                "src/main/java/chosen/ChosenTarget.java:method=calculate()|line=3",
                {"target_class": "chosen.ChosenTarget"},
            )
            assert disambiguated.ancestors == (
                "right.Parent",
            ), disambiguated.witness()
            assert right_parent.relative_to(project).as_posix() in disambiguated.files
            assert wrong_parent.relative_to(project).as_posix() not in disambiguated.files

            _expect_error(
                "ANCESTOR_TYPE_AMBIGUOUS",
                lambda: feature_scope.resolve_feature_envy_scope(
                    project,
                    [ambiguous],
                    "src/main/java/ambiguous/AmbiguousTarget.java:method=calculate()|line=4",
                    {"target_class": "ambiguous.AmbiguousTarget"},
                ),
            )
        finally:
            Path.read_bytes = original_read_bytes

        assert not any(path.name.startswith("Noise") for path in java_reads), java_reads
        assert receiver.resolve() not in java_reads, java_reads

    print(
        "target feature-envy scope self-check PASS "
        "receiver_source=excluded inherited_ancestor=returned "
        "private_ancestor_field=excluded child_shadow=preferred "
        "explicit_import=disambiguated wildcard_collision=fail_closed "
        "budgets=32_files/8MiB "
        "existing_profile_inherited_field_hit=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
