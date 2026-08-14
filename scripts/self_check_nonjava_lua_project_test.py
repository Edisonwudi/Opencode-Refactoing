#!/usr/bin/env python3
"""Verify the Lua project override runs a real suite and emits fresh evidence."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.config import load_project_overrides  # noqa: E402
from smell_core.guards import _project_test_execution_evidence  # noqa: E402


def _write_fake_lua(path: Path, *, duplicate_sentinel: bool = False) -> None:
    sentinel = "print('final OK !!!')\n"
    if duplicate_sentinel:
        sentinel += "print('final OK !!!')\n"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "assert sys.argv[1:3] == ['-e', '_U=true']\n"
        "assert pathlib.Path(sys.argv[3]).name == 'all.lua'\n"
        "print('Starting Tests')\n"
        + sentinel,
        encoding="utf-8",
    )
    path.chmod(0o755)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lua-project-test-") as raw_temp:
        temp = Path(raw_temp)
        projects = temp / "projects.yaml"
        projects.write_text(
            "projects:\n"
            "  - root: /opt/projects/c/lua\n"
            "    language: c\n"
            "    build:\n"
            "      command: make\n"
            "    test:\n"
            "      command: ./lua -e \\\"print('ok')\\\"\n"
            "  - root: /opt/projects/c/other\n"
            "    language: c\n"
            "    test:\n"
            "      command: make test\n",
            encoding="utf-8",
        )
        overrides = load_project_overrides(str(projects))
        assert len(overrides) == 2
        lua = next(item for item in overrides if item.root.as_posix().endswith("/lua"))
        other = next(item for item in overrides if item.root.as_posix().endswith("/other"))
        assert lua.build.command == "make"
        assert lua.test.command is None
        assert lua.test.script == (
            'python3 /agent-src/scripts/run_lua_project_tests.py '
            '--project-root "${project_root}"\n'
        )
        assert other.test.command == "make test"

        project = temp / "lua"
        (project / "testes").mkdir(parents=True)
        (project / "testes" / "all.lua").write_text("-- fixture\n", encoding="utf-8")
        _write_fake_lua(project / "lua")
        started_ns = time.time_ns()
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_lua_project_tests.py"),
                "--project-root",
                str(project),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout
        report = project / ".smell-test-reports" / "TEST-lua-all.xml"
        root = ET.parse(report).getroot()
        assert root.attrib["tests"] == "1"
        evidence = _project_test_execution_evidence(
            SimpleNamespace(project_root=project, language="c"),
            started_ns,
            {"output": completed.stdout},
        )
        assert evidence["success"] is True
        assert evidence["tests"] == 1
        assert evidence["reports"] == [".smell-test-reports/TEST-lua-all.xml"]

        _write_fake_lua(project / "lua", duplicate_sentinel=True)
        failed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_lua_project_tests.py"),
                "--project-root",
                str(project),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        assert failed.returncode != 0
        assert not report.exists()

    print("non-Java Lua project-test self-check: PASS suite=all.lua evidence=junit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
