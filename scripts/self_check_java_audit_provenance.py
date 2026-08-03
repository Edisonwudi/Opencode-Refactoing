#!/usr/bin/env python3
"""Focused checks for Java finding-contract audit provenance."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from audit_java_finding_contract import _snapshot_production_source_provenance
from smell_core.java.semantic_detector import run_java_semantic_detector
from smell_core.java.source_layout import discover_java_source_layout


def _profile() -> dict:
    return {
        "id": "java-product/data_clumps/v4",
        "schema": 4,
        "language": "java",
        "smell": "data_clumps",
        "source_layout": "static-build-descriptor-roles-v4",
        "selector_input": "validated-target-context-only-v4",
        "smell_evidence": "audit-only",
        "implementation": {"sha256": "0" * 64},
    }


def _detector_projection(project: Path) -> list[tuple[str, str, int]]:
    detection = run_java_semantic_detector(project)
    assert detection.ok, detection.error
    return sorted(
        (
            finding.file,
            str(finding.attributes.get("group") or ""),
            int(finding.score),
        )
        for finding in detection.findings["data_clumps"]
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="java-audit-provenance-") as temp_dir:
        project = Path(temp_dir)
        production_paths = []
        for name, method in (("A", "first"), ("B", "second"), ("C", "third")):
            path = project / "src" / "main" / "java" / "p" / f"{name}.java"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"package p; class {name} {{ void {method}(int x, int y, int z) {{}} }}\n",
                encoding="utf-8",
            )
            production_paths.append(path.relative_to(project).as_posix())
        test_source = project / "src" / "test" / "java" / "p" / "Injected.java"
        test_source.parent.mkdir(parents=True, exist_ok=True)
        test_source.write_text(
            "package p; class Injected { void fake(int x, int y, int z) {} }\n",
            encoding="utf-8",
        )

        before = _detector_projection(project)
        test_source.write_text(
            "package p; class Injected { void changed(String alpha, String beta, String gamma) {} }\n",
            encoding="utf-8",
        )
        after = _detector_projection(project)
        assert before == after, (before, after)
        assert len(before) == 3, before

        snapshot = {
            "ok": True,
            "adapter": "data_clumps",
            "detector": "python_semantic_detector",
            "detector_profile": _profile(),
            # The removed substring heuristic rejected this harmless key.
            "latest": {"status": "stable"},
            "finding_identity": {"file": production_paths[0], "group": "int:x|int:y|int:z"},
            "occurrence_catalog": [
                {"file": path, "begin_line": 1}
                for path in production_paths
            ],
            "migration_closure": {
                "target": {"file": production_paths[0]},
                "production_call_sites": [{"file": production_paths[1]}],
            },
        }
        plan = {
            "worklist": [
                {"kind": "remaining_occurrence", "file": production_paths[0], "begin_line": 1}
            ]
        }
        layout = discover_java_source_layout(project)
        valid = _snapshot_production_source_provenance(
            snapshot,
            smell="data_clumps",
            project_root=project,
            source_layout=layout,
            resolution_plan=plan,
        )
        assert valid["ok"] is True, valid
        assert valid["checked_source_references"] == 7, valid

        test_occurrence = copy.deepcopy(snapshot)
        test_occurrence["occurrence_catalog"].append({
            "file": test_source.relative_to(project).as_posix(),
            "begin_line": 1,
        })
        rejected_occurrence = _snapshot_production_source_provenance(
            test_occurrence,
            smell="data_clumps",
            project_root=project,
            source_layout=layout,
            resolution_plan=plan,
        )
        assert rejected_occurrence["ok"] is False, rejected_occurrence
        assert any(
            item.startswith("TEST_SOURCE_IN_PRODUCT_CONTRACT:snapshot.occurrence_catalog")
            for item in rejected_occurrence["violations"]
        ), rejected_occurrence

        test_plan = copy.deepcopy(plan)
        test_plan["worklist"][0]["file"] = test_source.relative_to(project).as_posix()
        rejected_worklist = _snapshot_production_source_provenance(
            snapshot,
            smell="data_clumps",
            project_root=project,
            source_layout=layout,
            resolution_plan=test_plan,
        )
        assert rejected_worklist["ok"] is False, rejected_worklist
        assert any(
            item.startswith("TEST_SOURCE_IN_PRODUCT_CONTRACT:resolution_plan.worklist")
            for item in rejected_worklist["violations"]
        ), rejected_worklist

        wrong_profile = copy.deepcopy(snapshot)
        wrong_profile["detector_profile"]["source_layout"] = "unknown"
        rejected_profile = _snapshot_production_source_provenance(
            wrong_profile,
            smell="data_clumps",
            project_root=project,
            source_layout=layout,
            resolution_plan=plan,
        )
        assert rejected_profile["ok"] is False, rejected_profile
        assert "PRODUCT_SOURCE_LAYOUT_PROFILE_MISMATCH" in rejected_profile["violations"], rejected_profile

    print(
        "java-audit-provenance-self-check PASS "
        "latest_key=allowed test_source=blocked test_perturbation=equivalent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
