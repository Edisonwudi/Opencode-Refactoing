#!/usr/bin/env python3
"""Focused declaration-level reference checks for Java Dead Code."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java.semantic_detector import run_java_semantic_detector  # noqa: E402


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _dead_methods(root: Path) -> set[tuple[str, str]]:
    result = run_java_semantic_detector(root)
    assert result.ok, result.error
    return {
        (item.class_name, item.method)
        for item in result.findings["dead_code"]
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dead-code-reference-resolution-") as temp_dir:
        project = Path(temp_dir)
        _write(
            project,
            "SameName.java",
            """\
class SameNameTarget {
  private void helper() {}
}
class SameNameCaller {
  void live() { helper(); }
  void helper() {}
}
""",
        )
        _write(
            project,
            "Overloaded.java",
            """\
class Overloaded {
  private void select(int value) {}
  private void select(String value) {}
  private void mixedVisibility(int value) {}
  void mixedVisibility(String value) {}
  private void uncertain(String value) {}
  private void uncertain(Object value) {}
  void live() {
    select("used");
    mixedVisibility("public overload");
    uncertain(null);
  }
}
class InheritedApi {
  void inherited(String value) {}
}
class InheritedOverload extends InheritedApi {
  private void inherited(int value) {}
  void live() { inherited("parent overload"); }
}
""",
        )
        _write(
            project,
            "Recursive.java",
            """\
class Recursive {
  private void loop() { loop(); }
}
""",
        )
        _write(
            project,
            "Referenced.java",
            """\
class Referenced {
  private void used() {}
  private static void staticUsed() {}
  private void receiverUsed() {}
  private void methodReferenceUsed() {}
  void live(Referenced peer) {
    used();
    Referenced.staticUsed();
    peer.receiverUsed();
    Runnable callback = this::methodReferenceUsed;
  }
}
""",
        )

        dead = _dead_methods(project)
        assert ("SameNameTarget", "helper()") in dead, dead
        assert ("Overloaded", "select(int value)") in dead, dead
        assert ("Overloaded", "select(java.lang.String value)") not in dead, dead
        assert ("Overloaded", "mixedVisibility(int value)") in dead, dead
        assert ("InheritedOverload", "inherited(int value)") in dead, dead
        # Java selects String as the unique most-specific target for null;
        # the broader Object overload remains genuinely unreferenced.
        assert ("Overloaded", "uncertain(java.lang.String value)") not in dead, dead
        assert ("Overloaded", "uncertain(java.lang.Object value)") in dead, dead
        assert ("Recursive", "loop()") in dead, dead
        assert ("Referenced", "used()") not in dead, dead
        assert ("Referenced", "staticUsed()") not in dead, dead
        assert ("Referenced", "receiverUsed()") not in dead, dead
        assert ("Referenced", "methodReferenceUsed()") not in dead, dead

    print(
        "dead-code-reference-resolution-self-check PASS "
        "owner=exact overload=signature most-specific=java recursion=ignored "
        "unresolved=fail-closed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
