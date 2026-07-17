#!/usr/bin/env python3
"""Focused self-check for Java AST-NCSS long-method measurement."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.java.ast_ncss import run_ast_ncss  # noqa: E402
from smell_core.java.syntactic_detector import count_non_comment_loc  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ast-ncss-self-check-") as temp_dir:
        root = Path(temp_dir)
        source = root / "NcssFixture.java"
        source.write_text(
            "class NcssFixture {\n"
            "  void target() { int value = 1; value++; if (value > 0) { value--; } }\n"
            "}\n",
            encoding="utf-8",
        )
        ncloc = count_non_comment_loc("{ int value = 1; value++; if (value > 0) { value--; } }")
        detected = run_ast_ncss(source, root, 1)
        if not detected.ok:
            raise AssertionError(detected.error)
        target = next((item for item in detected.findings if item.method.startswith("target(")), None)
        if target is None:
            raise AssertionError("Java AST did not report the one-line multi-statement fixture")
        if target.score <= ncloc:
            raise AssertionError(f"Expected AST-NCSS ({target.score}) to exceed NCLOC ({ncloc})")
        boundary = run_ast_ncss(source, root, int(target.score))
        if not boundary.ok or not boundary.findings:
            raise AssertionError("AST-NCSS reports a method when score equals the threshold")
        cleared = run_ast_ncss(source, root, int(target.score) + 1)
        if not cleared.ok or cleared.findings:
            raise AssertionError("AST-NCSS must clear when the threshold exceeds the score")
        print(f"ast-ncss-self-check PASS ncloc={ncloc} ast_ncss={target.score:g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
