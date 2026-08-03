#!/usr/bin/env python3
"""Self-check for explicit-scope LPL and Data Clumps Guards."""

from __future__ import annotations

import sys
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from smell_core.java import target_relational_guards as relational  # noqa: E402
from smell_core.java import target_guard  # noqa: E402
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
    parse_widths: list[int] = []
    original_parse_scope = relational._parse_scope

    def tracked_parse_scope(root: Path, files: object) -> object:
        frozen = tuple(files)  # type: ignore[arg-type]
        parse_widths.append(len(frozen))
        return original_parse_scope(root, frozen)

    relational._parse_scope = tracked_parse_scope
    try:
        ordinary = evaluate_data_clumps_guard(
            project,
            location,
            {"group": group},
            source_files=source_files,
        )
    finally:
        relational._parse_scope = original_parse_scope
    assert parse_widths and max(parse_widths) == 1, parse_widths
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
        {"group": "int:a|int:b"},
        source_files=source_files,
    )
    assert invalid["ok"] is False, invalid
    assert "DATA_CLUMPS_GROUP_INVALID" in _codes(invalid), invalid

    large_files = tuple(
        _write(
            project,
            f"src/main/java/large/Large{name}.java",
            f"""\
package large;
class Large{name} {{
  void {method}(String host, int port, boolean secure, long timeout) {{}}
}}
""",
        ).relative_to(project)
        for name, method in (
            ("First", "open"),
            ("Second", "close"),
            ("Third", "open"),
        )
    )
    large = evaluate_data_clumps_guard(
        project,
        (
            "src/main/java/large/LargeFirst.java:method="
            "open(String host, int port, boolean secure, long timeout)|line=3"
        ),
        {
            "group": (
                "java.lang.String:host|int:port|boolean:secure|long:timeout"
            )
        },
        source_files=large_files,
    )
    assert large["ok"] is True, large
    assert large["target_smell_present"] is True, large
    assert large["objectives"] == {
        "occurrence_count": 3,
        "class_count": 3,
        "method_name_count": 2,
    }, large
    assert len(large["entity_identity"]["group"].split("|")) == 4, large

    generic_files = tuple(
        _write(
            project,
            f"src/main/java/generic/{owner}.java",
            f"""\
package generic;
import java.util.List;
class {owner} {{
  void {method}(List<{element}> rows, int offset, boolean strict) {{}}
}}
""",
        ).relative_to(project)
        for owner, method, element in (
            ("First", "load", "String"),
            ("Second", "save", "Integer"),
            ("Third", "load", "?"),
        )
    )
    generic = evaluate_data_clumps_guard(
        project,
        (
            "src/main/java/generic/First.java:method="
            "load(java.util.List rows, int offset, boolean strict)|line=4"
        ),
        {
            "group": (
                "java.util.List<java.lang.String>:rows|int:offset|boolean:strict"
            )
        },
        source_files=generic_files,
    )
    assert generic["ok"] is True, generic
    assert generic["target_smell_present"] is True, generic
    assert generic["target_match_count"] == 1, generic
    assert generic["objectives"] == {
        "occurrence_count": 3,
        "class_count": 3,
        "method_name_count": 2,
    }, generic
    assert "<" not in generic["entity_identity"]["group"], generic

    qualified_files: list[Path] = []
    _write(project, "src/main/java/right/Token.java", "package right; class Token {}\n")
    _write(project, "src/main/java/wrong/Token.java", "package wrong; class Token {}\n")
    for package, owner, method, imported in (
        ("anchor", "First", "send", "right.Token"),
        ("anchor", "Second", "receive", "right.Token"),
        ("anchor", "Third", "send", "right.Token"),
        ("decoy", "Wrong", "receive", "wrong.Token"),
    ):
        qualified_files.append(
            _write(
                project,
                f"src/main/java/{package}/{owner}.java",
                f"""\
package {package};
import {imported};
class {owner} {{ void {method}(Token token, int port, boolean secure) {{}} }}
""",
            ).relative_to(project)
        )
    qualified = evaluate_data_clumps_guard(
        project,
        "src/main/java/anchor/First.java:method=send|line=3",
        {"group": "Token:token|int:port|boolean:secure"},
        source_files=qualified_files,
    )
    assert qualified["ok"] is True, qualified
    assert qualified["target_smell_present"] is True, qualified
    assert qualified["objectives"]["occurrence_count"] == 3, qualified
    assert "src/main/java/decoy/Wrong.java" not in qualified["witness"][
        "occurrence_files"
    ], qualified

    nested = _write(
        project,
        "src/main/java/q/GeometryUtils.java",
        """\
package q;
class GeometryUtils {
  static class Target {}
  static class First { void send(Target target, int port, boolean secure) {} }
  static class Second { void receive(Target target, int port, boolean secure) {} }
  static class Third { void send(Target target, int port, boolean secure) {} }
}
""",
    )
    nested_result = evaluate_data_clumps_guard(
        project,
        "src/main/java/q/GeometryUtils.java:method=send|line=4",
        {"group": "Target:target|int:port|boolean:secure"},
        source_files=(nested.relative_to(project),),
    )
    assert nested_result["target_smell_present"] is True, nested_result
    assert "q.geometryutils.target:target" in nested_result["entity_identity"][
        "group"
    ], nested_result

    many_candidates = tuple(
        _write(
            project,
            f"src/main/java/noise/Noise{index:02d}.java",
            f"""\
package noise;
class Noise{index:02d} {{
  void hostOnly(String host) {{}}
  void portOnly(int port) {{}}
  void secureOnly(boolean secure) {{}}
}}
""",
        ).relative_to(project)
        for index in range(40)
    )
    streamed = evaluate_data_clumps_guard(
        project,
        location,
        {"group": group},
        source_files=(*source_files, *many_candidates),
    )
    assert streamed["ok"] is True, streamed
    assert streamed["witness"]["scope_file_count"] == 44, streamed
    assert streamed["objectives"]["occurrence_count"] == 2, streamed
    assert streamed["witness"]["scan_mode"] == "target_anchor_then_stream", streamed

    camel_case = _write(
        project,
        "src/main/java/q/CamelCase.java",
        """\
package q;
class CamelCase {
  void publish(String oldValue, int newValue, boolean emitEvent) {}
}
""",
    )

    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=target-relational-self-check",
            "-c",
            "user.email=target-relational@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        completed = subprocess.run(
            command,
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    queried = target_guard._data_clump_candidate_files(project, group)
    assert len(queried) > 32, queried
    camel_queried = target_guard._data_clump_candidate_files(
        project,
        "java.lang.String:oldvalue|int:newvalue|boolean:emitevent",
    )
    assert camel_case.relative_to(project).as_posix() in camel_queried, camel_queried

    print(
        "  ok   Data Clumps >=3 group + target anchor + streaming relation"
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
