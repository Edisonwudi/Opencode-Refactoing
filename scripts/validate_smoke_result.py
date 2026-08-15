#!/usr/bin/env python3
"""Validate one final smoke result from structured contract fields only."""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DECISION_LIMIT = 64 * 1024
EVIDENCE_LIMIT = 2 * 1024 * 1024
BASELINE_SCHEMA = "smell.baseline.decision/v1"
VERIFY_SCHEMA = "smell.verify.decision/v1"

FORBIDDEN_FINAL_STATUSES = {
    "BRIDGE_FAILED",
    "CHECKOUT_ERROR",
    "OPENCODE_FAILED",
    "OPENCODE_TIMEOUT",
    "PROVIDER_INSUFFICIENT_QUOTA",
    "PROVIDER_QUOTA_FAILED",
    "RUNNER_FAILED",
    "VERIFY_OUTPUT_PARSE_FAILED",
}

# These values indicate framework/configuration failure when they occur in a
# structured status/code/reason field. They are deliberately not searched as
# substrings across result.json: messages and historical Guard diagnostics are
# business evidence, not the smoke framework verdict.
STRUCTURED_FRAMEWORK_FAILURES = {
    "BRIDGE_FAILED",
    "CHECKPOINT_BASELINE_SEAL_MISMATCH",
    "CHECKPOINT_RECAPTURE_REQUIRED",
    "CHECKPOINT_VERIFICATION_CONTRACT_MISMATCH",
    "DETECTOR_PROFILE_MISMATCH",
    "GUARD_EVIDENCE_TOO_LARGE",
    "GUARD_SCOPE_TOO_LARGE",
    "RELATION_SCOPE_TOO_LARGE",
    "TARGET_AMBIGUOUS",
    "TARGET_GUARD_UNAVAILABLE",
    "UNSUPPORTED_GUARD",
    "VERIFY_OUTPUT_PARSE_FAILED",
}

BASELINE_FAILURES = {
    "BASELINE_FINDING_NOT_FOUND",
    "BASELINE_METRIC_UNAVAILABLE",
    "BASELINE_CAPTURE_FAILED",
}

STRUCTURED_CODE_FIELDS = {
    "code",
    "error",
    "error_code",
    "failure_category",
    "reason",
    "status",
}
CODE_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9_]+)(?::|\s|$)")


class ResultValidationError(ValueError):
    pass


def compact_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ResultValidationError(message)


def _structured_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = CODE_PREFIX_RE.match(value.strip())
    return match.group(1) if match else ""


def _collect_structured_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in STRUCTURED_CODE_FIELDS:
                code = _structured_code(item)
                if code:
                    codes.add(code)
            if isinstance(item, (Mapping, list)):
                codes.update(_collect_structured_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.update(_collect_structured_codes(item))
    return codes


def _top_level_baseline_failure_codes(baseline: Mapping[str, Any]) -> set[str]:
    return {
        code
        for key in STRUCTURED_CODE_FIELDS
        if (code := _structured_code(baseline.get(key))) in BASELINE_FAILURES
    }


def _resolve_artifact_path(raw: str, results_root: Path) -> Path:
    if raw.startswith("/runs/"):
        return results_root / raw.removeprefix("/runs/")
    return Path(raw)


def _require_build_test_contract(
    build_test: Any,
    *,
    expected_mode: str,
    sample_test_required: bool,
) -> None:
    require(
        isinstance(build_test, Mapping) and build_test.get("success") is True,
        "PASS/IMPROVED build/test failed",
    )
    require(
        str(build_test.get("verification_mode") or expected_mode) == expected_mode,
        "build/test verification mode mismatch",
    )
    details = build_test.get("details")
    require(isinstance(details, Mapping), "build/test stage details missing")
    required_stages = ["build", "test"]
    if expected_mode == "project_full" and (
        sample_test_required or details.get("sample_test") is not None
    ):
        required_stages.append("sample_test")
    for stage in required_stages:
        item = details.get(stage)
        label = stage.replace("_", " ")
        require(isinstance(item, Mapping), f"{label} stage missing")
        require(item.get("success") is True, f"{label} stage failed")
        require(item.get("status") != "skipped", f"{label} stage skipped")


def validate_result(
    result: Mapping[str, Any],
    *,
    result_path: Path,
    expected_smell: str,
    expected_sample_id: str,
    expected_mode: str,
    expected_test_changes: bool,
    results_root: Path,
) -> None:
    require(result.get("smell") == expected_smell, "result smell mismatch")
    require(
        str(result.get("sample_id")) == expected_sample_id,
        "result sample mismatch",
    )
    require(
        result.get("verification_mode") == expected_mode,
        "verification mode mismatch",
    )
    require(
        result.get("allow_test_changes") is expected_test_changes,
        "test policy mismatch",
    )
    status = result.get("status")
    require(isinstance(status, str) and bool(status), "missing final status")
    require(
        status not in FORBIDDEN_FINAL_STATUSES,
        f"forbidden final status: {status}",
    )
    require(isinstance(result.get("accepted"), bool), "accepted is not boolean")
    require(isinstance(result.get("progress"), bool), "progress is not boolean")
    require(isinstance(result.get("resolution"), str), "resolution missing")

    baseline = result.get("baseline_capture")
    require(isinstance(baseline, Mapping), "baseline decision missing")
    require(
        baseline.get("success") is True,
        f"baseline capture failed: {baseline.get('status')}",
    )
    require(
        baseline.get("status") == "BASELINE_CAPTURED",
        "baseline status mismatch",
    )
    require(baseline.get("schema_version") == BASELINE_SCHEMA, "baseline schema mismatch")
    verification_policy = baseline.get("verification_policy")
    require(
        isinstance(verification_policy, Mapping),
        "baseline verification policy missing",
    )
    require(
        verification_policy.get("contract_version") == 5,
        "baseline verification contract version mismatch",
    )
    require(
        verification_policy.get("verification_mode") == expected_mode,
        "baseline verification mode mismatch",
    )
    sample_test_required = verification_policy.get("sample_test_command_present")
    require(
        isinstance(sample_test_required, bool),
        "baseline sample test declaration missing",
    )
    baseline_failures = sorted(_top_level_baseline_failure_codes(baseline))
    require(
        not baseline_failures,
        f"baseline capture failed: {baseline_failures}",
    )
    require(
        compact_size(baseline) < DECISION_LIMIT,
        "baseline decision exceeds 64 KiB",
    )
    require(isinstance(result.get("revision_audit"), Mapping), "revision audit missing")
    require(isinstance(result.get("dataset_audit"), Mapping), "dataset audit missing")

    attempts = result.get("attempts")
    require(
        isinstance(attempts, list) and len(attempts) == 1,
        "not one final attempt",
    )
    final = attempts[0]
    require(isinstance(final, Mapping), "final attempt is not an object")
    require(final.get("verify_source") == "runner_final", "verify source mismatch")
    require(
        final.get("verify_returncode") == 0,
        "final verify return code is nonzero",
    )
    require(final.get("status") == status, "attempt/result status mismatch")
    verify = final.get("verify_payload")
    require(isinstance(verify, Mapping), "verify decision missing")
    require(verify.get("schema_version") == VERIFY_SCHEMA, "verify schema mismatch")
    for key in ("status", "resolution", "accepted", "progress"):
        require(verify.get(key) == result.get(key), f"verify/result {key} mismatch")
    require(compact_size(verify) < DECISION_LIMIT, "verify decision exceeds 64 KiB")

    framework_failures = sorted(
        _collect_structured_codes(final) & STRUCTURED_FRAMEWORK_FAILURES
    )
    require(
        not framework_failures,
        f"structured framework failure: {framework_failures}",
    )

    artifacts = verify.get("artifacts")
    index = verify.get("artifact_index")
    require(
        isinstance(artifacts, Mapping) and isinstance(index, Mapping),
        "artifact index missing",
    )
    require(
        "verify_full" not in artifacts and "verify_full" not in index,
        "verify_full fallback present",
    )
    evidence_raw = artifacts.get("guard_evidence")
    require(
        isinstance(evidence_raw, str) and bool(evidence_raw),
        "guard evidence artifact missing",
    )
    evidence_index = index.get("guard_evidence")
    require(isinstance(evidence_index, Mapping), "guard evidence index missing")
    require(
        isinstance(evidence_index.get("bytes"), int),
        "guard evidence byte size missing",
    )
    evidence_path = _resolve_artifact_path(evidence_raw, results_root)
    require(evidence_path.is_file(), "guard evidence artifact not found")
    evidence_size = evidence_path.stat().st_size
    require(
        0 < evidence_size <= EVIDENCE_LIMIT,
        "guard evidence artifact outside size limit",
    )
    require(
        evidence_size == evidence_index["bytes"],
        "guard evidence byte index mismatch",
    )
    require(
        not list(result_path.parent.rglob("verify.full.json")),
        "verify.full.json fallback exists",
    )

    if status == "PASS":
        checkpoint = verify.get("checkpoint")
        build_test = verify.get("build_test_guard")
        require(result.get("resolution") == "resolved", "PASS is not resolved")
        require(
            result.get("accepted") is True and result.get("progress") is True,
            "PASS flags invalid",
        )
        require(verify.get("success") is True, "PASS verify success false")
        _require_build_test_contract(
            build_test,
            expected_mode=expected_mode,
            sample_test_required=sample_test_required,
        )
        require(
            isinstance(checkpoint, Mapping) and checkpoint.get("accepted") is True,
            "PASS checkpoint rejected",
        )
        require(checkpoint.get("resolution") == "resolved", "PASS checkpoint not resolved")
        require(checkpoint.get("verify_status") == "PASS", "PASS checkpoint status mismatch")
        require(
            checkpoint.get("build_test_success") is True,
            "PASS checkpoint build/test false",
        )
    elif status == "IMPROVED":
        checkpoint = verify.get("checkpoint")
        build_test = verify.get("build_test_guard")
        require(
            result.get("resolution") == "improved",
            "IMPROVED resolution mismatch",
        )
        require(
            result.get("accepted") is False and result.get("progress") is True,
            "IMPROVED flags invalid",
        )
        require(verify.get("success") is False, "IMPROVED verify success true")
        _require_build_test_contract(
            build_test,
            expected_mode=expected_mode,
            sample_test_required=sample_test_required,
        )
        require(
            isinstance(checkpoint, Mapping) and checkpoint.get("accepted") is False,
            "IMPROVED accepted",
        )
        require(
            checkpoint.get("resolution") == "improved",
            "IMPROVED checkpoint mismatch",
        )
        require(
            checkpoint.get("verify_status") == "IMPROVED",
            "IMPROVED status mismatch",
        )
        require(
            checkpoint.get("build_test_success") is True,
            "IMPROVED build/test false",
        )
    else:
        require(
            result.get("resolution") == "unresolved",
            "failure is not unresolved",
        )
        require(
            result.get("accepted") is False and result.get("progress") is False,
            "failure flags invalid",
        )
        require(verify.get("success") is False, "failure verify success true")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 6:
        raise SystemExit(
            "usage: validate_smoke_result.py RESULT SMELL SAMPLE_ID "
            "VERIFICATION_MODE ALLOW_TEST_CHANGES RESULTS_ROOT"
        )
    result_path = Path(args[0])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, Mapping):
        raise SystemExit("result is not an object")
    try:
        validate_result(
            result,
            result_path=result_path,
            expected_smell=args[1],
            expected_sample_id=args[2],
            expected_mode=args[3],
            expected_test_changes=args[4].lower() == "true",
            results_root=Path(args[5]),
        )
    except ResultValidationError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
