#!/usr/bin/env python3
"""Normalize and validate Maven Resolver metadata for an offline file repository.

Maven records the repository id that supplied each artifact in
``_remote.repositories``.  A delivery image can contain every required JAR and
still fail offline when those marker files name ``central`` while the bundled
settings expose the same directory as ``local-all``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_REPOSITORY = Path("/opt/buildenv/offline-home/.m2/repository")
DEFAULT_REPOSITORY_ID = "local-all"


@dataclass
class ScanResult:
    marker_files: int = 0
    marker_entries: int = 0
    invalid_entries: int = 0
    changed_files: int = 0
    last_updated_files: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize or validate Maven _remote.repositories metadata."
    )
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--repository-id", default=DEFAULT_REPOSITORY_ID)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate without writing; exit nonzero on foreign repository ids or .lastUpdated files.",
    )
    return parser.parse_args()


def normalize_marker(text: str, repository_id: str) -> tuple[str, int, int]:
    output: list[str] = []
    entries = 0
    invalid = 0
    for raw_line in text.splitlines():
        line = raw_line
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ">" in line and line.endswith("="):
            artifact, recorded_id = line.rsplit(">", 1)
            recorded_id = recorded_id[:-1]
            entries += 1
            # An empty id ("artifact>=") denotes an artifact installed directly
            # into the local repository and is already valid offline.
            if recorded_id and recorded_id != repository_id:
                invalid += 1
                line = f"{artifact}>{repository_id}="
        output.append(line)
    suffix = "\n" if text.endswith("\n") or output else ""
    return "\n".join(output) + suffix, entries, invalid


def scan_repository(repository: Path, repository_id: str, *, check: bool) -> ScanResult:
    if not repository.is_dir():
        raise FileNotFoundError(f"Maven repository not found: {repository}")
    if not repository_id or any(char.isspace() for char in repository_id):
        raise ValueError(f"Invalid Maven repository id: {repository_id!r}")

    result = ScanResult()
    for marker in sorted(repository.rglob("_remote.repositories")):
        result.marker_files += 1
        original = marker.read_text(encoding="utf-8")
        normalized, entries, invalid = normalize_marker(original, repository_id)
        result.marker_entries += entries
        result.invalid_entries += invalid
        if invalid and not check:
            marker.write_text(normalized, encoding="utf-8")
            result.changed_files += 1

    stale_files = sorted(repository.rglob("*.lastUpdated"))
    result.last_updated_files = len(stale_files)
    if not check:
        for stale in stale_files:
            stale.unlink()
    return result


def metadata_fingerprint(repository: Path, settings_files: tuple[Path, ...] = ()) -> str:
    """Hash resolver metadata and Maven settings that must stay immutable during a batch."""
    digest = hashlib.sha256()
    paths = sorted(repository.rglob("_remote.repositories"))
    paths.extend(sorted(repository.rglob("*.lastUpdated")))
    paths.extend(sorted(path for path in settings_files if path.is_file()))
    for path in paths:
        try:
            relative = path.relative_to(repository)
            name = f"repository/{relative.as_posix()}"
        except ValueError:
            name = f"settings/{path.resolve()}"
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    try:
        result = scan_repository(
            args.repository.resolve(),
            args.repository_id,
            check=args.check,
        )
    except (OSError, ValueError) as exc:
        print(f"maven-offline-repo error={exc}", file=sys.stderr)
        return 2

    mode = "check" if args.check else "normalize"
    print(
        f"maven-offline-repo mode={mode} repository={args.repository} "
        f"repository_id={args.repository_id} marker_files={result.marker_files} "
        f"marker_entries={result.marker_entries} invalid_entries={result.invalid_entries} "
        f"changed_files={result.changed_files} last_updated_files={result.last_updated_files}"
    )
    if args.check and (result.invalid_entries or result.last_updated_files):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
