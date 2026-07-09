from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable


@dataclass(frozen=True)
class LanguageSupport:
    name: str
    extensions: tuple[str, ...]
    idea_backed: bool = False


_REGISTRY: Dict[str, LanguageSupport] = {
    "java": LanguageSupport(name="java", extensions=(".java",), idea_backed=True),
    "python": LanguageSupport(name="python", extensions=(".py",), idea_backed=False),
    "c": LanguageSupport(name="c", extensions=(".c", ".h"), idea_backed=False),
    "cpp": LanguageSupport(
        name="cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
        idea_backed=False,
    ),
}


def register_language(support: LanguageSupport) -> None:
    if not support.name:
        raise ValueError("language support must have a name")
    _REGISTRY[support.name] = support


def supported_languages() -> Iterable[LanguageSupport]:
    return tuple(_REGISTRY.values())


def get_language(name: str) -> LanguageSupport | None:
    return _REGISTRY.get(name)
