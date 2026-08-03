#!/usr/bin/env python3
"""Self-check for explicit-scope LPL and Data Clumps Guards."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from smell_core.java.target_relational_guards import (  # noqa: E402
    evaluate_data_clumps_guard,
    evaluate_long_parameter_list_guard,
)


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _codes(result: dict) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in result.get("guard_violations", [])
        if isinstance(item, dict)
    }


def _lpl_source(parameter_type: str = "Request") -> str:
    return f"""\
package p;
class Request {{}}
class Service {{
  void process(String message) {{}}
  void process({parameter_type} request) {{}}
}}
"""


def check_long_parameter_list(project: Path) -> None:
    target = _write(
        project,
        "src/main/java/p/Service.java",
        """\
package p;
class Request {}
class Service {
  void process(String message) {}
  void process(int first, long second, String third, boolean fourth,
               double fifth, Request sixth) {}
}
""",
    )
    _write(
        project,
        "unrelated/Trap.java",
        "class Trap { void process(int a, int b, int c, int d, int e, int f) {",
    )
    location = (
        "src/main/java/p/Service.java:method="
        "process(int first, long second, String third, boolean fourth, "
        "double fifth, Request sixth)|line=5"
    )
    ordinary = evaluate_long_parameter_list_guard(
        project,
        location,
        {},
    )
    forged = evaluate_long_parameter_list_guard(
        project,
        location,
        {"score": -1000, "threshold": 1000},
    )
    assert ordinary["ok"] is True, ordinary
    assert ordinary["target_match_count"] == 1, ordinary
    assert ordinary["target_smell_present"] is True, ordinary
    assert ordinary["objectives"] == {"parameter_count": 6}, ordinary
    assert ordinary["entity_identity"] == forged["entity_identity"], forged
    assert ordinary["objectives"] == forged["objectives"], forged
    assert ordinary["witness"]["scope_files"] == [
        "src/main/java/p/Service.java"
    ], ordinary

    varargs_target = _write(
        project,
        "src/main/java/p/VarargsService.java",
        """\
package p;
class VarargsService {
  void join(String separator, int start, int end, boolean skipNulls,
            String prefix, char... characters) {}
}
""",
    )
    varargs = evaluate_long_parameter_list_guard(
        project,
        (
            "src/main/java/p/VarargsService.java:method="
            "join(String separator, int start, int end, boolean skipNulls, "
            "String prefix, char... characters)|line=3"
        ),
        {},
    )
    assert varargs["ok"] is True, varargs
    assert varargs["target_smell_present"] is True, varargs
    assert varargs["objectives"] == {"parameter_count": 6}, varargs
    assert varargs["entity_identity"]["parameter_types"][-1] == "char...", varargs
    assert varargs["witness"]["target"]["parameter_count"] == 6, varargs
    assert varargs_target.exists()

    frozen = ordinary["entity_identity"]
    assert frozen["class"] == "p.Service", frozen
    target.write_text(_lpl_source("Request"), encoding="utf-8")
    resolved = evaluate_long_parameter_list_guard(
        project,
        location,
        {"entity_identity": frozen, "score": 999999},
        analysis_files=("src/main/java/p/Service.java",),
    )
    assert resolved["ok"] is True, resolved
    assert resolved["target_match_count"] == 0, resolved
    assert resolved["target_missing"] is True, resolved
    assert resolved["target_smell_present"] is False, resolved
    assert resolved["objectives"] == {"parameter_count": 1}, resolved
    assert not resolved["guard_violations"], resolved
    assert resolved["witness"]["successor"]["parameter_types"] == [
        "p.Request"
    ], resolved

    target.write_text(_lpl_source("Object"), encoding="utf-8")
    weak = evaluate_long_parameter_list_guard(
        project,
        location,
        {"entity_identity": frozen},
        analysis_files=("src/main/java/p/Service.java",),
    )
    assert "LPL_STRONG_SUCCESSOR_REQUIRED" in _codes(weak), weak

    target.write_text(_lpl_source("Request"), encoding="utf-8")
    no_changed_scope = evaluate_long_parameter_list_guard(
        project,
        location,
        {"entity_identity": frozen},
    )
    assert "LPL_SUCCESSOR_NOT_FOUND" in _codes(no_changed_scope), no_changed_scope

    target.write_text(
        "package p;\nclass Request {}\nclass Replacement {}\n",
        encoding="utf-8",
    )
    cross_package = _write(
        project,
        "src/main/java/other/Service.java",
        """\
package other;
class Request {}
class Service {
  void process(int first, long second, String third, boolean fourth,
               double fifth, Request sixth) {}
  void process(Request request) {}
}
""",
    )
    wrong_owner = evaluate_long_parameter_list_guard(
        project,
        location,
        {"entity_identity": frozen},
        analysis_files=(
            target.relative_to(project),
            cross_package.relative_to(project),
        ),
    )
    assert wrong_owner["target_match_count"] == 0, wrong_owner
    assert wrong_owner["target_missing"] is True, wrong_owner
    assert "LPL_SUCCESSOR_NOT_FOUND" in _codes(wrong_owner), wrong_owner
    assert "successor" not in wrong_owner["witness"], wrong_owner

    print(
        "  ok   LPL frozen FQ owner + unique strong changed-scope successor"
    )


def _data_clump_source(class_name: str, method_name: str, *, secure: str = "secure") -> str:
    return f"""\
package q;
class {class_name} {{
  void {method_name}(String host, int port, boolean {secure}) {{}}
  void unrelated(long alpha, long beta, long gamma) {{}}
}}
"""


def check_data_clumps(project: Path) -> None:
    first = _write(
        project,
        "src/main/java/q/First.java",
        _data_clump_source("First", "send"),
    )
    second = _write(
        project,
        "src/main/java/q/Second.java",
        _data_clump_source("Second", "receive"),
    )
    third = _write(
        project,
        "src/main/java/q/Third.java",
        _data_clump_source("Third", "send"),
    )
    text_candidate = _write(
        project,
        "dataset/TextCandidate.java",
        """\
package trap;
class TextCandidate {
  void hostOnly(String host) {}
  void portOnly(int port) {}
  void secureOnly(boolean secure) {}
}
""",
    )
    location = (
        "src/main/java/q/First.java:method="
        "send(String host, int port, boolean secure)|line=3"
    )
    group = "java.lang.String:host|int:port|boolean:secure"
    source_files = (
        first.relative_to(project),
        second.relative_to(project),
        third.relative_to(project),
        text_candidate.relative_to(project),
    )
    ordinary = evaluate_data_clumps_guard(
        project,
        location,
        {"group": group},
        source_files=source_files,
    )
    forged = evaluate_data_clumps_guard(
        project,
        location,
        {"group": group, "score": 5000, "threshold": 1},
        source_files=source_files,
    )
    assert ordinary["ok"] is True, ordinary
    assert ordinary["target_match_count"] == 1, ordinary
    assert ordinary["target_smell_present"] is True, ordinary
    assert ordinary["objectives"] == {
        "occurrence_count": 3,
        "class_count": 3,
        "method_name_count": 2,
    }, ordinary
    assert ordinary["objectives"] == forged["objectives"], forged
    assert ordinary["entity_identity"] == forged["entity_identity"], forged
    assert "dataset/TextCandidate.java" in ordinary["witness"]["scope_files"], ordinary
    assert ordinary["witness"]["occurrence_files"] == [
        "src/main/java/q/First.java",
        "src/main/java/q/Second.java",
        "src/main/java/q/Third.java",
    ], ordinary

    target_only = evaluate_data_clumps_guard(
        project,
        location,
        {"group": group},
    )
    assert target_only["objectives"] == {
        "occurrence_count": 1,
        "class_count": 1,
        "method_name_count": 1,
    }, target_only
    assert target_only["target_smell_present"] is False, target_only
    assert target_only["witness"]["scope_files"] == [
        "src/main/java/q/First.java"
    ], target_only
    assert target_only["witness"]["occurrence_files"] == [
        "src/main/java/q/First.java"
    ], target_only

    first.write_text(
        "package q;\nclass First { void unrelated(int value) {} }\n",
        encoding="utf-8",
    )
    wrong_package = _write(
        project,
        "src/main/java/other/First.java",
        _data_clump_source("First", "send").replace(
            "package q;",
            "package other;",
        ),
    )
    wrong_anchor = evaluate_data_clumps_guard(
        project,
        location,
        {"entity_identity": ordinary["entity_identity"]},
        source_files=(
            second.relative_to(project),
            third.relative_to(project),
            wrong_package.relative_to(project),
            text_candidate.relative_to(project),
        ),
    )
    assert wrong_anchor["target_match_count"] == 0, wrong_anchor
    assert wrong_anchor["target_missing"] is True, wrong_anchor
    assert wrong_anchor["target_smell_present"] is False, wrong_anchor
    assert "src/main/java/other/First.java" in (
        wrong_anchor["witness"]["occurrence_files"]
    ), wrong_anchor

    first.write_text(
        _data_clump_source("First", "send"),
        encoding="utf-8",
    )

    third.write_text(
        _data_clump_source("Third", "send", secure="tls"),
        encoding="utf-8",
    )
    reduced = evaluate_data_clumps_guard(
        project,
        location,
        {"entity_identity": ordinary["entity_identity"], "score": -1},
        source_files=source_files,
    )
    assert reduced["ok"] is True, reduced
    assert reduced["target_smell_present"] is False, reduced
    assert reduced["objectives"] == {
        "occurrence_count": 2,
        "class_count": 2,
        "method_name_count": 2,
    }, reduced

    invalid = evaluate_data_clumps_guard(
        project,
        location,
        {"group": "int:a|int:b|int:c|int:d"},
        source_files=source_files,
    )
    assert invalid["ok"] is False, invalid
    assert "DATA_CLUMPS_GROUP_INVALID" in _codes(invalid), invalid

    print(
        "  ok   Data Clumps real occurrence files + strict frozen anchor"
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="target-relational-guards-") as raw:
        project = Path(raw) / "project"
        project.mkdir()
        check_long_parameter_list(project)
        check_data_clumps(project)
    print(
        "target-relational-guards-self-check PASS "
        "scope=explicit detector=unused score=ignored"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
