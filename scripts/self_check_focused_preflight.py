#!/usr/bin/env python3
"""Check that focused preflight is bounded and never accepts a sample."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import (  # noqa: E402
    CommandConfig,
    ProjectOverride,
    load_project_overrides,
    load_refactor_config,
    resolve_run_config,
)
from smell_core.guards import run_focused_preflight  # noqa: E402


PROJECTS = {
    "rrdtool": ("c", "/opt/projects/c/rrdtool"),
    "tmux": ("c", "/opt/projects/c/tmux"),
    "protobuf-29.3": ("cpp", "/opt/projects/cpp/protobuf-29.3"),
    "yaml-cpp": ("cpp", "/opt/projects/cpp/yaml-cpp"),
}


def _write_projects(path: Path) -> None:
    lines = ["projects:"]
    for _name, (language, root) in PROJECTS.items():
        lines.extend(
            [
                f"  - root: {root}",
                f"    language: {language}",
                "    build:",
                "      command: old-full-build",
                "    test:",
                "      command: old-full-test",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolved(project: Path, focused: CommandConfig):
    project.mkdir(parents=True, exist_ok=True)
    source = project / "sample.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    config = resolve_run_config(
        refactor_config=load_refactor_config(None),
        project_overrides=[],
        project_root=str(project),
        smell="long_method",
        location="sample.py:method=target|line=1",
        cli_language="python",
        verification_mode="project_full",
    )
    config.focused_preflight = focused
    return config


def _assert_non_accepting(result: dict[str, object], status: str) -> None:
    assert result["schema_version"] == 1, result
    assert result["type"] == "focused_preflight", result
    assert result["status"] == status, result
    assert result["status"] != "PASS", result
    assert result["acceptance"] is False, result
    assert result["project_full_executed"] is False, result
    assert result["cache_scope"] == "compiler_outputs_only", result
    assert result["test_result_reused"] is False, result
    assert result["pass_reused"] is False, result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="focused-preflight-") as raw_temp:
        temp = Path(raw_temp)

        not_applicable = run_focused_preflight(
            _resolved(temp / "none", CommandConfig())
        )
        _assert_non_accepting(not_applicable, "NOT_APPLICABLE")
        assert not_applicable["success"] is True, not_applicable
        assert not_applicable["execution"] is None, not_applicable

        ready_project = temp / "ready"
        ready_project.mkdir()
        ready = run_focused_preflight(
            _resolved(
                ready_project,
                CommandConfig(
                    script="""
                    cd "${project_root}"
                    mkdir -p build-refactoragent
                    printf ready > build-refactoragent/focused.txt
                    """,
                ),
            )
        )
        _assert_non_accepting(ready, "READY")
        assert ready["success"] is True, ready
        assert ready["execution"]["success"] is True, ready
        assert (ready_project / "build-refactoragent" / "focused.txt").is_file()

        failed_project = temp / "failed"
        failed_project.mkdir()
        failed = run_focused_preflight(
            _resolved(failed_project, CommandConfig(command="exit 17"))
        )
        _assert_non_accepting(failed, "FAILED")
        assert failed["success"] is False, failed
        assert failed["execution"]["returncode"] == 17, failed

        canonical = temp / "canonical"
        execution_root = temp / "execution"
        canonical.mkdir()
        execution_root.mkdir()
        (execution_root / "sample.py").write_text(
            "def target():\n    return 1\n",
            encoding="utf-8",
        )
        rebased = resolve_run_config(
            refactor_config=load_refactor_config(None),
            project_overrides=[
                ProjectOverride(
                    root=canonical,
                    language="python",
                    focused_preflight=CommandConfig(
                        command=f"test -d {canonical.resolve()}"
                    ),
                )
            ],
            project_root=str(execution_root),
            project_override_root=str(canonical),
            smell="long_method",
            location="sample.py:method=target|line=1",
            cli_language="python",
            verification_mode="project_full",
        )
        assert str(canonical.resolve()) not in (
            rebased.focused_preflight.command or ""
        )
        assert str(execution_root.resolve()) in (
            rebased.focused_preflight.command or ""
        )
        assert rebased.to_dict()["focused_preflight"] == {
            "command": f"test -d {execution_root.resolve()}",
            "script": None,
        }

        projects = temp / "projects.yaml"
        _write_projects(projects)
        overrides = {
            item.root.name: item for item in load_project_overrides(str(projects))
        }
        assert set(overrides) == set(PROJECTS), overrides
        for name, item in overrides.items():
            script = item.focused_preflight.script or ""
            assert script, name
            assert "ctest" not in script, (name, script)
            assert "make check" not in script, (name, script)
            assert ".smell-test-reports" not in script, (name, script)
            assert "PASS" not in script, (name, script)

        protobuf = overrides["protobuf-29.3"]
        protobuf_focused = protobuf.focused_preflight.script or ""
        protobuf_full = protobuf.build.script or ""
        assert (
            'build_dir="${TMPDIR:-/tmp}/refactoragent-protobuf-29.3-build"'
            in protobuf_focused
        )
        assert "--target protoc lite-test upb-test --parallel 1" in protobuf_focused
        assert "rm -rf" not in protobuf_full
        assert (
            'build_dir="${TMPDIR:-/tmp}/refactoragent-protobuf-29.3-build"'
            in protobuf_full
        )
        assert "--target protoc lite-test upb-test --parallel 1" in protobuf_full

        yaml_cpp = overrides["yaml-cpp"]
        yaml_focused = yaml_cpp.focused_preflight.script or ""
        yaml_full = yaml_cpp.build.script or ""
        assert "--target yaml-cpp" in yaml_focused
        assert "--target yaml-cpp yaml-cpp-tests" in yaml_full
        assert "rm -rf" not in yaml_full

        rrdtool = overrides["rrdtool"]
        rrdtool_focused = rrdtool.focused_preflight.script or ""
        assert 'make -j"${SMELL_BUILD_JOBS:-1}"' in rrdtool_focused
        assert "run_rrdtool_project_tests.py" in rrdtool_focused
        assert "--focused-libdbi-probe" in rrdtool_focused
        assert "make distclean" not in (rrdtool.build.script or "")

        tmux = overrides["tmux"]
        tmux_focused = tmux.focused_preflight.script or ""
        assert 'make -C "${build_dir}" -j"${SMELL_BUILD_JOBS:-1}"' in tmux_focused
        assert "run_tmux_project_tests.py" not in tmux_focused
        assert "input-keys.sh" not in tmux_focused
        assert "rm -rf" not in (tmux.build.script or "")

    print(
        "focused preflight self-check: PASS statuses=NOT_APPLICABLE/READY/FAILED "
        "acceptance=false project_full_executed=false tests_cached=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
