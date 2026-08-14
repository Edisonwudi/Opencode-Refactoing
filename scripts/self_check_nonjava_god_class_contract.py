#!/usr/bin/env python3
"""Focused checks for the source-derived non-Java God Class contract."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_adapters import (  # noqa: E402
    capture_metric_snapshot,
    detector_profile_for,
)
from smell_core.god_class import (  # noqa: E402
    NONJAVA_GOD_CLASS_PROFILE_ID,
    nonjava_god_class_metrics,
    nonjava_god_class_product_profile,
)
from smell_core.location import LocationTarget  # noqa: E402


def _python_candidate(methods: int) -> str:
    lines = ["class Candidate:", "    class_field = 1"]
    for index in range(methods):
        lines.extend([
            f"    def method_{index}(self, value):",
            f"        self.field_{index} = value",
            "        if value > 0:",
            "            value -= 1",
            "        if value > 1:",
            "            value -= 1",
            *[f"        value += {n}" for n in range(7)],
            "        return value",
        ])
    return "\n".join(lines) + "\n"


def _c_candidate(methods: int) -> str:
    fields = "int first;\nint second;\n"
    bodies = []
    for index in range(methods):
        padding = "\n".join(f"    value += {n};" for n in range(7))
        bodies.append(
            f"int method_{index}(int value) {{\n"
            "    if (value > 0) value--;\n"
            "    if (value > 1) value--;\n"
            f"{padding}\n"
            "    return value;\n"
            "}"
        )
    return fields + "int declared_only(int value);\n" + "\n".join(bodies) + "\n"


def _cpp_candidate(methods: int) -> str:
    bodies = []
    for index in range(methods):
        padding = "\n".join(f"        value += {n};" for n in range(7))
        bodies.append(
            f"    int method_{index}(int value) {{\n"
            "        if (value > 0) value--;\n"
            "        if (value > 1) value--;\n"
            f"{padding}\n"
            "        return value;\n"
            "    }"
        )
    return (
        "class Candidate {\npublic:\n"
        "    int first;\n"
        "    int second;\n"
        "    void declared_only();\n"
        + "\n".join(bodies)
        + "\n};\n"
    )


def _cpp_out_of_class_candidate(methods: int) -> str:
    declarations = "\n".join(
        f"    int method_{index}(int value);" for index in range(methods)
    )
    definitions = []
    for index in range(methods):
        padding = "\n".join(f"    value += {n};" for n in range(7))
        definitions.append(
            f"int Candidate::method_{index}(int value) {{\n"
            "    if (value > 0) value--;\n"
            "    if (value > 1) value--;\n"
            f"{padding}\n"
            "    return value;\n"
            "}"
        )
    return (
        "class Candidate {\npublic:\n"
        "    int first;\n"
        "    int second;\n"
        "    void declared_only();\n"
        f"{declarations}\n"
        "};\n"
        + "\n".join(definitions)
        + "\n"
    )


def main() -> int:
    sources = {
        "python": _python_candidate(10),
        "c": _c_candidate(10),
        "cpp": _cpp_candidate(10),
    }
    expected = {
        "python": {"nom": 10, "nof": 11, "wmc": 20},
        "c": {"nom": 10, "nof": 2, "wmc": 20},
        "cpp": {"nom": 11, "nof": 2, "wmc": 21},
    }
    for language, source in sources.items():
        metrics = nonjava_god_class_metrics(source, language)
        for name, value in expected[language].items():
            assert metrics[name] == value, (language, metrics)
        assert metrics["loc"] >= 100, (language, metrics)
        profile = nonjava_god_class_product_profile(metrics)
        assert profile["id"] == NONJAVA_GOD_CLASS_PROFILE_ID, profile
        assert profile["finding_present"] is True, profile
        assert profile["pass"] is False, profile
        assert "atfd" not in metrics, metrics
        assert profile["unsupported_metrics"] == [{
            "name": "atfd",
            "participates_in_finding": False,
            "reason": "nonjava_foreign_owner_resolution_unavailable",
        }], profile

        static_profile = detector_profile_for(SimpleNamespace(
            smell="god_class",
            language=language,
        ))
        assert static_profile["definition"] == (
            "source_derived_multi_metric_profile"
        ), static_profile
        assert static_profile["profile"]["id"] == (
            NONJAVA_GOD_CLASS_PROFILE_ID
        ), static_profile
        if language == "cpp":
            assert static_profile["cpp_owner_definition_closure"] == (
                "same-explicit-file-exact-qualified-owner-v1"
            ), static_profile

    inline_cpp = nonjava_god_class_metrics(_cpp_candidate(10), "cpp")
    out_of_class_cpp = nonjava_god_class_metrics(
        _cpp_out_of_class_candidate(10),
        "cpp",
    )
    assert out_of_class_cpp["nom"] == inline_cpp["nom"], (
        inline_cpp,
        out_of_class_cpp,
    )
    assert out_of_class_cpp["wmc"] == inline_cpp["wmc"], (
        "moving the same owner methods out of class must not reduce WMC",
        inline_cpp,
        out_of_class_cpp,
    )
    assert nonjava_god_class_product_profile(out_of_class_cpp)[
        "finding_present"
    ] is True, out_of_class_cpp

    left_only = (
        "namespace left {\n"
        + _cpp_out_of_class_candidate(10)
        + "}\n"
    )
    same_named_namespaces = (
        left_only
        + "namespace right {\n"
        + _cpp_out_of_class_candidate(3)
        + "}\n"
    )
    left_metrics = nonjava_god_class_metrics(
        left_only,
        "cpp",
        class_name="left::Candidate",
    )
    qualified_metrics = nonjava_god_class_metrics(
        same_named_namespaces,
        "cpp",
        class_name="left::Candidate",
    )
    assert qualified_metrics == left_metrics, (
        "same-named class in another namespace must not contribute",
        left_metrics,
        qualified_metrics,
    )
    try:
        nonjava_god_class_metrics(
            same_named_namespaces,
            "cpp",
            class_name="Candidate",
        )
    except ValueError as exc:
        assert "missing or ambiguous" in str(exc), exc
    else:
        raise AssertionError(
            "an unqualified duplicate C++ God Class owner must fail closed"
        )

    with tempfile.TemporaryDirectory() as directory:
        project_root = Path(directory)
        target_file = project_root / "candidate.cpp"
        target_file.write_text(
            _cpp_out_of_class_candidate(10),
            encoding="utf-8",
        )
        target = LocationTarget(
            raw="candidate.cpp:class=Candidate",
            project_path=Path("candidate.cpp"),
            file_path=target_file,
            class_name="Candidate",
        )
        snapshot = capture_metric_snapshot(
            SimpleNamespace(
                smell="god_class",
                language="cpp",
                project_root=project_root,
                locations=[target],
                finding_contract={},
                target_context={},
            ),
            "",
        )
        assert snapshot["ok"] is True, snapshot
        assert snapshot["objectives"]["wmc"] == inline_cpp["wmc"], snapshot
        assert snapshot["finding_present"] is True, snapshot

    reduced = nonjava_god_class_product_profile({
        "nom": 5,
        "nof": 3,
        "wmc": 20,
        "loc": 60,
    })
    assert reduced["finding_present"] is False, reduced
    assert reduced["pass"] is True, reduced
    assert reduced["triggered_signals"] == [], reduced

    print("non-Java God Class contract self-check: PASS languages=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
