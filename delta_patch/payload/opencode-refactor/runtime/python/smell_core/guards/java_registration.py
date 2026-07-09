"""Register Java-specific guard handlers into the guards registry."""
from __future__ import annotations

from ..java.smell_guards import (
    run_java_clone_guard,
    run_java_smell_guard,
    run_java_syntactic_guard,
)
from .registry import register_clone_guard, register_smell_guard, register_syntactic_guard

register_smell_guard("java", run_java_smell_guard)
register_syntactic_guard("java", run_java_syntactic_guard)
register_clone_guard("java", run_java_clone_guard)
