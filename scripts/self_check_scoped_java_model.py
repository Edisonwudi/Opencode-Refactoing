#!/usr/bin/env python3
"""Prove the scoped Java model reads only explicit production sources."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import smell_core.java.semantic_detector as detector  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scoped-java-model-") as temp_dir:
        temp = Path(temp_dir)
        project = temp / "project"
        selected = project / "src/main/java/example/Selected.java"
        support = project / "src/main/java/example/Support.java"
        test_source = project / "src/test/java/example/SelectedTest.java"
        generated = project / "build/generated/example/Generated.java"
        outside = temp / "Outside.java"

        _write(
            selected,
            "package example;\n"
            "class Selected {\n"
            "  private final Support support = new Support();\n"
            "  int work() { return support.value(); }\n"
            "}\n",
        )
        _write(
            support,
            "package example;\n"
            "class Support { int value() { return 7; } }\n",
        )
        _write(
            test_source,
            "package example; class SelectedTest { void testWork() {} }\n",
        )
        _write(
            generated,
            "package example; class Generated { int generated() { return 1; } }\n",
        )
        _write(outside, "class Outside {}\n")
        _write(
            project / "pom.xml",
            "<project><modelVersion>4.0.0</modelVersion></project>\n",
        )

        unrelated_count = 512
        for index in range(unrelated_count):
            _write(
                project / f"src/main/java/unrelated/Noise{index:04d}.java",
                f"package unrelated; class Noise{index:04d} "
                f"{{ int value() {{ return {index}; }} }}\n",
            )

        forbidden_names = (
            "_list_java_files",
            "_detect_feature_envy",
            "_detect_refused_bequest",
            "_detect_data_clumps",
            "_detect_god_class",
            "_detect_dead_code",
        )
        originals = {name: getattr(detector, name) for name in forbidden_names}
        original_read_bytes = Path.read_bytes
        java_reads: list[Path] = []

        def forbidden(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("scoped model invoked source discovery or a detector")

        def tracked_read_bytes(path: Path) -> bytes:
            if path.suffix.casefold() == ".java":
                java_reads.append(path.resolve())
            return original_read_bytes(path)

        try:
            for name in forbidden_names:
                setattr(detector, name, forbidden)
            Path.read_bytes = tracked_read_bytes
            model = detector.build_scoped_project_model(
                project,
                [
                    support.relative_to(project),
                    selected,
                    selected.relative_to(project),
                    test_source.relative_to(project),
                    generated.relative_to(project),
                ],
            )
        finally:
            Path.read_bytes = original_read_bytes
            for name, original in originals.items():
                setattr(detector, name, original)

        expected_files = {
            "src/main/java/example/Selected.java",
            "src/main/java/example/Support.java",
        }
        assert {item.rel_path for item in model.files} == expected_files, model.files
        assert java_reads == sorted({selected.resolve(), support.resolve()}), java_reads
        assert set(model.classes) == {"example.Selected", "example.Support"}, model.classes
        assert {item.method_name for item in model.methods} == {"work", "value"}, model.methods
        assert not any("Noise" in item.class_name for item in model.classes.values())

        try:
            detector.build_scoped_project_model(project, [outside])
        except ValueError as exc:
            assert "SCOPED_SOURCE_OUTSIDE_PROJECT" in str(exc), exc
        else:
            raise AssertionError("outside source was admitted to the scoped model")

        non_java = project / "src/main/java/example/README.txt"
        _write(non_java, "not Java\n")
        try:
            detector.build_scoped_project_model(project, [non_java])
        except ValueError as exc:
            assert "SCOPED_SOURCE_NOT_JAVA_FILE" in str(exc), exc
        else:
            raise AssertionError("non-Java input was admitted to the scoped model")

    print(
        "scoped-java-model self-check PASS "
        f"explicit=2 unrelated={unrelated_count} tests=excluded duplicates=excluded "
        "discovery=blocked detectors=blocked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
