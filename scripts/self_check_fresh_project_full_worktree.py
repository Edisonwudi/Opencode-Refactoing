#!/usr/bin/env python3
"""Adversarial checks for isolated project_full verification worktrees."""
from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNTIME = ROOT / "runtime" / "python"
if str(PYTHON_RUNTIME) not in sys.path:
    sys.path.insert(0, str(PYTHON_RUNTIME))

from bridge import smell_bridge
from smell_core.config import (
    CommandConfig,
    DefaultsConfig,
    ResolvedRunConfig,
    SmellProfile,
)


def _run(root: Path, *args: str) -> str:
    return subprocess.run(
        list(args),
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _status(root: Path) -> str:
    return _run(
        root,
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )


def _config(root: Path) -> ResolvedRunConfig:
    original_marker = root.resolve() / "build-left-this.marker"
    return ResolvedRunConfig(
        project_root=root,
        dataset_root=root,
        idea_project_root=root,
        build_root=root,
        smell="nested_complexity",
        language="c",
        locations=[],
        defaults=DefaultsConfig(
            shell_timeout=30,
            run_build=True,
            run_tests=True,
        ),
        build=CommandConfig(
            command=(
                "test -f src/untracked_api.h && "
                "grep -q 'candidate build metadata' Makefile && "
                "grep -q 'candidate test source' tests/declared_test.c && "
                f"test ! -e {shlex.quote(str(original_marker))} && "
                f": > {shlex.quote(str(original_marker))}"
            )
        ),
        test=CommandConfig(
            command=(
                "test ! -e tests/cached/list1-rrdcached.sock && "
                "mkdir -p tests/cached tests/cache && "
                ": > tests/cached/list1-rrdcached.sock && "
                ": > tests/cache/state && "
                "printf '%s\\n' '<testsuite tests=\"1\" failures=\"0\" "
                "errors=\"0\"><testcase name=\"fresh\"/></testsuite>' "
                "> TEST-fresh.xml"
            )
        ),
        sample_test=CommandConfig(),
        env={},
        cwd=root,
        profile=SmellProfile(instruction="", guards=[]),
        verification_mode="project_full",
        build_source="fixture",
        test_source="fixture",
    )


def _verify_args(artifact_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        skip_build_test=False,
        run_build_test=True,
        snapshot=True,
        baseline_seal="",
        smell_evidence="",
        artifact_root=str(artifact_root),
        output_detail="decision",
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fresh-project-full-check-") as raw:
        fixture = Path(raw)
        root = fixture / "agent-worktree"
        root.mkdir()
        _run(root, "git", "init", "-q")
        _run(root, "git", "config", "user.email", "guard@example.invalid")
        _run(root, "git", "config", "user.name", "Guard Check")
        _write(root, "src/target.c", "int target(void) { return 1; }\n")
        _write(root, "tests/declared_test.c", "baseline test source\n")
        _write(root, "Makefile", "# baseline build metadata\n")
        _run(root, "git", "add", ".")
        _run(root, "git", "commit", "-qm", "baseline")
        baseline_commit = _run(root, "git", "rev-parse", "HEAD").strip()

        _write(root, "src/target.c", "int target(void) { return 2; }\n")
        _write(root, "src/untracked_api.h", "int target(void);\n")
        _write(root, "tests/declared_test.c", "candidate test source\n")
        _write(root, "Makefile", "# candidate build metadata\n")
        before = _status(root)
        worktrees_before = _run(root, "git", "worktree", "list", "--porcelain")
        config = _config(root)

        original_resolve = smell_bridge._resolve
        original_checkpoint_context = smell_bridge._checkpoint_context
        original_run_smell_guards = smell_bridge.run_smell_guards
        original_run_build_test_guard = smell_bridge.run_build_test_guard
        isolated_envs: list[dict[str, object]] = []

        def _recording_build_test_guard(resolved, **kwargs):
            tmpdir = Path(resolved.env["TMPDIR"])
            tmux_tmpdir = Path(resolved.env["TMUX_TMPDIR"])
            isolated_envs.append({
                "TMPDIR": str(tmpdir),
                "TMUX_TMPDIR": str(tmux_tmpdir),
                "tmpdir_exists": tmpdir.is_dir(),
                "tmux_tmpdir_exists": tmux_tmpdir.is_dir(),
            })
            return original_run_build_test_guard(resolved, **kwargs)

        try:
            smell_bridge._resolve = lambda _args: config
            smell_bridge._checkpoint_context = lambda *_args: (
                None,
                {
                    "baseline_project_commit": baseline_commit,
                    "test_changes": {
                        "allow_test_changes": True,
                        "success": True,
                    },
                },
            )
            smell_bridge.run_smell_guards = lambda *_args, **_kwargs: []
            smell_bridge.run_build_test_guard = _recording_build_test_guard
            first = smell_bridge.cmd_verify(
                _verify_args(fixture / "artifacts-first")
            )
            after_first = _status(root)
            second = smell_bridge.cmd_verify(
                _verify_args(fixture / "artifacts-second")
            )
            after_second = _status(root)
        finally:
            smell_bridge._resolve = original_resolve
            smell_bridge._checkpoint_context = original_checkpoint_context
            smell_bridge.run_smell_guards = original_run_smell_guards
            smell_bridge.run_build_test_guard = original_run_build_test_guard

        assert first["status"] == "PASS", first
        assert second["status"] == "PASS", second
        assert before == after_first == after_second, (
            before,
            after_first,
            after_second,
        )
        assert not (root / "tests/cached/list1-rrdcached.sock").exists()
        assert not (root / "tests/cache/state").exists()
        assert not (root / "build-left-this.marker").exists()
        first_cwd = first["build_test_guard"]["details"]["test"]["cwd"]
        second_cwd = second["build_test_guard"]["details"]["test"]["cwd"]
        first_build_cwd = first["build_test_guard"]["details"]["build"]["cwd"]
        second_build_cwd = second["build_test_guard"]["details"]["build"]["cwd"]
        assert first_build_cwd == first_cwd, (first_build_cwd, first_cwd)
        assert second_build_cwd == second_cwd, (second_build_cwd, second_cwd)
        assert first_cwd != str(root), first_cwd
        assert second_cwd != str(root), second_cwd
        assert first_cwd != second_cwd, (first_cwd, second_cwd)
        assert not Path(first_cwd).exists(), first_cwd
        assert not Path(second_cwd).exists(), second_cwd
        assert Path(first["artifacts"]["diff"]).read_bytes() == Path(
            second["artifacts"]["diff"]
        ).read_bytes()
        assert len(isolated_envs) == 2, isolated_envs
        assert all(item["tmpdir_exists"] for item in isolated_envs), isolated_envs
        assert all(
            item["tmux_tmpdir_exists"] for item in isolated_envs
        ), isolated_envs
        isolated_cwds = (first_cwd, second_cwd)
        for key in ("TMPDIR", "TMUX_TMPDIR"):
            values = [str(item[key]) for item in isolated_envs]
            assert len(set(values)) == 2, (key, values)
            for value, isolated_cwd in zip(values, isolated_cwds):
                try:
                    Path(value).resolve().relative_to(root.resolve())
                except ValueError:
                    pass
                else:
                    raise AssertionError((key, value, root))
                try:
                    Path(value).resolve().relative_to(
                        Path(isolated_cwd).resolve()
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError((key, value, isolated_cwd))
                assert not Path(value).exists(), value
        assert _run(root, "git", "worktree", "list", "--porcelain") == (
            worktrees_before
        )

        snapshot = smell_bridge._snapshot_project(
            root,
            base_commit=baseline_commit,
        )
        assert "src/untracked_api.h" in snapshot["diff"]["stdout"], snapshot
        assert "tests/declared_test.c" in snapshot["diff"]["stdout"], snapshot
        assert "Makefile" in snapshot["diff"]["stdout"], snapshot

        unresolved = dict(snapshot)
        unresolved["base_commit"] = "missing-frozen-base"
        unresolved_result = smell_bridge._run_project_full_in_fresh_worktree(
            config,
            unresolved,
        )
        assert unresolved_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            unresolved_result
        )
        assert unresolved_result["verification_isolation"]["stage"] == (
            "resolve_base_commit"
        ), unresolved_result

        malformed = dict(snapshot)
        malformed["diff"] = {
            "returncode": 0,
            "stdout": "this is not a Git patch\n",
            "stderr": "",
        }
        malformed_result = smell_bridge._run_project_full_in_fresh_worktree(
            config,
            malformed,
        )
        assert malformed_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            malformed_result
        )
        assert malformed_result["verification_isolation"]["stage"] == (
            "apply_snapshot"
        ), malformed_result

        original_snapshot_project = smell_bridge._snapshot_project
        original_resolve = smell_bridge._resolve
        original_checkpoint_context = smell_bridge._checkpoint_context
        original_run_smell_guards = smell_bridge.run_smell_guards
        original_run_build_test_guard = smell_bridge.run_build_test_guard
        unexpected_guard_calls: list[bool] = []

        def _unexpected_guard(*_args, **_kwargs):
            unexpected_guard_calls.append(True)
            raise AssertionError("malformed snapshot must not run build/test")

        try:
            smell_bridge._snapshot_project = lambda *_args, **_kwargs: malformed
            smell_bridge._resolve = lambda _args: config
            smell_bridge._checkpoint_context = lambda *_args: (
                None,
                {
                    "baseline_project_commit": baseline_commit,
                    "test_changes": {
                        "allow_test_changes": True,
                        "success": True,
                    },
                },
            )
            smell_bridge.run_smell_guards = lambda *_args, **_kwargs: []
            smell_bridge.run_build_test_guard = _unexpected_guard
            malformed_verify = smell_bridge.cmd_verify(
                SimpleNamespace(
                    **{
                        **vars(_verify_args(fixture / "artifacts-malformed")),
                        "snapshot": False,
                    }
                )
            )
        finally:
            smell_bridge._snapshot_project = original_snapshot_project
            smell_bridge._resolve = original_resolve
            smell_bridge._checkpoint_context = original_checkpoint_context
            smell_bridge.run_smell_guards = original_run_smell_guards
            smell_bridge.run_build_test_guard = original_run_build_test_guard
        assert unexpected_guard_calls == [], unexpected_guard_calls
        assert malformed_verify["accepted"] is False, malformed_verify
        assert malformed_verify["status"] == "FINAL_VERIFY_INFRA_FAILED", (
            malformed_verify
        )
        assert malformed_verify["build_test_guard"]["verification_isolation"][
            "stage"
        ] == "apply_snapshot", malformed_verify
        assert malformed_verify["failure_pack"]["failure_category"] == (
            "FINAL_VERIFY_INFRA_FAILED"
        ), malformed_verify["failure_pack"]
        assert malformed_verify["failure_pack"]["retryable"] is False, (
            malformed_verify["failure_pack"]
        )
        assert malformed_verify["failure_pack"]["repair_contract"][
            "repair_agent_may_edit"
        ] is False, malformed_verify["failure_pack"]
        assert _status(root) == before, (_status(root), before)

        outside_config = replace(config, cwd=fixture / "outside-project")
        original_run_build_test_guard = smell_bridge.run_build_test_guard
        fallback_calls: list[bool] = []

        def _unexpected_fallback(*_args, **_kwargs):
            fallback_calls.append(True)
            raise AssertionError("invalid rebase must not run or fallback")

        try:
            smell_bridge.run_build_test_guard = _unexpected_fallback
            outside_result = smell_bridge._run_project_full_in_fresh_worktree(
                outside_config,
                snapshot,
            )
        finally:
            smell_bridge.run_build_test_guard = original_run_build_test_guard
        assert fallback_calls == [], fallback_calls
        assert outside_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            outside_result
        )
        assert outside_result["verification_isolation"]["stage"] == (
            "resolve_verification_root"
        ), outside_result
        assert _run(root, "git", "worktree", "list", "--porcelain") == (
            worktrees_before
        )

        try:
            smell_bridge.run_build_test_guard = _unexpected_fallback
            raised_result = smell_bridge._run_project_full_in_fresh_worktree(
                config,
                snapshot,
            )
        finally:
            smell_bridge.run_build_test_guard = original_run_build_test_guard
        assert fallback_calls == [True], fallback_calls
        assert raised_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", raised_result
        assert raised_result["verification_isolation"]["stage"] == (
            "run_build_test_guard"
        ), raised_result
        assert raised_result["verification_isolation"]["cleanup_success"] is True
        assert _run(root, "git", "worktree", "list", "--porcelain") == (
            worktrees_before
        )

        original_run_git = smell_bridge._run_git

        def _report_cleanup_failure(args: list[str], cwd: Path):
            if args[:3] == ["worktree", "remove", "--force"]:
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "fixture cleanup report failure",
                }
            return original_run_git(args, cwd)

        try:
            smell_bridge._run_git = _report_cleanup_failure
            cleanup_result = smell_bridge._run_project_full_in_fresh_worktree(
                config,
                snapshot,
            )
        finally:
            smell_bridge._run_git = original_run_git
        assert cleanup_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            cleanup_result
        )
        assert cleanup_result["verification_isolation"]["stage"] == (
            "cleanup_worktree"
        ), cleanup_result
        assert cleanup_result["verification_isolation"]["cleanup_success"] is False
        assert _run(root, "git", "worktree", "list", "--porcelain") == (
            worktrees_before
        )

        original_run_git = smell_bridge._run_git

        def _fail_worktree_add(args: list[str], cwd: Path):
            if args[:3] == ["worktree", "add", "--detach"]:
                return {
                    "returncode": 128,
                    "stdout": "",
                    "stderr": "fixture worktree creation failure",
                }
            return original_run_git(args, cwd)

        try:
            smell_bridge._run_git = _fail_worktree_add
            create_result = smell_bridge._run_project_full_in_fresh_worktree(
                config,
                snapshot,
            )
        finally:
            smell_bridge._run_git = original_run_git
        assert create_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            create_result
        )
        assert create_result["verification_isolation"]["stage"] == (
            "create_worktree"
        ), create_result
        assert create_result["verification_isolation"]["cleanup_success"] is True
        assert _status(root) == before, (_status(root), before)

        original_run_git = smell_bridge._run_git

        def _partially_register_then_fail(args: list[str], cwd: Path):
            result = original_run_git(args, cwd)
            if args[:3] == ["worktree", "add", "--detach"]:
                assert result["returncode"] == 0, result
                return {
                    **result,
                    "returncode": 128,
                    "stderr": "fixture failure after worktree registration",
                }
            return result

        try:
            smell_bridge._run_git = _partially_register_then_fail
            partial_create_result = (
                smell_bridge._run_project_full_in_fresh_worktree(
                    config,
                    snapshot,
                )
            )
        finally:
            smell_bridge._run_git = original_run_git
        assert partial_create_result["reason"] == "FINAL_VERIFY_INFRA_FAILED", (
            partial_create_result
        )
        assert partial_create_result["verification_isolation"]["stage"] == (
            "create_worktree"
        ), partial_create_result
        assert partial_create_result["verification_isolation"][
            "cleanup_success"
        ] is True, partial_create_result
        assert _run(root, "git", "worktree", "list", "--porcelain") == (
            worktrees_before
        )

        java_config = replace(config, language="java")
        original_snapshot_project = smell_bridge._snapshot_project
        original_resolve = smell_bridge._resolve
        original_checkpoint_context = smell_bridge._checkpoint_context
        original_run_smell_guards = smell_bridge.run_smell_guards
        original_run_build_test_guard = smell_bridge.run_build_test_guard
        java_roots: list[Path] = []

        def _java_direct_guard(resolved, **_kwargs):
            java_roots.append(Path(resolved.project_root))
            return {
                "type": "build_test",
                "success": True,
                "message": "fixture Java direct verification",
                "verification_mode": "project_full",
                "build_source": "fixture",
                "test_source": "fixture",
                "sample_test_source": "",
                "test_location": "",
                "test_command_hash": "",
                "details": {
                    "build": {"success": True, "status": "ok"},
                    "test": {"success": True, "status": "ok"},
                    "sample_test": None,
                },
            }

        try:
            smell_bridge._snapshot_project = lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(AssertionError("Java must not force a fresh snapshot"))
            smell_bridge._resolve = lambda _args: java_config
            smell_bridge._checkpoint_context = lambda *_args: (
                None,
                {
                    "baseline_project_commit": baseline_commit,
                    "test_changes": {
                        "allow_test_changes": True,
                        "success": True,
                    },
                },
            )
            smell_bridge.run_smell_guards = lambda *_args, **_kwargs: []
            smell_bridge.run_build_test_guard = _java_direct_guard
            java_verify = smell_bridge.cmd_verify(
                SimpleNamespace(
                    **{
                        **vars(_verify_args(fixture / "artifacts-java")),
                        "snapshot": False,
                    }
                )
            )
        finally:
            smell_bridge._snapshot_project = original_snapshot_project
            smell_bridge._resolve = original_resolve
            smell_bridge._checkpoint_context = original_checkpoint_context
            smell_bridge.run_smell_guards = original_run_smell_guards
            smell_bridge.run_build_test_guard = original_run_build_test_guard
        assert java_verify["status"] == "PASS", java_verify
        assert java_roots == [root], java_roots
        assert java_verify["build_test_guard"]["verification_isolation"] is None

    print(
        "fresh project_full worktree self-check passed: consecutive isolated "
        "verification; legal untracked source replay; resolve/create/apply "
        "fail closed; agent worktree unchanged"
    )


if __name__ == "__main__":
    main()
