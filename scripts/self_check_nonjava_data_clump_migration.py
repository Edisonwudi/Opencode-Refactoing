#!/usr/bin/env python3
"""Focused checks for controlled non-Java Data Clumps migration."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.data_clump_migration import (  # noqa: E402
    DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT,
    DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT,
    authorize_data_clump_compatibility_changes,
    evaluate_data_clump_declaration_migration,
)
from smell_core.checkpoint_adapters import detector_profile_for  # noqa: E402
from smell_core.checkpoint_contract import checkpoint_gate_result  # noqa: E402
from smell_core.data_clumps import (  # noqa: E402
    evaluate_data_clump_checkpoint_contract,
)
from smell_core.target_patch_identity import ast_declaration_identity  # noqa: E402


GROUP = "int:end|int:retry|int:start"


def _target(
    target_index: int,
    name: str,
    line: int,
    signature: str,
    parameters: list[str],
    *,
    resolved: bool = True,
) -> dict[str, object]:
    return {
        "target_index": target_index,
        "file": "targets.py",
        "method": name,
        "resolved": resolved,
        "begin_line": line,
        "end_line": line + 1,
        "signature_text": signature,
        "parameter_fingerprints": parameters,
        "parameter_slots": [
            {"slot": index, "type": "int", "member": member}
            for index, member in enumerate(GROUP.split("|"))
        ],
        "group": GROUP,
        "declaration_identity": ast_declaration_identity(name, ""),
        "body_copy_not_applicable": "empty_function_body",
        "body_windows": [],
        "body_text": "",
    }


def _successor(target_index: int = 0) -> dict[str, object]:
    value = _target(
        target_index,
        "process_bounds",
        1,
        "def process_bounds(bounds: Bounds):",
        ["Bounds:bounds"],
    )
    value["parameter_slots"] = []
    value["group"] = ""
    return value


def _renamed_patch(*, wrapper: bool = False) -> str:
    added_wrapper = (
        "+def alpha(start: int, end: int, retry: int):\n"
        "+    return process_bounds(Bounds(start, end, retry))\n"
        if wrapper
        else ""
    )
    return (
        "diff --git a/targets.py b/targets.py\n"
        "--- a/targets.py\n"
        "+++ b/targets.py\n"
        "@@ -1,2 +1,4 @@\n"
        "-def alpha(start: int, end: int, retry: int):\n"
        "+def process_bounds(bounds: Bounds):\n"
        "+    return bounds.start + bounds.end + bounds.retry\n"
        f"{added_wrapper}"
    )


def _complete_migration(*, wrapper: bool = False) -> dict[str, object]:
    baseline = [_target(
        0,
        "alpha",
        1,
        "def alpha(start: int, end: int, retry: int):",
        ["int:start", "int:end", "int:retry"],
    )]
    current = [_successor()]
    return evaluate_data_clump_declaration_migration(
        baseline,
        current,
        changed_patch=_renamed_patch(wrapper=wrapper),
        language="python",
        group=GROUP,
    )


def _check_complete_and_exact_authorization() -> None:
    migration = _complete_migration()
    assert migration["ok"] is True, migration
    assert migration["old_group_entries_removed"] is True, migration
    assert migration["project_full_required"] is True, migration
    assert migration["closure_status"] == "requires_project_full", migration
    exact = {
        "violations": [{
            "code": "PUBLIC_PYTHON_SIGNATURE_CHANGED",
            "target_index": 0,
            "file": "targets.py",
            "owner": "",
            "method": "alpha",
        }],
    }
    authorized = authorize_data_clump_compatibility_changes(
        exact,
        migration,
        production_patch=_renamed_patch(),
        group=GROUP,
    )
    assert authorized["ok"] is True, authorized
    assert authorized["authorized"][0]["migration_target_index"] == 0

    mismatch = copy.deepcopy(exact)
    mismatch["violations"][0]["method"] = "unrelated"
    rejected = authorize_data_clump_compatibility_changes(
        mismatch,
        migration,
        production_patch=_renamed_patch(),
        group=GROUP,
    )
    assert rejected["ok"] is False, rejected

    unavailable = copy.deepcopy(exact)
    unavailable["violations"][0]["code"] = (
        "PUBLIC_PYTHON_SIGNATURE_UNAVAILABLE"
    )
    rejected = authorize_data_clump_compatibility_changes(
        unavailable,
        migration,
        production_patch=_renamed_patch(),
        group=GROUP,
    )
    assert rejected["ok"] is False, rejected

    no_final_gate = copy.deepcopy(migration)
    no_final_gate["project_full_required"] = False
    rejected = authorize_data_clump_compatibility_changes(
        exact,
        no_final_gate,
        production_patch=_renamed_patch(),
        group=GROUP,
    )
    assert rejected["ok"] is False, rejected


def _check_no_patch_and_wrapper_rejected() -> None:
    baseline = [_target(
        0,
        "alpha",
        1,
        "def alpha(start: int, end: int, retry: int):",
        ["int:start", "int:end", "int:retry"],
    )]
    current = [_successor()]
    missing = evaluate_data_clump_declaration_migration(
        baseline,
        current,
        changed_patch=None,
        language="python",
        group=GROUP,
    )
    assert missing["ok"] is False, missing
    assert missing["error"] == "changed_target_hunks_unavailable", missing

    wrapper = _complete_migration(wrapper=True)
    assert wrapper["ok"] is False, wrapper
    assert wrapper["old_group_entries_removed"] is False, wrapper
    assert wrapper["parallel_old_group_entries"], wrapper
    assert any(
        item.get("reason") == "parallel_old_group_entry_added"
        for item in wrapper["failures"]
    ), wrapper

    migration = _complete_migration()
    cross_file_patch = _renamed_patch() + (
        "diff --git a/compat.py b/compat.py\n"
        "--- a/compat.py\n"
        "+++ b/compat.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def alpha(start: int, end: int, retry: int):\n"
        "+    return process_bounds(Bounds(start, end, retry))\n"
    )
    cross_file = authorize_data_clump_compatibility_changes(
        {"violations": []},
        migration,
        production_patch=cross_file_patch,
        group=GROUP,
    )
    assert cross_file["ok"] is False, cross_file
    assert any(
        item.get("code") == "DATA_CLUMP_PARALLEL_OLD_GROUP_ENTRY_ADDED"
        and any(
            entry.get("file") == "compat.py"
            for entry in list(item.get("entries") or [])
        )
        for item in cross_file["violations"]
    ), cross_file


def _check_cpp_virtual_authorization_binds_owner_and_successor() -> None:
    predecessor = {
        "target_index": 0,
        "file": "iface.hpp",
        "declared_name": "f",
        "owner_qualified_name": "A",
        "signature_text": (
            "virtual void f(int start, int end, int retry) = 0;"
        ),
        "parameter_fingerprints": [
            "int:start", "int:end", "int:retry",
        ],
        "parameter_slots": [],
    }
    successor = {
        "target_index": 0,
        "file": "iface.hpp",
        "declared_name": "f",
        "owner_qualified_name": "A",
        "signature_text": "virtual void f(Bounds bounds) = 0;",
        "parameter_fingerprints": ["Bounds:bounds"],
        "parameter_slots": [],
    }
    migration = {
        "language": "cpp",
        "applicable": True,
        "ok": True,
        "project_full_required": True,
        "closure_status": "requires_project_full",
        "old_group_entries_removed": True,
        "parallel_old_group_entries": [],
        "migrated_target_indexes": [0],
        "lineage": [{
            "predecessor": predecessor,
            "successors": [successor],
            "patch_witness": [{
                "kind": "strict_patch_anchor_with_changed_signature",
                "file": "iface.hpp",
                "baseline_begin_line": 2,
                "current_begin_line": 2,
            }],
            "old_group_entry_removed": True,
            "relation": "one_to_one",
        }],
    }
    compatibility = {"violations": [{
        "code": "CPP_PURE_VIRTUAL_ABI_CHANGED",
        "file": "iface.hpp",
        "method": "f",
        "baseline_declaration": (
            "virtual void f ( int start , int end , int retry ) = 0 ;"
        ),
    }]}
    wrong_owner_patch = (
        "diff --git a/iface.hpp b/iface.hpp\n"
        "--- a/iface.hpp\n"
        "+++ b/iface.hpp\n"
        "@@ -1,3 +1,5 @@\n"
        " class A {\n"
        "- virtual void f(int start, int end, int retry) = 0;\n"
        "+ virtual void f(Bounds bounds) {}\n"
        " };\n"
        "+class B {\n"
        "+ virtual void f(Bounds bounds) = 0;\n"
    )
    wrong_owner = authorize_data_clump_compatibility_changes(
        compatibility,
        migration,
        production_patch=wrong_owner_patch,
        group=GROUP,
    )
    assert wrong_owner["ok"] is False, wrong_owner
    assert wrong_owner["authorized"] == [], wrong_owner

    nested_wrong_owner_patch = (
        "diff --git a/iface.hpp b/iface.hpp\n"
        "--- a/iface.hpp\n"
        "+++ b/iface.hpp\n"
        "@@ -1,7 +1,8 @@\n"
        " class A {\n"
        "- virtual void f(int start, int end, int retry) = 0;\n"
        "+ virtual void f(Bounds bounds) {}\n"
        "  class B\n"
        "  {\n"
        "+  virtual void f(Bounds bounds) = 0;\n"
        "  };\n"
        " };\n"
    )
    nested_wrong_owner = authorize_data_clump_compatibility_changes(
        compatibility,
        migration,
        production_patch=nested_wrong_owner_patch,
        group=GROUP,
    )
    assert nested_wrong_owner["ok"] is False, nested_wrong_owner
    assert nested_wrong_owner["authorized"] == [], nested_wrong_owner

    same_owner_patch = (
        "diff --git a/iface.hpp b/iface.hpp\n"
        "--- a/iface.hpp\n"
        "+++ b/iface.hpp\n"
        "@@ -1,3 +1,3 @@\n"
        " class A {\n"
        "- virtual void f(int start, int end, int retry) = 0;\n"
        "+ virtual void f(Bounds bounds) = 0;\n"
        " };\n"
    )
    same_owner = authorize_data_clump_compatibility_changes(
        compatibility,
        migration,
        production_patch=same_owner_patch,
        group=GROUP,
    )
    assert same_owner["ok"] is True, same_owner
    assert len(same_owner["authorized"]) == 1, same_owner


def _check_cpp_cross_file_member_wrapper_rejected() -> None:
    predecessor = {
        "target_index": 0,
        "file": "iface.hpp",
        "declared_name": "f",
        "owner_qualified_name": "A",
        "signature_text": "int f(int start, int end, int retry);",
        "parameter_fingerprints": [
            "int:start", "int:end", "int:retry",
        ],
        "parameter_slots": [],
    }
    successor = {
        "target_index": 0,
        "file": "iface.hpp",
        "declared_name": "f",
        "owner_qualified_name": "A",
        "signature_text": "int f(Bounds bounds);",
        "parameter_fingerprints": ["Bounds:bounds"],
        "parameter_slots": [],
    }
    migration = {
        "language": "cpp",
        "applicable": True,
        "ok": True,
        "project_full_required": True,
        "closure_status": "requires_project_full",
        "old_group_entries_removed": True,
        "parallel_old_group_entries": [],
        "migrated_target_indexes": [0],
        "lineage": [{
            "predecessor": predecessor,
            "successors": [successor],
            "patch_witness": [{
                "kind": "strict_patch_anchor_with_changed_signature",
                "file": "iface.hpp",
                "baseline_begin_line": 2,
                "current_begin_line": 2,
            }],
            "old_group_entry_removed": True,
            "relation": "one_to_one",
        }],
    }
    cross_file_member_wrapper_patch = (
        "diff --git a/iface.hpp b/iface.hpp\n"
        "--- a/iface.hpp\n"
        "+++ b/iface.hpp\n"
        "@@ -1,3 +1,3 @@\n"
        " class A {\n"
        "- int f(int start, int end, int retry);\n"
        "+ int f(Bounds bounds);\n"
        " };\n"
        "diff --git a/compat.hpp b/compat.hpp\n"
        "--- a/compat.hpp\n"
        "+++ b/compat.hpp\n"
        "@@ -1,2 +1,5 @@\n"
        " class A {\n"
        "+ int f(int start, int end, int retry) {\n"
        "+   return f(Bounds{start, end, retry});\n"
        "+ }\n"
        " };\n"
    )
    rejected = authorize_data_clump_compatibility_changes(
        {"violations": []},
        migration,
        production_patch=cross_file_member_wrapper_patch,
        group=GROUP,
    )
    assert rejected["ok"] is False, rejected
    assert any(
        item.get("code") == "DATA_CLUMP_PARALLEL_OLD_GROUP_ENTRY_ADDED"
        and any(
            entry.get("file") == "compat.hpp"
            and entry.get("owner_qualified_name") == "A"
            for entry in list(item.get("entries") or [])
        )
        for item in rejected["violations"]
    ), rejected


def _check_partial_migration_rejected() -> None:
    baseline = [
        _target(
            0,
            "alpha",
            1,
            "def alpha(start: int, end: int, retry: int):",
            ["int:start", "int:end", "int:retry"],
        ),
        _target(
            1,
            "beta",
            3,
            "def beta(start: int, end: int, retry: int):",
            ["int:start", "int:end", "int:retry"],
        ),
    ]
    successor = _successor()
    unresolved = _target(
        1,
        "beta",
        3,
        "",
        [],
        resolved=False,
    )
    checkpoint = evaluate_data_clump_checkpoint_contract(
        {
            "occurrences": [],
            "target_snapshots": [successor, unresolved],
            "unresolved_targets": [{"target_index": 1}],
        },
        language="python",
        baseline_occurrence_contract=baseline,
        changed_patch=_renamed_patch(),
    )
    assert checkpoint["target_patch_identity_ok"] is False, checkpoint
    assert any(
        item.get("target_index") == 1
        for item in checkpoint["target_patch_identity_failures"]
    ), checkpoint


def _check_many_to_one_lineage_is_explicit() -> None:
    baseline = [
        _target(
            0,
            "alpha",
            1,
            "def alpha(start: int, end: int, retry: int):",
            ["int:start", "int:end", "int:retry"],
        ),
        _target(
            1,
            "beta",
            2,
            "def beta(start: int, end: int, retry: int):",
            ["int:start", "int:end", "int:retry"],
        ),
    ]
    current = [_successor(index) for index in (0, 1)]
    patch = (
        "diff --git a/targets.py b/targets.py\n"
        "--- a/targets.py\n"
        "+++ b/targets.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def alpha(start: int, end: int, retry: int):\n"
        "-def beta(start: int, end: int, retry: int):\n"
        "+def process_bounds(bounds: Bounds):\n"
        "+    return bounds.start + bounds.end + bounds.retry\n"
    )
    migration = evaluate_data_clump_declaration_migration(
        baseline,
        current,
        changed_patch=patch,
        language="python",
        group=GROUP,
    )
    assert migration["ok"] is True, migration
    assert migration["relation_kinds"] == ["many_to_one"], migration
    assert len(migration["lineage"]) == 2, migration


def _check_migration_requires_project_full_mode() -> None:
    profile = detector_profile_for(SimpleNamespace(
        smell="data_clumps",
        language="python",
    ))
    assert profile["version"].endswith("/data_clumps/v4"), profile
    assert profile["declaration_migration_contract"] == (
        DATA_CLUMP_DECLARATION_MIGRATION_CONTRACT
    ), profile
    assert profile["migration_closure_contract"] == (
        DATA_CLUMP_PROJECT_FULL_CLOSURE_CONTRACT
    ), profile
    assert profile["migration_final_verification"] == "project_full", profile

    metrics = {
        "ok": True,
        "candidate_count": 0,
        "target_patch_identity_ok": True,
        "target_missing": False,
        "finding_present": False,
        "project_full_required": True,
        "declaration_migration_mode": "api_abi_migration",
        "objectives": {"occurrence_count": 2},
    }
    checkpoint = {
        "required": True,
        "smell": "data_clumps",
        "checkpoint_id": "c-data-clump-migration",
        "current_metrics": metrics,
        "delta": {"metric_progress": True, "reason": "METRIC_PROGRESS"},
    }
    sample_optimized = checkpoint_gate_result(
        "data_clumps",
        {**checkpoint, "verification_mode": "sample_optimized"},
    )
    assert sample_optimized is not None, sample_optimized
    assert sample_optimized["success"] is False, sample_optimized
    assert sample_optimized["details"]["reason"] == (
        "DATA_CLUMPS_PROJECT_FULL_REQUIRED"
    ), sample_optimized
    assert checkpoint_gate_result(
        "data_clumps",
        {**checkpoint, "verification_mode": "project_full"},
    ) is None


def main() -> int:
    _check_complete_and_exact_authorization()
    _check_no_patch_and_wrapper_rejected()
    _check_cpp_virtual_authorization_binds_owner_and_successor()
    _check_cpp_cross_file_member_wrapper_rejected()
    _check_partial_migration_rejected()
    _check_many_to_one_lineage_is_explicit()
    _check_migration_requires_project_full_mode()
    print(
        "Non-Java Data Clumps migration self-check passed: exact lineage, "
        "project_full obligation, local/cross-file wrapper rejection, "
        "owner-bound C++ ABI authorization, and auditable many-to-one mapping"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
