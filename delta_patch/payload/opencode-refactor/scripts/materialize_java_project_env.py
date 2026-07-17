#!/usr/bin/env python3
"""Materialize shared Java build environment from project command scripts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from java_verification_policy import normalize_verification_command


JAVA_HOME_PATTERN = re.compile(r'^export JAVA_HOME="([^"]+)"\s*$', re.MULTILINE)
COMMON_ENV = {
    "BUILDENV": "/opt/buildenv",
    "HOME": "/opt/buildenv/offline-home",
    "GRADLE_USER_HOME": "/opt/buildenv/offline-home/.gradle",
    "MAVEN_USER_HOME": "/opt/buildenv/offline-home/.m2",
    "TZ": "Asia/Shanghai",
}
BASE_PATH = "/opt/idea-refactoring/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Populate each Java project env from the JAVA_HOME already declared in its "
            "build/test scripts, so sample-level commands use the same JDK."
        )
    )
    parser.add_argument("projects_yaml")
    return parser.parse_args()


def declared_java_home(project: dict, source: Path) -> str:
    # Sample-optimized verification replaces the configured test command, so its
    # inherited environment must match the project's test phase. Build scripts
    # remain authoritative for (and may intentionally use) a different JDK.
    script = str((project.get("test") or {}).get("script") or "")
    values = set(JAVA_HOME_PATTERN.findall(script))
    root = str(project.get("root") or "<missing-root>")
    if not values:
        raise ValueError(f"{source}: {root} does not declare JAVA_HOME in its test script")
    if len(values) != 1:
        raise ValueError(f"{source}: {root} declares multiple test JAVA_HOME values: {sorted(values)}")
    return values.pop()


def main() -> int:
    path = Path(parse_args().projects_yaml).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects = data.get("projects") or []
    if not projects:
        raise ValueError(f"{path}: projects list is empty")

    for project in projects:
        java_home = declared_java_home(project, path)
        project_name = Path(str(project.get("root") or "")).name
        for phase in ("build", "test"):
            phase_config = project.get(phase) or {}
            if "script" in phase_config:
                phase_config["script"] = normalize_verification_command(
                    str(phase_config.get("script") or ""), project_name
                )
            if "command" in phase_config:
                phase_config["command"] = normalize_verification_command(
                    str(phase_config.get("command") or ""), project_name
                )
            project[phase] = phase_config
        env = dict(project.get("env") or {})
        expected = {
            **COMMON_ENV,
            "JAVA_HOME": java_home,
            "PATH": f"{java_home}/bin:{BASE_PATH}",
        }
        conflicts = {
            key: (env[key], value)
            for key, value in expected.items()
            if key in env and str(env[key]) != value
        }
        if conflicts:
            root = str(project.get("root") or "<missing-root>")
            raise ValueError(f"{path}: {root} has conflicting shared env: {conflicts}")
        env.update(expected)
        project["env"] = env

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    print(f"materialized Java env for {len(projects)} projects in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
