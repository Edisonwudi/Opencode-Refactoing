from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class LocationTarget:
    raw: str
    project_path: Path
    file_path: Path
    line: Optional[int] = None
    method: Optional[str] = None
    class_name: Optional[str] = None
    start_line: Optional[int] = None
    signature_text: Optional[str] = None
    parameter_count: Optional[int] = None
    param_type_fingerprint: Optional[str] = None

    @property
    def display_path(self) -> str:
        return str(self.project_path)


def split_location_descriptors(location: str) -> List[str]:
    descriptors: List[str] = []
    for group in str(location or "").split(";"):
        for part in re.split(r"\s*<->\s*", group):
            stripped = part.strip()
            if stripped:
                descriptors.append(stripped)
    return descriptors


def parse_location_descriptor(location: str, project_root: Path) -> LocationTarget:
    method_marker = ":method="
    class_marker = ":class="
    descriptor = str(location or "").strip()
    if not descriptor:
        raise ValueError("Location descriptor is empty.")
    idx = descriptor.rfind(":")
    method_idx = descriptor.find(method_marker)
    class_idx = descriptor.find(class_marker)
    named_markers = [
        (marker_idx, marker)
        for marker_idx, marker in ((method_idx, "method"), (class_idx, "class"))
        if marker_idx >= 0
    ]
    named_markers.sort(key=lambda item: item[0])
    if idx <= 0 and not named_markers:
        raise ValueError(f'Invalid location format (expect "<file>:<line|method=...|class=...>"): {descriptor}')
    method = None
    class_name = None
    if named_markers:
        marker_idx, marker = named_markers[0]
        file_part = descriptor[:marker_idx]
        locator = descriptor[marker_idx + 1 :]
        method = None
        class_name = None
        line = None
        if "|line=" in locator:
            name_part, line_part = locator.split("|line=", 1)
            line = int(line_part.strip()) if line_part.strip() else None
        else:
            name_part = locator
        if marker == "method":
            method = name_part.removeprefix("method=").strip() or None
            if not method:
                raise ValueError(f"Missing method name in location: {descriptor}")
        else:
            class_name = name_part.removeprefix("class=").strip() or None
            if not class_name:
                raise ValueError(f"Missing class name in location: {descriptor}")
    else:
        file_part = descriptor[:idx]
        line_text = descriptor[idx + 1 :].strip()
        if not line_text.isdigit():
            raise ValueError(f"Invalid line locator in location: {descriptor}")
        line = int(line_text)
    project_root = project_root.expanduser().resolve()
    declared_path = Path(file_part).expanduser()
    if declared_path.is_absolute():
        file_path = declared_path.resolve()
        try:
            project_path = file_path.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(
                f"Location path must be inside project root '{project_root}': {descriptor}"
            ) from exc
    else:
        file_path = (project_root / declared_path).resolve()
        try:
            project_path = file_path.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(
                f"Location path escapes project root '{project_root}': {descriptor}"
            ) from exc
    return LocationTarget(
        raw=descriptor,
        project_path=project_path,
        file_path=file_path,
        line=line,
        method=method,
        class_name=class_name,
    )


def parse_locations(location: str, project_root: Path) -> List[LocationTarget]:
    return [parse_location_descriptor(part, project_root) for part in split_location_descriptors(location)]
