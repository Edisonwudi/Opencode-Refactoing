#!/usr/bin/env python3
"""Fail-fast integrity audit for the packaged Java offline dependency closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


ARCHIVE_SUFFIXES = {".aar", ".ear", ".jar", ".war", ".zip"}
PACKAGE_SUFFIXES = ARCHIVE_SUFFIXES | {".module", ".pom"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--maven-repository",
        type=Path,
        default=Path("/opt/buildenv/offline-home/.m2/repository"),
    )
    parser.add_argument(
        "--gradle-home",
        type=Path,
        default=Path("/opt/buildenv/offline-home/.gradle"),
    )
    parser.add_argument("--projects-root", type=Path, default=Path("/opt/projects"))
    parser.add_argument("--jdk-root", type=Path, default=Path("/opt/buildenv/jdks"))
    parser.add_argument(
        "--maven-toolchains",
        type=Path,
        default=Path("/opt/buildenv/offline-home/.m2/toolchains.xml"),
    )
    parser.add_argument(
        "--maven-settings",
        type=Path,
        action="append",
        default=[
            Path("/opt/buildenv/maven-offline-settings.xml"),
            Path("/opt/buildenv/maven-global-settings.xml"),
            Path("/opt/buildenv/offline-home/.m2/settings.xml"),
        ],
    )
    parser.add_argument("--repository-id", default="local-all")
    parser.add_argument("--verify-archives", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _fingerprint(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    counts: Counter[str] = Counter()

    def error(category: str, path: Path | str, detail: str) -> None:
        errors.append({"category": category, "path": str(path), "detail": detail})

    def warning(category: str, path: Path | str, detail: str) -> None:
        warnings.append({"category": category, "path": str(path), "detail": detail})

    roots = [args.maven_repository, args.gradle_home, args.projects_root, args.jdk_root]
    for root in roots:
        if not root.is_dir():
            error("MISSING_ROOT", root, "required offline dependency root is absent")

    settings_bytes: list[bytes] = []
    for settings in args.maven_settings:
        if not settings.is_file():
            error("MAVEN_SETTINGS_MISSING", settings, "settings file is absent")
            continue
        payload = settings.read_bytes()
        settings_bytes.append(payload)
        try:
            document = ET.fromstring(payload)
        except ET.ParseError as exc:
            error("MAVEN_SETTINGS_INVALID_XML", settings, str(exc))
            continue
        mirrors = []
        for mirror in (node for node in document.iter() if _tag(node) == "mirror"):
            values = {_tag(child): (child.text or "").strip() for child in mirror}
            mirrors.append(values)
        if not any(
            item.get("id") == args.repository_id
            and item.get("url")
            == f"file://{args.maven_repository.resolve().as_posix()}"
            for item in mirrors
        ):
            error(
                "MAVEN_MIRROR_MISMATCH",
                settings,
                f"no {args.repository_id} mirror points at {args.maven_repository.resolve()}",
            )
    if settings_bytes and any(payload != settings_bytes[0] for payload in settings_bytes[1:]):
        error("MAVEN_SETTINGS_DRIFT", "maven-settings", "packaged settings files differ")

    toolchain_homes: list[Path] = []
    if not args.maven_toolchains.is_file():
        error(
            "MAVEN_TOOLCHAINS_MISSING",
            args.maven_toolchains,
            "active Maven toolchains file is absent",
        )
    else:
        try:
            toolchains = ET.parse(args.maven_toolchains)
        except (ET.ParseError, OSError) as exc:
            error("MAVEN_TOOLCHAINS_INVALID_XML", args.maven_toolchains, str(exc))
        else:
            for node in toolchains.iter():
                if _tag(node) != "jdkHome":
                    continue
                value = (node.text or "").strip()
                counts["maven_jdk_toolchains"] += 1
                if not value:
                    error(
                        "MAVEN_TOOLCHAIN_HOME_EMPTY",
                        args.maven_toolchains,
                        "jdkHome is empty",
                    )
                    continue
                home = Path(value)
                toolchain_homes.append(home)
                if not home.is_absolute():
                    error(
                        "MAVEN_TOOLCHAIN_HOME_NOT_ABSOLUTE",
                        args.maven_toolchains,
                        f"jdkHome={value}",
                    )
                    continue
                for executable in ("java", "javac"):
                    candidate = home / "bin" / executable
                    if not os.access(candidate, os.X_OK):
                        error(
                            "MAVEN_TOOLCHAIN_JDK_UNUSABLE",
                            candidate,
                            f"jdkHome={home}",
                        )

    marker_paths = sorted(args.maven_repository.rglob("_remote.repositories"))
    marker_payloads: list[Path] = []
    for marker in marker_paths:
        counts["maven_marker_files"] += 1
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            error("MAVEN_MARKER_UNREADABLE", marker, str(exc))
            continue
        marker_payloads.append(marker)
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ">" not in line or not line.endswith("="):
                error("MAVEN_MARKER_MALFORMED", marker, line)
                continue
            artifact, recorded = line.rsplit(">", 1)
            repository_id = recorded[:-1]
            counts["maven_marker_entries"] += 1
            if repository_id and repository_id != args.repository_id:
                error(
                    "MAVEN_FOREIGN_REPOSITORY_ID",
                    marker,
                    f"{artifact} records repository id {repository_id!r}",
                )
            target = marker.parent / artifact
            if not target.is_file():
                error("MAVEN_MARKER_TARGET_MISSING", target, f"referenced by {marker}")

    last_updated = sorted(args.maven_repository.rglob("*.lastUpdated"))
    for path in last_updated:
        error("MAVEN_LAST_UPDATED_PRESENT", path, "stale resolver failure marker")
    counts["maven_last_updated_files"] = len(last_updated)

    xml_paths = sorted(args.maven_repository.rglob("*.pom"))
    xml_paths.extend(sorted(args.maven_repository.rglob("maven-metadata*.xml")))
    for path in xml_paths:
        counts["maven_xml_files"] += 1
        try:
            ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            warning(
                "MAVEN_XML_NON_STANDARD",
                path,
                f"strict XML parser rejected cached metadata; real Maven plans remain authoritative: {exc}",
            )

    package_files: list[Path] = []
    archive_files: list[Path] = []
    scan_roots = [
        args.maven_repository,
        args.gradle_home / "caches" / "modules-2" / "files-2.1",
        args.gradle_home / "wrapper" / "dists",
    ]
    for root in scan_roots:
        if not root.exists():
            warning("CACHE_SUBTREE_ABSENT", root, "cache subtree is absent")
            continue
        for path in root.rglob("*"):
            if path.is_symlink() and not path.exists():
                error("BROKEN_SYMLINK", path, f"target={os.readlink(path)}")
                continue
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in PACKAGE_SUFFIXES:
                package_files.append(path)
                counts[f"package_{suffix[1:]}"] += 1
                if path.stat().st_size == 0:
                    error("ZERO_LENGTH_PACKAGE", path, "dependency file is empty")
            if suffix in ARCHIVE_SUFFIXES:
                archive_files.append(path)

    if args.verify_archives:
        for path in archive_files:
            counts["archives_checked"] += 1
            try:
                with zipfile.ZipFile(path) as archive:
                    corrupt_member = archive.testzip()
                if corrupt_member:
                    error("ARCHIVE_CRC_FAILED", path, corrupt_member)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                error("ARCHIVE_INVALID", path, str(exc))

    wrapper_properties = sorted(
        args.projects_root.rglob("gradle/wrapper/gradle-wrapper.properties")
    )
    for properties in wrapper_properties:
        counts["gradle_wrappers"] += 1
        wrapper_jar = properties.with_name("gradle-wrapper.jar")
        if not wrapper_jar.is_file() or wrapper_jar.stat().st_size == 0:
            error("GRADLE_WRAPPER_JAR_MISSING", wrapper_jar, f"required by {properties}")
        values = {}
        for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().replace("\\:", ":")
        distribution = Path(
            urllib.parse.urlparse(values.get("distributionUrl", "")).path
        ).name
        stem = re.sub(r"\.(zip|tar\.gz)$", "", distribution)
        extracted = re.sub(r"-(bin|all)$", "", stem)
        candidates = list(
            (args.gradle_home / "wrapper" / "dists").rglob(
                f"{extracted}/bin/gradle"
            )
        )
        if not candidates:
            candidates = list(
                (args.gradle_home / "wrapper" / "dists").rglob(distribution)
            )
        if not candidates:
            error(
                "GRADLE_DISTRIBUTION_MISSING",
                properties,
                f"cached distribution not found: {distribution}",
            )

    maven_wrapper_properties = sorted(
        args.projects_root.rglob(".mvn/wrapper/maven-wrapper.properties")
    )
    for properties in maven_wrapper_properties:
        counts["maven_wrappers"] += 1
        wrapper_jar = properties.with_name("maven-wrapper.jar")
        wrapper_scripts = [properties.parents[2] / "mvnw"]
        if not wrapper_jar.is_file() and not any(path.is_file() for path in wrapper_scripts):
            error(
                "MAVEN_WRAPPER_BOOTSTRAP_MISSING",
                properties,
                "neither maven-wrapper.jar nor mvnw bootstrap is present",
            )

    java_homes: list[Path] = []
    for java in sorted(args.jdk_root.rglob("bin/java")):
        home = java.parent.parent
        if any(home == existing or home in existing.parents for existing in java_homes):
            continue
        java_homes.append(home)
        javac = home / "bin" / "javac"
        if not os.access(java, os.X_OK):
            error("JAVA_NOT_EXECUTABLE", java, "java is not executable")
        if not os.access(javac, os.X_OK):
            if home.name == "jre" and os.access(home.parent / "bin" / "javac", os.X_OK):
                counts["nested_jre_homes"] += 1
                continue
            error("JAVAC_MISSING", javac, f"JDK home={home}")
            continue
        completed = subprocess.run(
            [str(java), "-version"], text=True, capture_output=True, check=False
        )
        if completed.returncode != 0:
            error("JAVA_VERSION_FAILED", java, completed.stderr[-1000:])
        counts["jdk_homes"] += 1

    gradle_modules = args.gradle_home / "caches" / "modules-2" / "files-2.1"
    gradle_metadata = sorted((args.gradle_home / "caches").glob("modules-2/metadata-*"))
    if not gradle_modules.is_dir():
        error("GRADLE_MODULE_CACHE_MISSING", gradle_modules, "files-2.1 is absent")
    if not gradle_metadata:
        error(
            "GRADLE_METADATA_CACHE_MISSING",
            args.gradle_home / "caches" / "modules-2",
            "no metadata-* cache exists",
        )
    counts["gradle_metadata_roots"] = len(gradle_metadata)

    report: dict[str, Any] = {
        "schema_version": 1,
        "success": not errors,
        "repository_id": args.repository_id,
        "counts": dict(sorted(counts.items())),
        "fingerprints": {
            "maven_resolver_metadata_sha256": _fingerprint(
                marker_payloads + last_updated, args.maven_repository
            ),
            "maven_settings_sha256": (
                hashlib.sha256(settings_bytes[0]).hexdigest() if settings_bytes else ""
            ),
            "maven_toolchains_sha256": (
                hashlib.sha256(args.maven_toolchains.read_bytes()).hexdigest()
                if args.maven_toolchains.is_file()
                else ""
            ),
        },
        "errors": errors,
        "warnings": warnings,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"offline-integrity success={report['success']} "
        f"errors={len(errors)} warnings={len(warnings)} "
        f"counts={json.dumps(report['counts'], sort_keys=True)} "
        f"report={args.report}"
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
