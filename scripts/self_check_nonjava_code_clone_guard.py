#!/usr/bin/env python3
"""Regression checks for the shared non-Java clone token contract."""
from __future__ import annotations

import difflib
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.analysis import clone_normalized_token_score  # noqa: E402
from smell_core.checkpoint_adapters import (  # noqa: E402
    _code_clone,
    capture_metric_snapshot,
)
from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402
from smell_core.checkpoints import _diff_patch, _finding_contract  # noqa: E402
from smell_core.guards import _run_code_clone_guard  # noqa: E402
from smell_core.guards.context import GuardRunContext  # noqa: E402
from smell_core.location import parse_location_descriptor  # noqa: E402
from smell_core.target_patch_identity import (  # noqa: E402
    AST_DECLARATION_IDENTITY_CONTRACT,
    evaluate_clone_target_patch_identity,
)


def _cpp_body(*, calls: int = 0, add: bool = False, bare_return: bool = False) -> str:
    statements = ["a();"] * calls
    if add:
        statements.append("b += 1;")
    if bare_return:
        statements.append("return;")
    return "\n".join(statements)


def _python_body(*, calls: int = 0, assign: bool = False, bare_return: bool = False, pass_: bool = False) -> str:
    statements = ["a()"] * calls
    if assign:
        statements.append("value = 1")
    if bare_return:
        statements.append("return")
    if pass_:
        statements.append("pass")
    return "\n".join(statements)


def _source(language: str, first_body: str, second_body: str) -> tuple[str, int]:
    if language in {"c", "cpp"}:
        first_lines = ["void first() {", *[f"  {line}" for line in first_body.splitlines()], "}"]
        second_line = len(first_lines) + 2
        second_lines = ["void second() {", *[f"  {line}" for line in second_body.splitlines()], "}"]
        return "\n".join([*first_lines, "", *second_lines, ""]), second_line
    if language == "python":
        first_lines = ["def first():", *[f"    {line}" for line in first_body.splitlines()]]
        second_line = len(first_lines) + 2
        second_lines = ["def second():", *[f"    {line}" for line in second_body.splitlines()]]
        return "\n".join([*first_lines, "", *second_lines, ""]), second_line
    raise AssertionError(f"unsupported test language: {language}")


def _evaluate(language: str, body: str) -> tuple[dict[str, object], dict[str, object]]:
    suffix = {"c": ".c", "cpp": ".cpp", "python": ".py"}[language]
    with tempfile.TemporaryDirectory(prefix=f"nonjava-clone-{language}-") as raw:
        project = Path(raw)
        source_path = project / f"sample{suffix}"
        source, second_line = _source(language, body, body)
        source_path.write_text(source, encoding="utf-8")
        config = SimpleNamespace(
            language=language,
            smell="code_clone_type1",
            project_root=project,
            finding_contract={},
            locations=[
                parse_location_descriptor(
                    f"{source_path.name}:method=first|line=1",
                    project,
                ),
                parse_location_descriptor(
                    f"{source_path.name}:method=second|line={second_line}",
                    project,
                ),
            ],
        )
        ordinary = _run_code_clone_guard(config, {"type": "code_clone_type1"})
        checkpoint = _code_clone(config, "")
        return ordinary, checkpoint


def _assert_case(language: str, body: str, expected_score: int, expected_success: bool) -> None:
    ordinary, checkpoint = _evaluate(language, body)
    assert ordinary["success"] is expected_success, ordinary
    details = ordinary["details"]
    assert isinstance(details, dict), ordinary
    assert details["clone_token_count"] == expected_score, ordinary
    assert checkpoint["objectives"]["clone_token_count"] == expected_score, checkpoint
    assert checkpoint["finding_present"] is (not expected_success), checkpoint
    assert checkpoint["declaration_identity_valid"] is True, checkpoint
    assert all(
        item["declaration_identity"]["declared_name"] in {"first", "second"}
        and isinstance(
            item["declaration_identity"]["owner_qualified_name"],
            str,
        )
        for item in checkpoint["target_anchor_contract"]
    ), checkpoint


def _assert_target_resolution_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-resolution-") as raw:
        project = Path(raw)
        source_path = project / "sample.py"
        source_path.write_text("def first():\n    return 1\n", encoding="utf-8")
        first = parse_location_descriptor(
            "sample.py:method=first|line=1",
            project,
        )
        missing_second = parse_location_descriptor(
            "sample.py:method=second|line=1",
            project,
        )
        partial = _run_code_clone_guard(
            SimpleNamespace(language="python", locations=[first, missing_second]),
            {"type": "code_clone_type1"},
        )
        assert partial["success"] is False, partial
        assert partial["details"]["target_resolution"] == "partial", partial

        missing_both = _run_code_clone_guard(
            SimpleNamespace(
                language="python",
                locations=[
                    parse_location_descriptor(
                        "sample.py:method=third|line=1",
                        project,
                    ),
                    parse_location_descriptor(
                        "sample.py:method=fourth|line=1",
                        project,
                    ),
                ],
            ),
            {"type": "code_clone_type1"},
        )
        assert missing_both["success"] is False, missing_both
        assert missing_both["details"]["target_resolution"] == "none", missing_both

        missing_file = _run_code_clone_guard(
            SimpleNamespace(
                language="python",
                locations=[
                    first,
                    parse_location_descriptor(
                        "missing.py:method=second|line=1",
                        project,
                    ),
                ],
            ),
            {"type": "code_clone_type1"},
        )
        assert missing_file["success"] is False, missing_file
        assert missing_file["details"]["target_resolution"] == (
            "source_not_parseable"
        ), missing_file

        invalid = _run_code_clone_guard(
            SimpleNamespace(language="python", locations=[first]),
            {"type": "code_clone_type1"},
        )
        assert invalid["success"] is False, invalid
        assert invalid["details"]["target_resolution"] == "invalid_location", invalid


def _patch(before: str, after: str, file_name: str) -> str:
    unified = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{file_name}",
        tofile=f"b/{file_name}",
    ))
    return f"diff --git a/{file_name} b/{file_name}\n{unified}"


def _patch_many(changes: list[tuple[str, str, str]]) -> str:
    return "".join(
        _patch(before, after, file_name)
        for file_name, before, after in changes
    )


def _assert_repository_diff_context_is_frozen() -> None:
    with tempfile.TemporaryDirectory(prefix="clone-diff-context-") as raw:
        project = Path(raw)
        source = project / "sample.py"
        stable = [f"# stable {index}" for index in range(20)]
        before = "\n".join([
            "def first():",
            "    return 1",
            *stable,
            "def keep():",
            "    return 0",
            "",
        ])
        after = "\n".join([
            *stable,
            "def first():",
            "    return 2",
            "def keep():",
            "    return 0",
            "",
        ])
        source.write_text(before, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(
            ["git", "config", "user.email", "guard@example.invalid"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Guard Check"],
            cwd=project,
            check=True,
        )
        subprocess.run(["git", "add", "sample.py"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "baseline"],
            cwd=project,
            check=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        # This user setting would merge both edits into one giant hunk unless
        # checkpoint patch generation freezes its own context.
        subprocess.run(
            ["git", "config", "diff.context", "100"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "diff.interHunkContext", "100"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "diff.mnemonicPrefix", "true"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "diff.algorithm", "histogram"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "diff.indentHeuristic", "true"],
            cwd=project,
            check=True,
        )
        source.write_text(after, encoding="utf-8")
        patch = _diff_patch(project, base, ["sample.py"], fail_closed=True)
        assert isinstance(patch, str), patch
        assert "--- a/sample.py" in patch, patch
        assert "+++ b/sample.py" in patch, patch
        assert sum(line.startswith("@@") for line in patch.splitlines()) == 2, patch
        identity = {
            "contract": AST_DECLARATION_IDENTITY_CONTRACT,
            "declared_name": "first",
            "owner_qualified_name": "",
        }
        frozen = [{
            "target_index": 0,
            "file": "sample.py",
            "method": "first",
            "begin_line": 1,
            "declaration_identity": identity,
            "signature_sha256": "0" * 64,
        }]
        current = [{
            "target_index": 0,
            "file": "sample.py",
            "method": "first",
            "begin_line": 21,
            "resolved": True,
            "declaration_identity": identity,
            "signature_sha256": "0" * 64,
        }]
        result = evaluate_clone_target_patch_identity(
            frozen,
            current,
            changed_patch=patch,
        )
        assert result["ok"] is False, result


def _checkpoint_config(
    project: Path,
    file_name: str,
    *,
    first_line: int = 1,
    second_line: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        language="python",
        smell="code_clone_type1",
        project_root=project,
        finding_contract={},
        target_context={},
        locations=[
            parse_location_descriptor(
                f"{file_name}:method=first|line={first_line}",
                project,
            ),
            parse_location_descriptor(
                f"{file_name}:method=second|line={second_line}",
                project,
            ),
        ],
    )


def _freeze_clone_contract(
    config: SimpleNamespace,
) -> dict[str, object]:
    baseline = capture_metric_snapshot(config, "")
    assert baseline["ok"] is True, baseline
    assert baseline["finding_present"] is True, baseline
    contract = _finding_contract(
        "code_clone_type1",
        baseline,
        config.target_context,
    )
    assert len(contract["baseline_target_anchors"]) == 2, contract
    assert contract["detector_profile"]["version"] == (
        f"nonjava-target-guard/{config.language}/code_clone_type1/v5"
    ), contract
    assert contract["detector_profile"]["retained_endpoint_reanchor_contract"] == (
        "clone-retained-endpoint-same-hunk-owner-name-signature-bijection-v1"
    ), contract
    assert contract["detector_profile"]["related_occurrence_closure"] == (
        "frozen-complete-declaration-removed-occurrence-closure-v1"
    ), contract
    assert len(str(contract["detector_profile_hash"])) == 64, contract
    assert all(
        item["declaration_identity"]["declared_name"]
        for item in contract["baseline_target_anchors"]
    ), contract
    config.finding_contract = contract
    return baseline


def _python_owner_source(
    owner: str,
    first_signature: str,
    first_body: str,
    second_body: str,
) -> tuple[str, int]:
    first = [
        f"class {owner}:",
        f"    {first_signature}",
        *[f"        {line}" for line in first_body.splitlines()],
    ]
    second_line = len(first) + 2
    second = [
        "def second():",
        *[f"    {line}" for line in second_body.splitlines()],
    ]
    return "\n".join([*first, "", *second, ""]), second_line


def _assert_same_name_decoy_rejected() -> None:
    large = _python_body(calls=7)
    small = "return 0"
    original = ["def first():", *[f"    {line}" for line in large.splitlines()]]
    decoy = ["def first():", *[f"    {line}" for line in small.splitlines()]]
    second_line = len(original) + len(decoy) + 3
    second = ["def second():", *[f"    {line}" for line in large.splitlines()]]
    before = "\n".join([*original, "", *decoy, "", *second, ""])
    after = "\n".join([*decoy, "", *second, ""])

    with tempfile.TemporaryDirectory(prefix="nonjava-clone-decoy-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(project, file_name, second_line=second_line)
        baseline = _freeze_clone_contract(config)

        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["finding_present"] is False, current
        assert current["target_missing"] is False, current
        assert current["target_patch_identity_ok"] is False, current
        assert current["guard_violations"][0]["code"] == (
            "CLONE_TARGET_DECLARATION_IDENTITY_FAILED"
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["metric_progress"] is False, delta
        assert delta["reason"] == "SEMANTIC_CONTRACT_REGRESSION", delta

        ordinary = _run_code_clone_guard(
            config,
            {"type": "code_clone_type1"},
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="code_clone_type1",
                current_metrics=current,
            ),
        )
        assert ordinary["success"] is False, ordinary
        assert ordinary["details"]["detector"] == "checkpoint_current_metrics", ordinary


def _assert_frozen_patch_anchor_beats_nearest_same_name() -> None:
    large = _python_body(calls=7)
    decoy = ["def first():", "    return -1"]
    target = ["def first():", *[f"    {line}" for line in large.splitlines()]]
    target_line = len(decoy) + 2
    second_line = target_line + len(target) + 1
    second = ["def second():", *[f"    {line}" for line in large.splitlines()]]
    before = "\n".join([*decoy, "", *target, "", *second, ""])

    inserted = [f"# inserted context {index}" for index in range(10)]
    current_target = ["def first():", "    return 0"]
    current_target_line = target_line + len(inserted)
    after = "\n".join([
        *decoy,
        *inserted,
        "",
        *current_target,
        "",
        *second,
        "",
    ])

    with tempfile.TemporaryDirectory(prefix="nonjava-clone-mapped-anchor-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(
            project,
            file_name,
            first_line=target_line,
            second_line=second_line,
        )
        baseline = _freeze_clone_contract(config)

        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["target_patch_identity_ok"] is True, current
        assert current["target_missing"] is False, current
        assert current["finding_present"] is False, current
        assert current["target_anchor_contract"][0]["begin_line"] == (
            current_target_line
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["reason"] == "METRIC_PROGRESS", delta
        assert delta["metric_progress"] is True, delta


def _assert_same_hunk_retained_endpoint_move_accepted() -> None:
    large = _python_body(calls=7)
    before = "\n".join([
        "class A:",
        "    def first(self):",
        *[f"        {line}" for line in large.splitlines()],
        "",
        "    def stable(self):",
        "        return 1",
        "",
        "class B:",
        "    def second(self):",
        *[f"        {line}" for line in large.splitlines()],
        "",
        "    def stable(self):",
        "        return 2",
        "",
    ])
    second_line = before.splitlines().index("    def second(self):") + 1
    after = "\n".join([
        "class A:",
        "    def stable(self):",
        "        return 1",
        "",
        "    def first(self):",
        "        return helper_a()",
        "",
        "class B:",
        "    def stable(self):",
        "        return 2",
        "",
        "    def second(self):",
        "        return helper_b()",
        "",
    ])
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-same-hunk-move-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(
            project,
            file_name,
            first_line=2,
            second_line=second_line,
        )
        baseline = _freeze_clone_contract(config)
        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["target_patch_identity_ok"] is True, current
        assert current["target_missing"] is False, current
        assert current["finding_present"] is False, current
        reanchors = current.get("target_patch_identity_reanchors") or []
        assert len(reanchors) == 2, current
        assert all(
            item.get("contract")
            == "clone-retained-endpoint-same-hunk-owner-name-signature-bijection-v1"
            for item in reanchors
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["reason"] == "METRIC_PROGRESS", delta


def _assert_cross_hunk_retained_endpoint_move_rejected() -> None:
    large = _python_body(calls=7)
    first = ["def first():", *[f"    {line}" for line in large.splitlines()]]
    stable = [
        line
        for index in range(12)
        for line in (f"def stable_{index}():", f"    return {index}", "")
    ]
    second_line = len(first) + 2 + len(stable)
    second = ["def second():", *[f"    {line}" for line in large.splitlines()]]
    before = "\n".join([*first, "", *stable, *second, ""])
    moved = ["def first():", "    return 0"]
    after = "\n".join([*stable, *second, "", *moved, ""])
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-cross-hunk-move-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(
            project,
            file_name,
            first_line=1,
            second_line=second_line,
        )
        baseline = _freeze_clone_contract(config)
        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["finding_present"] is False, current
        assert current["target_patch_identity_ok"] is False, current
        assert any(
            item.get("reason") == "clone_endpoint_reanchor_not_same_unique_hunk"
            for item in current["target_patch_identity_failures"]
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["reason"] == "SEMANTIC_CONTRACT_REGRESSION", delta


def _assert_checkpoint_context_reused() -> None:
    large = _python_body(calls=7)
    before, second_line = _python_owner_source(
        "A",
        "def first(self):",
        large,
        large,
    )
    after, _ = _python_owner_source(
        "A",
        "def first(self, value=0):",
        "return value",
        large,
    )
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-checkpoint-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(
            project,
            file_name,
            first_line=2,
            second_line=second_line,
        )
        baseline = _freeze_clone_contract(config)
        assert (
            config.finding_contract["baseline_target_anchors"][0]
            ["declaration_identity"]["owner_qualified_name"]
            == "A"
        ), config.finding_contract

        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["target_patch_identity_ok"] is True, current
        assert current["finding_present"] is False, current
        assert (
            current["target_anchor_contract"][0]["signature_sha256"]
            != config.finding_contract["baseline_target_anchors"][0]["signature_sha256"]
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["metric_progress"] is True, delta

        ordinary = _run_code_clone_guard(
            config,
            {"type": "code_clone_type1"},
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="code_clone_type1",
                current_metrics=current,
            ),
        )
        assert ordinary["success"] is True, ordinary
        assert ordinary["details"]["detector"] == "checkpoint_current_metrics", ordinary


def _assert_owner_decoy_rejected() -> None:
    large = _python_body(calls=7)
    before, second_line = _python_owner_source(
        "A",
        "def first(self):",
        large,
        large,
    )
    after, _ = _python_owner_source(
        "B",
        "def first(self):",
        "return 0",
        large,
    )
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-owner-") as raw:
        project = Path(raw)
        file_name = "sample.py"
        source_path = project / file_name
        source_path.write_text(before, encoding="utf-8")
        config = _checkpoint_config(
            project,
            file_name,
            first_line=2,
            second_line=second_line,
        )
        baseline = _freeze_clone_contract(config)

        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, file_name),
        )
        assert current["finding_present"] is False, current
        assert current["target_patch_identity_ok"] is False, current
        failures = current["target_patch_identity_failures"]
        assert any(
            strict.get("reason") == "target_declaration_identity_changed"
            and strict.get("baseline_declaration_identity", {}).get(
                "owner_qualified_name"
            ) == "A"
            and strict.get("current_declaration_identity", {}).get(
                "owner_qualified_name"
            ) == "B"
            for item in failures
            for strict in list(item.get("strict_failures") or [item])
        ), current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["reason"] == "SEMANTIC_CONTRACT_REGRESSION", delta


def _assert_deleted_same_name_overrides_rejected() -> None:
    """Deleting two behavior-bearing overrides must not look like deduplication."""
    before = """class Base:
    def formfield(self, **kwargs):
        return kwargs

class PositiveBig(Base):
    def formfield(self, **kwargs):
        return super().formfield(**{"min_value": 0, **kwargs})

class PositiveSmall(Base):
    def formfield(self, **kwargs):
        return super().formfield(**{"min_value": 0, **kwargs})
"""
    after = """class Base:
    def formfield(self, **kwargs):
        return kwargs

class PositiveBig(Base):
    pass

class PositiveSmall(Base):
    pass
"""
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-overrides-") as raw:
        project = Path(raw)
        source_path = project / "sample.py"
        source_path.write_text(before, encoding="utf-8")
        config = SimpleNamespace(
            language="python",
            smell="code_clone_type1",
            project_root=project,
            finding_contract={},
            target_context={},
            locations=[
                parse_location_descriptor(
                    "sample.py:method=formfield(self, **kwargs)|line=6",
                    project,
                ),
                parse_location_descriptor(
                    "sample.py:method=formfield(self, **kwargs)|line=10",
                    project,
                ),
            ],
        )
        baseline = _freeze_clone_contract(config)
        source_path.write_text(after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch(before, after, "sample.py"),
        )
        assert current["finding_present"] is False, current
        assert current["target_patch_identity_ok"] is False, current
        changed_owners = {
            str(strict.get("baseline_declaration_identity", {}).get(
                "owner_qualified_name"
            ))
            for item in current["target_patch_identity_failures"]
            for strict in list(item.get("strict_failures") or [item])
            if strict.get("reason") == "target_declaration_identity_changed"
        }
        assert changed_owners == {"PositiveBig", "PositiveSmall"}, current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["metric_progress"] is False, delta
        assert delta["reason"] == "SEMANTIC_CONTRACT_REGRESSION", delta


def _assert_unfrozen_third_override_deletion_rejected() -> None:
    """Only a removed complete third occurrence closes the local edit gate."""

    shared_override = (
        "    def formfield(self, **kwargs):\n"
        "        return super().formfield("
        "**{\"min_value\": 0, **kwargs})\n"
    )
    before = (
        "class Base:\n"
        "    def formfield(self, **kwargs):\n"
        "        return kwargs\n\n"
        "class PositiveBig(Base):\n"
        f"{shared_override}\n"
        "class PositiveInteger(Base):\n"
        f"{shared_override}\n"
        "class PositiveSmall(Base):\n"
        f"{shared_override}"
    )

    def refactored(*, delete_third: bool) -> str:
        third = (
            "    pass\n"
            if delete_third
            else shared_override
        )
        return (
            "class Base:\n"
            "    def formfield(self, **kwargs):\n"
            "        return kwargs\n\n"
            "class PositiveBig(Base):\n"
            "    def formfield(self, **kwargs):\n"
            "        return self._positive_formfield(**kwargs)\n\n"
            "class PositiveInteger(Base):\n"
            f"{shared_override}\n"
            "class PositiveSmall(Base):\n"
            f"{third}"
        )

    lines = before.splitlines()
    target_lines = [
        lines.index(f"class {owner}(Base):") + 2
        for owner in ("PositiveBig", "PositiveInteger")
    ]
    third_override_line = (
        lines.index("class PositiveSmall(Base):") + 2
    )
    for scenario, delete_third, remove_witness in (
        ("deleted_third", True, False),
        ("untouched_third", False, False),
        ("missing_complete_witness", False, True),
    ):
        with tempfile.TemporaryDirectory(
            prefix="nonjava-clone-third-override-"
        ) as raw:
            project = Path(raw)
            source_path = project / "fields.py"
            source_path.write_text(before, encoding="utf-8")
            config = SimpleNamespace(
                language="python",
                smell="code_clone_type1",
                project_root=project,
                finding_contract={},
                target_context={},
                locations=[
                    parse_location_descriptor(
                        "fields.py:method=formfield(self, **kwargs)"
                        f"|line={line}",
                        project,
                    )
                    for line in target_lines
                ],
            )
            baseline = _freeze_clone_contract(config)
            if remove_witness:
                config.finding_contract["baseline_target_anchors"][0].pop(
                    "complete_first_token_sha256"
                )
            after = refactored(delete_third=delete_third)
            source_path.write_text(after, encoding="utf-8")
            current = capture_metric_snapshot(
                config,
                "",
                changed_patch=_patch(before, after, "fields.py"),
            )
            closure = current.get("clone_related_occurrence_closure") or {}
            assert current["finding_present"] is False, current
            delta = evaluate_checkpoint_contract(
                baseline,
                current,
                has_production_diff=True,
                smell="code_clone_type1",
            ).to_dict()
            ordinary = _run_code_clone_guard(
                config,
                {"type": "code_clone_type1"},
                GuardRunContext(
                    checkpoint_required=True,
                    checkpoint_smell="code_clone_type1",
                    current_metrics=current,
                ),
            )
            if scenario == "deleted_third":
                assert current["target_patch_identity_ok"] is True, current
                assert closure.get("ok") is False, current
                assert closure.get("reason") == (
                    "unfrozen_related_clone_occurrence_deleted"
                ), current
                assert closure.get(
                    "unfrozen_removed_occurrence_count"
                ) == 1, current
                assert closure["unfrozen_removed_occurrences"][0][
                    "frozen_target_indexes"
                ] == [], current
                assert closure["unfrozen_removed_occurrences"][0][
                    "start_line"
                ] == third_override_line, current
                assert any(
                    item.get("code")
                    == "UNFROZEN_RELATED_CLONE_OCCURRENCE_DELETED"
                    for item in current.get("guard_violations") or []
                ), current
                assert delta["metric_progress"] is False, delta
                assert delta["reason"] == (
                    "SEMANTIC_CONTRACT_REGRESSION"
                ), delta
                assert ordinary["success"] is False, ordinary
            elif scenario == "untouched_third":
                assert current["target_patch_identity_ok"] is True, current
                assert closure.get("ok") is True, current
                assert closure.get("unfrozen_removed_occurrence_count") == 0
                assert not current.get("guard_violations"), current
                assert delta["metric_progress"] is True, delta
                assert ordinary["success"] is True, ordinary
            else:
                assert current["target_patch_identity_ok"] is False, current
                assert closure.get("ok") is False, current
                assert closure.get("reason") == (
                    "baseline_complete_declaration_witness_invalid"
                ), current
                assert any(
                    item.get("code")
                    == "CLONE_RELATED_OCCURRENCE_CLOSURE_UNAVAILABLE"
                    for item in current.get("guard_violations") or []
                ), current
                assert delta["metric_progress"] is False, delta
                assert ordinary["success"] is False, ordinary


def _assert_parse_failure_closed() -> None:
    fixtures = {
        "python": (
            ".py",
            "def first():\n    return 1\n",
            "def second():\n    return ]\n",
        ),
        "c": (
            ".c",
            "void first(void) { return; }\n",
            "void second(void) { return ] ; }\n",
        ),
        "cpp": (
            ".cpp",
            "void first() { return; }\n",
            "void second() { return ] ; }\n",
        ),
    }
    for language, (suffix, left_source, right_source) in fixtures.items():
        with tempfile.TemporaryDirectory(
            prefix=f"nonjava-clone-parse-{language}-"
        ) as raw:
            project = Path(raw)
            left_name = f"left{suffix}"
            right_name = f"right{suffix}"
            (project / left_name).write_text(left_source, encoding="utf-8")
            (project / right_name).write_text(right_source, encoding="utf-8")
            config = SimpleNamespace(
                language=language,
                smell="code_clone_type1",
                project_root=project,
                finding_contract={},
                target_context={},
                locations=[
                    parse_location_descriptor(
                        f"{left_name}:method=first|line=1",
                        project,
                    ),
                    parse_location_descriptor(
                        f"{right_name}:method=second|line=1",
                        project,
                    ),
                ],
            )
            snapshot = capture_metric_snapshot(config, "")
            assert snapshot["ok"] is False, snapshot
            assert snapshot["error"] == "TARGET_SOURCE_NOT_PARSEABLE", snapshot
            parse_files = snapshot["source_file_parseability"]["files"]
            assert len(parse_files) == 2, snapshot
            assert parse_files[0]["parseable"] is True, snapshot
            assert parse_files[1]["parseable"] is False, snapshot

            ordinary = _run_code_clone_guard(
                config,
                {"type": "code_clone_type1"},
            )
            assert ordinary["success"] is False, ordinary
            assert ordinary["details"]["target_resolution"] == (
                "source_not_parseable"
            ), ordinary


def _assert_exact_shared_declaration_consolidation() -> None:
    body = _cpp_body(calls=8)
    changed_body = "\n".join([*body.splitlines()[:-1], "b();"])

    def source(function_body: str, name: str = "shared") -> str:
        return "\n".join([
            f"void {name}(void) {{",
            *[f"  {line}" for line in function_body.splitlines()],
            "}",
            "",
        ])

    for replacement_body, replacement_name, expected in (
        (body, "shared", True),
        (changed_body, "shared", False),
        (body, "renamed", False),
    ):
        with tempfile.TemporaryDirectory(
            prefix="nonjava-clone-consolidation-"
        ) as raw:
            project = Path(raw)
            before = source(body)
            left = project / "left.c"
            right = project / "right.c"
            common = project / "common.c"
            left.write_text(before, encoding="utf-8")
            right.write_text(before, encoding="utf-8")
            common.write_text("", encoding="utf-8")
            config = SimpleNamespace(
                language="c",
                smell="code_clone_type1",
                project_root=project,
                finding_contract={},
                target_context={},
                locations=[
                    parse_location_descriptor(
                        "left.c:method=shared|line=1",
                        project,
                    ),
                    parse_location_descriptor(
                        "right.c:method=shared|line=1",
                        project,
                    ),
                ],
            )
            baseline = _freeze_clone_contract(config)
            left.write_text("", encoding="utf-8")
            right.write_text("", encoding="utf-8")
            common_after = source(replacement_body, replacement_name)
            common.write_text(common_after, encoding="utf-8")
            current = capture_metric_snapshot(
                config,
                "",
                changed_patch=_patch_many([
                    ("left.c", before, ""),
                    ("right.c", before, ""),
                    ("common.c", "", common_after),
                ]),
            )
            consolidation = current.get("clone_consolidation") or {}
            assert consolidation.get("ok") is expected, current
            assert (
                current.get("target_absence_allowed") is True
            ) is expected, current
            delta = evaluate_checkpoint_contract(
                baseline,
                current,
                has_production_diff=True,
                smell="code_clone_type1",
            ).to_dict()
            ordinary = _run_code_clone_guard(
                config,
                {"type": "code_clone_type1"},
                GuardRunContext(
                    checkpoint_required=True,
                    checkpoint_smell="code_clone_type1",
                    current_metrics=current,
                ),
            )
            assert delta["metric_progress"] is expected, delta
            assert ordinary["success"] is expected, ordinary
            if expected:
                assert consolidation["implementation_count"] == 1, current
                assert len(consolidation["relocated_declarations"]) == 1, current
            else:
                assert current["target_patch_identity_ok"] is False, current


def _assert_partial_declaration_deletion_rejected() -> None:
    """Deleting only the declaration anchor must not authorize consolidation."""

    body = "\n".join(["    a()"] * 8 + ["    return 1"])
    before = f"MARKER = 0\n\ndef shared():\n{body}\n"
    # The old body remains live under ``if True``.  Only the frozen ``def``
    # line is removed, separated from the earlier replacement by context.
    after = f"if True:\n\n{body}\n"
    common_after = f"def shared():\n{body}\n"
    with tempfile.TemporaryDirectory(
        prefix="nonjava-clone-partial-deletion-"
    ) as raw:
        project = Path(raw)
        for file_name in ("left.py", "right.py"):
            (project / file_name).write_text(before, encoding="utf-8")
        (project / "common.py").write_text("", encoding="utf-8")
        config = SimpleNamespace(
            language="python",
            smell="code_clone_type1",
            project_root=project,
            finding_contract={},
            target_context={},
            locations=[
                parse_location_descriptor(
                    "left.py:method=shared|line=3",
                    project,
                ),
                parse_location_descriptor(
                    "right.py:method=shared|line=3",
                    project,
                ),
            ],
        )
        baseline = _freeze_clone_contract(config)
        for file_name in ("left.py", "right.py"):
            (project / file_name).write_text(after, encoding="utf-8")
        (project / "common.py").write_text(common_after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch_many([
                ("left.py", before, after),
                ("right.py", before, after),
                ("common.py", "", common_after),
            ]),
        )
        consolidation = current.get("clone_consolidation") or {}
        assert consolidation.get("ok") is False, current
        assert consolidation.get("reason") == (
            "clone_endpoint_deletion_unverified"
        ), current
        assert any(
            item.get("reason") == "baseline_declaration_not_exactly_deleted"
            for item in consolidation.get("failures") or []
        ), current
        assert current.get("target_absence_allowed") is not True, current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["metric_progress"] is False, delta


def _assert_nginx_style_complete_deletion_accepted() -> None:
    """Accept the replay route: two complete C definitions become one."""

    declaration = """ngx_int_t
ngx_file_aio_init(ngx_file_t *file, ngx_pool_t *pool)
{
    ngx_event_aio_t  *aio;

    aio = ngx_pcalloc(pool, sizeof(ngx_event_aio_t));
    if (aio == NULL) {
        return NGX_ERROR;
    }

    aio->file = file;
    aio->fd = file->fd;
    aio->event.data = aio;
    aio->event.ready = 1;
    aio->event.log = file->log;
    file->aio = aio;

    return NGX_OK;
}
"""
    with tempfile.TemporaryDirectory(prefix="nonjava-clone-nginx-style-") as raw:
        project = Path(raw)
        for file_name in ("ngx_file_aio_read.c", "ngx_linux_aio_read.c"):
            (project / file_name).write_text(declaration, encoding="utf-8")
        (project / "ngx_files.c").write_text("", encoding="utf-8")
        method = "ngx_file_aio_init(ngx_file_t *file, ngx_pool_t *pool)"
        config = SimpleNamespace(
            language="c",
            smell="code_clone_type1",
            project_root=project,
            finding_contract={},
            target_context={},
            locations=[
                parse_location_descriptor(
                    f"ngx_file_aio_read.c:method={method}|line=1",
                    project,
                ),
                parse_location_descriptor(
                    f"ngx_linux_aio_read.c:method={method}|line=1",
                    project,
                ),
            ],
        )
        baseline = _freeze_clone_contract(config)
        for file_name in ("ngx_file_aio_read.c", "ngx_linux_aio_read.c"):
            (project / file_name).write_text("", encoding="utf-8")
        (project / "ngx_files.c").write_text(declaration, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch_many([
                ("ngx_file_aio_read.c", declaration, ""),
                ("ngx_linux_aio_read.c", declaration, ""),
                ("ngx_files.c", "", declaration),
            ]),
        )
        consolidation = current.get("clone_consolidation") or {}
        assert consolidation.get("ok") is True, current
        assert consolidation.get("implementation_count") == 1, current
        assert consolidation.get("deletion_contract") == (
            "target-old-anchor-exact-declaration-deletion-v2"
        ), current
        assert current.get("target_absence_allowed") is True, current
        delta = evaluate_checkpoint_contract(
            baseline,
            current,
            has_production_diff=True,
            smell="code_clone_type1",
        ).to_dict()
        assert delta["metric_progress"] is True, delta


def _assert_decorator_reassignment_rejected() -> None:
    """A Python decorator is part of the frozen complete declaration."""

    body = "\n".join(["    a()"] * 8 + ["    return 1"])
    before = (
        "def deco(fn):\n"
        "    return fn\n\n"
        "@deco\n"
        f"def shared():\n{body}\n\n"
        "def other():\n"
        "    return 0\n"
    )
    # Removing only shared makes the unchanged decorator attach to ``other``.
    after = (
        "def deco(fn):\n"
        "    return fn\n\n"
        "@deco\n"
        "def other():\n"
        "    return 0\n"
    )
    common_after = f"@deco\ndef shared():\n{body}\n"
    with tempfile.TemporaryDirectory(
        prefix="nonjava-clone-decorator-reassignment-"
    ) as raw:
        project = Path(raw)
        for file_name in ("left.py", "right.py"):
            (project / file_name).write_text(before, encoding="utf-8")
        (project / "common.py").write_text("", encoding="utf-8")
        config = SimpleNamespace(
            language="python",
            smell="code_clone_type1",
            project_root=project,
            finding_contract={},
            target_context={},
            locations=[
                parse_location_descriptor(
                    "left.py:method=shared|line=5",
                    project,
                ),
                parse_location_descriptor(
                    "right.py:method=shared|line=5",
                    project,
                ),
            ],
        )
        _freeze_clone_contract(config)
        witness = config.finding_contract["baseline_target_anchors"][0][
            "declaration_deletion_witness"
        ]
        assert witness["start_line"] == 4, witness
        for file_name in ("left.py", "right.py"):
            (project / file_name).write_text(after, encoding="utf-8")
        (project / "common.py").write_text(common_after, encoding="utf-8")
        current = capture_metric_snapshot(
            config,
            "",
            changed_patch=_patch_many([
                ("left.py", before, after),
                ("right.py", before, after),
                ("common.py", "", common_after),
            ]),
        )
        consolidation = current.get("clone_consolidation") or {}
        assert consolidation.get("ok") is False, current
        assert consolidation.get("reason") == (
            "clone_endpoint_deletion_unverified"
        ), current
        assert current.get("target_absence_allowed") is not True, current


def main() -> int:
    cpp_before = _cpp_body(calls=38, add=True, bare_return=True)
    cpp_after = _cpp_body(add=True, bare_return=True)
    assert clone_normalized_token_score(cpp_before, cpp_before, "cpp")[2] == 159
    assert clone_normalized_token_score(cpp_after, cpp_after, "cpp")[2] == 7
    _assert_case("cpp", cpp_after, 7, True)

    _assert_case("cpp", _cpp_body(calls=8, add=True), 37, False)
    _assert_case("cpp", _cpp_body(calls=5, add=True), 25, False)
    _assert_case("cpp", _cpp_body(calls=6), 24, True)

    _assert_case("c", _cpp_body(calls=4, bare_return=True), 18, False)
    _assert_case("c", _cpp_body(calls=3, add=True), 17, True)

    python_21 = _python_body(calls=7)
    _assert_case("python", python_21, 21, False)
    _assert_case(
        "python",
        "\n".join([*["a()"] * 5, "return;"]),
        17,
        False,
    )
    _assert_case(
        "python",
        _python_body(calls=5, bare_return=True),
        16,
        True,
    )
    _assert_target_resolution_contract()
    _assert_same_name_decoy_rejected()
    _assert_frozen_patch_anchor_beats_nearest_same_name()
    _assert_same_hunk_retained_endpoint_move_accepted()
    _assert_cross_hunk_retained_endpoint_move_rejected()
    _assert_repository_diff_context_is_frozen()
    _assert_checkpoint_context_reused()
    _assert_owner_decoy_rejected()
    _assert_deleted_same_name_overrides_rejected()
    _assert_unfrozen_third_override_deletion_rejected()
    _assert_parse_failure_closed()
    _assert_exact_shared_declaration_consolidation()
    _assert_partial_declaration_deletion_rejected()
    _assert_nginx_style_complete_deletion_accepted()
    _assert_decorator_reassignment_rejected()

    print(
        "non-Java clone guard self-check passed: "
        "cpp=159->7_pass,37_fail,25_fail,24_pass "
        "c=18_fail,17_pass "
        "python=21_fail,17_fail,16_pass partial_missing=fail_closed "
        "same_name_decoy=rejected frozen_patch_anchor=selected "
        "same_hunk_endpoint_move=accepted cross_hunk_move=rejected diff_context=frozen "
        "mapped_signature_change=accepted "
        "owner_decoy=rejected deleted_overrides=rejected parse_errors=fail_closed "
        "third_override_deletion=rejected untouched_third=accepted "
        "missing_complete_witness=fail_closed "
        "checkpoint_context=reused exact_shared_consolidation=accepted "
        "changed_shared_body=rejected renamed_shared_body=rejected "
        "partial_declaration_deletion=rejected nginx_style_move=accepted "
        "decorator_reassignment=rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
