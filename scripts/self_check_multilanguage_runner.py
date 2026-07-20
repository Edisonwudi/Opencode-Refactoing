#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

import run_smell_dataset as runner  # noqa: E402
from smell_core.config import CommandConfig, _rebase_command_config  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="multilanguage-runner-") as raw:
        root = Path(raw)
        project = root / "project"
        project.mkdir()
        source = project / "demo.py"
        source.write_text("def f():\n    return 1\n", encoding="utf-8")
        dataset = root / "python.csv"
        with dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["sample_id", "language", "smell_type", "project_name", "project_path", "location"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "sample_id": "1",
                    "language": "python",
                    "smell_type": "long_method",
                    "project_name": "demo",
                    "project_path": str(project),
                    "location": f"{source}:method=f|line=1",
                }
            )

        sample = runner._load_samples(dataset)[0]
        prompt = runner._task_prompt(
            sample,
            argparse.Namespace(idea_refactor_cli=""),
            "local",
            "java-refactor-agent",
        )
        assert "Repair this one python smell" in prompt
        assert "Repair this one Java smell" not in prompt

        args = argparse.Namespace(agent="", idea=False, opencode_bin="opencode", model="test/model")
        assert runner._select_agent(sample, args) == "smell-refactor-agent"
        command = runner._opencode_run_command(args, "smell-refactor-agent")
        assert command[command.index("--command") + 1] == "smell-refactor-run"
        rebased = _rebase_command_config(
            CommandConfig(script=f'cd "{project}"\npython -m compileall demo.py'),
            project,
            root / "execution-worktree",
        )
        assert str(root / "execution-worktree") in str(rebased.script)
        assert str(project) not in str(rebased.script)

        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_smell_dataset.py"), "--dataset", str(dataset), "--idea", "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode == 2, proc
        assert "IDEA_UNSUPPORTED_LANGUAGE" in proc.stderr

        direct = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_smell_dataset.py"), "--dataset", str(dataset), "--no-idea", "--dry-run"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert direct.returncode == 0, direct.stderr
        assert "python" in direct.stdout

    print("Multilanguage runner self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
