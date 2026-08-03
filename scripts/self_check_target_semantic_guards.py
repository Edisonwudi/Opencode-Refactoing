#!/usr/bin/env python3
"""Regression checks for target-scoped semantic Guard evaluation."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java import semantic_detector as semantic  # noqa: E402
from smell_core.java.target_semantic_guards import (  # noqa: E402
    capture_target_semantic_guard,
    evaluate_target_semantic_guard,
)


DECISION_KEYS = {
    "ok",
    "target_match_count",
    "target_smell_present",
    "target_missing",
    "objectives",
    "entity_identity",
    "witness",
    "guard_violations",
}

EVALUATORS = {
    "feature_envy": "_detect_feature_envy",
    "god_class": "_detect_god_class",
    "refused_bequest": "_detect_refused_bequest",
    "dead_code": "_detect_dead_code",
}

FEATURE_ENVY_BEFORE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class Subject {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
}
"""

FEATURE_ENVY_AFTER = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
  void doWork() { a(); b(); c(); d(); }
}
class Subject {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) { RECEIVER.doWork(); }
}
"""

FEATURE_ENVY_RELOCATED = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class Subject {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) { relocatedWork(value); }
  void relocatedWork(String value) {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
}
"""

FEATURE_ENVY_CHANGED_DIFF = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class AlternativeCollaborator {
  void w() {}
  void x() {}
  void y() {}
  void z() {}
}
class Subject {
  static final Collaborator RECEIVER = new Collaborator();
  static final AlternativeCollaborator ALTERNATE = new AlternativeCollaborator();
  void target(String value) { RECEIVER.a(); }
  void unrelatedFlow(long sequence) {
    ALTERNATE.w();
    ALTERNATE.x();
    ALTERNATE.y();
    ALTERNATE.z();
  }
}
"""

FEATURE_ENVY_PEER_BEFORE = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class SubjectWithPeer {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
  void existingPeer(int value) {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
}
"""

FEATURE_ENVY_PEER_AFTER = """\
class Collaborator {
  void a() {}
  void b() {}
  void c() {}
  void d() {}
}
class SubjectWithPeer {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) { RECEIVER.a(); }
  void existingPeer(int value) {
    RECEIVER.a();
    RECEIVER.b();
    RECEIVER.c();
    RECEIVER.d();
  }
}
"""

REFUSED_BEQUEST_BEFORE = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
}
class Child extends Parent {
  @Override void first() { throw new UnsupportedOperationException(); }
  @Override void second() { throw new UnsupportedOperationException(); }
}
"""

REFUSED_BEQUEST_AFTER = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
}
class Child extends Parent {
  @Override void first() { super.first(); }
  @Override void second() { throw new UnsupportedOperationException(); }
}
"""

REFUSED_BEQUEST_RELOCATED = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
}
class Child extends Parent {
  @Override void first() { super.first(); }
  @Override void second() { throw new UnsupportedOperationException(); }
}
class OtherChild extends Parent {
  @Override void first() { throw new UnsupportedOperationException(); }
}
"""

REFUSED_BEQUEST_NEW = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
}
class Child extends Parent {
  @Override void first() { super.first(); }
  @Override void second() { throw new UnsupportedOperationException(); }
  @Override void third() { throw new UnsupportedOperationException(); }
}
"""

DEAD_CODE_BEFORE = """\
class DeadFixture {
  private void unusedHelper() { int marker = 1; }
  void live() { System.out.println("live"); }
}
"""

DEAD_CODE_AFTER = """\
class DeadFixture {
  void live() { System.out.println("live"); }
}
"""

DEAD_CODE_RELOCATED = """\
class MovedDeadFixture {
  private void unusedHelper() { int marker = 1; }
}
"""


def _god_class(name: str) -> str:
    methods: list[str] = []
    for index in range(10):
        field_name = "cacheState" if index < 5 else "reportState"
        methods.append(
            f"  void method{index}(int value) {{\n"
            "    if (value > 0) value--;\n"
            "    if (value > 1) value--;\n"
            f"    value += this.{field_name};\n"
            "  }"
        )
    padding = "\n".join("  // calibration padding" for _ in range(80))
    return (
        f"class {name} {{\n"
        "  int cacheState;\n"
        "  int reportState;\n"
        + "\n".join(methods)
        + "\n"
        + padding
        + "\n}\n"
    )


GOD_CLASS_BEFORE = _god_class("Candidate")
GOD_CLASS_AFTER = "class Candidate { void work() {} }\n"
GOD_CLASS_RELOCATED = GOD_CLASS_AFTER + _god_class("Recipient")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _line_number(source: str, marker: str) -> int:
    for index, line in enumerate(source.splitlines(), start=1):
        if marker in line:
            return index
    raise AssertionError(f"missing source marker: {marker}")


def _assert_decision(decision: dict[str, Any]) -> None:
    assert set(decision) == DECISION_KEYS, decision
    assert isinstance(decision["ok"], bool), decision
    assert isinstance(decision["target_match_count"], int), decision
    assert isinstance(decision["target_smell_present"], bool), decision
    assert isinstance(decision["target_missing"], bool), decision
    assert len(decision["witness"]) <= 6, decision
    assert len(decision["guard_violations"]) <= 8, decision
    assert len(json.dumps(decision, sort_keys=True)) < 8192, decision


def _assert_capture(decision: dict[str, Any]) -> None:
    _assert_decision(decision)
    assert decision["ok"] is True, decision
    assert decision["target_match_count"] == 1, decision
    assert decision["target_smell_present"] is True, decision
    assert decision["target_missing"] is False, decision
    assert decision["entity_identity"], decision
    assert decision["guard_violations"] == [], decision


def _assert_resolved(decision: dict[str, Any], *, target_missing: bool) -> None:
    _assert_decision(decision)
    assert decision["ok"] is True, decision
    assert decision["target_match_count"] == 0, decision
    assert decision["target_smell_present"] is False, decision
    assert decision["target_missing"] is target_missing, decision
    assert decision["guard_violations"] == [], decision


def _call_scoped(
    smell: str,
    operation: Callable[..., dict[str, Any]],
    *args: Any,
    expected_files: Iterable[Path],
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one Guard while every unrelated evaluator/discovery path is fatal."""
    expected = {path.resolve() for path in expected_files}
    original_read_bytes = Path.read_bytes
    java_reads: list[Path] = []
    patched_names = [
        "run_java_semantic_detector",
        "_list_java_files",
        *(
            evaluator
            for candidate, evaluator in EVALUATORS.items()
            if candidate != smell
        ),
    ]
    originals = {name: getattr(semantic, name) for name in patched_names}

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("target Guard invoked full discovery or another smell evaluator")

    def tracked_read_bytes(path: Path) -> bytes:
        if path.suffix.casefold() == ".java":
            java_reads.append(path.resolve())
        return original_read_bytes(path)

    try:
        for name in patched_names:
            setattr(semantic, name, forbidden)
        Path.read_bytes = tracked_read_bytes
        decision = operation(smell, *args, **kwargs)
    finally:
        Path.read_bytes = original_read_bytes
        for name, original in originals.items():
            setattr(semantic, name, original)

    assert java_reads, "the target scope was not parsed"
    assert set(java_reads).issubset(expected), (java_reads, expected)
    _assert_decision(decision)
    return decision


def _exercise(
    project: Path,
    *,
    smell: str,
    source: Path,
    before: str,
    after: str,
    relocated: str,
    location: str,
    selector: dict[str, Any],
    relocation_files: list[Path] | None = None,
    relocation_code: str,
    target_missing: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _write(source, before)
    relative_source = source.relative_to(project)
    baseline = _call_scoped(
        smell,
        capture_target_semantic_guard,
        project,
        location,
        selector,
        [relative_source],
        expected_files=[source],
    )
    _assert_capture(baseline)

    _write(source, after)
    resolved = _call_scoped(
        smell,
        evaluate_target_semantic_guard,
        project,
        location,
        selector,
        [relative_source],
        baseline,
        expected_files=[source],
    )
    _assert_resolved(resolved, target_missing=target_missing)

    _write(source, relocated)
    scoped_files = relocation_files or [source]
    relocated_decision = _call_scoped(
        smell,
        evaluate_target_semantic_guard,
        project,
        location,
        selector,
        [item.relative_to(project) for item in scoped_files],
        baseline,
        expected_files=scoped_files,
    )
    assert relocated_decision["target_smell_present"] is False, relocated_decision
    assert any(
        item.startswith(relocation_code)
        for item in relocated_decision["guard_violations"]
    ), relocated_decision
    return baseline, relocated_decision


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="target-semantic-guards-") as temp_dir:
        project = Path(temp_dir) / "project"
        _write(
            project / "pom.xml",
            "<project><modelVersion>4.0.0</modelVersion></project>\n",
        )
        unrelated_count = 256
        for index in range(unrelated_count):
            _write(
                project / f"src/main/java/unrelated/Noise{index:04d}.java",
                f"package unrelated; class Noise{index:04d} "
                f"{{ int value() {{ return {index}; }} }}\n",
            )

        feature_baseline, _ = _exercise(
            project,
            smell="feature_envy",
            source=project / "src/main/java/FeatureFixture.java",
            before=FEATURE_ENVY_BEFORE,
            after=FEATURE_ENVY_AFTER,
            relocated=FEATURE_ENVY_RELOCATED,
            location="src/main/java/FeatureFixture.java:method=target(String)",
            selector={"target_class": "Subject"},
            relocation_code="FEATURE_ENVY_RELOCATED:",
            target_missing=False,
        )

        feature_source = project / "src/main/java/FeatureFixture.java"
        _write(feature_source, FEATURE_ENVY_CHANGED_DIFF)
        unrelated_line = _line_number(
            FEATURE_ENVY_CHANGED_DIFF,
            "void unrelatedFlow(long sequence)",
        )
        changed_diff = _call_scoped(
            "feature_envy",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/FeatureFixture.java:method=target(String)",
            {"target_class": "Subject"},
            [feature_source.relative_to(project)],
            feature_baseline,
            expected_files=[feature_source],
            changed_line_ranges={
                feature_source.relative_to(project).as_posix(): [
                    (unrelated_line, unrelated_line + 5)
                ]
            },
        )
        assert any(
            item.startswith("FEATURE_ENVY_RELOCATED:")
            and "#Subject#unrelatedFlow(long)" in item
            for item in changed_diff["guard_violations"]
        ), changed_diff

        peer_source = project / "src/main/java/FeaturePeerFixture.java"
        _write(peer_source, FEATURE_ENVY_PEER_BEFORE)
        peer_baseline = _call_scoped(
            "feature_envy",
            capture_target_semantic_guard,
            project,
            "src/main/java/FeaturePeerFixture.java:method=target(String)",
            {"target_class": "SubjectWithPeer"},
            [peer_source.relative_to(project)],
            expected_files=[peer_source],
        )
        _assert_capture(peer_baseline)
        assert any(
            item.get("role") == "baseline_peer"
            and item.get("method") == "existingPeer(int)"
            for item in peer_baseline["witness"]
        ), peer_baseline

        _write(peer_source, FEATURE_ENVY_PEER_AFTER)
        target_line = _line_number(
            FEATURE_ENVY_PEER_AFTER,
            "void target(String value)",
        )
        peer_unchanged = _call_scoped(
            "feature_envy",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/FeaturePeerFixture.java:method=target(String)",
            {"target_class": "SubjectWithPeer"},
            [peer_source.relative_to(project)],
            peer_baseline,
            expected_files=[peer_source],
            changed_line_ranges={
                peer_source.relative_to(project).as_posix(): [
                    {"start": target_line, "end": target_line}
                ]
            },
        )
        _assert_resolved(peer_unchanged, target_missing=False)

        peer_line = _line_number(
            FEATURE_ENVY_PEER_AFTER,
            "void existingPeer(int value)",
        )
        peer_changed = _call_scoped(
            "feature_envy",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/FeaturePeerFixture.java:method=target(String)",
            {"target_class": "SubjectWithPeer"},
            [peer_source.relative_to(project)],
            peer_baseline,
            expected_files=[peer_source],
            changed_line_ranges={
                peer_source.relative_to(project).as_posix(): [
                    (peer_line, peer_line + 5)
                ]
            },
        )
        assert any(
            item.startswith("FEATURE_ENVY_RELOCATED:")
            and "#SubjectWithPeer#existingPeer(int)" in item
            for item in peer_changed["guard_violations"]
        ), peer_changed

        _exercise(
            project,
            smell="god_class",
            source=project / "src/main/java/GodFixture.java",
            before=GOD_CLASS_BEFORE,
            after=GOD_CLASS_AFTER,
            relocated=GOD_CLASS_RELOCATED,
            location="src/main/java/GodFixture.java:class=Candidate",
            selector={"target_class": "Candidate"},
            relocation_code="GOD_CLASS_RELOCATED:",
            target_missing=False,
        )

        refused_baseline, refused_relocated = _exercise(
            project,
            smell="refused_bequest",
            source=project / "src/main/java/RefusedFixture.java",
            before=REFUSED_BEQUEST_BEFORE,
            after=REFUSED_BEQUEST_AFTER,
            relocated=REFUSED_BEQUEST_RELOCATED,
            location="src/main/java/RefusedFixture.java:method=first()",
            selector={"target_class": "Child", "parent": "Parent"},
            relocation_code="REFUSED_BEQUEST_RELOCATED:",
            target_missing=False,
        )
        assert any(
            item.get("role") == "baseline_peer"
            and item.get("method") == "second()"
            for item in refused_baseline["witness"]
        ), refused_baseline
        assert not any(
            "#Child#second()" in item
            for item in refused_relocated["guard_violations"]
        ), refused_relocated
        assert any(
            "#OtherChild#first()" in item
            for item in refused_relocated["guard_violations"]
        ), refused_relocated

        refused_source = project / "src/main/java/RefusedFixture.java"
        _write(refused_source, REFUSED_BEQUEST_NEW)
        refused_new = _call_scoped(
            "refused_bequest",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/RefusedFixture.java:method=first()",
            {"target_class": "Child", "parent": "Parent"},
            [refused_source.relative_to(project)],
            refused_baseline,
            expected_files=[refused_source],
        )
        assert not any(
            "#Child#second()" in item
            for item in refused_new["guard_violations"]
        ), refused_new
        assert any(
            "#Child#third()" in item
            for item in refused_new["guard_violations"]
        ), refused_new

        dead_source = project / "src/main/java/DeadFixture.java"
        moved_dead = project / "src/main/java/MovedDead.java"
        _write(dead_source, DEAD_CODE_BEFORE)
        baseline = _call_scoped(
            "dead_code",
            capture_target_semantic_guard,
            project,
            "src/main/java/DeadFixture.java:method=unusedHelper()",
            {"target_class": "DeadFixture"},
            [dead_source.relative_to(project)],
            expected_files=[dead_source],
        )
        _assert_capture(baseline)

        _write(dead_source, DEAD_CODE_AFTER)
        resolved = _call_scoped(
            "dead_code",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/DeadFixture.java:method=unusedHelper()",
            {"target_class": "DeadFixture"},
            [dead_source.relative_to(project)],
            baseline,
            expected_files=[dead_source],
        )
        _assert_resolved(resolved, target_missing=True)

        _write(moved_dead, DEAD_CODE_RELOCATED)
        relocated = _call_scoped(
            "dead_code",
            evaluate_target_semantic_guard,
            project,
            "src/main/java/DeadFixture.java:method=unusedHelper()",
            {"target_class": "DeadFixture"},
            [dead_source.relative_to(project), moved_dead.relative_to(project)],
            baseline,
            expected_files=[dead_source, moved_dead],
        )
        assert relocated["target_smell_present"] is False, relocated
        assert any(
            item.startswith("DEAD_CODE_RELOCATED:")
            for item in relocated["guard_violations"]
        ), relocated

    print(
        "target-semantic-guards self-check PASS "
        f"smells=4 unrelated_java_not_read={unrelated_count} "
        "full_detector=blocked other_evaluators=blocked decisions=bounded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
