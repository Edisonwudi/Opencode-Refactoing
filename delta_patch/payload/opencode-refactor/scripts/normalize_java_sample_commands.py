#!/usr/bin/env python3
"""Remove per-row JDK overrides so project configuration remains authoritative."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from java_verification_policy import normalize_verification_command


JAVA_HOME_EXPORT = re.compile(r'\s*export\s+JAVA_HOME=(?:"[^"]*"|\'[^\']*\'|[^;]*);?')
JAVA_PATH_EXPORT = re.compile(
    r'\s*export\s+PATH=(?:"\$JAVA_HOME/bin:\$PATH"|\'\$JAVA_HOME/bin:\$PATH\');?'
)
MAVEN_SETTINGS_ARG = re.compile(
    r"(?<!\S)-gs\s+(?P<quote>['\"]?)(?P<path>/[^\s'\"]*/maven-(?:offline|global)-settings\.xml)(?P=quote)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove JAVA_HOME/PATH overrides from Java dataset command columns."
    )
    parser.add_argument("dataset_root")
    parser.add_argument(
        "--project-root-base",
        default="",
        help="Rewrite project_path as <base>/<project_name> for container delivery.",
    )
    parser.add_argument(
        "--buildenv-root",
        default="",
        help="Rewrite absolute Maven -gs paths under this build environment root.",
    )
    return parser.parse_args()


def normalize(command: str, project_name: str = "", buildenv_root: Path | None = None) -> str:
    command = JAVA_HOME_EXPORT.sub("", command)
    command = JAVA_PATH_EXPORT.sub("", command)
    if buildenv_root is not None:
        command = MAVEN_SETTINGS_ARG.sub(
            lambda match: f"-gs {buildenv_root / Path(match.group('path')).name}", command
        )
    command = re.sub(r';\s*;', ";", command).strip(" ;")
    return normalize_verification_command(command, project_name)


def main() -> int:
    args = parse_args()
    root = Path(args.dataset_root).resolve()
    project_root_base = Path(args.project_root_base) if args.project_root_base else None
    buildenv_root = Path(args.buildenv_root) if args.buildenv_root else None
    csv_paths = sorted(root.glob("*.csv"))
    if not csv_paths:
        raise ValueError(f"No CSV files found under {root}")

    changed_rows = 0
    for csv_path in csv_paths:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        command_fields = [name for name in fieldnames if name.endswith("_command")]
        for row in rows:
            row_changed = False
            if project_root_base is not None and "project_path" in row:
                project_name = str(row.get("project_name") or "").strip()
                if not project_name:
                    raise ValueError(f"{csv_path.name}:{row.get('sample_id', '?')} has empty project_name")
                expected_path = str(project_root_base / project_name)
                if row.get("project_path") != expected_path:
                    row["project_path"] = expected_path
                    row_changed = True
            for field in command_fields:
                before = str(row.get(field) or "")
                after = normalize(before, str(row.get("project_name") or ""), buildenv_root)
                if "JAVA_HOME" in after:
                    raise ValueError(
                        f"{csv_path.name}:{row.get('sample_id', '?')} retains JAVA_HOME in {field}"
                    )
                if after != before:
                    row[field] = after
                    row_changed = True
            changed_rows += int(row_changed)
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"normalized Java sample commands in {len(csv_paths)} CSV files; changed_rows={changed_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
