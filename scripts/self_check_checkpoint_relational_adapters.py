#!/usr/bin/env python3
"""End-to-end checks for Data Clumps and Type-1 Clone checkpoint adapters."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java.smell_guards import _find_clone_target_method
from smell_core.java.syntactic_detector import load_java_source_model

DATA_BEFORE = """\
class A { void target(boolean confReq, int maxTokSize, int qop) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, int qop) {} }
"""
DATA_AFTER = """\
class A { void target(boolean confReq, int maxTokSize, int qop) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, long qop) {} }
"""
DATA_PARAMETER_OBJECT_AFTER = """\
final class RequestOptions {
  private final boolean confReq;
  private final int maxTokSize;
  private final int qop;
  RequestOptions(boolean confReq, int maxTokSize, int qop) {
    this.confReq = confReq;
    this.maxTokSize = maxTokSize;
    this.qop = qop;
  }
}
class A { void target(RequestOptions options) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, int qop) {} }
"""
DATA_FAKE_WRAPPER_AFTER = """\
final class RequestOptions {
  RequestOptions(boolean confReq, int maxTokSize, int qop) {}
}
class A { void target(RequestOptions options) {} }
class B { void other(boolean confReq, int maxTokSize, int qop) {} }
class C { void third(boolean confReq, int maxTokSize, int qop) {} }
"""
CLONE_BODY = "int total = 0; for (int i = 0; i < 20; i++) { total += i; } if (total > 10) { total--; } consume(total);"
CLONE_BEFORE = f"class Fixture {{\n  void left() {{ {CLONE_BODY} }}\n  void right() {{ {CLONE_BODY} }}\n  void consume(int value) {{}}\n}}\n"
CLONE_MUTATION_ONLY = f"class Fixture {{\n  void left() {{ {CLONE_BODY} }}\n  void right() {{ int total = 0; consume(total); }}\n  void consume(int value) {{}}\n}}\n"
CLONE_MOVED_TWICE = f"""\
class Fixture {{
  void left() {{ leftShared(); }}
  void right() {{ rightShared(); }}
  void leftShared() {{ {CLONE_BODY} }}
  void rightShared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
CLONE_WITH_DELEGATING_FALLBACK = f"""\
class Fixture {{
  boolean ready;
  void left() {{ if (ready) {{ shared(); return; }} {CLONE_BODY} }}
  void right() {{ shared(); }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
CLONE_AFTER = f"""\
class Fixture {{
  void left() {{ shared(); }}
  void right() {{ shared(); }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
CLONE_TYPED_ADAPTER_AFTER = f"""\
class Fixture {{
  void left() {{ leftAdapter(); }}
  void right() {{ rightAdapter(); }}
  void leftAdapter() {{ shared(1); }}
  void rightAdapter() {{ shared(2); }}
  void shared(int variant) {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
CLONE_SHARED_WITH_PARALLEL_HELPERS = f"""\
class Fixture {{
  void left() {{ shared(); }}
  void right() {{ shared(); }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
class LeftAdapter {{
  void leftover() {{ int count = 0; for (int i = 0; i < 20; i++) {{ count += i; }} consume(count); }}
  void consume(int value) {{}}
}}
class RightAdapter {{
  void leftover() {{ int count = 0; for (int i = 0; i < 20; i++) {{ count += i; }} consume(count); }}
  void consume(int value) {{}}
}}
"""
CLONE_SHARED_WITH_OVERLOADED_HELPERS = f"""\
class Fixture {{
  void left() {{ shared(); }}
  void right() {{ shared(); }}
  void shared() {{ {CLONE_BODY} }}
  void leftover(int ignored) {{ int count = 0; for (int i = 0; i < 20; i++) {{ count += i; }} consume(count); }}
  void leftover(short ignored) {{ int count = 0; for (int i = 0; i < 20; i++) {{ count += i; }} consume(count); }}
  void consume(int value) {{}}
}}
"""
CLONE_PARENT_BEFORE = f"""\
class Parent {{}}
class Left extends Parent {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
class Right extends Parent {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
"""
CLONE_PARENT_AFTER = f"""\
class Parent {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
class Left extends Parent {{}}
class Right extends Parent {{}}
"""
CLONE_TRANSITIVE_PARENT_BEFORE = f"""\
class Parent {{}}
class Middle extends Parent {{}}
class Left extends Parent {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
class Right extends Middle {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
"""
CLONE_TRANSITIVE_PARENT_AFTER = f"""\
class Parent {{ void work() {{ {CLONE_BODY} }} void consume(int value) {{}} }}
class Middle extends Parent {{}}
class Left extends Parent {{}}
class Right extends Middle {{}}
"""
CLONE_PARENT_CONSTRUCTOR_BEFORE = """\
class Parent {
  Parent() {}
}
class Left extends Parent {
  int below;
  int above;
  int width;
  int height;
  int depth;
  boolean between;
  Left(int below, int above, int width, int height, int depth, boolean between) {
    this.below = below;
    this.above = above;
    this.width = width;
    this.height = height;
    this.depth = depth;
    this.between = between;
  }
}
class Right extends Parent {
  int below;
  int above;
  int width;
  int height;
  int depth;
  boolean between;
  Right(int below, int above, int width, int height, int depth, boolean between) {
    this.below = below;
    this.above = above;
    this.width = width;
    this.height = height;
    this.depth = depth;
    this.between = between;
  }
}
"""
CLONE_PARENT_CONSTRUCTOR_AFTER = """\
class Parent {
  int below;
  int above;
  int width;
  int height;
  int depth;
  boolean between;
  Parent(int below, int above, int width, int height, int depth, boolean between) {
    this.below = below;
    this.above = above;
    this.width = width;
    this.height = height;
    this.depth = depth;
    this.between = between;
  }
}
class Left extends Parent {
  Left(int below, int above, int width, int height, int depth, boolean between) {
    super(below, above, width, height, depth, between);
  }
}
class Right extends Parent {
  Right(int below, int above, int width, int height, int depth, boolean between) {
    super(below, above, width, height, depth, between);
  }
}
"""
OWNER_CLONE_BEFORE = f"""\
class Left {{
  void remove() {{ remove(0); {CLONE_BODY} }}
  void remove(int ignored) {{}}
  void consume(int value) {{}}
}}
class Owner {{
  void remove() {{ remove(0); {CLONE_BODY} }}
  static void remove(int ignored) {{}}
  void consume(int value) {{}}
}}
"""
OWNER_CLONE_AFTER = f"""\
class Left {{
  void remove() {{ Owner.remove(0); }}
  void remove(int ignored) {{}}
  void consume(int value) {{}}
}}
class Owner {{
  void remove() {{ remove(0); }}
  static void remove(int ignored) {{ {CLONE_BODY} }}
  static void consume(int value) {{}}
}}
"""
REMOVED_TARGET_BEFORE = f"""\
class Left {{
  void run() {{ left(); }}
  void left() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
class Owner {{
  void right() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
REMOVED_TARGET_AFTER = f"""\
class Left {{
  void run() {{ Owner.shared(); }}
  void consume(int value) {{}}
}}
class Owner {{
  void right() {{ shared(); }}
  static void shared() {{ {CLONE_BODY} }}
  static void consume(int value) {{}}
}}
"""
REMOVED_TARGET_INSTANCE_OWNER_AFTER = f"""\
class Left {{
  private final Owner owner = new Owner();
  void run() {{ owner.right(); }}
  void consume(int value) {{}}
}}
class Owner {{
  void right() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
REMOVED_TARGET_UNRELATED_CALL_AFTER = f"""\
class Left {{
  private final Owner owner = new Owner();
  void run() {{}}
  void observe() {{ owner.right(); }}
  void consume(int value) {{}}
}}
class Owner {{
  void right() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
OVERLOAD_BEFORE = f"""\
class Fixture {{
  void work(int[] values) {{ {CLONE_BODY} }}
  void work(short[] values) {{ {CLONE_BODY} }}
  void work(byte[] values) {{ consume(values.length); }}
  void consume(int value) {{}}
}}
"""
OVERLOAD_SCOPED_AFTER = f"""\
class Fixture {{
  void work(int[] values) {{ shared(); }}
  void work(short[] values) {{ shared(); }}
  void work(byte[] values) {{ consume(values.length); }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
OVERLOAD_TOO_BROAD_AFTER = f"""\
class Fixture {{
  void work(int[] values) {{ shared(); }}
  void work(short[] values) {{ shared(); }}
  void work(byte[] values) {{ shared(); }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
OVERLOAD_EXISTING_FAMILY_BEFORE = f"""\
class Fixture {{
  void work(int[] values) {{ {CLONE_BODY} }}
  void work(short[] values) {{ {CLONE_BODY} }}
  void work(byte[] values) {{ {CLONE_BODY} }}
  void work(long[] values) {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""
OVERLOAD_EXISTING_FAMILY_AFTER = f"""\
class Fixture {{
  void work(int[] values) {{ shared(); }}
  void work(short[] values) {{ shared(); }}
  void work(byte[] values) {{ {CLONE_BODY} }}
  void work(long[] values) {{ {CLONE_BODY} }}
  void shared() {{ {CLONE_BODY} }}
  void consume(int value) {{}}
}}
"""

OVERLOAD_LINE_SHIFT_BEFORE = """\
class Fixture {
  void work(int[] values) { consume(values.length); }
  void work(short[] values) { consume(values.length); }
  void work(byte[] values) { consume(values.length); }
  void consume(int value) {}
}
"""
OVERLOAD_LINE_SHIFT_AFTER = """\
class Fixture {
  void work(int[] values) { shared(values); }
  void helperInsertedBeforeSecondTarget() {
    consume(1);
    consume(2);
    consume(3);
    consume(4);
  }
  void work(short[] values) { shared(values); }
  void work(byte[] values) { consume(values.length); }
  void shared(Object values) {}
  void consume(int value) {}
}
"""


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), env=env, text=True, capture_output=True, check=False)


def _bridge(project: Path, env: dict[str, str], command: str, smell: str, location: str, evidence: str) -> dict:
    args = [
        sys.executable, str(BRIDGE), command,
        "--project-root", str(project), "--language", "java",
        "--smell", smell, "--location", location, "--smell-evidence", evidence,
    ]
    if command == "verify":
        args.extend(["--verification-mode", "local", "--skip-build-test"])
    result = _run(args, ROOT, env)
    if result.returncode:
        raise AssertionError(f"{smell} {command}: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _case(
    smell: str,
    before: str,
    after: str,
    location: str,
    evidence: str,
    objective: str,
    rejected_intermediates: tuple[str, ...] = (),
) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix=f"checkpoint-{smell}-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(before, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "runtime" / "python")}
        for command in (["git", "init", "-q"], ["git", "add", "Fixture.java"]):
            result = _run(command, project, env)
            if result.returncode:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project, env)
        if result.returncode:
            raise AssertionError(result.stderr)
        baseline = _bridge(project, env, "capture-baseline", smell, location, evidence)
        before_value = float(baseline["metrics"]["objectives"][objective])
        unchanged = _bridge(project, env, "verify", smell, location, evidence)
        assert unchanged["smell_guard"]["results"][0]["details"]["reason"] == "EDIT_REQUIRED", unchanged
        for rejected in rejected_intermediates:
            source.write_text(rejected, encoding="utf-8")
            invalid = _bridge(project, env, "verify", smell, location, evidence)
            assert invalid.get("status") in {"SMELL_GUARD_FAILED", "IMPROVED"}, invalid
            assert invalid.get("success") is False, invalid
            assert invalid.get("accepted") is False, invalid
            if invalid.get("status") == "IMPROVED":
                assert invalid.get("progress") is True, invalid
                assert invalid.get("resolution") == "improved", invalid
        source.write_text(after, encoding="utf-8")
        repaired = _bridge(project, env, "verify", smell, location, evidence)
        if repaired.get("status") != "PASS":
            raise AssertionError(f"{smell} repaired source did not pass: {repaired}")
        after_value = float(repaired["checkpoint"]["delta"]["objectives"][objective]["after"])
        assert after_value < before_value
        return before_value, after_value


def _transitive_parent_multifile_case() -> tuple[float, float]:
    before = {
        "Parent.java": "class Parent {}\n",
        "Middle.java": (
            "class Middle extends Parent { "
            "Object existing(Object raw) { return (java.util.List<String>) raw; } "
            "}\n"
        ),
        "Left.java": (
            f"class Left extends Parent {{ void work() {{ {CLONE_BODY} }} "
            "void consume(int value) {} }\n"
        ),
        "Right.java": (
            f"class Right extends Middle {{ void work() {{ {CLONE_BODY} }} "
            "void consume(int value) {} }\n"
        ),
    }
    after = {
        "Parent.java": (
            f"class Parent {{ void work() {{ {CLONE_BODY} }} "
            "void consume(int value) {} }\n"
        ),
        "Left.java": "class Left extends Parent {}\n",
        "Right.java": "class Right extends Middle {}\n",
    }
    with tempfile.TemporaryDirectory(prefix="checkpoint-code-clone-transitive-") as temp_dir:
        project = Path(temp_dir)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "runtime" / "python")}
        for name, content in before.items():
            (project / name).write_text(content, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", "."]):
            result = _run(command, project, env)
            if result.returncode:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project, env)
        if result.returncode:
            raise AssertionError(result.stderr)
        location = "Left.java:method=work|line=1 <-> Right.java:method=work|line=1"
        evidence = "tokens=30; group_size=2"
        baseline = _bridge(project, env, "capture-baseline", "code_clone_type1", location, evidence)
        before_value = float(baseline["metrics"]["objectives"]["clone_token_count"])
        for name, content in after.items():
            (project / name).write_text(content, encoding="utf-8")
        repaired = _bridge(project, env, "verify", "code_clone_type1", location, evidence)
        if repaired.get("status") != "PASS":
            raise AssertionError(f"multifile transitive parent did not pass: {repaired}")
        after_value = float(repaired["checkpoint"]["delta"]["objectives"]["clone_token_count"]["after"])
        return before_value, after_value


def _line_shifted_overload_identity_case() -> str:
    with tempfile.TemporaryDirectory(prefix="checkpoint-code-clone-overload-identity-") as temp_dir:
        source = Path(temp_dir) / "Fixture.java"
        _, baseline_methods = load_java_source_model(
            source,
            "Fixture.java",
            OVERLOAD_LINE_SHIFT_BEFORE,
        )
        _, current_methods = load_java_source_model(
            source,
            "Fixture.java",
            OVERLOAD_LINE_SHIFT_AFTER,
        )
        location = SimpleNamespace(
            project_path="Fixture.java",
            method="work",
            line=3,
            start_line=None,
        )
        baseline_target = _find_clone_target_method(baseline_methods, location, None)
        if baseline_target is None or "short[]" not in baseline_target.signature:
            raise AssertionError(f"baseline overload did not resolve to short[]: {baseline_target}")
        current_target = _find_clone_target_method(current_methods, location, baseline_target)
        if current_target is None or "short[]" not in current_target.signature:
            raise AssertionError(f"shifted overload did not retain short[] identity: {current_target}")
        return "PASS"


def main() -> int:
    line_shifted_overload_identity = _line_shifted_overload_identity_case()
    data = _case(
        "data_clumps", DATA_BEFORE, DATA_PARAMETER_OBJECT_AFTER,
        "Fixture.java:method=target|line=1",
        "group=boolean:confreq|int:maxtoksize|int:qop; occurrences=3",
        "occurrence_count",
        rejected_intermediates=(DATA_FAKE_WRAPPER_AFTER,),
    )
    data_type_change = _case(
        "data_clumps", DATA_BEFORE, DATA_AFTER,
        "Fixture.java:method=target|line=1",
        "group=boolean:confreq|int:maxtoksize|int:qop; occurrences=3",
        "occurrence_count",
    )
    clone = _case(
        "code_clone_type1", CLONE_BEFORE, CLONE_AFTER,
        "Fixture.java:method=left|line=2 <-> Fixture.java:method=right|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
        rejected_intermediates=(
            CLONE_MUTATION_ONLY,
            CLONE_MOVED_TWICE,
            CLONE_SHARED_WITH_PARALLEL_HELPERS,
            CLONE_SHARED_WITH_OVERLOADED_HELPERS,
            CLONE_WITH_DELEGATING_FALLBACK,
        ),
    )
    parent_clone = _case(
        "code_clone_type1", CLONE_PARENT_BEFORE, CLONE_PARENT_AFTER,
        "Fixture.java:method=work|line=2 <-> Fixture.java:method=work|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    transitive_parent_clone = _case(
        "code_clone_type1", CLONE_TRANSITIVE_PARENT_BEFORE, CLONE_TRANSITIVE_PARENT_AFTER,
        "Fixture.java:method=work|line=3 <-> Fixture.java:method=work|line=4",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    transitive_parent_multifile_clone = _transitive_parent_multifile_case()
    parent_constructor_clone = _case(
        "code_clone_type1", CLONE_PARENT_CONSTRUCTOR_BEFORE, CLONE_PARENT_CONSTRUCTOR_AFTER,
        "Fixture.java:method=Left|line=8 <-> Fixture.java:method=Right|line=18",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    typed_adapter_clone = _case(
        "code_clone_type1", CLONE_BEFORE, CLONE_TYPED_ADAPTER_AFTER,
        "Fixture.java:method=left|line=2 <-> Fixture.java:method=right|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    qualified_owner_clone = _case(
        "code_clone_type1", OWNER_CLONE_BEFORE, OWNER_CLONE_AFTER,
        "Fixture.java:method=remove|line=2 <-> Fixture.java:method=remove|line=7",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    removed_target_clone = _case(
        "code_clone_type1", REMOVED_TARGET_BEFORE, REMOVED_TARGET_AFTER,
        "Fixture.java:method=left|line=3 <-> Fixture.java:method=right|line=7",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    removed_target_instance_owner_clone = _case(
        "code_clone_type1", REMOVED_TARGET_BEFORE, REMOVED_TARGET_INSTANCE_OWNER_AFTER,
        "Fixture.java:method=left|line=3 <-> Fixture.java:method=right|line=7",
        "tokens=30; group_size=2",
        "clone_token_count",
        rejected_intermediates=(REMOVED_TARGET_UNRELATED_CALL_AFTER,),
    )
    scoped_overload_clone = _case(
        "code_clone_type1", OVERLOAD_BEFORE, OVERLOAD_SCOPED_AFTER,
        "Fixture.java:method=work|line=2 <-> Fixture.java:method=work|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    expanded_overload_clone = _case(
        "code_clone_type1", OVERLOAD_BEFORE, OVERLOAD_TOO_BROAD_AFTER,
        "Fixture.java:method=work|line=2 <-> Fixture.java:method=work|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    existing_family_clone = _case(
        "code_clone_type1", OVERLOAD_EXISTING_FAMILY_BEFORE, OVERLOAD_EXISTING_FAMILY_AFTER,
        "Fixture.java:method=work|line=2 <-> Fixture.java:method=work|line=3",
        "tokens=30; group_size=2",
        "clone_token_count",
    )
    print(
        "checkpoint-relational-adapters-self-check PASS unchanged_pass=0 "
        f"data_clumps={data[0]:g}->{data[1]:g} "
        f"data_clumps_type_change={data_type_change[0]:g}->{data_type_change[1]:g} "
        f"code_clone_type1={clone[0]:g}->{clone[1]:g} "
        f"parent_clone={parent_clone[0]:g}->{parent_clone[1]:g} "
        f"transitive_parent_clone={transitive_parent_clone[0]:g}->{transitive_parent_clone[1]:g} "
        f"transitive_parent_multifile_clone={transitive_parent_multifile_clone[0]:g}->{transitive_parent_multifile_clone[1]:g} "
        f"parent_constructor_clone={parent_constructor_clone[0]:g}->{parent_constructor_clone[1]:g} "
        f"typed_adapter_clone={typed_adapter_clone[0]:g}->{typed_adapter_clone[1]:g} "
        f"qualified_owner_clone={qualified_owner_clone[0]:g}->{qualified_owner_clone[1]:g} "
        f"removed_target_clone={removed_target_clone[0]:g}->{removed_target_clone[1]:g} "
        f"removed_target_instance_owner_clone={removed_target_instance_owner_clone[0]:g}->{removed_target_instance_owner_clone[1]:g} "
        f"scoped_overload_clone={scoped_overload_clone[0]:g}->{scoped_overload_clone[1]:g} "
        f"expanded_overload_clone={expanded_overload_clone[0]:g}->{expanded_overload_clone[1]:g} "
        f"existing_family_clone={existing_family_clone[0]:g}->{existing_family_clone[1]:g} "
        f"line_shifted_overload_identity={line_shifted_overload_identity}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
