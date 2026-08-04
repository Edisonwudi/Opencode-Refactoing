"""Explicit evidence adapters for declared Java sample tests.

Native Maven and Gradle tasks keep their own JUnit XML.  JUnit4 command-line
suites use a report-producing JUnit runner with real counts.  A declared Java
``main`` test is different: its process boundary is part of the contract, so it
is executed unchanged in a child JVM and receives a structured attestation
only after that exact process succeeds.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import CommandConfig, interpolate_command_text
from .java_test_attestation_runner import ATTESTATION_ADAPTER_ID


JAVA_TEST_EVIDENCE_ADAPTER_ID = ATTESTATION_ADAPTER_ID
_JUNIT_ADAPTER_CLASS = "DeclaredJavaTestReportAdapter"
_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_$][\w.$]*)\s*;", re.MULTILINE)
_DIRECT_JAVA_RE = re.compile(
    r"(?P<java>(?<![\w.-])(?:[^\s;&|]*/)?java)\s+"
    r"(?P<classpath_flag>-cp|-classpath)\s+"
    r"(?P<classpath>\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s+"
    r"(?P<main>[A-Za-z_$][\w.$]*)"
    r"(?P<args>[^;&|\n]*)"
)


def _junit_adapter_source_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "java"
        / f"{_JUNIT_ADAPTER_CLASS}.java"
    )


def _main_runner_path() -> Path:
    return Path(__file__).resolve().with_name("java_test_attestation_runner.py")


def _source_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def declared_java_test_sources(config: Any) -> Tuple[Dict[str, Path], str]:
    """Resolve declared test classes to current source files without guessing."""
    project_root = Path(config.project_root).expanduser().resolve()
    dataset_root = Path(config.dataset_root).expanduser().resolve()
    locations = [
        item.strip()
        for item in str(getattr(config, "sample_test_location", "") or "").split(";")
        if item.strip()
    ]
    if not locations:
        return {}, "sample_test_location_missing"

    sources: Dict[str, Path] = {}
    for location in locations:
        raw = Path(location)
        candidates = [raw] if raw.is_absolute() else [dataset_root / raw, project_root / raw]
        source = next((path.resolve() for path in candidates if path.is_file()), None)
        if source is None:
            return {}, f"declared_test_source_missing:{location}"
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}, f"declared_test_source_unreadable:{location}"
        package = _PACKAGE_RE.search(text)
        if package is None:
            return {}, f"declared_test_package_missing:{location}"
        class_name = f"{package.group(1)}.{source.stem}"
        if class_name in sources:
            return {}, f"declared_test_class_duplicate:{class_name}"
        sources[class_name] = source
    return sources, ""


def _junit_reporting_invocation(
    match: re.Match[str],
    *,
    classes: List[str],
    project_root: Path,
) -> str:
    java = match.group("java")
    javac = f"{java[:-4]}javac"
    classpath = match.group("classpath")
    adapter_source = _junit_adapter_source_path()
    adapter_classes = project_root / ".smell-artifacts" / "test-evidence-adapter" / "classes"
    report_dir = project_root / ".smell-artifacts" / "test-reports"
    class_args = " ".join(shlex.quote(name) for name in classes)
    return (
        "( adapter_classes="
        f"{shlex.quote(str(adapter_classes))}; "
        'mkdir -p "$adapter_classes"; '
        f"{javac} -encoding UTF-8 -d \"$adapter_classes\" "
        f"{shlex.quote(str(adapter_source))}; "
        f"{java} {match.group('classpath_flag')} \"$adapter_classes\":{classpath} "
        f"{_JUNIT_ADAPTER_CLASS} --report-dir {shlex.quote(str(report_dir))} "
        f"--mode junit4 {class_args} )"
    )


def _main_attestation_invocation(
    match: re.Match[str],
    *,
    declared_class: str,
    source: Path,
    project_root: Path,
    contract_command_sha256: str,
) -> str:
    report_dir = project_root / ".smell-artifacts" / "test-attestations"
    original = match.group(0).strip()
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(_main_runner_path()))} "
        f"--report-dir {shlex.quote(str(report_dir))} "
        f"--declared-class {shlex.quote(declared_class)} "
        f"--source {shlex.quote(str(source))} "
        f"--contract-command-sha256 {shlex.quote(contract_command_sha256)} "
        f"-- {original}"
    )


def _adapt_direct_java_command(
    command: str,
    *,
    declared_sources: Dict[str, Path],
    project_root: Path,
    contract_command_sha256: str,
) -> Tuple[str, List[str], Dict[str, str], str]:
    covered: List[str] = []
    modes: List[str] = []
    evidence_kinds: Dict[str, str] = {}
    declared = set(declared_sources)

    def replace(match: re.Match[str]) -> str:
        main_class = match.group("main")
        try:
            args = shlex.split(match.group("args") or "")
        except ValueError:
            return match.group(0)

        if main_class == "org.junit.runner.JUnitCore":
            selected = list(args)
            if not selected or len(selected) != len(set(selected)):
                return match.group(0)
            if any(item not in declared for item in selected):
                return match.group(0)
            mode = "junit4"
            replacement = _junit_reporting_invocation(
                match,
                classes=selected,
                project_root=project_root,
            )
            kind = "junit4_xml"
        elif main_class in declared and not args:
            selected = [main_class]
            mode = "main"
            replacement = _main_attestation_invocation(
                match,
                declared_class=main_class,
                source=declared_sources[main_class],
                project_root=project_root,
                contract_command_sha256=contract_command_sha256,
            )
            kind = "declared_main_attestation"
        else:
            return match.group(0)

        covered.extend(selected)
        modes.append(mode)
        evidence_kinds.update({name: kind for name in selected})
        return replacement

    adapted = _DIRECT_JAVA_RE.sub(replace, command)
    if set(covered) != declared or len(covered) != len(declared_sources):
        missing = sorted(declared.difference(covered))
        reason = "direct_java_declared_class_mismatch"
        if missing:
            reason += ":" + ",".join(missing)
        return command, [], {}, reason
    return adapted, sorted(set(modes)), dict(sorted(evidence_kinds.items())), ""


def prepare_java_sample_test_command(
    config: Any,
) -> Tuple[CommandConfig, Dict[str, object]]:
    """Return the deterministic command and its evidence-adapter identity."""
    original = getattr(config, "sample_test", None) or CommandConfig()
    metadata: Dict[str, object] = {
        "adapter_id": JAVA_TEST_EVIDENCE_ADAPTER_ID,
        "selected": False,
        "reason": "native_xml_report_expected",
        "declared_test_classes": [],
        "execution_modes": [],
        "evidence_kinds": {},
        "implementation_sha256": {
            "junit4_reporter": _source_sha256(_junit_adapter_source_path()),
            "declared_main_runner": _source_sha256(_main_runner_path()),
        },
    }
    if str(getattr(config, "language", "") or "").strip().lower() != "java":
        metadata["reason"] = "non_java"
        return original, metadata

    declared_sources, error = declared_java_test_sources(config)
    metadata["declared_test_classes"] = list(declared_sources)
    if error:
        metadata["reason"] = error
        return original, metadata

    is_script = bool(str(getattr(original, "script", "") or "").strip())
    configured = str(
        getattr(original, "script" if is_script else "command", "") or ""
    )
    if not configured.strip():
        metadata["reason"] = "sample_test_command_missing"
        return original, metadata

    rendered = interpolate_command_text(configured, Path(config.project_root))
    command_sha256 = hashlib.sha256(
        str(getattr(config, "sample_test_command", configured) or configured)
        .strip()
        .encode("utf-8")
    ).hexdigest()
    adapted, modes, evidence_kinds, error = _adapt_direct_java_command(
        rendered,
        declared_sources=declared_sources,
        project_root=Path(config.project_root).expanduser().resolve(),
        contract_command_sha256=command_sha256,
    )
    if error:
        metadata["reason"] = error
        return original, metadata
    if adapted == rendered:
        return original, metadata

    metadata.update(
        {
            "selected": True,
            "reason": "",
            "execution_modes": modes,
            "evidence_kinds": evidence_kinds,
        }
    )
    if is_script:
        return CommandConfig(script=adapted), metadata
    return CommandConfig(command=adapted), metadata


def reset_java_sample_test_evidence(project_root: Path) -> None:
    """Remove only product-owned evidence before a fresh sample test run."""
    artifacts = Path(project_root).expanduser().resolve() / ".smell-artifacts"
    for name in ("test-attestations", "test-reports", "test-evidence-adapter"):
        shutil.rmtree(artifacts / name, ignore_errors=True)


def java_sample_test_evidence_contract(config: Any) -> Dict[str, object]:
    """Freeze adapter selection and implementation, not machine paths."""
    _command, metadata = prepare_java_sample_test_command(config)
    return metadata
