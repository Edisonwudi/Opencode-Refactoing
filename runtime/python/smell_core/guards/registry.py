"""Guard handler registry for language-specific dispatch.

Each language registers its own guard handlers via ``register_guard``.
The generic guard logic in ``__init__.py`` queries the registry before
falling through to language-agnostic implementations.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..config import ResolvedRunConfig
from .context import GuardRunContext

# Type alias for guard handler callables.
# A handler receives (config, guard_dict, optional run context) and returns a result dict or None.
GuardHandlerFn = Callable[[ResolvedRunConfig, Dict[str, Any], Optional[GuardRunContext]], Optional[Dict[str, object]]]

# Type alias for syntactic guard callables.
SyntacticGuardFn = Callable[[ResolvedRunConfig, str, Dict[str, Any]], Optional[Dict[str, object]]]

# Type alias for clone guard callables.
CloneGuardFn = Callable[[ResolvedRunConfig, Dict[str, Any]], Optional[Dict[str, object]]]

# --- Registries ---

_SMELL_GUARDS: Dict[str, GuardHandlerFn] = {}
_SYNTACTIC_GUARDS: Dict[str, SyntacticGuardFn] = {}
_CLONE_GUARDS: Dict[str, CloneGuardFn] = {}


def register_smell_guard(language: str, handler: GuardHandlerFn) -> None:
    """Register a language-specific smell guard handler.

    The handler is called for every guard entry. It should return a result
    dict if it handled the guard, or ``None`` to fall through.
    """
    _SMELL_GUARDS[language] = handler


def register_syntactic_guard(language: str, handler: SyntacticGuardFn) -> None:
    """Register a language-specific syntactic guard.

    Called as ``handler(config, guard_type, thresholds)`` — returns a result
    dict or ``None``.
    """
    _SYNTACTIC_GUARDS[language] = handler


def register_clone_guard(language: str, handler: CloneGuardFn) -> None:
    """Register a language-specific clone guard.

    Called as ``handler(config, guard)`` — returns a result dict or ``None``.
    """
    _CLONE_GUARDS[language] = handler


def get_smell_guard(language: str) -> Optional[GuardHandlerFn]:
    return _SMELL_GUARDS.get(language)


def get_syntactic_guard(language: str) -> Optional[SyntacticGuardFn]:
    return _SYNTACTIC_GUARDS.get(language)


def get_clone_guard(language: str) -> Optional[CloneGuardFn]:
    return _CLONE_GUARDS.get(language)
