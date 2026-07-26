#!/usr/bin/env python3
"""Focused regression checks for the Refused Bequest semantic guard."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.java.semantic_detector import (  # noqa: E402
    analyze_refused_bequest_target,
    run_java_semantic_detector,
)
from smell_core.java.smell_guards import _run_semantic_guard  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402
from smell_core.detector_utils import (  # noqa: E402
    parse_expected_state_field,
    parse_parent_from_evidence,
    parse_structural_expectation,
)


PARENT = """\
class Parent {
  void first() {}
  void second() {}
  void third() {}
  void fourth() {}
  void fifth() {}
}
"""


def _findings(child_declaration: str):
    with tempfile.TemporaryDirectory(prefix="refused-bequest-self-check-") as temp_dir:
        root = Path(temp_dir)
        (root / "Fixture.java").write_text(PARENT + child_declaration, encoding="utf-8")
        result = run_java_semantic_detector(root)
        if not result.ok:
            raise AssertionError(result.error)
        return result.findings["refused_bequest"]


def _capability_profile(parent_declaration: str, child_declaration: str):
    with tempfile.TemporaryDirectory(prefix="refused-bequest-capability-profile-") as temp_dir:
        root = Path(temp_dir)
        source = root / "Fixture.java"
        source.write_text(parent_declaration + child_declaration, encoding="utf-8")
        return analyze_refused_bequest_target(
            root,
            target_file=source,
            method="target",
            line=6,
            reported_parent="ParentCapability",
        )


def _capability_guard(
    parent_declaration: str,
    child_declaration: str,
    *,
    structural: bool = True,
    structural_expectation: str = "",
    expected_state_field: str = "",
):
    with tempfile.TemporaryDirectory(prefix="refused-bequest-capability-guard-") as temp_dir:
        root = Path(temp_dir)
        source = root / "Fixture.java"
        source.write_text(parent_declaration + child_declaration, encoding="utf-8")
        expectation = structural_expectation or ("capability_split" if structural else "")
        evidence = "parents=ParentCapability; flags=explicit_unsupported_throw"
        if expectation:
            evidence += f"; structural_expectation={expectation}"
        if expected_state_field:
            evidence += f"; expected_state_field={expected_state_field}"
        config = SimpleNamespace(
            project_root=root,
            language="java",
            locations=[
                parse_location_descriptor(
                    "Fixture.java:method=target|line=6",
                    root,
                )
            ],
        )
        return _run_semantic_guard(config, "refused_bequest", evidence)


def main() -> int:
    if parse_parent_from_evidence("quality=STRICT_PASS; parents=Parent|Ancestor; flags=empty_override") != "parent":
        raise AssertionError("dataset parents= evidence must resolve to its primary parent")
    if (
        parse_structural_expectation(
            "flags=explicit_unsupported_throw; structural_expectation=capability_split"
        )
        != "capability_split"
    ):
        raise AssertionError("structural expectation must be parsed from dataset evidence")
    if parse_structural_expectation("flags=explicit_unsupported_throw"):
        raise AssertionError("missing structural expectation must remain empty")
    if (
        parse_structural_expectation(
            "flags=explicit_unsupported_throw; "
            "structural_expectation=rejecting_override_removed"
        )
        != "rejecting_override_removed"
    ):
        raise AssertionError("rejecting override expectation must be parsed from dataset evidence")
    if (
        parse_expected_state_field(
            "structural_expectation=state_getter; expected_state_field=isMultipleValues"
        )
        != "isMultipleValues"
    ):
        raise AssertionError("state getter backing field must be parsed from dataset evidence")

    parent_contract = """\
interface ParentCapability {
  Object target();
}
"""
    unchanged = _capability_profile(
        parent_contract,
        """\
class Child implements ParentCapability {
  public Object target() {
    throw new UnsupportedOperationException();
  }
}
""",
    )
    if not unchanged["inherits_reported_parent"]:
        raise AssertionError("implemented interfaces must be part of the capability profile")
    if not unchanged["child_declares_target"] or not unchanged["parent_declares_target"]:
        raise AssertionError("profile must include target declarations in child and parent")
    if unchanged["capability_split_satisfied"]:
        raise AssertionError("an unchanged capability relationship must not satisfy a split")

    moved_to_parent = _capability_profile(
        """\
interface ParentCapability {
  default Object target() {
    throw new UnsupportedOperationException();
  }
}
""",
        """\
class Child implements ParentCapability {
}
""",
    )
    if moved_to_parent["capability_split_satisfied"]:
        raise AssertionError("moving rejection into a parent default method is not a split")

    parent_removed = _capability_profile(
        parent_contract,
        """\
class Child {
  public Object target() {
    throw new UnsupportedOperationException();
  }
}
""",
    )
    if not parent_removed["capability_split_satisfied"]:
        raise AssertionError("removing the incompatible parent relationship must satisfy a split")

    contract_reduced = _capability_profile(
        """\
interface ParentCapability {
  Object supported();
}
""",
        """\
class Child implements ParentCapability {
  public Object supported() {
    return new Object();
  }
}
""",
    )
    if not contract_reduced["capability_split_satisfied"]:
        raise AssertionError("removing the target capability from parent and child must satisfy a split")

    moved_to_parent_guard = _capability_guard(
        """\
interface ParentCapability {
  default Object target() {
    throw new UnsupportedOperationException();
  }
}
""",
        """\
class Child implements ParentCapability {
}
""",
    )
    if moved_to_parent_guard["success"]:
        raise AssertionError("guard must reject moving rejection into a parent default method")

    helper_hidden_guard = _capability_guard(
        parent_contract,
        """\
class Child implements ParentCapability {
  public Object target() {
    throw unsupported();
  }
  private UnsupportedOperationException unsupported() {
    return new UnsupportedOperationException();
  }
}
""",
    )
    if helper_hidden_guard["success"]:
        raise AssertionError("guard must reject helper-hidden rejection when capability is unchanged")

    parent_removed_guard = _capability_guard(
        parent_contract,
        """\
class Child {
  public Object target() {
    throw unsupported();
  }
  private UnsupportedOperationException unsupported() {
    return new UnsupportedOperationException();
  }
}
""",
    )
    if not parent_removed_guard["success"]:
        raise AssertionError("guard must accept removal of the incompatible parent relation")

    rejecting_override_parent = """\
class ParentCapability {
  Object target() {
    return null;
  }
}
"""
    rejecting_override_present = _capability_guard(
        rejecting_override_parent,
        """\
class Child extends ParentCapability {
  @Override public Object target() {
    return new Object();
  }
}
""",
        structural_expectation="rejecting_override_removed",
    )
    if rejecting_override_present["success"]:
        raise AssertionError("rejecting override contract must fail while the child still declares the method")

    rejecting_override_removed = _capability_guard(
        rejecting_override_parent,
        """\
class Child extends ParentCapability {
}
""",
        structural_expectation="rejecting_override_removed",
    )
    if not rejecting_override_removed["success"]:
        raise AssertionError("rejecting override contract must accept inheritance of the safe parent method")

    rejecting_override_parent_removed = _capability_guard(
        rejecting_override_parent,
        """\
class Child {
}
""",
        structural_expectation="rejecting_override_removed",
    )
    if rejecting_override_parent_removed["success"]:
        raise AssertionError(
            "rejecting override contract is specific to inheriting the reported safe parent method"
        )

    state_getter_guard = _capability_guard(
        parent_contract,
        """\
class Child implements ParentCapability {
  private final Object state = new Object();
  public Object target() {
    return this.state;
  }
}
""",
        structural_expectation="state_getter",
        expected_state_field="state",
    )
    if not state_getter_guard["success"]:
        raise AssertionError("state getter must accept a direct return of its declared backing field")

    for invalid_return in ("false", "true", "otherState"):
        invalid_state_getter_guard = _capability_guard(
            parent_contract,
            f"""\
class Child implements ParentCapability {{
  private final Object state = new Object();
  private final Object otherState = new Object();
  public Object target() {{
    return {invalid_return};
  }}
}}
""",
            structural_expectation="state_getter",
            expected_state_field="state",
        )
        if invalid_state_getter_guard["success"]:
            raise AssertionError(
                f"state getter must reject returning {invalid_return} instead of its backing field"
            )

    logging_only = """\
class Child extends Parent {
  @Override void first() { throw new UnsupportedOperationException(); }
  @Override void second() { LOG.warn("not supported"); }
}
"""
    if len(_findings(logging_only)) != 1:
        raise AssertionError("a logging-only override must not bypass Refused Bequest detection")

    delegated = """\
class Child extends Parent {
  @Override void first() { throw new UnsupportedOperationException(); }
  @Override void second() { owner.run(); }
}
"""
    if _findings(delegated):
        raise AssertionError("real delegation should reduce the rejecting override count")

    restructured = """\
class Child {
  void first() { throw new UnsupportedOperationException(); }
  void second() { throw new UnsupportedOperationException(); }
}
"""
    if _findings(restructured):
        raise AssertionError("removing the inappropriate inheritance must eliminate the smell")

    print("refused_bequest guard semantic self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
