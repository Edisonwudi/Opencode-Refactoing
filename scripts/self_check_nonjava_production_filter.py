#!/usr/bin/env python3
"""Production-diff filtering and Python switch metrics for non-Java checkpoints.

Covers three integration gaps found by the non-Java checkpoint audit:

1. C++ projects keep production code in ``.h`` headers (rocksdb); the header
   must count as a production source for ``language=cpp``.
2. Plain ``build/`` trees (CMake compiler probes, Gradle generated sources)
   are outputs and must never count as a production diff.
3. Python has no switch statement; the switch_statements objective must count
   dispatch branches (if/elif chains, match statements) via tree-sitter, not
   the word "case" inside ``#`` comments.
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
    result = subprocess.run(
        [sys.executable, str(BRIDGE), *args, "--config", str(CONFIG)],
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


def main() -> int:
    print("Non-Java production filter / switch metric self-check")
    check_cpp_header_is_production()
    check_build_dir_not_production()
    check_python_switch_metric()
    print("Non-Java production filter / switch metric self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
