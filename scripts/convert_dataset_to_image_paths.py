#!/usr/bin/env python3
"""Convert in-repo dataset CSVs to the in-image (container) path format.

Non-Java (``dataset/nonjava/<lang>/*.csv``):
  project_path / location  ``<local checkout root>/<name>`` -> ``/opt/projects/<lang>/<name>``
Java (``dataset/java/delivery_schema/*.csv``):
  ``<Java_Project>/.buildenv`` -> ``/opt/buildenv`` (test/focused test commands)
  ``<Java_Project>/<name>``    -> ``/opt/projects/<name>``  (project_path and
  any other column embedding an absolute project path)

The result mirrors the snapshot payload consumed by the container images, so
the CSVs work as-is when this repo is mounted at ``/agent-src``.  The local
checkout roots are the same ones the snapshot pipeline derives from
``project-revisions.<lang>.json``; validation scripts remap container paths
back to these roots when they need to read source files locally.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NONJAVA_ROOT = ROOT / "dataset" / "nonjava"
JAVA_ROOT = ROOT / "dataset" / "java" / "delivery_schema"

NONJAVA_LOCAL_ROOTS = {
    "c": Path("/Users/a1-6/Code/Project/C_Project"),
    "cpp": Path("/Users/a1-6/Code/Project/CPP_Project"),
    "python": Path("/Users/a1-6/Code/Project/Python_Project"),
}
JAVA_LOCAL_ROOT = "/Users/a1-6/Code/Project/Java_Project"
JAVA_PROJECT_RE = re.compile(re.escape(JAVA_LOCAL_ROOT) + r"/([^/\s\"';]+)")
# Provenance metadata columns: the image keeps these original-machine paths
# verbatim (they point at curation-time artifacts, not runtime locations).
JAVA_SKIP_COLUMNS = frozenset({"coverage_source", "projectfull_run_dir"})


def convert_nonjava() -> int:
    changed = 0
    for language, local_root in NONJAVA_LOCAL_ROOTS.items():
        for csv_path in sorted(NONJAVA_ROOT.joinpath(language).glob("*.csv")):
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                fieldnames = list(rows[0].keys()) if rows else []
            touched = False
            for row in rows:
                name = row["project_name"]
                container_root = f"/opt/projects/{language}/{name}"
                project_local_root = str(local_root / name)
                for column in ("project_path", "location"):
                    value = row.get(column) or ""
                    if project_local_root in value:
                        row[column] = value.replace(project_local_root, container_root)
                        touched = True
            if touched:
                with csv_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                changed += 1
                print(f"converted {csv_path.relative_to(ROOT)}")
    return changed


def convert_java() -> int:
    changed = 0
    for csv_path in sorted(JAVA_ROOT.glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            fieldnames = list(rows[0].keys()) if rows else []
        touched = False
        for row in rows:
            for column, value in list(row.items()):
                if column in JAVA_SKIP_COLUMNS:
                    continue
                if not value or JAVA_LOCAL_ROOT not in value:
                    continue
                value = value.replace(f"{JAVA_LOCAL_ROOT}/.buildenv", "/opt/buildenv")
                value = JAVA_PROJECT_RE.sub(r"/opt/projects/\1", value)
                if value != row[column]:
                    row[column] = value
                    touched = True
        if touched:
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            changed += 1
            print(f"converted {csv_path.relative_to(ROOT)}")
    return changed


def main() -> int:
    changed = convert_nonjava() + convert_java()
    print(f"done: {changed} CSV file(s) converted to container path format")
    return 0


if __name__ == "__main__":
    sys.exit(main())
