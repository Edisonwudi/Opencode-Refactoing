#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from validate_smoke_result import ResultValidationError, validate_result  # noqa: E402


def _base_result(evidence_path: Path) -> dict[str, object]:
    evidence_size = evidence_path.stat().st_size
    checkpoint = {
        "accepted": False,
        "resolution": "unresolved",
        "verify_status": "SMELL_GUARD_FAILED",
        "build_test_success": None,
    }
    verify = {
        "schema_version": "smell.verify.decision/v1",
        "success": False,
        "accepted": False,
        "progress": False,
        "status": "SMELL_GUARD_FAILED",
        "resolution": "unresolved",
        "smell_guard": {
            "success": False,
            "failure_count": 1,
            "results": [
                {
                    "type": "long_method",
                    "success": False,
                    "message": (
                        "Historical diagnostic text may mention "
                        "DETECTOR_PROFILE_MISMATCH without changing the final contract."
                    ),
                    "details": {"reason": "BASELINE_METRIC_UNAVAILABLE"},
                }
            ],
        },
        "build_test_guard": None,
        "checkpoint": checkpoint,
        "artifacts": {"guard_evidence": str(evidence_path)},
        "artifact_index": {
            "guard_evidence": {
                "path": str(evidence_path),
                "bytes": evidence_size,
            }
        },
    }
    return {
        "smell": "long_method",
        "sample_id": "1",
        "verification_mode": "project_full",
        "allow_test_changes": True,
        "status": "SMELL_GUARD_FAILED",
        "accepted": False,
        "progress": False,
        "resolution": "unresolved",
        "baseline_capture": {
            "schema_version": "smell.baseline.decision/v1",
            "success": True,
            "status": "BASELINE_CAPTURED",
            "verification_policy": {
                "contract_version": 4,
                "verification_mode": "project_full",
            },
        },
        "revision_audit": {},
        "dataset_audit": {},
        "attempts": [
            {
                "verify_source": "runner_final",
                "verify_returncode": 0,
                "status": "SMELL_GUARD_FAILED",
                "verify_payload": verify,
            }
        ],
    }


def _validate(result: dict[str, object], result_path: Path, results_root: Path) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    validate_result(
        result,
        result_path=result_path,
        expected_smell="long_method",
        expected_sample_id="1",
        expected_mode="project_full",
        expected_test_changes=True,
        results_root=results_root,
    )


def _expect_failure(
    result: dict[str, object],
    result_path: Path,
    results_root: Path,
    expected_text: str,
) -> None:
    try:
        _validate(result, result_path, results_root)
    except ResultValidationError as exc:
        assert expected_text in str(exc), exc
        return
    raise AssertionError(f"validator unexpectedly accepted: {result_path}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="smoke-result-validator-") as raw_temp:
        root = Path(raw_temp)
        evidence = root / "guard-evidence.json"
        evidence.write_text('{"schema_version":1}\n', encoding="utf-8")

        ordinary_guard_failure = _base_result(evidence)
        _validate(
            ordinary_guard_failure,
            root / "ordinary" / "result.json",
            root,
        )
        print("  ok   business Guard failure is valid despite historical marker text")

        baseline_failed = copy.deepcopy(ordinary_guard_failure)
        baseline_failed["baseline_capture"] = {
            "schema_version": "smell.baseline.decision/v1",
            "success": False,
            "status": "BASELINE_METRIC_UNAVAILABLE",
        }
        _expect_failure(
            baseline_failed,
            root / "baseline-failed" / "result.json",
            root,
            "baseline capture failed",
        )
        print("  ok   structured baseline failure still rejects the smoke result")

        stale_verification_contract = copy.deepcopy(ordinary_guard_failure)
        stale_verification_contract["baseline_capture"]["verification_policy"][
            "contract_version"
        ] = 1
        _expect_failure(
            stale_verification_contract,
            root / "stale-verification-contract" / "result.json",
            root,
            "baseline verification contract version mismatch",
        )
        print("  ok   smoke requires c000 verification contract v4")

        profile_mismatch = copy.deepcopy(ordinary_guard_failure)
        profile_verify = profile_mismatch["attempts"][0]["verify_payload"]
        profile_verify["checkpoint"]["reason"] = "DETECTOR_PROFILE_MISMATCH"
        _expect_failure(
            profile_mismatch,
            root / "profile-mismatch" / "result.json",
            root,
            "structured framework failure",
        )
        print("  ok   structured detector profile mismatch is rejected")

        passed = copy.deepcopy(ordinary_guard_failure)
        passed.update(
            {
                "status": "PASS",
                "accepted": True,
                "progress": True,
                "resolution": "resolved",
            }
        )
        pass_attempt = passed["attempts"][0]
        pass_attempt["status"] = "PASS"
        pass_verify = pass_attempt["verify_payload"]
        pass_verify.update(
            {
                "success": True,
                "status": "PASS",
                "accepted": True,
                "progress": True,
                "resolution": "resolved",
                "smell_guard": {"success": True, "failure_count": 0, "results": []},
                "build_test_guard": {
                    "success": True,
                    "verification_mode": "project_full",
                    "details": {
                        "build": {"success": True, "status": "pass"},
                        "test": {"success": True, "status": "pass"},
                        "sample_test": {"success": True, "status": "pass"},
                    },
                },
                "checkpoint": {
                    "accepted": True,
                    "resolution": "resolved",
                    "verify_status": "PASS",
                    "build_test_success": True,
                },
            }
        )
        _validate(passed, root / "pass" / "result.json", root)

        missing_sample_stage = copy.deepcopy(passed)
        del missing_sample_stage["attempts"][0]["verify_payload"]["build_test_guard"][
            "details"
        ]["sample_test"]
        _expect_failure(
            missing_sample_stage,
            root / "pass-missing-sample-stage" / "result.json",
            root,
            "sample test stage",
        )
        print("  ok   PASS requires build, project test, and sample test stages")

    print("smoke result validator self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
