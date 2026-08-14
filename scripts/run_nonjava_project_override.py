#!/usr/bin/env python3
"""Execute one exact-root project override without invoking a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import (  # noqa: E402
    load_project_overrides,
)
from smell_core.guards import (  # noqa: E402
    _project_test_execution_evidence,
    _run_command_config,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--projects", required=True)
    parser.add_argument("--language", required=True, choices=("python", "c", "cpp"))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    project = Path(args.project_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    actual_commit = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if actual_commit != args.expected_commit:
        raise SystemExit(
            f"project revision mismatch: expected {args.expected_commit}, got {actual_commit}"
        )

    configured = load_project_overrides(args.projects)
    matches = [
        item for item in configured if item.root.expanduser().resolve() == project
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one effective exact project config, got {len(matches)}")
    override = matches[0]
    timeout = int(override.shell_timeout or 600)
    started = time.monotonic()
    build = _run_command_config(
        override.build,
        cwd=project,
        env=override.env,
        label="build",
        project_root=project,
        source="runtime-overlay",
        timeout_seconds=timeout,
    )
    (output_dir / "build.log").write_text(
        str(build.get("output") or ""), encoding="utf-8"
    )

    test = None
    evidence = None
    test_started_ns = None
    if build.get("success") is True:
        test_started_ns = time.time_ns()
        test = _run_command_config(
            override.test,
            cwd=project,
            env=override.env,
            label="test",
            project_root=project,
            source="runtime-overlay",
            force_fresh_test_execution=True,
            timeout_seconds=timeout,
        )
        (output_dir / "test.log").write_text(
            str(test.get("output") or ""), encoding="utf-8"
        )
        if test.get("success") is True:
            evidence = _project_test_execution_evidence(
                SimpleNamespace(project_root=project, language=args.language),
                test_started_ns,
                test,
            )

    success = bool(
        build.get("success") is True
        and isinstance(test, dict)
        and test.get("success") is True
        and isinstance(evidence, dict)
        and evidence.get("success") is True
    )
    result = {
        "schema_version": 1,
        "success": success,
        "project_root": str(project),
        "project_commit": actual_commit,
        "language": args.language,
        "projects_config": str(Path(args.projects).resolve()),
        "shell_timeout": timeout,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "build": {
            key: value
            for key, value in build.items()
            if key not in {"output", "tail"}
        },
        "test": (
            {key: value for key, value in test.items() if key not in {"output", "tail"}}
            if isinstance(test, dict)
            else None
        ),
        "execution_evidence": evidence,
    }
    result_path = output_dir / "result.json"
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result["artifacts"] = {
        name: _sha256(output_dir / name)
        for name in ("build.log", "test.log", "result.json")
        if (output_dir / name).is_file()
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"success": success, "evidence": evidence}, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
