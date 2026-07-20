#!/usr/bin/env python3
"""Regression checks for the long_parameter_list lingering-signature fallback.

The finding matcher anchors on original arity / line, so an agent can make a
target "unfindable" while the original long signature still exists. The
fallback rescan must fail the guard in that case and pass only when the long
signature is truly gone.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "runtime" / "python"))

from smell_core.java.smell_guards import run_java_syntactic_guard  # noqa: E402


def _params(prefix: str, count: int) -> str:
    return ", ".join(f"int {prefix}{i}" for i in range(count))


def _run_guard(root: Path, *, method, line, parameter_count=None):
    target = SimpleNamespace(
        file_path=root / "Host.java",
        project_path="Host.java",
        method=method,
        line=line,
        start_line=None,
        parameter_count=parameter_count,
        param_type_fingerprint=None,
    )
    config = SimpleNamespace(language="java", project_root=root, locations=[target])
    return run_java_syntactic_guard(config, "long_parameter_list", {"long_parameter_list": 5}, "")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lpl-lingering-") as tmp:
        root = Path(tmp)

        # A) Untouched long constructor: normal matcher path must still fail.
        (root / "Host.java").write_text(
            f"class Host {{\n  Host({_params('p', 13)}) {{ }}\n}}\n", encoding="utf-8"
        )
        verdict = _run_guard(root, method="Host", line=2, parameter_count=13)
        assert verdict and verdict["success"] is False, verdict

        # B) sid-61 shape: a small overload appears and the long constructor
        # drifts away from the dataset anchor. The arity/line matcher returns
        # None; the lingering fallback must fail the guard instead.
        (root / "Host.java").write_text(
            "class Host {\n"
            "  Host(int a, int b) { }\n"
            "\n"
            "  // javadoc added by the agent pushing the target down\n"
            "  // more padding\n"
            f"  Host({_params('p', 13)}) {{ }}\n"
            "}\n",
            encoding="utf-8",
        )
        verdict_b = _run_guard(root, method=None, line=2, parameter_count=None)
        assert verdict_b and verdict_b["success"] is False, verdict_b
        assert "lingering-signature" in str(verdict_b["message"]), verdict_b["message"]

        # C) Long signature genuinely replaced by a parameter object: pass.
        (root / "Host.java").write_text(
            "class HostParams {\n"
            "  final int value;\n"
            "  HostParams(int value) { this.value = value; }\n"
            "}\n"
            "class Host {\n"
            "  Host(HostParams params) { }\n"
            "}\n",
            encoding="utf-8",
        )
        verdict_c = _run_guard(root, method="Host", line=6, parameter_count=None)
        assert verdict_c and verdict_c["success"] is True, verdict_c

    print("lpl-lingering-guard self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
