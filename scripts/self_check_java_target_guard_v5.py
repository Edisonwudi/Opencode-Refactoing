#!/usr/bin/env python3
"""End-to-end contract check for the Java target-only Guard v5 path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core import checkpoint_adapters  # noqa: E402
from smell_core.checkpoint_adapters import CHECKPOINT_SMELLS, detector_profile_for  # noqa: E402
from smell_core.checkpoints import _require_current_checkpoint_versions  # noqa: E402
from smell_core.config import ResolvedRunConfig  # noqa: E402
from smell_core.guard_scope import GuardVerificationScope  # noqa: E402
from smell_core.java.target_guard import (  # noqa: E402
    capture_java_target_guard,
    evaluate_java_target_guard,
)
from smell_core.location import parse_location_descriptor  # noqa: E402
from bridge.smell_bridge import (  # noqa: E402
    GUARD_EVIDENCE_MAX_BYTES,
    _write_guard_evidence_artifact,
)


def _long_method(statements: int) -> str:
    body = "\n".join(f"    consume({index});" for index in range(statements))
    return (
        "class Fixture {\n"
        "  void target() {\n"
        f"{body}\n"
        "  }\n"
        "  void consume(int value) {}\n"
        "}\n"
    )


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _bridge(
    project: Path,
    env: dict[str, str],
    command: str,
) -> tuple[dict[str, object], str]:
    result = _run(
        [
            sys.executable,
            str(BRIDGE),
            command,
            "--output-detail",
            "decision",
            "--project-root",
            str(project),
            "--language",
            "java",
            "--smell",
            "long_method",
            "--location",
            "Fixture.java:method=target|line=2",
            "--projects",
            str(project / "projects.yaml"),
            "--verification-mode",
            "project_full",
        ],
        ROOT,
        env,
    )
    if result.returncode != 0:
        raise AssertionError(f"{command} failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout), result.stdout


def _profile_matrix() -> None:
    dummy = object.__new__(ResolvedRunConfig)
    dummy.language = "java"
    for smell in sorted(CHECKPOINT_SMELLS):
        dummy.smell = smell
        profile = detector_profile_for(dummy)
        assert profile["id"] == f"java-target-guard/{smell}/v5", profile
        assert profile["schema"] == 5, profile
        assert profile["source_discovery"] == "forbidden", profile
        assert profile["smell_discovery"] == "forbidden", profile
        if smell == "data_clumps":
            assert profile["minimum_group_size"] == 3, profile
            assert profile["active_parse_file_limit"] == 1, profile
            assert "scope_file_limit" not in profile, profile
            assert "scope_byte_limit" not in profile, profile


def _feature_envy_dispatch() -> None:
    before = """\
class Collaborator { void a() {} void b() {} void c() {} void d() {} }
class Subject extends MissingBase {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) {
    RECEIVER.a(); RECEIVER.b(); RECEIVER.c(); RECEIVER.d();
  }
}
"""
    after = """\
class Collaborator { void a() {} void b() {} void c() {} void d() {} }
class Subject extends MissingBase {
  static final Collaborator RECEIVER = new Collaborator();
  void target(String value) { RECEIVER.a(); }
}
"""
    changed_diff = """\
class Collaborator { void a() {} void b() {} void c() {} void d() {} }
class Alternative { void w() {} void x() {} void y() {} void z() {} }
class Subject extends MissingBase {
  static final Collaborator RECEIVER = new Collaborator();
  static final Alternative ALTERNATE = new Alternative();
  void target(String value) { RECEIVER.a(); }
  void unrelatedFlow(long sequence) {
    ALTERNATE.w(); ALTERNATE.x(); ALTERNATE.y(); ALTERNATE.z();
  }
}
"""
    with tempfile.TemporaryDirectory(prefix="feature-envy-v5-dispatch-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(before, encoding="utf-8")
        location = parse_location_descriptor(
            "Fixture.java:method=target|line=4", project
        )
        scope = GuardVerificationScope(
            changed_files=(),
            changed_production_files=(),
            target_files=("Fixture.java",),
            analysis_files=("Fixture.java",),
        )
        config = type("GuardConfig", (), {})()
        config.project_root = project
        config.language = "java"
        config.smell = "feature_envy"
        config.locations = [location]
        config.target_context = {}
        config.guard_contract = {}
        config.guard_scope = scope
        captured = checkpoint_adapters.capture_metric_snapshot(
            config,
            "score=999;finding_present=false",
        )
        assert captured["ok"] is True and captured["target_smell_present"] is True, captured
        assert (
            captured["witness"][0]["relation_scope"]["relation_state"]
            == "target_only_sufficient"
        ), captured
        source.write_text(after, encoding="utf-8")
        config.guard_contract = {
            "entity_identity": captured["entity_identity"],
            "witness": captured["witness"],
        }
        evaluated = checkpoint_adapters.capture_metric_snapshot(config, "score=0")
        assert evaluated["ok"] is True, evaluated
        assert evaluated["target_smell_present"] is False, evaluated
        assert not evaluated["guard_violations"], evaluated

        source.write_text(changed_diff, encoding="utf-8")
        config.guard_scope = GuardVerificationScope(
            changed_files=("Fixture.java",),
            changed_production_files=("Fixture.java",),
            target_files=("Fixture.java",),
            analysis_files=("Fixture.java",),
            changed_line_ranges=(("Fixture.java", 7, 9),),
        )
        changed = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert any(
            str(item).startswith("FEATURE_ENVY_RELOCATED:")
            and "#Subject#unrelatedFlow(long)" in str(item)
            for item in changed["guard_violations"]
        ), changed


def _guard_evidence_hard_limit() -> None:
    with tempfile.TemporaryDirectory(prefix="guard-evidence-limit-") as temp_dir:
        path = Path(temp_dir) / "guard-evidence.json"
        try:
            _write_guard_evidence_artifact(
                path,
                {"payload": "x" * GUARD_EVIDENCE_MAX_BYTES},
            )
        except ValueError as exc:
            assert "GUARD_EVIDENCE_TOO_LARGE" in str(exc), exc
        else:
            raise AssertionError("oversized Guard evidence was written")
        assert not path.exists()


def _changed_noise_is_not_an_eager_parse_scope() -> None:
    """Prove Git-diff metadata does not become a common Java AST scope."""
    from smell_core.java import target_relational_guards, target_semantic_guards

    with tempfile.TemporaryDirectory(prefix="guard-v5-changed-noise-") as temp_dir:
        project = Path(temp_dir)
        (project / "Target.java").write_text(
            "class Target { void target(int a, int b, int c, int d, int e, int f) {} }\n",
            encoding="utf-8",
        )
        noise = []
        for index in range(50):
            relative = f"Noise{index:02d}.java"
            (project / relative).write_text(
                f"class Noise{index:02d} {{ void unrelated() {{}} }}\n",
                encoding="utf-8",
            )
            noise.append(relative)
        changed = tuple(sorted(noise))
        location = parse_location_descriptor(
            "Target.java:method=target(int,int,int,int,int,int)|line=1",
            project,
        )
        config = type("GuardConfig", (), {})()
        config.project_root = project
        config.language = "java"
        config.locations = [location]
        config.target_context = {}
        config.guard_scope = GuardVerificationScope(
            changed_files=changed,
            changed_production_files=changed,
            target_files=("Target.java",),
            analysis_files=("Target.java",),
        )

        semantic_scopes: list[tuple[str, ...]] = []
        original_semantic_capture = (
            target_semantic_guards.capture_target_semantic_guard
        )

        def fake_semantic_capture(
            _smell: str,
            _root: Path,
            _location: str,
            _selector: object,
            analysis_files: object,
            _classpath: str = "",
        ) -> dict[str, object]:
            semantic_scopes.append(tuple(sorted(str(item) for item in analysis_files)))
            return {
                "ok": True,
                "target_match_count": 1,
                "target_smell_present": True,
                "target_missing": False,
                "objectives": {"loc": 1},
                "entity_identity": {"smell": "god_class", "class": "Target"},
                "witness": [],
                "guard_violations": [],
            }

        target_semantic_guards.capture_target_semantic_guard = fake_semantic_capture
        try:
            config.smell = "god_class"
            config.guard_contract = {}
            captured = capture_java_target_guard(config)
        finally:
            target_semantic_guards.capture_target_semantic_guard = (
                original_semantic_capture
            )
        assert captured["ok"] is True, captured
        assert semantic_scopes == [("Target.java",)], semantic_scopes

        relational_scopes: list[tuple[str, ...]] = []
        original_lpl = target_relational_guards.evaluate_long_parameter_list_guard

        def fake_lpl(
            _root: Path,
            _location: object,
            _selector: object,
            *,
            analysis_files: object = (),
        ) -> dict[str, object]:
            relational_scopes.append(
                tuple(sorted(str(item) for item in analysis_files))
            )
            return {
                "ok": True,
                "target_match_count": 0,
                "target_smell_present": False,
                "target_missing": True,
                "objectives": {"parameter_count": 1},
                "entity_identity": {
                    "file": "Target.java",
                    "class": "Target",
                    "method": "target",
                    "parameter_types": ["int"] * 6,
                },
                "witness": {},
                "guard_violations": [],
            }

        target_relational_guards.evaluate_long_parameter_list_guard = fake_lpl
        try:
            config.smell = "long_parameter_list"
            config.guard_contract = {
                "entity_identity": {
                    "file": "Target.java",
                    "class": "Target",
                    "method": "target",
                    "parameter_types": ["int"] * 6,
                }
            }
            evaluated = evaluate_java_target_guard(config)
        finally:
            target_relational_guards.evaluate_long_parameter_list_guard = original_lpl
        assert evaluated["ok"] is True, evaluated
        assert relational_scopes == [("Target.java",)], relational_scopes

        # Feature Envy and Clone intentionally inspect changed methods, but
        # must fail closed before AST construction when that smell-specific
        # scope exceeds the common resource budget.
        config.smell = "feature_envy"
        config.guard_contract = {
            "entity_identity": {
                "smell": "feature_envy",
                "file": "Target.java",
                "class": "Target",
                "method": "target(int,int,int,int,int,int)",
            }
        }
        oversized = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert oversized["ok"] is False, oversized
        assert oversized["error"] == "GUARD_SCOPE_TOO_LARGE", oversized


def _refused_bequest_cross_file_dispatch() -> None:
    before = """\
class Subject extends AbstractCapability {
  public Object target() { throw new UnsupportedOperationException(); }
}
"""
    after = """\
class Subject extends AbstractCapability {
  public Object target() { return new Object(); }
}
"""
    with tempfile.TemporaryDirectory(prefix="refused-cross-file-v5-") as temp_dir:
        project = Path(temp_dir)
        subject = project / "Subject.java"
        subject.write_text(before, encoding="utf-8")
        (project / "AbstractCapability.java").write_text(
            "abstract class AbstractCapability implements Capability {}\n",
            encoding="utf-8",
        )
        (project / "Capability.java").write_text(
            "interface Capability { Object target(); }\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        for args in (["git", "init", "-q"], ["git", "add", "."]):
            result = _run(list(args), project, env)
            assert result.returncode == 0, result.stderr
        result = _run(
            [
                "git",
                "-c",
                "user.name=guard-v5-self-check",
                "-c",
                "user.email=guard-v5@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            project,
            env,
        )
        assert result.returncode == 0, result.stderr

        location = parse_location_descriptor(
            "Subject.java:method=target()|line=2", project
        )
        config = type("GuardConfig", (), {})()
        config.project_root = project
        config.language = "java"
        config.smell = "refused_bequest"
        config.locations = [location]
        # No dataset/CSV hierarchy anchor is needed: the relation scope starts
        # at the caller-supplied target and follows source inheritance.
        config.target_context = {}
        config.guard_contract = {}
        config.guard_scope = GuardVerificationScope(
            changed_files=(),
            changed_production_files=(),
            target_files=("Subject.java",),
            analysis_files=("Subject.java",),
        )
        captured = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert captured["ok"] is True, captured
        assert captured["target_smell_present"] is True, captured
        assert captured["entity_identity"]["parent"] == "Capability", captured
        relation_scope = captured["witness"][0]["relation_scope"]
        assert relation_scope["relation_state"] == "expanded", relation_scope
        assert set(relation_scope["scope_files"]) == {
            "AbstractCapability.java",
            "Capability.java",
            "Subject.java",
        }, relation_scope

        subject.write_text(after, encoding="utf-8")
        config.guard_contract = {
            "entity_identity": captured["entity_identity"],
            "witness": captured["witness"],
        }
        config.guard_scope = GuardVerificationScope(
            changed_files=("Subject.java",),
            changed_production_files=("Subject.java",),
            target_files=("Subject.java",),
            analysis_files=("Subject.java",),
            changed_line_ranges=(("Subject.java", 2, 2),),
        )
        evaluated = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert evaluated["ok"] is True, evaluated
        assert evaluated["target_smell_present"] is False, evaluated
        assert not evaluated["guard_violations"], evaluated

        subject.write_text(
            "class Subject { public Object target() { return new Object(); } }\n",
            encoding="utf-8",
        )
        relation_removed = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert relation_removed["ok"] is True, relation_removed
        assert relation_removed["target_smell_present"] is False, relation_removed
        assert not relation_removed["guard_violations"], relation_removed


def _refused_bequest_target_first_dispatch() -> None:
    before = """\
interface Capability { Object target(); }
class Subject extends MissingBase implements Capability {
  public Object target() { throw new UnsupportedOperationException(); }
}
"""
    after = """\
interface Capability { Object target(); }
class Subject extends MissingBase implements Capability {
  public Object target() { return new Object(); }
}
"""
    with tempfile.TemporaryDirectory(prefix="refused-target-first-v5-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Subject.java"
        source.write_text(before, encoding="utf-8")
        env = os.environ.copy()
        for args in (["git", "init", "-q"], ["git", "add", "."]):
            result = _run(list(args), project, env)
            assert result.returncode == 0, result.stderr
        result = _run(
            [
                "git",
                "-c",
                "user.name=guard-v5-self-check",
                "-c",
                "user.email=guard-v5@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            project,
            env,
        )
        assert result.returncode == 0, result.stderr

        config = type("GuardConfig", (), {})()
        config.project_root = project
        config.language = "java"
        config.smell = "refused_bequest"
        config.locations = [
            parse_location_descriptor(
                "Subject.java:method=target()|line=3",
                project,
            )
        ]
        config.target_context = {}
        config.guard_contract = {}
        config.guard_scope = GuardVerificationScope(
            changed_files=(),
            changed_production_files=(),
            target_files=("Subject.java",),
            analysis_files=("Subject.java",),
        )
        captured = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert captured["ok"] is True, captured
        assert captured["target_smell_present"] is True, captured
        assert captured["entity_identity"]["parent"] == "Capability", captured
        assert (
            captured["witness"][0]["relation_scope"]["relation_state"]
            == "target_only_sufficient"
        ), captured

        source.write_text(after, encoding="utf-8")
        config.guard_contract = {
            "entity_identity": captured["entity_identity"],
            "witness": captured["witness"],
        }
        config.guard_scope = GuardVerificationScope(
            changed_files=("Subject.java",),
            changed_production_files=("Subject.java",),
            target_files=("Subject.java",),
            analysis_files=("Subject.java",),
            changed_line_ranges=(("Subject.java", 3, 3),),
        )
        evaluated = checkpoint_adapters.capture_metric_snapshot(config, "")
        assert evaluated["ok"] is True, evaluated
        assert evaluated["target_smell_present"] is False, evaluated
        assert not evaluated["guard_violations"], evaluated


def main() -> int:
    try:
        _require_current_checkpoint_versions(
            {"schema_version": 5, "contract_version": 4}
        )
    except ValueError as exc:
        assert "checkpoint contract v5" in str(exc), exc
    else:
        raise AssertionError("checkpoint contract v4 did not require recapture")

    _profile_matrix()
    _feature_envy_dispatch()
    _guard_evidence_hard_limit()
    _changed_noise_is_not_an_eager_parse_scope()
    _refused_bequest_cross_file_dispatch()
    _refused_bequest_target_first_dispatch()
    with tempfile.TemporaryDirectory(prefix="java-target-guard-v5-") as temp_dir:
        project = Path(temp_dir)
        source = project / "Fixture.java"
        source.write_text(_long_method(65), encoding="utf-8")
        (project / "projects.yaml").write_text(
            "projects:\n"
            f"- root: {json.dumps(str(project))}\n"
            "  language: java\n"
            "  build:\n"
            "    command: \"true\"\n"
            "  test:\n"
            "    command: \"true\"\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "runtime" / "python")
        for args in (["git", "init", "-q"], ["git", "add", "."]):
            result = _run(list(args), project, env)
            assert result.returncode == 0, result.stderr
        result = _run(
            [
                "git",
                "-c",
                "user.name=guard-v5-self-check",
                "-c",
                "user.email=guard-v5@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            project,
            env,
        )
        assert result.returncode == 0, result.stderr

        baseline, baseline_stdout = _bridge(project, env, "capture-baseline")
        assert baseline["status"] == "BASELINE_CAPTURED", baseline
        assert "guard_contract" in baseline and "finding_contract" not in baseline, baseline
        assert len(baseline_stdout.encode("utf-8")) < 64 * 1024
        manifest_path = Path(baseline["artifacts"]["baseline_manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 5, manifest
        assert manifest["contract_version"] == 5, manifest
        assert "guard_contract" in manifest and "finding_contract" not in manifest
        assert "baseline_finding_catalog" not in json.dumps(manifest)
        assert "baseline_occurrence_contract" not in json.dumps(manifest)

        env["SMELL_BASELINE_SEAL"] = str(baseline["baseline_seal"])
        unchanged, _ = _bridge(project, env, "verify")
        assert unchanged["status"] == "SMELL_GUARD_FAILED", unchanged
        assert unchanged["checkpoint"]["delta"]["reason"] == "EDIT_REQUIRED", unchanged

        source.write_text(_long_method(2), encoding="utf-8")
        repaired, repaired_stdout = _bridge(project, env, "verify")
        assert repaired["status"] == "PASS", repaired
        assert repaired["accepted"] is True and repaired["success"] is True, repaired
        assert len(repaired_stdout.encode("utf-8")) < 64 * 1024
        artifacts = repaired["artifacts"]
        assert "guard_evidence" in artifacts, artifacts
        assert "verify_full" not in artifacts, artifacts
        evidence_path = Path(artifacts["guard_evidence"])
        assert evidence_path.stat().st_size < 2 * 1024 * 1024
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["status"] == "PASS" and evidence["accepted"] is True

    print(
        "java-target-guard-v5 self-check PASS "
        "smells=11 schema=5 contract=5 full_scan=blocked "
        "decision_lt_64KiB evidence_lt_2MiB verify_full=removed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
