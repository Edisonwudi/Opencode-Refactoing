#!/usr/bin/env python3
"""Positive and negative checks for target-local API/ABI continuity."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.compatibility_contract import (  # noqa: E402
    evaluate_target_local_compatibility,
)
from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402


def _target(
    index: int,
    signature: str,
    *,
    name: str,
    owner: str = "",
) -> dict[str, object]:
    return {
        "target_index": index,
        "file": "src/package/api.py",
        "resolved": True,
        "signature_text": signature,
        "declaration_identity": {
            "declared_name": name,
            "owner_qualified_name": owner,
        },
    }


def main() -> None:
    public_before = _target(
        0,
        "def remove_cookie_by_name(jar, name: str, domain=None, path=None):",
        name="remove_cookie_by_name",
    )
    public_after = _target(
        0,
        "def remove_cookie_by_name(jar, query: CookieQuery):",
        name="remove_cookie_by_name",
    )
    public = evaluate_target_local_compatibility(
        language="python",
        baseline_targets=[public_before],
        current_targets=[public_after],
        production_patch="",
    )
    assert public["ok"] is False, public
    assert public["violations"][0]["code"] == (
        "PUBLIC_PYTHON_SIGNATURE_CHANGED"
    ), public

    private_before = _target(
        0,
        "def _find(self, name: str, domain=None, path=None):",
        name="_find",
        owner="CookieJar",
    )
    private_after = _target(
        0,
        "def _find(self, query: CookieQuery):",
        name="_find",
        owner="CookieJar",
    )
    private = evaluate_target_local_compatibility(
        language="python",
        baseline_targets=[private_before],
        current_targets=[private_after],
        production_patch="",
    )
    assert private["ok"] is True, private

    constructor_before = _target(
        0,
        "def __init__(self, name, disable_cache=False, pool_size=10):",
        name="__init__",
        owner="PyPiRepository",
    )
    constructor_after = _target(
        0,
        "def __init__(self, name, pool_config: PoolConfig | None = None):",
        name="__init__",
        owner="PyPiRepository",
    )
    constructor = evaluate_target_local_compatibility(
        language="python",
        baseline_targets=[constructor_before],
        current_targets=[constructor_after],
        production_patch="",
    )
    assert constructor["ok"] is False, constructor

    unchanged = evaluate_target_local_compatibility(
        language="python",
        baseline_targets=[public_before],
        current_targets=[dict(public_before)],
        production_patch="",
    )
    assert unchanged["ok"] is True, unchanged

    virtual_patch = """diff --git a/include/eventhandler.h b/include/eventhandler.h
--- a/include/eventhandler.h
+++ b/include/eventhandler.h
@@ -20,4 +20,6 @@ class EventHandler {
-  virtual void OnMapStart(const Mark& mark, const std::string& tag,
-                          anchor_t anchor, Style style) = 0;
+  virtual void OnMapStart(const Mark& mark, const std::string& tag,
+                          anchor_t anchor, Style style) {}
+  virtual void OnMapStart(const NodeStart& node_start) = 0;
 };
"""
    virtual = evaluate_target_local_compatibility(
        language="cpp",
        baseline_targets=[],
        current_targets=[],
        production_patch=virtual_patch,
    )
    assert virtual["ok"] is False, virtual
    assert virtual["violations"][0]["code"] == (
        "CPP_PURE_VIRTUAL_ABI_CHANGED"
    ), virtual
    assert virtual["violations"][0]["method"] == "OnMapStart", virtual
    gated = evaluate_checkpoint_contract(
        {
            "ok": True,
            "candidate_count": 1,
            "finding_present": True,
            "objectives": {"occurrence_count": 4},
        },
        {
            "ok": True,
            "candidate_count": 0,
            "finding_present": False,
            "objectives": {"occurrence_count": 0},
            "guard_violations": virtual["violations"],
        },
        has_production_diff=True,
        smell="data_clumps",
    ).to_dict()
    assert gated["metric_progress"] is False, gated
    assert gated["reason"] == "SEMANTIC_CONTRACT_REGRESSION", gated

    source_only_patch = """diff --git a/src/worker.cpp b/src/worker.cpp
--- a/src/worker.cpp
+++ b/src/worker.cpp
@@ -1 +1 @@
-void Worker::run(int a, int b) {}
+void Worker::run(Request request) {}
"""
    source_only = evaluate_target_local_compatibility(
        language="cpp",
        baseline_targets=[],
        current_targets=[],
        production_patch=source_only_patch,
    )
    assert source_only["ok"] is True, source_only

    whitespace_only = """diff --git a/include/eventhandler.h b/include/eventhandler.h
--- a/include/eventhandler.h
+++ b/include/eventhandler.h
@@ -1 +1 @@
-  virtual void OnMapStart(const Mark&, Style) = 0;
+virtual void OnMapStart( const Mark & , Style )=0;
"""
    whitespace = evaluate_target_local_compatibility(
        language="cpp",
        baseline_targets=[],
        current_targets=[],
        production_patch=whitespace_only,
    )
    assert whitespace["ok"] is True, whitespace

    print(
        "non-Java compatibility contract self-check passed: "
        "public function=blocked public constructor=blocked private=allowed "
        "pure virtual ABI=blocked source-only=allowed whitespace=allowed"
    )


if __name__ == "__main__":
    main()
