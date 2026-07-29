#!/usr/bin/env python3
"""Self-check dependency-audit classification and aggregation."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from audit_java_image_dependencies import (
    _collect_text,
    classify_dependency_failure,
    summarize_results,
)


def check_dataset_snapshot_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    dataset_dir = root / "dataset" / "java" / "delivery_schema"
    refused_files = sorted(path.name for path in dataset_dir.glob("refused_bequest*.csv"))
    assert refused_files == ["refused_bequest.csv"], refused_files
    refused_csv = dataset_dir / "refused_bequest.csv"
    with refused_csv.open(
        newline="", encoding="utf-8-sig"
    ) as handle:
        assert sum(1 for _ in csv.DictReader(handle)) == 30
    refused_sha256 = hashlib.sha256(refused_csv.read_bytes()).hexdigest()

    dockerfile = (
        root
        / "docker"
        / "java-refactor-delivery"
        / "Dockerfile.r7.3.18-main-refused-bequest"
    ).read_text(encoding="utf-8")
    cleanup = "RUN rm -rf /opt/dataset/java/delivery_schema"
    snapshot_copy = (
        "COPY dataset/java/delivery_schema/ /opt/dataset/java/delivery_schema/"
    )
    assert cleanup in dockerfile
    assert snapshot_copy in dockerfile
    assert dockerfile.index(cleanup) < dockerfile.index(snapshot_copy)
    assert f"ARG REFUSED_BEQUEST_CSV_SHA256={refused_sha256}" in dockerfile
    assert "org.opencontainers.refactor.dataset-snapshot=" in dockerfile
    assert "org.opencontainers.refactor.refused-bequest-csv-sha256=" in dockerfile
    assert "| sha256sum -c -" in dockerfile

    manifest = json.loads(
        (root / "delivery" / "java-current.json").read_text(encoding="utf-8")
    )
    refused_manifest = manifest["dataset"]["refused_bequest"]
    assert refused_manifest["path"] == str(refused_csv.relative_to(root))
    assert refused_manifest["row_count"] == 30
    assert refused_manifest["sha256"] == refused_sha256
    acceptance = manifest["acceptance"]
    assert acceptance["fresh_container_rounds"] == 2
    assert acceptance["sample_count"] == 30
    assert acceptance["unique_plan_count"] == 4
    assert acceptance["pass_results"] == 8
    assert acceptance["confirmed_missing_count"] == 0
    assert acceptance["resolution_failure_count"] == 0
    assert acceptance["semantic_plan_comparison"] == "PASS"


def check_orchestration() -> None:
    with tempfile.TemporaryDirectory(prefix="dependency-audit-orchestration-") as raw:
        root = Path(raw)
        fake_baseline = root / "fake_baseline.py"
        fake_baseline.write_text(
            textwrap.dedent(
                """\
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                report = Path(args[args.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if "--list-execution-plans" in args:
                    value = {
                        "sample_count": 2,
                        "plans": [
                            {
                                "execution_id": "plan-pass",
                                "project_name": "demo",
                                "sample_keys": ["demo.csv:1"],
                            },
                            {
                                "execution_id": "plan-missing",
                                "project_name": "demo",
                                "sample_keys": ["demo.csv:2"],
                            },
                        ],
                    }
                    report.write_text(json.dumps(value), encoding="utf-8")
                    raise SystemExit(0)

                execution_id = args[args.index("--execution-id") + 1]
                if execution_id == "plan-pass":
                    value = {
                        "execution_id": execution_id,
                        "project_name": "demo",
                        "sample_keys": ["demo.csv:1"],
                        "status": "pass",
                        "first_pass": True,
                        "build_success": True,
                        "test_success": True,
                    }
                    returncode = 0
                else:
                    value = {
                        "execution_id": execution_id,
                        "project_name": "demo",
                        "sample_keys": ["demo.csv:2"],
                        "status": "build_failed",
                        "first_pass": False,
                        "build_success": False,
                        "test_success": False,
                    }
                    (report.parent / "build.log").write_text(
                        "No cached version of org.example:demo:1.0 "
                        "available for offline mode.\\n",
                        encoding="utf-8",
                    )
                    returncode = 1
                report.write_text(json.dumps(value), encoding="utf-8")
                raise SystemExit(returncode)
                """
            ),
            encoding="utf-8",
        )
        output = root / "audit"
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("audit_java_image_dependencies.py")),
                "--baseline-script",
                str(fake_baseline),
                "--dataset-root",
                str(root / "dataset"),
                "--project-revisions",
                str(root / "project-revisions.json"),
                "--jobs",
                "2",
                "--output",
                str(output),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 1, completed.stderr
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        assert report["complete"] is True
        assert report["plan_count"] == 2
        assert report["category_counts"] == {
            "OFFLINE_DEPENDENCY_MISSING": 1,
            "PASS": 1,
        }
        assert report["confirmed_missing_count"] == 1
        assert (output / "plans" / "plan-pass" / "result.json").is_file()
        assert (output / "plans" / "plan-missing" / "build.log").is_file()


def main() -> int:
    check_dataset_snapshot_contract()
    passed = classify_dependency_failure(
        {"first_pass": True},
        "Downloaded org.example:present:1.0.0 before the image was frozen.",
    )
    assert passed["category"] == "PASS"
    assert passed["coordinates"] == []

    gradle = classify_dependency_failure(
        {"first_pass": False, "status": "build_failed", "build_success": False},
        (
            "Could not resolve all artifacts for configuration 'classpath'.\n"
            "Could not download foojay-resolver-1.0.0.jar "
            "(org.gradle.toolchains:foojay-resolver:1.0.0): "
            "No cached version available for offline mode"
        ),
    )
    assert gradle["category"] == "OFFLINE_DEPENDENCY_MISSING"
    assert "org.gradle.toolchains:foojay-resolver:1.0.0" in gradle["coordinates"]

    gradle_variant = classify_dependency_failure(
        {"first_pass": False, "status": "build_failed", "build_success": False},
        "No cached version of com.example:settings-plugin:2.0 available for offline mode.",
    )
    assert gradle_variant["category"] == "OFFLINE_DEPENDENCY_MISSING"

    maven = classify_dependency_failure(
        {"first_pass": False, "status": "test_failed", "test_success": False},
        (
            "Cannot access local-all in offline mode and the artifact "
            "org.example:demo:jar:1.2.3 has not been downloaded from it before."
        ),
    )
    assert maven["category"] == "OFFLINE_DEPENDENCY_MISSING"
    assert "org.example:demo:jar:1.2.3" in maven["coordinates"]

    uncertain = classify_dependency_failure(
        {"first_pass": False, "status": "build_failed", "build_success": False},
        "Could not resolve all dependencies for configuration ':compileClasspath'.",
    )
    assert uncertain["category"] == "DEPENDENCY_RESOLUTION_FAILED"
    assert uncertain["confidence"] == "medium"

    assertion = classify_dependency_failure(
        {
            "first_pass": False,
            "status": "test_failed",
            "build_success": True,
            "test_success": False,
        },
        "Tests run: 1, Failures: 1\nexpected: <true> but was: <false>",
    )
    assert assertion["category"] == "TEST_FAILED"

    checkout = classify_dependency_failure(
        {
            "first_pass": False,
            "status": "checkout_error",
            "build_success": False,
            "test_success": False,
        },
        "checkout failed before build",
    )
    assert checkout["category"] == "INFRA_FAILED"

    with tempfile.TemporaryDirectory(prefix="dependency-audit-self-check-") as raw:
        plan_dir = Path(raw)
        (plan_dir / "build.log").write_text(
            "Could not resolve optional plugin during a successful build warning.\n",
            encoding="utf-8",
        )
        (plan_dir / "test.log").write_text(
            "Tests run: 1, Failures: 1\nexpected true but was false\n",
            encoding="utf-8",
        )
        test_result = {
            "first_pass": False,
            "status": "test_failed",
            "build_success": True,
            "test_success": False,
        }
        failed_phase_text = _collect_text(test_result, plan_dir)
        assert "optional plugin" not in failed_phase_text
        assert (
            classify_dependency_failure(test_result, failed_phase_text)["category"]
            == "TEST_FAILED"
        )

    plans = [{"execution_id": "one"}, {"execution_id": "two"}]
    partial = summarize_results(plans, [dict(passed, execution_id="one")])
    assert partial["success"] is False
    assert partial["completed_plan_count"] == 1

    complete = summarize_results(
        plans,
        [
            dict(passed, execution_id="one"),
            dict(gradle, execution_id="two"),
        ],
    )
    assert complete["success"] is False
    assert complete["confirmed_missing_count"] == 1
    assert complete["resolution_failure_count"] == 0
    assert complete["dependency_related_failure_count"] == 1
    assert complete["category_counts"] == {
        "OFFLINE_DEPENDENCY_MISSING": 1,
        "PASS": 1,
    }
    check_orchestration()
    print("java image dependency audit self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
