#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"


def _run(project: Path, *args: str) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(BRIDGE),
            *args,
            "--config",
            str(CONFIG),
            "--output-detail",
            "audit",
        ],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _target(
    language: str,
    body_lines: int,
    *,
    parameter_prefix: str = "p",
) -> str:
    suffix = "" if language == "c" else ""
    params = ", ".join(
        f"int {parameter_prefix}{index}" for index in range(8)
    )
    body = "\n".join(
        f"    value += {parameter_prefix}0 + {index};"
        for index in range(body_lines)
    )
    return (
        f"int target({params}) {{\n"
        "    int value = 0;\n"
        f"{body}\n"
        "    return value;\n"
        f"}}{suffix}\n"
    )


def _long_helper(param_count: int) -> str:
    params = ", ".join(f"int p{index}" for index in range(param_count))
    return f"\nstatic int extracted_helper({params}) {{ return p0; }}\n"


def _cpp_owner(
    owner: str,
    parameter_prefix: str = "p",
    *,
    method_name: str = "retained",
    first_parameter_type: str = "int",
) -> str:
    params = ", ".join(
        f"{first_parameter_type if index == 0 else 'int'} "
        f"{parameter_prefix}{index}"
        for index in range(6)
    )
    return (
        f"class {owner} {{\n"
        "public:\n"
        f"    int {method_name}({params}) {{ return {parameter_prefix}0; }}\n"
        "};\n"
    )


def _check(language: str, helper_params: int) -> None:
    with tempfile.TemporaryDirectory(prefix=f"cross-smell-{language}-") as raw:
        project = Path(raw)
        suffix = ".c" if language == "c" else ".cpp"
        source = project / f"target{suffix}"
        source.write_text(_target(language, 65), encoding="utf-8")
        _git(project, "init", "-q")
        _git(project, "config", "user.email", "self-check@example.invalid")
        _git(project, "config", "user.name", "Self Check")
        _git(project, "add", source.name)
        _git(project, "commit", "-qm", "baseline")

        common = (
            "--project-root",
            str(project),
            "--language",
            language,
            "--smell",
            "long_method",
            "--location",
            f"{source}:method=target|line=1",
        )
        baseline = _run(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline

        # The frozen target already exceeds the LPL threshold. Shortening its
        # body without changing that pre-existing declaration must not be
        # mistaken for a newly introduced cross-smell finding.
        source.write_text(
            _target(language, 2, parameter_prefix="renamed"),
            encoding="utf-8",
        )
        safe = _run(project, "verify", *common, "--skip-build-test")
        assert safe["status"] == "PASS", safe

        source.write_text(
            _target(language, 2, parameter_prefix="renamed")
            + _long_helper(helper_params),
            encoding="utf-8",
        )
        rejected = _run(project, "verify", *common, "--skip-build-test")
        assert rejected["status"] == "SMELL_GUARD_FAILED", rejected
        assert rejected["failure_pack"]["failure_category"] == (
            "CROSS_SMELL_REGRESSION"
        ), rejected
        semantic = rejected["checkpoint"]["delta"]["semantic_contract"]
        regressions = list(semantic.get("regressions") or [])
        assert any(
            item.startswith("CROSS_SMELL_LONG_PARAMETER_LIST_INTRODUCED")
            for item in regressions
        ), semantic
        findings = rejected["checkpoint"]["current_metrics"][
            "cross_smell_regression"
        ]["new_findings"]
        assert len(findings) == 1, findings
        assert findings[0]["name"] == "extracted_helper", findings
        assert findings[0]["parameter_count"] == helper_params, findings


def _check_cpp_move_lineage() -> None:
    with tempfile.TemporaryDirectory(prefix="cross-smell-cpp-move-") as raw:
        project = Path(raw)
        source = project / "a.cpp"
        moved = project / "b.cpp"
        source.write_text(
            _target("cpp", 65) + _cpp_owner("A", method_name="badName"),
            encoding="utf-8",
        )
        _git(project, "init", "-q")
        _git(project, "config", "user.email", "self-check@example.invalid")
        _git(project, "config", "user.name", "Self Check")
        _git(project, "add", source.name)
        _git(project, "commit", "-qm", "baseline")

        common = (
            "--project-root",
            str(project),
            "--language",
            "cpp",
            "--smell",
            "long_method",
            "--location",
            f"{source}:method=target|line=1",
        )
        baseline = _run(project, "capture-baseline", *common)
        assert baseline["success"] is True, baseline

        source.write_text(_target("cpp", 2), encoding="utf-8")
        moved.write_text(
            _cpp_owner(
                "B",
                parameter_prefix="renamed",
                method_name="goodName",
            ),
            encoding="utf-8",
        )
        retained_move = _run(project, "verify", *common, "--skip-build-test")
        assert retained_move["status"] == "PASS", retained_move
        move_contract = retained_move["checkpoint"]["current_metrics"][
            "cross_smell_regression"
        ]
        assert move_contract["new_findings"] == [], move_contract

        moved.write_text(
            _cpp_owner(
                "B",
                parameter_prefix="renamed",
                method_name="goodName",
                first_parameter_type="long",
            ),
            encoding="utf-8",
        )
        changed_signature = _run(
            project, "verify", *common, "--skip-build-test"
        )
        assert changed_signature["status"] == "SMELL_GUARD_FAILED", (
            changed_signature
        )
        assert changed_signature["failure_pack"]["failure_category"] == (
            "CROSS_SMELL_REGRESSION"
        ), changed_signature
        findings = changed_signature["checkpoint"]["current_metrics"][
            "cross_smell_regression"
        ]["new_findings"]
        assert len(findings) == 1, findings
        assert findings[0]["name"] == "goodName", findings
        assert findings[0]["owner"] == "B", findings
        assert findings[0]["parameter_fingerprints"] == [
            "long:renamed0",
            *(f"int:renamed{index}" for index in range(1, 6)),
        ], findings


def main() -> int:
    _check("c", 17)
    _check("cpp", 12)
    _check_cpp_move_lineage()
    print(
        "Cross-smell regression self-check passed: C=17 C++=12, "
        "baseline findings and C++ declaration moves retained"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
