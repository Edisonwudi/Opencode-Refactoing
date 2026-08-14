#!/usr/bin/env python3
"""Production-diff filtering and Python switch metrics for non-Java checkpoints.

Covers four integration gaps found by the non-Java checkpoint audit:

1. C++ projects keep production code in ``.h`` headers (rocksdb); the header
   must count as a production source for ``language=cpp``.
2. Plain ``build/`` trees (CMake compiler probes, Gradle generated sources)
   are outputs and must never count as a production diff.
3. Python has no switch statement; the switch_statements objective must count
   dispatch branches (if/elif chains, match statements) via tree-sitter, not
   the word "case" inside ``#`` comments.
4. God Class finding/PASS uses one source-derived multi-metric predicate.  The
   5% relative reduction is only the minimum for an ``IMPROVED`` continuation;
   a smaller edit may still PASS when it crosses the actual finding boundary.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"


def run_bridge(project: Path, *args: str) -> dict:
    bridge_args = [*args, "--output-detail", "audit"]
    result = subprocess.run(
        [sys.executable, str(BRIDGE), *bridge_args, "--config", str(CONFIG)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def git(project: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=project, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr!r}")


def init_repo(project: Path) -> None:
    git(project, "init", "-q")
    git(project, "config", "user.email", "self-check@example.invalid")
    git(project, "config", "user.name", "Self Check")


def cpp_function(name: str, lines: int) -> str:
    body = "\n".join(f"    value += {index};" for index in range(lines))
    return f"int {name}(int a, int b) {{\n    int value = 0;\n{body}\n    return value;\n}}\n"


def check_cpp_header_is_production() -> None:
    with tempfile.TemporaryDirectory(prefix="cpp-header-production-") as raw:
        project = Path(raw)
        (project / "fixture.cpp").write_text(cpp_function("target", 65), encoding="utf-8")
        (project / "helpers.h").write_text("#pragma once\ninline int helper(int v) { return v; }\n", encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "cpp",
            "--smell", "long_method",
            "--location", f"{project / 'fixture.cpp'}:method=target|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline

        # Header-only repair must be seen as a production diff for C++.
        with open(project / "helpers.h", "a", encoding="utf-8") as handle:
            handle.write("inline int helper2(int v) { return v * 2; }\n")
        header_only = run_bridge(project, "verify", *common, "--skip-build-test")
        delta = header_only["checkpoint"]["delta"]
        assert header_only["success"] is False, header_only
        assert delta["has_production_diff"] is True, header_only
        assert delta["reason"] == "NO_STRUCTURAL_PROGRESS", delta
        assert "helpers.h" in delta["changed_production_source_files"], delta

        # A real fix plus the header edit passes and lists both files.
        (project / "fixture.cpp").write_text(cpp_function("target", 2), encoding="utf-8")
        repaired = run_bridge(project, "verify", *common, "--skip-build-test")
        assert repaired["success"] is True, repaired
        files = repaired["checkpoint"]["delta"]["changed_production_source_files"]
        assert "fixture.cpp" in files and "helpers.h" in files, files
    print("  scenario cpp-header-production: header_only=NO_STRUCTURAL_PROGRESS repaired=PASS")


def check_build_dir_not_production() -> None:
    with tempfile.TemporaryDirectory(prefix="build-dir-filter-") as raw:
        project = Path(raw)
        (project / "fixture.cpp").write_text(cpp_function("target", 65), encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "cpp",
            "--smell", "long_method",
            "--location", f"{project / 'fixture.cpp'}:method=target|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline

        # A CMake probe source under build/ alone must not count as production.
        probe = project / "build" / "CMakeFiles" / "3.28.3" / "CompilerIdCXX" / "CMakeCXXCompilerId.cpp"
        probe.parent.mkdir(parents=True)
        probe.write_text(cpp_function("main", 3), encoding="utf-8")
        build_only = run_bridge(project, "verify", *common, "--skip-build-test")
        delta = build_only["checkpoint"]["delta"]
        assert build_only["success"] is False, build_only
        assert delta["has_production_diff"] is False, delta
        assert delta["reason"] == "EDIT_REQUIRED", delta
        assert not any("build/" in path.replace("\\", "/") for path in delta["changed_production_source_files"]), delta

        # A real repair passes without the probe polluting the production list.
        (project / "fixture.cpp").write_text(cpp_function("target", 2), encoding="utf-8")
        repaired = run_bridge(project, "verify", *common, "--skip-build-test")
        assert repaired["success"] is True, repaired
        files = repaired["checkpoint"]["delta"]["changed_production_source_files"]
        assert files == ["fixture.cpp"], files
    print("  scenario build-dir-filter: build_only=EDIT_REQUIRED repaired=PASS production=[fixture.cpp]")


PY_SWITCH_BEFORE = '''def display(value, field, empty_value_display):
    # BooleanField needs special-case null-handling, so it comes before the case dispatch
    if field is None:
        return empty_value_display
    elif field == 1:
        return "one"
    elif field == 2:
        return "two"
    elif field == 3:
        return "three"
    elif field == 4:
        return "four"
    elif field == 5:
        return "five"
    elif field == 6:
        return "six"
    elif field == 7:
        return "seven"
    elif field == 8:
        return "eight"
    else:
        return str(field)
'''

PY_SWITCH_AFTER = '''DISPLAY = {
    1: "one", 2: "two", 3: "three", 4: "four",
    5: "five", 6: "six", 7: "seven", 8: "eight",
}


def display(value, field, empty_value_display):
    # BooleanField needs special-case null-handling, so it comes before the case dispatch
    if field is None:
        return empty_value_display
    return DISPLAY.get(field, str(field))
'''


def check_python_switch_metric() -> None:
    with tempfile.TemporaryDirectory(prefix="python-switch-metric-") as raw:
        project = Path(raw)
        (project / "demo.py").write_text(PY_SWITCH_BEFORE, encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "python",
            "--smell", "switch_statements",
            "--location", f"{project / 'demo.py'}:method=display|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        objectives = baseline["metrics"]["objectives"]
        # The '#' comment mentions "case" twice; the objective must reflect the
        # real 10-branch if/elif/else chain, not comment tokens.
        assert objectives["switch_case_count"] == 10.0, objectives
        assert objectives["switch_density"] == 10.0, objectives
        assert baseline["metrics"]["switch_count"] == 1, baseline["metrics"]

        unchanged = run_bridge(project, "verify", *common, "--skip-build-test")
        assert unchanged["success"] is False, unchanged
        assert unchanged["checkpoint"]["delta"]["has_production_diff"] is False, unchanged

        (project / "demo.py").write_text(PY_SWITCH_AFTER, encoding="utf-8")
        repaired = run_bridge(project, "verify", *common, "--skip-build-test")
        assert repaired["success"] is True, repaired
        delta = repaired["checkpoint"]["delta"]
        assert delta["has_production_diff"] is True and delta["metric_progress"] is True, delta
        cases = delta["objectives"]["switch_case_count"]
        assert cases["before"] == 10.0 and cases["after"] == 1.0, cases
    print("  scenario python-switch-metric: baseline_cases=10 repaired=PASS cases 10->1")


def _py_god_class(methods: int, padding: int = 8) -> str:
    lines = ["class Big:"]
    for index in range(methods):
        lines.append(f"    def m{index}(self, v):")
        lines.append(f"        value = v + {index}")
        lines.append("        if value > 0:")
        lines.append("            value -= 1")
        lines.append("        if value > 1:")
        lines.append("            value -= 1")
        for n in range(padding):
            lines.append(f"        value += {n}")
        lines.append("        return value")
    lines.append("")
    return "\n".join(lines)


def check_god_class_min_reduction() -> None:
    with tempfile.TemporaryDirectory(prefix="god-class-floor-") as raw:
        project = Path(raw)
        source = project / "big.py"
        source.write_text(_py_god_class(11), encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "python",
            "--smell", "god_class",
            "--location", f"{source}:class=Big|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        baseline_loc = float(baseline["metrics"]["objectives"]["loc"])
        assert baseline_loc > 100, baseline

        # Token extraction (<5% of the class) must be rejected by the guard
        # AND must not be rescued by the improvement gate.
        trimmed = source.read_text(encoding="utf-8").splitlines()
        del trimmed[3:7]  # drop four body lines (~3% reduction)
        source.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        marginal = run_bridge(project, "verify", *common, "--skip-build-test")
        assert marginal["success"] is False, marginal
        assert marginal["status"] == "SMELL_GUARD_FAILED", marginal
        guard = marginal["smell_guard"]["results"][0]
        assert guard["type"] == "god_class" and guard["success"] is False, guard
        assert "IMPROVED, not PASS" in guard["message"], guard
        delta = marginal["checkpoint"]["delta"]
        assert delta["metric_progress"] is True, delta  # contract still sees the progress
        reduction = delta["objectives"]["loc"]["relative_reduction"]
        assert 0 < reduction < 0.05, delta

        # A cohesive extraction above the progress floor is IMPROVED while the
        # same multi-metric finding remains.
        source.write_text(_py_god_class(10), encoding="utf-8")
        improved = run_bridge(project, "verify", *common, "--skip-build-test")
        assert improved["success"] is False and improved["status"] == "IMPROVED", improved
        reduction = improved["checkpoint"]["delta"]["objectives"]["loc"]["relative_reduction"]
        assert reduction >= 0.05, reduction

        source.write_text(_py_god_class(8), encoding="utf-8")
        repaired = run_bridge(project, "verify", *common, "--skip-build-test")
        assert repaired["success"] is True and repaired.get("resolution") == "resolved", repaired
    print("  scenario god-class-floor: marginal=below-progress-floor split=IMPROVED detector_clear=PASS")


def _py_god_class_boundary(field_count: int) -> str:
    lines = ["class Big:"]
    lines.extend(f"    value_{index} = {index}" for index in range(field_count))
    for index in range(10):
        lines.extend([
            f"    def m{index}(self, value):",
            "        if value > 0: value -= 1",
            "        if value > 1: value -= 1",
            "        value += 1",
            "        return value",
        ])
    lines.append("")
    return "\n".join(lines)


def check_god_class_pass_uses_finding_boundary() -> None:
    with tempfile.TemporaryDirectory(prefix="god-class-boundary-") as raw:
        project = Path(raw)
        source = project / "big.py"
        source.write_text(_py_god_class_boundary(49), encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "python",
            "--smell", "god_class",
            "--location", f"{source}:class=Big|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        objectives = baseline["metrics"]["objectives"]
        assert objectives == {
            "loc": 100.0,
            "nof": 49.0,
            "nom": 10.0,
            "wmc": 20.0,
        }, objectives

        # Removing one field is only a 1% LOC reduction, but it removes the
        # second triggered signal (LOC) and therefore crosses the real product
        # predicate.  The 5% IMPROVED floor must not veto this PASS.
        source.write_text(_py_god_class_boundary(48), encoding="utf-8")
        repaired = run_bridge(project, "verify", *common, "--skip-build-test")
        assert repaired["success"] is True, repaired
        loc_delta = repaired["checkpoint"]["delta"]["objectives"]["loc"]
        assert 0 < loc_delta["relative_reduction"] < 0.05, loc_delta
    print("  scenario god-class-boundary: loc 100->99 under 5%=PASS")


def _cpp_god_class(name: str, fields: int = 110) -> str:
    members = "\n".join(f"    int value_{index};" for index in range(fields))
    methods = "\n".join(
        f"    int method_{index}(int value) {{ "
        "if (value > 0) value--; if (value > 1) value--; return value; }"
        for index in range(10)
    )
    return f"class {name} {{\npublic:\n{members}\n{methods}\n}};\n"


def check_god_class_requires_unique_definition() -> None:
    with tempfile.TemporaryDirectory(prefix="god-class-definition-") as raw:
        project = Path(raw)
        source = project / "big.cpp"
        original = _cpp_god_class("Big")
        source.write_text(original, encoding="utf-8")
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        common = (
            "--project-root", str(project),
            "--language", "cpp",
            "--smell", "god_class",
            "--location", f"{source}:class=Big|line=1",
        )
        baseline = run_bridge(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline
        baseline_loc = baseline["metrics"]["objectives"]["loc"]

        # A forward declaration at the frozen line is not a one-line class
        # definition and must not hide the unchanged body-bearing definition.
        source.write_text("class Big;\n" + original, encoding="utf-8")
        current = run_bridge(project, "verify", *common, "--skip-build-test")
        metrics = current["checkpoint"]["current_metrics"]
        assert current["success"] is False, current
        assert metrics["target_match_count"] == 1, metrics
        assert metrics["objectives"]["loc"] == baseline_loc, metrics
        assert current["checkpoint"]["delta"]["reason"] == "NO_STRUCTURAL_PROGRESS", current

        source.write_text(
            "class Big {\npublic:\n    int value int other;\n};\n",
            encoding="utf-8",
        )
        malformed = run_bridge(project, "verify", *common, "--skip-build-test")
        malformed_metrics = malformed["checkpoint"]["current_metrics"]
        assert malformed["success"] is False, malformed
        assert malformed["checkpoint"]["delta"]["reason"] == (
            "SEMANTIC_CONTRACT_REGRESSION"
        ), malformed
        assert malformed_metrics["target_match_count"] == 1, malformed_metrics
        assert malformed_metrics["target_parseable_match_count"] == 0, (
            malformed_metrics
        )

    with tempfile.TemporaryDirectory(prefix="god-class-ambiguous-") as raw:
        project = Path(raw)
        source = project / "ambiguous.cpp"
        source.write_text(
            "namespace Left {\n" + _cpp_god_class("Big") + "}\n"
            "namespace Right {\n" + _cpp_god_class("Big") + "}\n",
            encoding="utf-8",
        )
        init_repo(project)
        git(project, "add", ".")
        git(project, "commit", "-qm", "baseline")
        ambiguous = run_bridge(
            project,
            "capture-baseline",
            "--project-root", str(project),
            "--language", "cpp",
            "--smell", "god_class",
            "--location", f"{source}:class=Big|line=2",
        )
        assert ambiguous["success"] is False, ambiguous
        assert "target_class_definition_ambiguous" in ambiguous["error"], ambiguous
    print(
        "  scenario god-class-definition: forward_decl=ignored "
        "malformed=fail_closed ambiguous=fail_closed"
    )


def _c_god_class_with_parser_recovery() -> str:
    members = "\n".join(f"int value_{index};" for index in range(110))
    methods = "\n".join(
        f"int method_{index}(int value) {{ "
        "if (value > 0) value--; if (value > 1) value--; return value; }"
        for index in range(10)
    )
    return (
        "#define UNUSED(value) value\n"
        "int module_entry(int UNUSED(*value)) { return *value; }\n"
        f"{members}\n{methods}\n"
    )


def _cpp_god_class_with_parser_recovery() -> str:
    members = "\n".join(f"    int value_{index};" for index in range(110))
    methods = "\n".join(
        f"    int method_{index}(int value) {{ "
        "if (value > 0) value--; if (value > 1) value--; return value; }"
        for index in range(10)
    )
    return (
        "#define UNUSED(value) value\n"
        "class Big {\npublic:\n"
        "    int method(int UNUSED(*value)) { return *value; }\n"
        f"{members}\n{methods}\n"
        "};\n"
    )


def check_god_class_frozen_parser_recovery() -> None:
    cases = (
        ("c", "module.c", "module", _c_god_class_with_parser_recovery()),
        ("cpp", "big.cpp", "Big", _cpp_god_class_with_parser_recovery()),
    )
    for language, filename, class_name, original in cases:
        with tempfile.TemporaryDirectory(prefix=f"god-class-recovery-{language}-") as raw:
            project = Path(raw)
            source = project / filename
            source.write_text(original, encoding="utf-8")
            init_repo(project)
            git(project, "add", ".")
            git(project, "commit", "-qm", "baseline")
            common = (
                "--project-root", str(project),
                "--language", language,
                "--smell", "god_class",
                "--location", f"{source}:class={class_name}|line=1",
            )
            baseline = run_bridge(project, "capture-baseline", *common)
            assert baseline["success"] is True, baseline
            assert baseline["metrics"]["parser_recovery_required"] is True, baseline
            assert baseline["metrics"]["target_syntax_issue_witnesses"], baseline

            retained_source = original.replace("int value_0;\n", "", 1)
            retained_source = retained_source.replace(
                "(int UNUSED(*value))",
                "( int UNUSED(*value) )",
                1,
            )
            source.write_text(retained_source, encoding="utf-8")
            retained = run_bridge(project, "verify", *common, "--skip-build-test")
            assert retained["success"] is False, retained
            semantic = retained["checkpoint"]["delta"]["semantic_contract"]
            assert semantic.get("regressions") == [], retained

            if language == "c":
                malformed = original + "\nint newly_broken( {\n"
            else:
                malformed = original.replace(
                    "};\n",
                    "    int newly_broken() { return (1 + ); }\n};\n",
                )
            source.write_text(malformed, encoding="utf-8")
            rejected = run_bridge(project, "verify", *common, "--skip-build-test")
            assert rejected["success"] is False, rejected
            assert rejected["checkpoint"]["delta"]["reason"] == (
                "SEMANTIC_CONTRACT_REGRESSION"
            ), rejected
    print(
        "  scenario god-class-parser-recovery: frozen_c_cpp=accepted "
        "new_syntax_error=rejected"
    )


def main() -> int:
    print("Non-Java production filter / switch metric self-check")
    check_cpp_header_is_production()
    check_build_dir_not_production()
    check_python_switch_metric()
    check_god_class_min_reduction()
    check_god_class_pass_uses_finding_boundary()
    check_god_class_requires_unique_definition()
    check_god_class_frozen_parser_recovery()
    print("Non-Java production filter / switch metric self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
