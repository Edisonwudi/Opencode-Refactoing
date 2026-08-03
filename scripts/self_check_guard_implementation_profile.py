#!/usr/bin/env python3
"""Verify that each Java Guard profile hashes its real implementation closure."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_adapters import (  # noqa: E402
    _JAVA_GUARD_DISPATCH_MODULES,
    _JAVA_GUARD_ENTRY_MODULES,
    _JAVA_GUARD_ORCHESTRATOR_MODULE,
    _java_guard_dependency_closure,
    _java_guard_implementation_profile,
    _smell_core_imports,
    _smell_core_module_path,
)
from smell_core.loop_policy import CHECKPOINT_SMELLS  # noqa: E402


def main() -> int:
    package_root = ROOT / "runtime" / "python" / "smell_core"
    for smell in sorted(CHECKPOINT_SMELLS):
        closure = _java_guard_dependency_closure(smell)
        assert closure == tuple(sorted(set(closure))), (smell, closure)

        profile = _java_guard_implementation_profile(smell)
        assert profile == _java_guard_implementation_profile(smell), smell
        profile_paths = tuple(item["path"] for item in profile["files"])
        assert profile_paths == tuple(sorted(profile_paths)), (smell, profile_paths)
        assert set(profile_paths) == {"checkpoint_adapters.py", *closure}, smell
        assert {"__init__.py", "java/__init__.py"}.issubset(closure), (smell, closure)

        entry_paths = {
            _smell_core_module_path(module).relative_to(package_root).as_posix()
            for module in _JAVA_GUARD_ENTRY_MODULES[smell]
        }
        assert entry_paths.issubset(closure), (smell, entry_paths, closure)

        allowed_dispatch = frozenset(_JAVA_GUARD_ENTRY_MODULES[smell])
        for relative in closure:
            path = package_root / relative
            module_parts = Path(relative).with_suffix("").parts
            if module_parts[-1] == "__init__":
                module_parts = module_parts[:-1]
            module = ".".join(("smell_core", *module_parts))
            for imported, function_local in _smell_core_imports(module, path):
                if (
                    module == _JAVA_GUARD_ORCHESTRATOR_MODULE
                    and function_local
                    and imported in _JAVA_GUARD_DISPATCH_MODULES
                    and imported not in allowed_dispatch
                ):
                    continue
                imported_path = _smell_core_module_path(imported)
                imported_relative = imported_path.relative_to(package_root).as_posix()
                assert imported_relative in closure, (
                    smell,
                    relative,
                    imported_relative,
                )

    clone = set(_java_guard_dependency_closure("code_clone_type1"))
    assert "java/target_clone_guard.py" in clone
    assert "java/semantic_detector.py" in clone
    assert "java/target_relational_guards.py" not in clone

    clumps = set(_java_guard_dependency_closure("data_clumps"))
    assert "java/target_relational_guards.py" in clumps
    assert "java/data_clumps.py" not in clumps

    for scoped_smell in (
        "data_clumps",
        "dead_code",
        "feature_envy",
        "god_class",
        "long_parameter_list",
        "refused_bequest",
    ):
        scoped = set(_java_guard_dependency_closure(scoped_smell))
        assert "java/syntactic_detector.py" not in scoped, (scoped_smell, scoped)

    feature_envy = set(_java_guard_dependency_closure("feature_envy"))
    assert {
        "java/target_feature_envy_scope.py",
        "java/target_relation_scope.py",
        "java/target_semantic_guards.py",
    }.issubset(feature_envy)

    try:
        _java_guard_dependency_closure("unknown_smell")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown Java Guard route did not fail closed")

    print(f"guard implementation profile self-check PASS smells={len(CHECKPOINT_SMELLS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
