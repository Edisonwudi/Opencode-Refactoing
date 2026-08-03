#!/usr/bin/env python3
"""Self-check the read-only Java Target Guard baseline audit."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit_java_target_guard_baselines.py"


def _long_method(statements: int) -> str:
    body = "\n".join(f"    total += {index};" for index in range(statements))
    return (
        "class Fixture {\n"
        "  int target() {\n"
        "    int total = 0;\n"
        f"{body}\n"
        "    return total;\n"
        "  }\n"
        "}\n"
    )


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="target-guard-baseline-audit-") as raw:
        temp = Path(raw)
        projects_root = temp / "projects"
        project = projects_root / "fixture-project"
        project.mkdir(parents=True)
        (project / "Fixture.java").write_text(_long_method(65), encoding="utf-8")
        projects_yaml = temp / "projects.yaml"
        projects_yaml.write_text(
            "projects:\n"
            f"- root: {json.dumps(str(project))}\n"
            "  language: java\n"
            "  build:\n"
            "    command: \"true\"\n"
            "  test:\n"
            "    command: \"true\"\n",
            encoding="utf-8",
        )
        for args in (["git", "init", "-q"], ["git", "add", "."]):
            result = _run(list(args), project)
            assert result.returncode == 0, result.stderr
        committed = _run(
            [
                "git",
                "-c",
                "user.name=target-guard-audit-self-check",
                "-c",
                "user.email=target-guard-audit@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            project,
        )
        assert committed.returncode == 0, committed.stderr
        commit = _run(["git", "rev-parse", "HEAD"], project).stdout.strip()
        tree = _run(["git", "rev-parse", "HEAD^{tree}"], project).stdout.strip()
        revisions = temp / "project-revisions.json"
        revisions.write_text(
            json.dumps(
                {
                    "projects": {
                        "fixture-project": {
                            "project_commit": commit,
                            "tree_hash": tree,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        dataset = temp / "dataset"
        dataset.mkdir()
        with (dataset / "long_method.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "sample_id",
                    "language",
                    "smell_type",
                    "project_name",
                    "project_path",
                    "location",
                    "verification_mode",
                    "target_context_json",
                ),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "sample_id": "1",
                    "language": "java",
                    "smell_type": "long_method",
                    "project_name": "fixture-project",
                    "project_path": str(project),
                    "location": "Fixture.java:method=target|line=2",
                    "verification_mode": "project_full",
                    "target_context_json": "{}",
                }
            )
        output = temp / "audit"
        result = _run(
            [
                sys.executable,
                str(AUDIT),
                "--dataset-root",
                str(dataset),
                "--projects-root",
                str(projects_root),
                "--projects",
                str(projects_yaml),
                "--project-revisions",
                str(revisions),
                "--output",
                str(output),
                "--jobs",
                "1",
            ],
            ROOT,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        payload = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        assert payload["summary"]["rows"] == 1, payload
        assert payload["summary"]["baseline_captured"] == 1, payload
        assert payload["rows"][0]["status"] == "BASELINE_CAPTURED", payload
        assert payload["rows"][0]["target_match_count"] == 1, payload
        assert payload["contract"]["model_calls"] == 0, payload
        assert payload["contract"]["full_project_detector"] == "forbidden", payload
        assert output.with_suffix(".md").is_file()

    print(
        "java target-guard baseline audit self-check PASS "
        "model_calls=0 checkpoint_writes=0 full_detector=forbidden"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
