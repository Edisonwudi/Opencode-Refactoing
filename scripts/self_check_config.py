#!/usr/bin/env python3
"""Verify the runtime configuration exposes only values used by the runner."""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import (  # noqa: E402
    CommandConfig,
    DefaultsConfig,
    ProjectOverride,
    ProjectRootsConfig,
    RefactorConfig,
    ResolvedRunConfig,
    SmellProfile,
    load_refactor_config,
    load_project_overrides,
    resolve_run_config,
)
from smell_core.guards import _run_command_config  # noqa: E402


def main() -> int:
    defaults = DefaultsConfig.from_dict(
        {"shell_timeout": 37, "run_build": False, "run_tests": True}
    )
    assert asdict(defaults) == {
        "shell_timeout": 37,
        "run_build": False,
        "run_tests": True,
    }
    assert [item.name for item in fields(DefaultsConfig)] == [
        "shell_timeout",
        "run_build",
        "run_tests",
    ]
    assert [item.name for item in fields(RefactorConfig)] == ["defaults", "languages"]
    assert [item.name for item in fields(SmellProfile)] == ["guards", "retry_hint_template"]
    assert [item.name for item in fields(ProjectRootsConfig)] == ["dataset", "build"]
    resolved_fields = {item.name for item in fields(ResolvedRunConfig)}
    assert not {
        "llm",
        "idea_project_root",
        "idea_refactor_cli",
        "idea_refactor_ready",
    }.intersection(resolved_fields)

    with tempfile.TemporaryDirectory(prefix="config-self-check-") as raw_temp:
        temp = Path(raw_temp)
        with patch.dict(os.environ, {"MINI_SHELL_TIMEOUT": "321"}, clear=False):
            config = load_refactor_config(None)

        assert not hasattr(config, "llm")
        assert config.defaults.shell_timeout == 321

        project = temp / "project"
        project.mkdir()
        (project / "sample.py").write_text(
            "def target():\n    return 1\n",
            encoding="utf-8",
        )
        resolved = resolve_run_config(
            refactor_config=config,
            project_overrides=[],
            project_root=str(project),
            smell="long_method",
            location="sample.py:method=target|line=1",
            cli_language="python",
            verification_mode="project_full",
        )
        payload = resolved.to_dict()
        assert not {
            "llm",
            "idea_project_root",
            "idea_refactor_cli",
            "idea_refactor_ready",
        }.intersection(payload)
        assert set(payload["profile"]) == {"guards", "retry_hint_template"}
        assert payload["defaults"] == {
            "shell_timeout": 321,
            "run_build": True,
            "run_tests": True,
        }

        slow_project = ProjectOverride(
            root=project,
            language="python",
            shell_timeout=1500,
        )
        project_minimum = resolve_run_config(
            refactor_config=config,
            project_overrides=[slow_project],
            project_root=str(project),
            smell="long_method",
            location="sample.py:method=target|line=1",
            cli_language="python",
            verification_mode="project_full",
        )
        assert project_minimum.defaults.shell_timeout == 1500
        with patch.dict(
            os.environ,
            {"MINI_SHELL_TIMEOUT": "2000"},
            clear=False,
        ):
            raised_config = load_refactor_config(None)
        explicitly_raised = resolve_run_config(
            refactor_config=raised_config,
            project_overrides=[slow_project],
            project_root=str(project),
            smell="long_method",
            location="sample.py:method=target|line=1",
            cli_language="python",
            verification_mode="project_full",
        )
        assert explicitly_raised.defaults.shell_timeout == 2000

        started = time.monotonic()
        timeout_result = _run_command_config(
            CommandConfig(
                command=(
                    f"{shlex.quote(sys.executable)} -c "
                    "'import signal,sys,time; "
                    "signal.signal(signal.SIGTERM, lambda *_: "
                    "(print(\"terminated-tail\", flush=True), sys.exit(0))); "
                    "print(\"started\", flush=True); time.sleep(5)'"
                )
            ),
            cwd=project,
            env={},
            label="test",
            project_root=project,
            timeout_seconds=1,
        )
        assert timeout_result["success"] is False
        assert timeout_result["status"] == "timeout"
        assert timeout_result["returncode"] == 124
        assert "started" in timeout_result["output"]
        assert "terminated-tail" in timeout_result["output"]
        assert time.monotonic() - started < 3

        # A shell may exit immediately while a background child keeps the
        # captured stdout pipe open.  Timeout enforcement must still terminate
        # the original process group instead of waiting for that child.
        started = time.monotonic()
        background_timeout = _run_command_config(
            CommandConfig(
                command=(
                    f"{shlex.quote(sys.executable)} -c "
                    "'import time; time.sleep(5)' &"
                )
            ),
            cwd=project,
            env={},
            label="test",
            project_root=project,
            timeout_seconds=1,
        )
        assert background_timeout["status"] == "timeout", background_timeout
        assert time.monotonic() - started < 3

        missing_overlay = temp / "missing-project-test-overlay.yaml"
        with patch(
            "smell_core.config.bundled_projects_overlay_path",
            return_value=missing_overlay,
        ):
            try:
                load_project_overrides(None)
            except FileNotFoundError as exc:
                assert "Required runtime project-test overlay is missing" in str(exc)
            else:
                raise AssertionError("missing runtime project-test overlay must fail closed")

    print(
        "config self-check: PASS dead_fields=0 llm_fallback=0 "
        "shell_timeout=enforced project_test_overlay=required"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
