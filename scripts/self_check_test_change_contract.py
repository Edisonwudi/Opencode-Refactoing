#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.test_change_contract import (  # noqa: E402
    TestChangeContractError,
    capture_test_change_contract,
    discover_java_test_source_roots,
    evaluate_test_change_contract,
    is_java_test_source_path,
    is_java_verification_config_path,
    is_standard_java_test_path,
)
from smell_core.checkpoints import _is_production_source  # noqa: E402
from smell_core.java.syntactic_detector import run_java_syntactic_detector  # noqa: E402


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _expect_error(status: str, callback) -> None:
    try:
        callback()
    except TestChangeContractError as exc:
        assert exc.status == status, (exc.status, status, exc)
    else:
        raise AssertionError(f"expected {status}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="test-change-contract-") as temporary:
        project = Path(temporary) / "project"
        project.mkdir()
        unit = _write(project, "src/test/java/p/UnitTest.java", "class UnitTest {}\n")
        _write(project, "src/test/resources/fixture.json", "{}\n")
        fixture = _write(
            project,
            "module/src/testFixtures/java/p/Fixture.java",
            "class Fixture {}\n",
        )
        integration = _write(
            project,
            "module/src/integrationTest/java/p/FlowIT.java",
            "class FlowIT {}\n",
        )
        declared = _write(
            project,
            "qa/custom/ContractSpec.java",
            "class ContractSpec {}\n",
        )
        production = _write(project, "src/main/java/p/App.java", "class App {}\n")
        pom_text = "<project/>\n"
        pom = _write(project, "pom.xml", pom_text)
        gradle_text = """plugins { id 'java' }
sourceSets {
    contractTest {
        java.srcDir 'qa/contract'
    }
}
"""
        gradle_build = _write(project, "build.gradle", gradle_text)
        gradle_custom_text = (
            "class ConfiguredGradleTest { "
            "void bad(int a, int b, int c, int d, int e, int f) {} }\n"
        )
        gradle_custom_test = _write(
            project,
            "qa/contract/p/ConfiguredGradleTest.java",
            gradle_custom_text,
        )
        _write(
            project,
            "module/pom.xml",
            "<project><build><testSourceDirectory>qa-spec</testSourceDirectory></build></project>\n",
        )
        maven_custom_test = _write(
            project,
            "module/qa-spec/p/ConfiguredMavenTest.java",
            "class ConfiguredMavenTest {}\n",
        )
        gradle_settings = _write(project, "settings.gradle.kts", 'rootProject.name = "sample"\n')
        gradle_wrapper = _write(
            project,
            "gradle/wrapper/gradle-wrapper.properties",
            "distributionUrl=https://example.invalid/gradle.zip\n",
        )
        _write(project, "gradlew", "#!/bin/sh\n")
        build_src = _write(
            project,
            "buildSrc/src/main/kotlin/VerificationPlugin.kt",
            "class VerificationPlugin\n",
        )
        _write(
            project,
            "build-logic/src/main/kotlin/TestConventions.kt",
            "class TestConventions\n",
        )
        _write(project, ".mvn/maven.config", "-DskipTests=false\n")
        _write(project, "legacy/build.xml", "<project/>\n")
        _write(project, "MODULE.bazel", 'module(name = "sample")\n')
        _write(project, "src/main/java/p/BUILD.bazel", "java_library(name = \"p\")\n")
        _write(project, "tools/testing/java_rules.bzl", "def java_test_rule(): pass\n")
        _write(project, "src/main/resources/pom.xml", "<fixture/>\n")
        _write(project, "src/main/resources/build.gradle", "fixture=true\n")
        _write(project, "target/generated/pom.xml", "<generated/>\n")
        _write(project, "buildSrc/build/generated/Generated.kt", "generated\n")

        assert is_standard_java_test_path("src/test/java/p/UnitTest.java")
        assert is_standard_java_test_path("m/src/testFixtures/resources/data.txt")
        assert is_standard_java_test_path("m/src/integrationTest/java/p/FlowIT.java")
        assert is_standard_java_test_path("app/src/androidTest/java/p/UiTest.java")
        assert not is_standard_java_test_path("src/main/java/p/test/Helper.java")
        assert not is_standard_java_test_path("build.gradle")
        discovered_roots = set(discover_java_test_source_roots(project))
        assert {"qa/contract", "module/qa-spec"}.issubset(discovered_roots), discovered_roots
        assert is_java_test_source_path(
            gradle_custom_test,
            project_root=project,
            configured_test_roots=discovered_roots,
        )
        product_detection = run_java_syntactic_detector(project)
        assert product_detection.ok, product_detection.error
        assert not any(
            item.file == "qa/contract/p/ConfiguredGradleTest.java"
            for findings in product_detection.findings.values()
            for item in findings
        ), product_detection.findings
        assert is_java_test_source_path(
            maven_custom_test,
            project_root=project,
            configured_test_roots=discovered_roots,
        )
        assert not _is_production_source(
            "qa/contract/p/ConfiguredGradleTest.java",
            "java",
            project_root=project,
            configured_test_roots=tuple(sorted(discovered_roots)),
        )
        for config_path in (
            "pom.xml",
            "module/pom.xml",
            "build.gradle",
            "module/settings.gradle.kts",
            "gradle/wrapper/gradle-wrapper.properties",
            ".mvn/maven.config",
            "buildSrc/src/main/kotlin/Plugin.kt",
            "build-logic/src/main/kotlin/Conventions.kt",
            "legacy/build.xml",
            "MODULE.bazel",
            "src/main/java/p/BUILD.bazel",
            "tools/testing/java_rules.bzl",
        ):
            assert is_java_verification_config_path(config_path), config_path
        for ordinary_path in (
            "src/main/java/p/App.java",
            "src/main/resources/pom.xml",
            "src/main/resources/build.gradle",
            "docs/pom.xml.txt",
            "target/generated/pom.xml",
        ):
            assert not is_java_verification_config_path(ordinary_path), ordinary_path
        for test_source in (
            "src/test/java/p/UnitTest.java",
            "module/src/testFixtures/java/p/Fixture.java",
            "module/src/integrationTest/java/p/FlowIT.java",
            "app/src/androidTest/java/p/UiTest.java",
            "module/src/functionalTest/java/p/ContractTest.java",
        ):
            assert not _is_production_source(test_source, "java"), test_source
        assert _is_production_source("src/main/java/p/App.java", "java")

        baseline = capture_test_change_contract(
            project,
            declared_test_files="qa/custom/ContractSpec.java",
        )
        assert baseline["mode"] == "immutable"
        assert baseline["allow_test_changes"] is False
        assert baseline["semantic_audit"]["audit_version"] == 1
        assert set(baseline["standard_test_roots"]) == {
            "src/test",
            "module/src/testFixtures",
            "module/src/integrationTest",
            "qa/contract",
            "module/qa-spec",
        }
        assert set(baseline["files"]) == {
            "src/test/java/p/UnitTest.java",
            "src/test/resources/fixture.json",
            "module/src/testFixtures/java/p/Fixture.java",
            "module/src/integrationTest/java/p/FlowIT.java",
            "module/qa-spec/p/ConfiguredMavenTest.java",
            "qa/contract/p/ConfiguredGradleTest.java",
            "qa/custom/ContractSpec.java",
        }
        expected_verification_config = {
            ".mvn/maven.config",
            "MODULE.bazel",
            "build-logic/src/main/kotlin/TestConventions.kt",
            "build.gradle",
            "buildSrc/src/main/kotlin/VerificationPlugin.kt",
            "gradle/wrapper/gradle-wrapper.properties",
            "gradlew",
            "legacy/build.xml",
            "module/pom.xml",
            "pom.xml",
            "settings.gradle.kts",
            "src/main/java/p/BUILD.bazel",
            "tools/testing/java_rules.bzl",
        }
        assert set(baseline["verification_config_files"]) == expected_verification_config
        assert "target/generated/pom.xml" not in baseline["verification_config_files"]
        assert "buildSrc/build/generated/Generated.kt" not in baseline["verification_config_files"]
        frozen_test_sources = {
            (project / relative).resolve()
            for relative in baseline["semantic_audit"]["files"]
        }
        original_read_text = Path.read_text

        def reject_frozen_source_read(path: Path, *args, **kwargs):
            if path.resolve() in frozen_test_sources:
                raise AssertionError(f"unchanged test source was reread: {path}")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", reject_frozen_source_read):
            unchanged = evaluate_test_change_contract(project, baseline).to_dict()
        assert unchanged["success"] is True
        assert unchanged["status"] == "TEST_SOURCE_UNCHANGED"
        assert unchanged["verification_config_modified"] is False

        gradle_custom_test.write_text(
            "class ConfiguredGradleTest { int changed; }\n",
            encoding="utf-8",
        )
        source_reads: list[Path] = []

        def record_source_read(path: Path, *args, **kwargs):
            resolved = path.resolve()
            if resolved in frozen_test_sources:
                source_reads.append(resolved)
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", record_source_read):
            custom_blocked = evaluate_test_change_contract(project, baseline).to_dict()
        assert custom_blocked["status"] == "TEST_SOURCE_MODIFIED", custom_blocked
        assert [item["path"] for item in custom_blocked["changed"]] == [
            "qa/contract/p/ConfiguredGradleTest.java"
        ], custom_blocked
        assert source_reads == [gradle_custom_test.resolve()], source_reads
        gradle_custom_test.write_text(gradle_custom_text, encoding="utf-8")
        print("  ok   unchanged source audits are reused; changed source is reread once")
        print("  ok   configured Maven/Gradle test source roots are frozen")

        # Ordinary production changes do not alter either verification-input
        # manifest.
        production.write_text("class App { int value; }\n", encoding="utf-8")
        outside_only = evaluate_test_change_contract(project, baseline).to_dict()
        assert outside_only["modified"] is False, outside_only
        print("  ok   production changes remain outside both verification manifests")

        # Build/test discovery configuration is immutable under every policy.
        pom.write_text("<project><profiles/></project>\n", encoding="utf-8")
        gradle_build.write_text("plugins { id 'java-library' }\n", encoding="utf-8")
        gradle_wrapper.unlink()
        build_src.write_text("class VerificationPluginChanged\n", encoding="utf-8")
        added_gradle = "plugins { id 'java' }\n"
        _write(project, "module/settings.gradle", added_gradle)
        config_blocked = evaluate_test_change_contract(project, baseline).to_dict()
        assert config_blocked["success"] is False
        assert config_blocked["status"] == "VERIFICATION_CONFIG_MODIFIED"
        assert config_blocked["reason"] == "VERIFICATION_CONFIG_MODIFIED"
        assert config_blocked["test_source_modified"] is False
        assert config_blocked["verification_config_change_count"] == 5
        assert [item["path"] for item in config_blocked["verification_config_added"]] == [
            "module/settings.gradle"
        ]
        assert config_blocked["verification_config_added"][0]["after_sha256"] == _sha(
            added_gradle
        )
        assert [item["path"] for item in config_blocked["verification_config_changed"]] == [
            "build.gradle",
            "buildSrc/src/main/kotlin/VerificationPlugin.kt",
            "pom.xml",
        ]
        assert [item["path"] for item in config_blocked["verification_config_deleted"]] == [
            "gradle/wrapper/gradle-wrapper.properties"
        ]
        assert all(
            item["before_sha256"] and item["after_sha256"]
            for item in config_blocked["verification_config_changed"]
        )
        print("  ok   pom/Gradle/wrapper/buildSrc deltas fail with complete hashes")

        # Restore config exactly before exercising the independent test-source
        # policy below.
        pom.write_text(pom_text, encoding="utf-8")
        gradle_build.write_text(gradle_text, encoding="utf-8")
        gradle_wrapper.write_text(
            "distributionUrl=https://example.invalid/gradle.zip\n",
            encoding="utf-8",
        )
        build_src.write_text("class VerificationPlugin\n", encoding="utf-8")
        (project / "module/settings.gradle").unlink()
        restored = evaluate_test_change_contract(project, baseline).to_dict()
        assert restored["success"] is True, restored
        assert restored["verification_config_modified"] is False

        unit.write_text("class UnitTest { void changed() {} }\n", encoding="utf-8")
        fixture.unlink()
        added_content = "class NewFlowIT {}\n"
        _write(project, "new-module/src/integration-test/java/NewFlowIT.java", added_content)
        declared.write_text("class ContractSpec { int changed; }\n", encoding="utf-8")
        blocked = evaluate_test_change_contract(project, baseline).to_dict()
        assert blocked["success"] is False
        assert blocked["status"] == "TEST_SOURCE_MODIFIED"
        assert blocked["reason"] == "TEST_SOURCE_MODIFIED"
        assert blocked["change_count"] == 4, blocked
        assert [item["path"] for item in blocked["added"]] == [
            "new-module/src/integration-test/java/NewFlowIT.java"
        ]
        assert blocked["added"][0]["after_sha256"] == _sha(added_content)
        assert [item["path"] for item in blocked["changed"]] == [
            "qa/custom/ContractSpec.java",
            "src/test/java/p/UnitTest.java",
        ]
        assert [item["path"] for item in blocked["deleted"]] == [
            "module/src/testFixtures/java/p/Fixture.java"
        ]
        assert all(item["before_sha256"] for item in blocked["changed"])
        assert all(item["after_sha256"] for item in blocked["changed"])
        print("  ok   default policy blocks every test-tree delta with full hashes")

        allowed_root = Path(temporary) / "allowed"
        allowed_root.mkdir()
        allowed_test = _write(
            allowed_root,
            "src/test/java/AllowedTest.java",
            """class AllowedTest {
    @Test void preservesContract() { assertEquals(1, Api.oldCall()); }
}
""",
        )
        allowed_deleted = _write(
            allowed_root,
            "src/test/java/DeletedTest.java",
            "class DeletedTest {}\n",
        )
        allowed_pom = _write(allowed_root, "pom.xml", "<project/>\n")
        allowed_baseline = capture_test_change_contract(
            allowed_root,
            allow_test_changes=True,
        )
        assert allowed_baseline["mode"] == "api_migration"
        assert allowed_baseline["allow_test_changes"] is True
        assert allowed_baseline["semantic_audit"]["totals"]["test_methods"] == 1
        assert allowed_baseline["semantic_audit"]["totals"]["assertions"] == 1
        allowed_test.write_text(
            """class AllowedTest {
    @Test void preservesContract() { assertEquals(1, Api.newCall()); }
}
""",
            encoding="utf-8",
        )
        allowed_added_content = """class AddedTest {
    @Test void addsCoverage() { assertTrue(Api.isReady()); }
}
"""
        _write(
            allowed_root,
            "src/test/java/AddedTest.java",
            allowed_added_content,
        )
        allowed_result = evaluate_test_change_contract(
            allowed_root,
            allowed_baseline,
        ).to_dict()
        assert allowed_result["success"] is True
        assert allowed_result["status"] == "TEST_SOURCE_API_MIGRATION_ALLOWED"
        assert allowed_result["mode"] == "api_migration"
        assert allowed_result["modified"] is True
        assert allowed_result["test_source_modified"] is True
        assert len(allowed_result["changed"]) == 1
        assert len(allowed_result["added"]) == 1
        assert len(allowed_result["deleted"]) == 0
        assert allowed_result["added"][0]["after_sha256"] == _sha(
            allowed_added_content
        )
        assert allowed_result["verification_config_modified"] is False
        assert allowed_result["test_strength_violations"] == []
        assert allowed_result["current_test_strength"]["test_methods"] == 2
        assert allowed_result["current_test_strength"]["assertions"] == 2
        print("  ok   api_migration permits strength-preserving source API edits")

        policy_root = Path(temporary) / "api-migration-policy"
        policy_root.mkdir()
        strong_test = """class PolicyTest {
    @Test void first() { assertEquals(1, Api.oldCall()); }
    @Test void second() { assertTrue(Api.isReady()); }
    // @Disabled @Test void fake() { assertFalse(true); assumeTrue(false); }
    String text = "assertEquals @Disabled assumeTrue";
}
"""
        policy_test = _write(
            policy_root,
            "src/test/java/PolicyTest.java",
            strong_test,
        )
        policy_resource = _write(
            policy_root,
            "src/test/resources/fixture.json",
            '{"expected": true}\n',
        )
        policy_baseline = capture_test_change_contract(
            policy_root,
            allow_test_changes=True,
        )
        assert policy_baseline["mode"] == "api_migration"
        assert policy_baseline["semantic_audit"]["totals"]["test_methods"] == 2
        assert policy_baseline["semantic_audit"]["totals"]["assertions"] == 2
        assert policy_baseline["semantic_audit"]["totals"]["disabled_or_ignored"] == 0
        assert policy_baseline["semantic_audit"]["totals"]["assumption_skips"] == 0

        policy_test.write_text(
            """class PolicyTest {
    @Test void first() { Api.newCall(); }
}
""",
            encoding="utf-8",
        )
        weakened = evaluate_test_change_contract(
            policy_root,
            policy_baseline,
        ).to_dict()
        assert weakened["success"] is False
        assert weakened["status"] == "TEST_SOURCE_MIGRATION_REJECTED"
        assert {item["reason"] for item in weakened["test_strength_violations"]} == {
            "TEST_METHOD_COUNT_DECREASED",
            "ASSERTION_COUNT_DECREASED",
        }, weakened

        policy_test.write_text(
            strong_test.replace(
                "@Test void first()",
                "@Disabled @Test void first()",
            ),
            encoding="utf-8",
        )
        disabled = evaluate_test_change_contract(policy_root, policy_baseline).to_dict()
        assert disabled["success"] is False
        assert [item["reason"] for item in disabled["test_strength_violations"]] == [
            "DISABLED_OR_IGNORED_ADDED"
        ], disabled

        policy_test.write_text(
            strong_test.replace(
                "@Test void first() {",
                "@Test void first() { assumeTrue(Api.isReady());",
            ),
            encoding="utf-8",
        )
        assumed = evaluate_test_change_contract(policy_root, policy_baseline).to_dict()
        assert assumed["success"] is False
        assert [item["reason"] for item in assumed["test_strength_violations"]] == [
            "ASSUMPTION_SKIP_ADDED"
        ], assumed

        policy_test.write_text(strong_test, encoding="utf-8")
        policy_resource.write_text('{"expected": false}\n', encoding="utf-8")
        resource_edit = evaluate_test_change_contract(
            policy_root,
            policy_baseline,
        ).to_dict()
        assert resource_edit["success"] is False
        assert resource_edit["status"] == "TEST_SOURCE_MIGRATION_REJECTED"
        assert resource_edit["test_strength_violations"] == [
            {
                "reason": "NON_SOURCE_TEST_INPUT_MODIFIED",
                "path": "src/test/resources/fixture.json",
            }
        ], resource_edit
        print("  ok   api_migration rejects weakened, skipped, or non-source tests")

        allowed_deleted.unlink()
        deleted_result = evaluate_test_change_contract(
            allowed_root,
            allowed_baseline,
        ).to_dict()
        assert deleted_result["success"] is False
        assert deleted_result["status"] == "TEST_SOURCE_DELETED"
        assert deleted_result["deleted"] == [
            {
                "path": "src/test/java/DeletedTest.java",
                "before_sha256": _sha("class DeletedTest {}\n"),
                "after_sha256": "",
            }
        ]
        print("  ok   test-edit opt-in preserves every baseline test-file identity")

        allowed_pom.write_text("<project><build/></project>\n", encoding="utf-8")
        allowed_config_blocked = evaluate_test_change_contract(
            allowed_root,
            allowed_baseline,
        ).to_dict()
        assert allowed_config_blocked["success"] is False
        assert allowed_config_blocked["status"] == "VERIFICATION_CONFIG_MODIFIED"
        assert allowed_config_blocked["test_source_modified"] is True
        assert [
            item["path"]
            for item in allowed_config_blocked["verification_config_changed"]
        ] == ["pom.xml"]
        print("  ok   allow_test_changes never authorizes verification config edits")

        declared.unlink()
        deleted_declared = evaluate_test_change_contract(project, baseline).to_dict()
        assert "qa/custom/ContractSpec.java" in {
            item["path"] for item in deleted_declared["deleted"]
        }
        print("  ok   deleted nonstandard declared test remains tracked")

        _expect_error(
            "TEST_FILE_MISSING",
            lambda: capture_test_change_contract(
                project,
                declared_test_files="qa/custom/MissingTest.java",
            ),
        )
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: capture_test_change_contract(
                project,
                declared_test_files="../outside/Test.java",
            ),
        )
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: capture_test_change_contract(
                project,
                allow_test_changes="false",  # type: ignore[arg-type]
            ),
        )
        print("  ok   invalid declarations and mutable-looking policy values fail closed")

        # Internal digests detect an inconsistent manifest before a live tree
        # comparison. Authenticity is supplied by the outer controller seal.
        corrupted = dict(baseline)
        corrupted["tree_sha256"] = "0" * 64
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: evaluate_test_change_contract(project, corrupted),
        )
        corrupted_config = dict(baseline)
        corrupted_config["verification_config_tree_sha256"] = "0" * 64
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: evaluate_test_change_contract(project, corrupted_config),
        )
        corrupted_mode = dict(baseline)
        corrupted_mode["mode"] = "api_migration"
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: evaluate_test_change_contract(project, corrupted_mode),
        )
        corrupted_audit = dict(baseline)
        corrupted_audit["semantic_audit"] = {
            **baseline["semantic_audit"],
            "audit_sha256": "0" * 64,
        }
        _expect_error(
            "TEST_CHANGE_CONTRACT_SCHEMA_INVALID",
            lambda: evaluate_test_change_contract(project, corrupted_audit),
        )
        print("  ok   c000 digests are internally consistent; controller seal is authoritative")

        assert integration.is_file()

    print("test change contract self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
