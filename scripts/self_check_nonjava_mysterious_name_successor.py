#!/usr/bin/env python3
"""Adversarial checks for the non-Java Mysterious Name successor contract."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"
OBJECTIVE = "target_suspicious_name_present"


def _bridge(
    project: Path,
    command: str,
    language: str,
    smell: str,
    location: str,
    evidence: str,
    selector_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    kind = re.search(r"(?:^|;\s*)kind=([^;]+)", evidence)
    name = re.search(r"(?:^|;\s*)name=([^;]+)", evidence)
    assert smell == "mysterious_name" and kind and name, evidence
    selector = {
        "symbol_kind": kind.group(1).strip(),
        "symbol_name": name.group(1).strip(),
        **dict(selector_extra or {}),
    }
    args = [
        sys.executable,
        str(BRIDGE),
        command,
        "--output-detail",
        "audit",
        "--config",
        str(CONFIG),
        "--project-root",
        str(project),
        "--language",
        language,
        "--smell",
        smell,
        "--location",
        location,
        "--smell-evidence",
        evidence,
        "--target-context-json",
        json.dumps(selector, separators=(",", ":"), sort_keys=True),
    ]
    if command == "verify":
        args.append("--skip-build-test")
    result = subprocess.run(
        args,
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(
            f"{language}/{smell} {command}: {result.stderr}\n{result.stdout}"
        )
    return json.loads(result.stdout)


def _init_project(project: Path, filename: str, source: str) -> Path:
    target = project / filename
    target.write_text(source, encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", filename]):
        result = subprocess.run(command, cwd=project, text=True, capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=mysterious-name-self-check",
            "-c",
            "user.email=mysterious-name@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=project,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return target


def _exercise(
    language: str,
    filename: str,
    before: str,
    after: str,
    *,
    kind: str,
    name: str,
    reason: str = "too_short",
    location_line: int = 1,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"mn-{language}-") as temp_dir:
        project = Path(temp_dir)
        source = _init_project(project, filename, before)
        location = f"{filename}:method=proc|line={location_line}"
        evidence = f"kind={kind}; name={name}; reason={reason}; len={len(name)}"
        baseline = _bridge(
            project,
            "capture-baseline",
            language,
            "mysterious_name",
            location,
            evidence,
        )
        if baseline.get("success") is not True:
            raise AssertionError(baseline)
        source.write_text(after, encoding="utf-8")
        return _bridge(
            project,
            "verify",
            language,
            "mysterious_name",
            location,
            evidence,
        )


def _assert_pass(result: dict[str, object], label: str) -> None:
    assert result.get("success") is True, (label, result)
    checkpoint = dict(result.get("checkpoint") or {})
    current = dict(checkpoint.get("current_metrics") or {})
    delta = dict(checkpoint.get("delta") or {})
    successor = dict(current.get("successor_contract") or {})
    assert delta.get("reason") == "METRIC_PROGRESS", (label, delta)
    assert successor.get("status") == "accepted", (label, successor)
    assert successor.get("same_hunk", {}).get("ok") is True, (label, successor)
    guard_results = list(dict(result.get("smell_guard") or {}).get("results") or [])
    assert len(guard_results) == 1 and all(
        item.get("success") is True for item in guard_results
    ), (label, guard_results)
    assert guard_results[0]["details"]["guard"] == "checkpoint_contract", (
        label,
        guard_results,
    )


def _assert_parser_recovery_pass(
    result: dict[str, object],
    label: str,
) -> None:
    _assert_pass(result, label)
    checkpoint = dict(result.get("checkpoint") or {})
    current = dict(checkpoint.get("current_metrics") or {})
    assert current.get("target_file_parseable") is False, (label, current)
    assert current.get("target_container_boundary_complete") is True, (
        label,
        current,
    )
    assert current.get("parser_recovery_required") is True, (label, current)
    assert list(current.get("target_syntax_issue_witnesses") or []), (
        label,
        current,
    )


def _assert_fail(
    result: dict[str, object],
    label: str,
    *,
    reason: str | None = None,
    code: str | None = None,
) -> None:
    assert result.get("success") is False, (label, result)
    checkpoint = dict(result.get("checkpoint") or {})
    delta = dict(checkpoint.get("delta") or {})
    current = dict(checkpoint.get("current_metrics") or {})
    if reason:
        assert delta.get("reason") == reason, (label, delta)
    if code:
        codes = {
            str(item.get("code") or "")
            for item in list(current.get("guard_violations") or [])
            if isinstance(item, dict)
        }
        assert code in codes, (label, codes, current)


def _assert_syntax_fail(result: dict[str, object], label: str) -> None:
    assert result.get("success") is False, (label, result)
    checkpoint = dict(result.get("checkpoint") or {})
    delta = dict(checkpoint.get("delta") or {})
    current = dict(checkpoint.get("current_metrics") or {})
    if label.startswith("python/"):
        assert delta.get("reason") == "CURRENT_DETECTOR_UNAVAILABLE", (
            label,
            delta,
        )
        assert current.get("ok") is False, (label, current)
        assert current.get("error") == "MN_TARGET_FILE_SYNTAX_INVALID", (
            label,
            current,
        )
        return
    assert delta.get("reason") == "SEMANTIC_CONTRACT_REGRESSION", (
        label,
        delta,
    )
    semantic = dict(delta.get("semantic_contract") or {})
    assert semantic.get("regressions") == [
        "TARGET_SYNTAX_RECOVERY_REGRESSION"
    ], (label, semantic)
    assert list(semantic.get("new_syntax_issue_witnesses") or []), (
        label,
        semantic,
    )


def _assert_target_container_syntax_invalid_baseline(
    language: str,
    filename: str,
    source_text: str,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=f"mn-target-syntax-{language}-"
    ) as temp_dir:
        project = Path(temp_dir)
        _init_project(project, filename, source_text)
        result = _bridge(
            project,
            "capture-baseline",
            language,
            "mysterious_name",
            f"{filename}:method=proc|line=1",
            "kind=param; name=n; reason=too_short; len=1",
        )
        assert result.get("success") is False, result
        assert "MN_TARGET_CONTAINER_SYNTAX_INVALID" in str(
            result.get("error") or ""
        ), result


def _assert_ambiguous_baseline(
    language: str,
    filename: str,
    source_text: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"mn-ambiguous-{language}-") as temp_dir:
        project = Path(temp_dir)
        _init_project(project, filename, source_text)
        result = _bridge(
            project,
            "capture-baseline",
            language,
            "mysterious_name",
            f"{filename}:method=proc|line=1",
            "kind=local; name=n; reason=too_short; len=1",
        )
        assert result.get("success") is False, result
        assert "TARGET_AMBIGUOUS" in str(result.get("error") or ""), result


def _assert_duplicate_container_identity_baseline() -> None:
    source_text = (
        "def proc(n):\n    return n\n\n\n"
        "def proc(n):\n    return n\n"
    )
    with tempfile.TemporaryDirectory(prefix="mn-duplicate-container-") as temp_dir:
        project = Path(temp_dir)
        _init_project(project, "demo.py", source_text)
        result = _bridge(
            project,
            "capture-baseline",
            "python",
            "mysterious_name",
            "demo.py:method=proc|line=1",
            "kind=param; name=n; reason=too_short; len=1",
        )
        assert result.get("success") is False, result
        assert "MN_BASELINE_CONTAINER_IDENTITY_AMBIGUOUS" in str(
            result.get("error") or ""
        ), result


def _assert_macro_recovery_container_baseline() -> None:
    source_text = (
        "#define API_EXPORT\n"
        "API_EXPORT int proc(int n) {\n"
        "    return n + 1;\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory(prefix="mn-macro-recovery-") as temp_dir:
        project = Path(temp_dir)
        _init_project(project, "demo.c", source_text)
        result = _bridge(
            project,
            "capture-baseline",
            "c",
            "mysterious_name",
            "demo.c:method=proc|line=2",
            "kind=param; name=n; reason=too_short; len=1",
        )
        assert result.get("success") is True, result
        metrics = dict(result.get("metrics") or {})
        assert metrics.get("parser_recovery_required") is True, metrics
        assert metrics.get("target_container_boundary_complete") is True, metrics


def _assert_python_rebinding_is_one_symbol() -> None:
    result = _exercise(
        "python",
        "demo.py",
        (
            "def proc(value):\n"
            "    m = value + 1\n"
            "    if value > 0:\n"
            "        m = value + 2\n"
            "    return m\n"
        ),
        (
            "def proc(value):\n"
            "    mode = value + 1\n"
            "    if value > 0:\n"
            "        mode = value + 2\n"
            "    return mode\n"
        ),
        kind="local",
        name="m",
    )
    _assert_pass(result, "python/rebinding-local")


def _assert_explicit_multi_declaration_cohort() -> None:
    before = (
        "int proc(int value) {\n"
        "#if USE_FIRST\n"
        "    int n = value + 1;\n"
        "#else\n"
        "    int n = value + 2;\n"
        "#endif\n"
        "    return n;\n"
        "}\n"
    )
    after = before.replace("int n =", "int count =").replace(
        "return n;", "return count;"
    )
    with tempfile.TemporaryDirectory(prefix="mn-declaration-cohort-") as temp_dir:
        project = Path(temp_dir)
        source = _init_project(project, "demo.c", before)
        selector = {"declaration_lines": [3, 5]}
        baseline = _bridge(
            project,
            "capture-baseline",
            "c",
            "mysterious_name",
            "demo.c:method=proc|line=1",
            "kind=local; name=n; reason=too_short; len=1",
            selector,
        )
        assert baseline.get("success") is True, baseline
        source.write_text(after, encoding="utf-8")
        verified = _bridge(
            project,
            "verify",
            "c",
            "mysterious_name",
            "demo.c:method=proc|line=1",
            "kind=local; name=n; reason=too_short; len=1",
            selector,
        )
        _assert_pass(verified, "c/conditional-declaration-cohort")


def _assert_preprocessor_container_cohort_is_line_anchored() -> None:
    source_text = (
        "#if FIRST_CHECK\n"
        "int proc(int n) { return n; }\n"
        "#endif\n"
        "#if SECOND_CHECK\n"
        "int proc(int n) { return n + 1; }\n"
        "#endif\n"
    )
    with tempfile.TemporaryDirectory(prefix="mn-preprocessor-container-") as temp_dir:
        project = Path(temp_dir)
        _init_project(project, "demo.c", source_text)
        result = _bridge(
            project,
            "capture-baseline",
            "c",
            "mysterious_name",
            "demo.c:method=proc|line=2",
            "kind=param; name=n; reason=too_short; len=1",
        )
        assert result.get("success") is True, result
        metrics = dict(result.get("metrics") or {})
        identity = dict(metrics.get("finding_identity") or {})
        assert len(identity.get("container_cohort") or []) == 2, identity


def main() -> int:
    param_sources = {
        "python": (
            "demo.py",
            "def proc(n, value):\n    return n + value\n",
            "def proc(count, value):\n    return count + value\n",
        ),
        "c": (
            "demo.c",
            "int proc(int n, int value) { return n + value; }\n",
            "int proc(int count, int value) { return count + value; }\n",
        ),
        "cpp": (
            "demo.cpp",
            "int proc(int n, int value) { return n + value; }\n",
            "int proc(int count, int value) { return count + value; }\n",
        ),
    }
    local_sources = {
        "python": (
            "demo.py",
            "def proc(value):\n    n = value + 1\n    return n\n",
            "def proc(value):\n    count = value + 1\n    return count\n",
        ),
        "c": (
            "demo.c",
            "int proc(int value) {\n    int n = value + 1;\n    return n;\n}\n",
            "int proc(int value) {\n    int count = value + 1;\n    return count;\n}\n",
        ),
        "cpp": (
            "demo.cpp",
            "int proc(int value) {\n    int n = value + 1;\n    return n;\n}\n",
            "int proc(int value) {\n    int count = value + 1;\n    return count;\n}\n",
        ),
    }
    passed = 0
    for language, (filename, before, after) in param_sources.items():
        result = _exercise(
            language,
            filename,
            before,
            after,
            kind="param",
            name="n",
        )
        _assert_pass(result, f"{language}/param")
        passed += 1
    for language, (filename, before, after) in local_sources.items():
        result = _exercise(
            language,
            filename,
            before,
            after,
            kind="local",
            name="n",
        )
        _assert_pass(result, f"{language}/local")
        passed += 1

    parser_recovery_sources = {
        "c": (
            "demo.c",
            (
                "void *(CDECL *allocate)(unsigned long size);\n"
                "int proc(int n) { return n; }\n"
            ),
            (
                "void *(CDECL *allocate)(unsigned long size);\n"
                "int proc(int count) { return count; }\n"
            ),
            2,
        ),
        "cpp": (
            "demo.cpp",
            (
                "#if __has_include(<string_view>)\n#endif\n"
                "int proc(int n) { return n; }\n"
            ),
            (
                "#if __has_include(<string_view>)\n#endif\n"
                "int proc(int count) { return count; }\n"
            ),
            3,
        ),
    }
    for language, (
        filename,
        before,
        after,
        location_line,
    ) in parser_recovery_sources.items():
        result = _exercise(
            language,
            filename,
            before,
            after,
            kind="param",
            name="n",
            location_line=location_line,
        )
        _assert_parser_recovery_pass(
            result,
            f"{language}/preexisting_external_parser_recovery",
        )
        passed += 1

    same_shape_sibling_preserved = _exercise(
        "python",
        "demo.py",
        (
            "def proc(n):\n    return n\n\n\n"
            "def proc(q):\n    return q\n"
        ),
        (
            "def proc(count):\n    return count\n\n\n"
            "def proc(q):\n    return q\n"
        ),
        kind="param",
        name="n",
    )
    _assert_pass(same_shape_sibling_preserved, "same_shape_sibling_preserved")
    passed += 1

    syntax_error_sources = {
        "python": (
            "demo.py",
            "def proc(value):\n    n = value + 1\n    return n\n",
            "def proc(value):\n    count = value + 1\n    return count\nif (\n",
        ),
        "c": (
            "demo.c",
            "int proc(int value) { int n = value + 1; return n; }\n",
            (
                "int proc(int value) { int count = value + 1; "
                "return count; }\nint broken(\n"
            ),
        ),
        "cpp": (
            "demo.cpp",
            "int proc(int value) { int n = value + 1; return n; }\n",
            (
                "int proc(int value) { int count = value + 1; "
                "return count; }\nint broken(\n"
            ),
        ),
    }
    syntax_failed = 0
    for language, (filename, before, after) in syntax_error_sources.items():
        result = _exercise(
            language,
            filename,
            before,
            after,
            kind="local",
            name="n",
        )
        _assert_syntax_fail(result, f"{language}/target_syntax_error")
        syntax_failed += 1

    _assert_target_container_syntax_invalid_baseline(
        "c",
        "demo.c",
        "int proc(int n) { if (n) { return n; }\n",
    )
    _assert_target_container_syntax_invalid_baseline(
        "cpp",
        "demo.cpp",
        "int proc(int n) { if (n) { return n; }\n",
    )
    syntax_failed += 2

    negative_cases = [
        (
            "n_to_q",
            "def proc(value):\n    n = value + 1\n    return n\n",
            "def proc(value):\n    q = value + 1\n    return q\n",
            "n",
            "too_short",
            "MN_SUCCESSOR_NAME_STILL_SUSPICIOUS",
        ),
        (
            "tmp_to_data",
            "def proc(value):\n    tmp = value + 1\n    return tmp\n",
            "def proc(value):\n    data = value + 1\n    return data\n",
            "tmp",
            "low_info_name",
            "MN_SUCCESSOR_NAME_STILL_SUSPICIOUS",
        ),
        (
            "stale_reference",
            "def proc(value):\n    n = value + 1\n    return n\n",
            "def proc(value):\n    count = value + 1\n    return n\n",
            "n",
            "too_short",
            "MN_STALE_REFERENCE_REMAINS",
        ),
        (
            "unrelated_local_rename",
            "def proc(value):\n    n = value + 1\n    m = value + 2\n    return n + m\n",
            "def proc(value):\n    count = value + 1\n    mode = value + 2\n    return count + mode\n",
            "n",
            "too_short",
            "MN_LOCAL_MAPPING_NOT_UNIQUE",
        ),
    ]
    failed = syntax_failed
    for label, before, after, name, suspicious_reason, code in negative_cases:
        result = _exercise(
            "python",
            "demo.py",
            before,
            after,
            kind="local",
            name=name,
            reason=suspicious_reason,
        )
        _assert_fail(
            result,
            label,
            reason="SEMANTIC_CONTRACT_REGRESSION",
            code=code,
        )
        failed += 1

    param_multi = _exercise(
        "python",
        "demo.py",
        "def proc(n, m):\n    return n + m\n",
        "def proc(count, mode):\n    return count + mode\n",
        kind="param",
        name="n",
    )
    _assert_fail(
        param_multi,
        "unrelated_parameter_rename",
        reason="SEMANTIC_CONTRACT_REGRESSION",
        code="MN_PARAMETER_SLOT_MAPPING_NOT_UNIQUE",
    )
    failed += 1

    comments = "".join(f"    # unchanged spacing line {index}\n" for index in range(1, 12))
    cross_hunk = _exercise(
        "python",
        "demo.py",
        "def proc(value):\n    n = value + 1\n" + comments + "    return n\n",
        "def proc(value):\n" + comments + "    count = value + 1\n    return count\n",
        kind="local",
        name="n",
    )
    _assert_fail(
        cross_hunk,
        "cross_hunk_local_successor",
        reason="SEMANTIC_CONTRACT_REGRESSION",
        code="MN_DECLARATION_SUCCESSOR_NOT_UNIQUE_IN_SAME_HUNK",
    )
    failed += 1

    wrong_same_name = _exercise(
        "python",
        "demo.py",
        "def proc(n):\n    return n\n\n\ndef other(n):\n    return n\n",
        "def proc(n):\n    return n\n\n\ndef other(count):\n    return count\n",
        kind="param",
        name="n",
    )
    _assert_fail(wrong_same_name, "wrong_same_name_symbol")
    failed += 1

    owner_changed = _exercise(
        "python",
        "demo.py",
        "class Alpha:\n    def proc(self, n):\n        return n\n",
        "class Beta:\n    def proc(self, count):\n        return count\n",
        kind="param",
        name="n",
        location_line=2,
    )
    _assert_fail(owner_changed, "owner_changed", code="MN_CONTAINER_OWNER_CHANGED")
    failed += 1

    deleted = _exercise(
        "python",
        "demo.py",
        "def proc(n):\n    return n\n",
        "def other(count):\n    return count\n",
        kind="param",
        name="n",
    )
    _assert_fail(deleted, "container_deleted")
    failed += 1

    ambiguous_current = _exercise(
        "python",
        "demo.py",
        "def proc(n):\n    return n\n",
        (
            "def proc(count):\n    return count\n\n\n"
            "def proc(other_count):\n    return other_count\n"
        ),
        kind="param",
        name="n",
    )
    _assert_fail(ambiguous_current, "current_container_ambiguous")
    failed += 1

    deleted_target_replaced_by_same_shape_decoy = _exercise(
        "python",
        "demo.py",
        (
            "def proc(n):\n    return n\n\n\n"
            "def proc(q):\n    return q\n"
        ),
        "def proc(count):\n    return count\n",
        kind="param",
        name="n",
    )
    _assert_fail(
        deleted_target_replaced_by_same_shape_decoy,
        "deleted_target_replaced_by_same_shape_decoy",
        reason="TARGET_NOT_LOCATED",
        code="MN_CONTAINER_COHORT_CARDINALITY_CHANGED",
    )
    failed += 1


    same_cardinality_decoy_replacement = _exercise(
        "python",
        "demo.py",
        (
            "def proc(n):\n    return n\n\n\n"
            "def proc(q):\n    return q\n"
        ),
        (
            "def proc(count):\n    return count\n\n\n"
            "def proc(other_count):\n    return other_count\n"
        ),
        kind="param",
        name="n",
    )
    _assert_fail(
        same_cardinality_decoy_replacement,
        "same_cardinality_decoy_replacement",
        reason="TARGET_AMBIGUOUS",
        code="MN_CONTAINER_PATCH_IDENTITY_NOT_UNIQUE",
    )
    failed += 1

    _assert_ambiguous_baseline(
        "c",
        "demo.c",
        "int proc(void) { int n = 1; { int n = 2; } return n; }\n",
    )
    _assert_ambiguous_baseline(
        "cpp",
        "demo.cpp",
        "int proc() { int n = 1; { int n = 2; } return n; }\n",
    )
    failed += 2

    _assert_duplicate_container_identity_baseline()
    failed += 1

    _assert_macro_recovery_container_baseline()
    _assert_python_rebinding_is_one_symbol()
    _assert_explicit_multi_declaration_cohort()
    _assert_preprocessor_container_cohort_is_line_anchored()
    passed += 4

    print(
        "nonjava-mysterious-name-successor PASS "
        f"accepted={passed} rejected={failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
