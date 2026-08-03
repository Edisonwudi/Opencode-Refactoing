"""Static Java source-layout contract shared by detectors and verification.

The layout is derived only from checked-in build descriptors.  It never runs a
build tool and never consults dataset evidence.  A test input can be described
as a directory root, one exact file, or a Bazel glob; callers use the same
matcher for target admission, product scanning, production diffs, and c000 test
freezing.
"""

from __future__ import annotations

import fnmatch
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator


_IGNORED = frozenset(
    {".git", ".gradle", ".idea", "build", "dist", "node_modules", "out", "target"}
)
_TEST_SOURCE_SETS = frozenset(
    {
        "test", "testfixtures", "integrationtest", "integration-test",
        "integration_test", "functionaltest", "functional-test",
        "functional_test", "androidtest", "it",
    }
)
_LEGACY_TEST_ROOTS = frozenset(
    {"test", "tests", "testsrc", "test-src", "integration-test",
     "integration-tests", "integrationtest", "integrationtests"}
)
_DESCRIPTORS = frozenset(
    {"pom.xml", "build.gradle", "build.gradle.kts", "build.xml", "build", "build.bazel"}
)
_BUILD_FILES = frozenset(
    {
        "pom.xml", "mvnw", "mvnw.cmd", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "gradle.properties", "gradlew",
        "gradlew.bat", "init.gradle", "init.gradle.kts", "build.xml",
        "build.properties", "ant.properties", "ivy.xml", "ivysettings.xml",
        "build", "build.bazel", "workspace", "workspace.bazel", "module.bazel",
        "workspace.bzlmod", "bazeliskrc", "bazelw", "bazelw.bat",
    }
)
_BUILD_LOGIC_ROOTS = frozenset({"buildsrc", "build-logic", "build_logic", "buildlogic"})
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_MAVEN_GENERATED_PATH_PROPERTIES = frozenset(
    {
        "project.build.directory",
        "pom.build.directory",
        "project.build.outputdirectory",
        "project.build.testoutputdirectory",
    }
)


class JavaSourceLayoutError(ValueError):
    """A build descriptor or source link cannot be classified safely."""

    def __init__(self, status: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def to_unavailable(self) -> dict[str, object]:
        """Return a stable detector-facing failure payload.

        Source-layout discovery is part of the Java product detector contract:
        if an explicitly declared test tree cannot be classified, treating the
        remaining sources as a complete production tree would be unsafe.
        """
        return {
            "status": "DETECTOR_UNAVAILABLE",
            "component": "java_source_layout",
            "reason": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class JavaSourceLayout:
    project_root: Path
    test_roots: tuple[str, ...]
    test_files: tuple[str, ...]
    test_globs: tuple[str, ...]
    test_glob_excludes: tuple[str, ...]
    verification_files: tuple[str, ...]
    auxiliary_roots: tuple[str, ...] = ()

    def is_test_path(self, path: str | Path) -> bool:
        relative = _relative_lexical(self.project_root, path)
        if relative is None:
            return False
        if standard_test_root(relative) is not None:
            return True
        if relative in self.test_files:
            return True
        if any(relative == root or relative.startswith(root + "/") for root in self.test_roots):
            return True
        included = any(_glob_matches(relative, pattern) for pattern in self.test_globs)
        excluded = any(_glob_matches(relative, pattern) for pattern in self.test_glob_excludes)
        return included and not excluded

    def contains_test_descendant(self, path: str | Path) -> bool:
        relative = _relative_lexical(self.project_root, path)
        if relative is None:
            return False
        prefix = relative.rstrip("/") + "/"
        return any(item.startswith(prefix) for item in (*self.test_roots, *self.test_files)) or any(
            pattern.startswith(prefix) for pattern in self.test_globs
        )

    def is_auxiliary_path(self, path: str | Path) -> bool:
        """Return whether Maven only adds this source during test compilation.

        Auxiliary tools and benchmarks remain refactorable product inputs; they
        are not behavior-test oracles merely because Maven compiles them in the
        test phase.
        """
        relative = _relative_lexical(self.project_root, path)
        return bool(
            relative is not None
            and any(
                relative == root or relative.startswith(root + "/")
                for root in self.auxiliary_roots
            )
        )


def discover_java_source_layout(project_root: str | Path) -> JavaSourceLayout:
    root = Path(project_root).resolve()
    return _discover_java_source_layout_cached(str(root))


@lru_cache(maxsize=32)
def _discover_java_source_layout_cached(project_root: str) -> JavaSourceLayout:
    root = Path(project_root)
    if not root.is_dir():
        raise JavaSourceLayoutError("TEST_TREE_UNREADABLE", f"project root is not a directory: {root}")
    verification_contexts, source_descriptors = _discover_verification_graph(root)
    verification = tuple(sorted(verification_contexts))
    roots: set[str] = set()
    files: set[str] = set()
    globs: set[str] = set()
    excludes: set[str] = set()
    auxiliary_roots: set[str] = set()
    for relative in verification:
        if relative not in source_descriptors:
            continue
        path = root / relative
        name = path.name.casefold()
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            raise JavaSourceLayoutError(
                "VERIFICATION_CONFIG_UNREADABLE", f"cannot read Java build descriptor {path}: {exc}", path=str(path)
            ) from exc
        module = verification_contexts[relative]
        if name == "pom.xml":
            raw_roots, raw_auxiliary_roots = _maven_source_roles(
                text,
                descriptor=relative,
            )
            _add_explicit_roots(
                root,
                module,
                raw_roots,
                roots,
                descriptor=relative,
                build_system="maven",
            )
            _add_explicit_roots(
                root,
                module,
                raw_auxiliary_roots,
                auxiliary_roots,
                descriptor=relative,
                build_system="maven",
            )
        elif name in {"build.gradle", "build.gradle.kts"} or path.suffix.casefold() in {".gradle", ".kts"}:
            raw_roots = _gradle_test_roots(text)
            _add_roots(root, module, raw_roots, roots)
        elif name == "build.xml" or path.suffix.casefold() == ".xml":
            raw_roots = _ant_test_roots(text, descriptor=relative)
            _add_explicit_roots(
                root,
                module,
                raw_roots,
                roots,
                descriptor=relative,
                build_system="ant",
            )
        elif name in {"build", "build.bazel"}:
            raw_files, raw_globs, raw_excludes = _bazel_java_test_inputs(text)
            _add_files(root, module, raw_files, files)
            _add_patterns(root, module, raw_globs, globs)
            _add_patterns(root, module, raw_excludes, excludes)
    return JavaSourceLayout(
        project_root=root,
        test_roots=tuple(sorted(roots)),
        test_files=tuple(sorted(files)),
        test_globs=tuple(sorted(globs)),
        test_glob_excludes=tuple(sorted(excludes)),
        verification_files=verification,
        auxiliary_roots=tuple(sorted(auxiliary_roots)),
    )


def discover_java_verification_files(project_root: str | Path) -> tuple[str, ...]:
    """Return conventional config plus statically referenced Gradle/Ant scripts."""
    contexts, _ = _discover_verification_graph(Path(project_root).resolve())
    return tuple(sorted(contexts))


def _discover_verification_graph(root: Path) -> tuple[dict[str, Path], set[str]]:
    root = root.resolve()
    discovered: dict[str, Path] = {}
    source_descriptors: set[str] = set()
    try:
        for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            directory_names[:] = sorted(
                name for name in directory_names
                if name.casefold() not in _IGNORED and not name.casefold().startswith("bazel-")
            )
            directory_path = Path(directory)
            for name in sorted(file_names):
                relative = (directory_path / name).relative_to(root).as_posix()
                if is_java_verification_config_path(relative):
                    discovered[relative] = _configuration_module_root(
                        root, Path(relative)
                    )
                    if name.casefold() in _DESCRIPTORS:
                        source_descriptors.add(relative)
    except OSError as exc:
        raise JavaSourceLayoutError(
            "VERIFICATION_CONFIG_UNREADABLE", f"cannot inspect Java verification config under {root}: {exc}"
        ) from exc

    # Resolve references to a fixed point. Referenced scripts are authoritative
    # even when their names do not match a build-system convention.
    pending = list(sorted(discovered))
    inspected: set[str] = set()
    while pending:
        relative = pending.pop(0)
        if relative in inspected:
            continue
        inspected.add(relative)
        path = root / relative
        module_root = discovered[relative]
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            raise JavaSourceLayoutError(
                "VERIFICATION_CONFIG_UNREADABLE", f"cannot read Java verification config {path}: {exc}", path=str(path)
            ) from exc
        references: set[str] = set()
        if path.suffix.casefold() in {".gradle", ".kts"}:
            references.update(_gradle_applied_scripts(text))
        if path.name.casefold() == "build.xml" or path.suffix.casefold() == ".xml":
            references.update(_ant_imports(text))
        for raw in references:
            normalized = _normalize_local(root, module_root, raw)
            if normalized is None:
                continue
            referenced = root / normalized
            if not referenced.is_file():
                continue
            if normalized not in discovered:
                discovered[normalized] = module_root
                pending.append(normalized)
            source_descriptors.add(normalized)
    return discovered, source_descriptors


def _configuration_module_root(project_root: Path, relative: Path) -> Path:
    parts = relative.parts
    lowered = tuple(part.casefold() for part in parts)
    special = _BUILD_LOGIC_ROOTS | {"gradle", ".mvn", "nbproject"}
    for index, part in enumerate(lowered):
        if part in special:
            return project_root.joinpath(*parts[:index])
    return project_root.joinpath(*parts[:-1])


def is_java_verification_config_path(path: str | Path) -> bool:
    normalized = str(path).replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = tuple(part for part in normalized.split("/") if part)
    lowered = tuple(part.casefold() for part in parts)
    if any(part in _IGNORED or part.startswith("bazel-") for part in lowered[:-1]):
        return False
    name = lowered[-1]
    beneath_source = _beneath_source_tree(lowered)
    if name in {"build", "build.bazel"}:
        return True
    if (name.endswith(".bzl") or name.endswith(".bazel")) and not beneath_source:
        return True
    if name.startswith(".bazelrc") or name == ".bazelversion":
        return True
    first_src = lowered.index("src") if "src" in lowered else len(lowered)
    if any(index < first_src and part in _BUILD_LOGIC_ROOTS for index, part in enumerate(lowered)):
        return True
    if not beneath_source and (".mvn" in lowered or "nbproject" in lowered):
        return True
    if any(index < first_src and part == "gradle" for index, part in enumerate(lowered)):
        return True
    return name in _BUILD_FILES and not beneath_source


def standard_test_root(path: str | Path) -> str | None:
    normalized = str(path).replace("\\", "/").strip("/")
    parts = tuple(part for part in normalized.split("/") if part)
    lowered = tuple(part.casefold() for part in parts)
    for index in range(len(parts) - 1):
        if lowered[index] == "src" and lowered[index + 1] in _TEST_SOURCE_SETS:
            return "/".join(parts[: index + 2])
    for index, part in enumerate(lowered[:-1]):
        if part not in _LEGACY_TEST_ROOTS:
            continue
        prefix = lowered[:index]
        if "src" in prefix and any(item in {"main", "production"} for item in prefix):
            continue
        return "/".join(parts[: index + 1])
    return None


def _relative_lexical(root: Path, path: str | Path) -> str | None:
    raw = str(path).replace("\\", "/")
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(root).as_posix().strip("/")
        except ValueError:
            # resolve only as a secondary route; lexical paths preserve symlink
            # mount names needed by the frozen manifest.
            try:
                return candidate.resolve(strict=False).relative_to(root).as_posix().strip("/")
            except (OSError, ValueError):
                return None
    normalized = PurePosixPath(raw.lstrip("./"))
    if ".." in normalized.parts:
        return None
    return normalized.as_posix().strip("/")


def _glob_matches(relative: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    # Python's fnmatch requires a slash for **/, while Bazel glob("**/*.java")
    # also includes files in the package directory itself.
    return pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:])


def _xml_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _xml_properties(document: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in document.iter():
        if _xml_name(node.tag) == "properties":
            for child in node:
                value = str(child.text or "").strip()
                if value:
                    result[_xml_name(child.tag)] = value
        if _xml_name(node.tag) == "property":
            name = str(node.attrib.get("name") or "").strip()
            value = str(node.attrib.get("value") or node.attrib.get("location") or "").strip()
            if name and value:
                result[name.casefold()] = value
    return result


def _expand_properties(value: str, properties: dict[str, str]) -> str | None:
    current = value.strip()
    for _ in range(16):
        matches = list(re.finditer(r"\$\{([^}]+)\}", current))
        if not matches:
            return current
        changed = False
        for match in reversed(matches):
            key = match.group(1).casefold()
            if key in {"project.basedir", "basedir"}:
                # Keep the result module-relative. Replacing with an empty
                # string turns ``${basedir}/src/test`` into the filesystem-
                # absolute ``/src/test`` and makes a statically known path look
                # unavailable.
                replacement = "."
            elif key in properties:
                replacement = properties[key]
            else:
                return None
            current = current[:match.start()] + replacement + current[match.end():]
            changed = True
        if not changed:
            break
    return None


def _require_explicit_test_path(
    value: str,
    properties: dict[str, str],
    *,
    descriptor: str,
    build_system: str,
    element: str,
) -> str:
    """Expand one build-declared test path or fail closed.

    This is deliberately called only after Maven or Ant has unambiguously
    identified a test source/resource declaration. Unknown expressions in
    ordinary build logic remain outside this contract.
    """
    expanded = _expand_properties(value, properties)
    if expanded and not any(marker in expanded for marker in ("${", "@{", "$<")):
        return expanded
    raise JavaSourceLayoutError(
        "EXPLICIT_TEST_PATH_UNRESOLVED",
        (
            f"cannot resolve explicit {build_system} test path in "
            f"{descriptor} ({element}): {value}"
        ),
        descriptor=descriptor,
        build_system=build_system,
        element=element,
        value=value,
    )


def _is_maven_generated_path(
    value: str,
    properties: dict[str, str],
    *,
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Return whether a Maven test input is derived from build output.

    Maven projects commonly add generated test sources or resources below
    project.build.directory. Those paths are outputs, not checked-in test
    inputs, and the source walker excludes them independently. Property
    indirection is followed so this remains a build-model rule rather than a
    project-specific path exception.
    """
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == "target" or normalized.startswith("target/"):
        return True
    for match in re.finditer(r"\$\{([^}]+)\}", value):
        key = match.group(1).casefold()
        if key in _MAVEN_GENERATED_PATH_PROPERTIES:
            return True
        if key in properties and key not in seen:
            if _is_maven_generated_path(
                properties[key],
                properties,
                seen=seen | {key},
            ):
                return True
    return False


def _maven_explicit_test_path(
    value: str,
    properties: dict[str, str],
    *,
    descriptor: str,
    element: str,
) -> str | None:
    """Resolve a checked-in Maven test path; omit proven generated outputs."""
    if _is_maven_generated_path(value, properties):
        return None
    return _require_explicit_test_path(
        value,
        properties,
        descriptor=descriptor,
        build_system="maven",
        element=element,
    )


def _maven_source_roles(
    text: str,
    *,
    descriptor: str = "pom.xml",
) -> tuple[set[str], set[str]]:
    try:
        document = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JavaSourceLayoutError(
            "MAVEN_XML_PARSE_FAILED",
            f"cannot parse Maven descriptor {descriptor}: {exc}",
            descriptor=descriptor,
            build_system="maven",
            position=getattr(exc, "position", None),
        ) from exc
    properties = _xml_properties(document)
    roots: set[str] = set()
    auxiliary_roots: set[str] = set()
    for node in document.iter():
        name = _xml_name(node.tag)
        value = str(node.text or "").strip()
        if name in {"testsourcedirectory", "testsource", "testresourcedirectory"} and value:
            resolved = _maven_explicit_test_path(
                value,
                properties,
                descriptor=descriptor,
                element=name,
            )
            if resolved is not None:
                roots.add(resolved)
        if name == "testresource":
            for child in node:
                if _xml_name(child.tag) == "directory":
                    value = str(child.text or "").strip()
                    if value:
                        resolved = _maven_explicit_test_path(
                            value,
                            properties,
                            descriptor=descriptor,
                            element="testresource.directory",
                        )
                        if resolved is not None:
                            roots.add(resolved)
    for execution in document.iter():
        if _xml_name(execution.tag) != "execution":
            continue
        goal_values = {
            str(node.text or "").strip().casefold()
            for node in execution.iter() if _xml_name(node.tag) == "goal"
        }
        if "add-test-source" in goal_values:
            for node in execution.iter():
                if _xml_name(node.tag) != "source":
                    continue
                value = str(node.text or "").strip()
                if value:
                    expanded = _maven_explicit_test_path(
                        value,
                        properties,
                        descriptor=descriptor,
                        element="add-test-source.source",
                    )
                    if expanded is None:
                        continue
                    if _looks_like_behavior_test_root(expanded):
                        roots.add(expanded)
                    else:
                        auxiliary_roots.add(expanded)
        if "add-test-resource" in goal_values:
            for node in execution.iter():
                if _xml_name(node.tag) not in {"resource", "directory"}:
                    continue
                value = str(node.text or "").strip()
                if value:
                    resolved = _maven_explicit_test_path(
                        value,
                        properties,
                        descriptor=descriptor,
                        element=f"add-test-resource.{_xml_name(node.tag)}",
                    )
                    if resolved is not None:
                        roots.add(resolved)
    return roots, auxiliary_roots


def _looks_like_behavior_test_root(path: str) -> bool:
    if standard_test_root(path) is not None:
        return True
    return any(
        token in {"test", "tests", "testing", "spec", "specs"}
        for part in str(path).replace("\\", "/").split("/")
        for token in re.split(r"[-_.]", part.casefold())
        if token
    )


def _is_test_set(name: str) -> bool:
    normalized = re.sub(r"[-_]", "", name.casefold())
    return normalized in {"test", "it", "androidtest", "testfixtures"} or "test" in normalized


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _blocks_after(text: str, pattern: str) -> Iterator[tuple[re.Match[str], str]]:
    for match in re.finditer(pattern, text, re.MULTILINE):
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            continue
        end = _matching_delimiter(text, brace, "{", "}")
        if end is not None:
            yield match, text[brace + 1:end]


def _quoted_paths(fragment: str) -> set[str]:
    return {value for _, value in re.findall(r"(['\"])(.*?)\1", fragment, re.DOTALL) if value.strip()}


def _src_dirs(fragment: str) -> set[str]:
    roots: set[str] = set()
    api = re.compile(r"\b(?:java|kotlin|groovy|resources)?\s*\.?\s*(?:srcDir|srcDirs|setSrcDirs)\b")
    for match in api.finditer(fragment):
        index = match.end()
        depth = 0
        quote = ""
        while index < len(fragment):
            char = fragment[index]
            if quote:
                if char == quote and fragment[index - 1] != "\\":
                    quote = ""
            elif char in {'"', "'"}:
                quote = char
            elif char in "([{":
                depth += 1
            elif char in ")]}" and depth:
                depth -= 1
            elif depth == 0 and char in ";\n":
                break
            index += 1
        roots.update(_quoted_paths(fragment[match.end():index]))
    return roots


def _gradle_test_roots(text: str) -> set[str]:
    roots: set[str] = set()
    names: set[str] = set()
    regions: list[tuple[str, str]] = []

    # Direct sourceSets.foo and sourceSets["foo"] expressions have an exact
    # receiver; the statement is parsed independently of surrounding text.
    direct = re.compile(
        r"sourceSets\s*(?:\.\s*([A-Za-z_$][\w$-]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])"
    )
    for match in direct.finditer(text):
        name = match.group(1) or match.group(2)
        if not _is_test_set(name):
            continue
        names.add(name)
        end = match.end()
        while end < len(text) and text[end] not in "\n;":
            end += 1
        regions.append((name, text[match.end():end]))

    # create/named/register calls identify their own source set and optional
    # balanced configuration block.
    call_pattern = r"\bsourceSets\s*\.\s*(?:create|named|register)\s*\(\s*['\"]([^'\"]+)['\"][^)]*\)\s*\{"
    for match, body in _blocks_after(text, call_pattern):
        name = match.group(1)
        if _is_test_set(name):
            names.add(name)
            regions.append((name, body))

    # Kotlin delegated creation, both sourceSets.creating and `by creating`
    # inside a sourceSets block.
    delegated = r"\bval\s+([A-Za-z_$][\w$-]*)\s+by\s+sourceSets\.creating\b\s*\{"
    for match, body in _blocks_after(text, delegated):
        name = match.group(1)
        if _is_test_set(name):
            names.add(name)
            regions.append((name, body))
    for match in re.finditer(r"\bval\s+([A-Za-z_$][\w$-]*)\s+by\s+sourceSets\.creating\b", text):
        if _is_test_set(match.group(1)):
            names.add(match.group(1))

    # Names declared structurally inside sourceSets { ... }. Balanced blocks
    # replace the old proximity/window guess and work when the file is one line.
    for _, outer in _blocks_after(text, r"\bsourceSets\s*\{"):
        nested = r"(?:\bval\s+)?([A-Za-z_$][\w$-]*)\s*(?:by\s+creating\s*)?\{"
        for match, body in _blocks_after(outer, nested):
            name = match.group(1)
            if name not in {"java", "kotlin", "groovy", "resources"} and _is_test_set(name):
                names.add(name)
                regions.append((name, body))
        inside_call = r"\b(?:create|named|register)\s*\(\s*['\"]([^'\"]+)['\"][^)]*\)\s*\{"
        for match, body in _blocks_after(outer, inside_call):
            name = match.group(1)
            if _is_test_set(name):
                names.add(name)
                regions.append((name, body))
        # Single-line receiver form: sourceSets { contractTest.java.srcDir("x") }
        for match in re.finditer(r"\b([A-Za-z_$][\w$-]*)\s*\.\s*(?:java|kotlin|groovy|resources)\s*\.\s*(?:srcDir|srcDirs|setSrcDirs)\b", outer):
            name = match.group(1)
            if not _is_test_set(name):
                continue
            names.add(name)
            end = match.end()
            while end < len(outer) and outer[end] not in "\n;":
                end += 1
            regions.append((name, "srcDir" + outer[match.end():end]))

    roots.update(f"src/{name}" for name in names)
    for _, region in regions:
        roots.update(_src_dirs(region))
    return roots


def _ant_test_roots(
    text: str,
    *,
    descriptor: str = "build.xml",
) -> set[str]:
    try:
        document = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JavaSourceLayoutError(
            "ANT_XML_PARSE_FAILED",
            f"cannot parse Ant descriptor {descriptor}: {exc}",
            descriptor=descriptor,
            build_system="ant",
            position=getattr(exc, "position", None),
        ) from exc
    properties = _xml_properties(document)
    roots: set[str] = set()
    for target in document.iter():
        if _xml_name(target.tag) != "target" or not _is_test_set(str(target.attrib.get("name") or "")):
            continue
        for node in target.iter():
            if _xml_name(node.tag) == "javac":
                raw_values = [str(node.attrib.get("srcdir") or "")]
                raw_values.extend(
                    str(child.attrib.get("path") or child.attrib.get("location") or "")
                    for child in node.iter() if _xml_name(child.tag) == "src"
                )
                for raw in raw_values:
                    if not raw:
                        continue
                    expanded = _require_explicit_test_path(
                        raw,
                        properties,
                        descriptor=descriptor,
                        build_system="ant",
                        element="test-target.javac.srcdir",
                    )
                    roots.update(part.strip() for part in re.split(r"[;:]", expanded) if part.strip())
    return roots


def _call_bodies(text: str, name: str, opening: str = "(", closing: str = ")") -> Iterator[str]:
    for match in re.finditer(rf"\b{re.escape(name)}\s*{re.escape(opening)}", text):
        start = text.find(opening, match.start())
        end = _matching_delimiter(text, start, opening, closing)
        if end is not None:
            yield text[start + 1:end]


def _attribute_expression(body: str, attribute: str) -> str:
    match = re.search(rf"\b{re.escape(attribute)}\s*=", body)
    if not match:
        return ""
    start = match.end()
    depth = 0
    quote = ""
    index = start
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote and body[index - 1] != "\\":
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char in "([{" :
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]


def _bazel_java_test_inputs(text: str) -> tuple[set[str], set[str], set[str]]:
    files: set[str] = set()
    patterns: set[str] = set()
    excludes: set[str] = set()
    for body in _call_bodies(text, "java_test"):
        expression = _attribute_expression(body, "srcs")
        if not expression:
            continue
        glob_spans: list[tuple[int, int]] = []
        for match in re.finditer(r"\bglob\s*\(", expression):
            start = expression.find("(", match.start())
            end = _matching_delimiter(expression, start, "(", ")")
            if end is None:
                continue
            glob_spans.append((match.start(), end + 1))
            glob_body = expression[start + 1:end]
            exclude_expr = _attribute_expression(glob_body, "exclude")
            excludes.update(_quoted_paths(exclude_expr))
            include_part = glob_body[: glob_body.find("exclude") if "exclude" in glob_body else len(glob_body)]
            patterns.update(_quoted_paths(include_part))
        outside = "".join(
            char if not any(begin <= index < end for begin, end in glob_spans) else " "
            for index, char in enumerate(expression)
        )
        files.update(
            value for value in _quoted_paths(outside)
            if not value.startswith(":") and not value.startswith("//")
        )
    return files, patterns, excludes


def _gradle_applied_scripts(text: str) -> set[str]:
    patterns = (
        r"\bapply\s+from\s*:\s*['\"]([^'\"]+)['\"]",
        r"\bapply\s*\(\s*from\s*=\s*['\"]([^'\"]+)['\"]\s*\)",
        r"\bapply\s*\(\s*from\s*=\s*file\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\)",
    )
    result = {match.group(1) for pattern in patterns for match in re.finditer(pattern, text)}
    result.update(
        "rootProject.projectDir/" + match.group(1)
        for match in re.finditer(
            r"\bapply\s*\(\s*from\s*=\s*rootProject\.file\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\)",
            text,
        )
    )
    return result


def _ant_imports(text: str) -> set[str]:
    try:
        document = ET.fromstring(text)
    except ET.ParseError:
        return set()
    properties = _xml_properties(document)
    result: set[str] = set()
    for node in document.iter():
        if _xml_name(node.tag) not in {"import", "include"}:
            continue
        expanded = _expand_properties(str(node.attrib.get("file") or ""), properties)
        if expanded:
            result.add(expanded)
    return result


def _normalize_local(project_root: Path, module_root: Path, raw: str) -> str | None:
    value = raw.strip().strip("'\"").replace("\\", "/")
    for marker in ("${project.basedir}", "${basedir}", "$projectDir", "${projectDir}", "rootProject.projectDir/"):
        if value.startswith(marker):
            value = value[len(marker):].lstrip("/")
            module_root = project_root
            break
    if not value or "$" in value or "{" in value or "}" in value or "://" in value or _WINDOWS_ABSOLUTE.match(value):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = module_root / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(project_root).as_posix()
    except (OSError, ValueError):
        return None
    if not relative or any(part.casefold() in _IGNORED for part in Path(relative).parts):
        return None
    return relative.rstrip("/")


def _add_roots(project_root: Path, module_root: Path, raw: Iterable[str], output: set[str]) -> None:
    for value in raw:
        normalized = _normalize_local(project_root, module_root, value)
        if normalized:
            output.add(normalized)


def _add_explicit_roots(
    project_root: Path,
    module_root: Path,
    raw: Iterable[str],
    output: set[str],
    *,
    descriptor: str,
    build_system: str,
) -> None:
    """Add explicit Maven/Ant test roots without silently dropping one."""
    for value in raw:
        normalized = _normalize_local(project_root, module_root, value)
        if normalized is None:
            raise JavaSourceLayoutError(
                "EXPLICIT_TEST_PATH_UNRESOLVED",
                (
                    f"explicit {build_system} test path in {descriptor} cannot "
                    f"be classified inside the project: {value}"
                ),
                descriptor=descriptor,
                build_system=build_system,
                value=value,
            )
        output.add(normalized)


def _add_files(project_root: Path, module_root: Path, raw: Iterable[str], output: set[str]) -> None:
    _add_roots(project_root, module_root, raw, output)


def _add_patterns(project_root: Path, module_root: Path, raw: Iterable[str], output: set[str]) -> None:
    for value in raw:
        # Preserve glob metacharacters while canonicalizing the package prefix.
        marker = min((value.find(char) for char in "*[?" if char in value), default=len(value))
        prefix, suffix = value[:marker], value[marker:]
        base = _normalize_local(project_root, module_root, prefix.rstrip("/")) if prefix.rstrip("/") else module_root.relative_to(project_root).as_posix()
        if base is None:
            continue
        output.add((base.rstrip("/") + "/" + suffix.lstrip("/" )).strip("/"))


def _beneath_source_tree(parts: tuple[str, ...]) -> bool:
    # Buildlogic/gradle are ordinary package names under every source set, not
    # just src/main and src/test.
    return any(parts[index] == "src" and index + 2 < len(parts) for index in range(len(parts) - 2))
