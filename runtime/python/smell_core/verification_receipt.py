from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FORMAL_VERIFICATION_RECEIPT_SCHEMA = "smell.formal-verification-receipt/v1"
VERIFY_DECISION_SCHEMA = "smell.verify.decision/v1"


def _record(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _bounded_string(
    value: Any,
    *,
    limit: int,
    nonempty: bool = False,
) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= limit
        and (not nonempty or value.strip())
    )


def validate_formal_verification_receipt(
    value: Any,
    *,
    decision: Mapping[str, Any] | None = None,
    require_project_full_pass: bool = False,
) -> Mapping[str, Any] | None:
    """Validate the shared, smell-neutral formal verification receipt.

    The receipt binds one candidate identity to Guard/build/test evidence.  It
    deliberately does not interpret a smell metric or acceptance threshold.
    """

    receipt = _record(value)
    identity = _record(receipt.get("candidate_identity")) if receipt else None
    guard = _record(receipt.get("guard")) if receipt else None
    build_test = _record(receipt.get("build_test")) if receipt else None
    artifact_refs = _record(receipt.get("artifact_refs")) if receipt else None
    isolation_value = receipt.get("fresh_isolation") if receipt else None
    isolation = _record(isolation_value)
    if (
        receipt is None
        or receipt.get("schema_version") != FORMAL_VERIFICATION_RECEIPT_SCHEMA
        or receipt.get("terminal_stage") != "formal_verify"
        or not _bounded_string(receipt.get("status"), limit=128, nonempty=True)
        or not isinstance(receipt.get("success"), bool)
        or not isinstance(receipt.get("accepted"), bool)
        or not _bounded_string(receipt.get("resolution"), limit=128)
        or identity is None
        or not _bounded_string(identity.get("baseline_revision"), limit=128, nonempty=True)
        or not _bounded_string(identity.get("baseline_tree"), limit=128)
        or not _bounded_string(identity.get("production_diff"), limit=128, nonempty=True)
        or not _bounded_string(identity.get("test_tree"), limit=128)
        or not _bounded_string(identity.get("verification_config_tree"), limit=128)
        or receipt.get("outcome") not in {"pass", "test_failed", "failed"}
        or not _bounded_string(
            receipt.get("diagnostic_signature"), limit=128, nonempty=True
        )
        or guard is None
        or not isinstance(guard.get("success"), bool)
        or not isinstance(guard.get("failure_count"), int)
        or isinstance(guard.get("failure_count"), bool)
        or int(guard.get("failure_count")) < 0
        or not _bounded_string(guard.get("artifact_ref"), limit=1024)
        or build_test is None
        or not isinstance(build_test.get("success"), bool)
        or not _bounded_string(build_test.get("reason"), limit=128)
        or not isinstance(build_test.get("project_full_executed"), bool)
        or not _bounded_string(build_test.get("build_status"), limit=128)
        or not _bounded_string(build_test.get("test_status"), limit=128)
        or not _bounded_string(build_test.get("sample_test_status"), limit=128)
        or (isolation_value is not None and isolation is None)
        or artifact_refs is None
        or len(artifact_refs) > 24
        or any(
            not _bounded_string(name, limit=128, nonempty=True)
            or not _bounded_string(path, limit=1024, nonempty=True)
            for name, path in artifact_refs.items()
        )
    ):
        return None

    if decision is not None and any(
        receipt.get(key) != decision.get(key)
        for key in ("status", "success", "accepted", "resolution")
    ):
        return None

    if receipt.get("accepted") is True and (
        receipt.get("status") != "PASS"
        or receipt.get("success") is not True
        or receipt.get("resolution") != "resolved"
        or receipt.get("outcome") != "pass"
        or receipt.get("diagnostic_signature") != "PASS"
        or guard.get("success") is not True
        or guard.get("failure_count") != 0
        or build_test.get("success") is not True
    ):
        return None

    if require_project_full_pass:
        required_refs = {"guard_evidence", "build_result", "test_result", "diff"}
        isolation_contract = (
            isolation.get("contract_version"),
            isolation.get("mode"),
        ) if isolation is not None else (None, None)
        if (
            receipt.get("accepted") is not True
            or build_test.get("project_full_executed") is not True
            or isolation is None
            or isolation_contract not in {
                (
                    "project-full-fresh-worktree/v1",
                    "detached_git_worktree",
                ),
                (
                    "project-full-direct-output-cleanup/v1",
                    "runner_checkout_with_output_cleanup",
                ),
            }
            or isolation.get("success") is not True
            or isolation.get("stage") != "completed"
            or isolation.get("cleanup_success") is not True
            or not required_refs.issubset(artifact_refs)
        ):
            return None
    return receipt


def validate_formal_verification_decision(
    value: Any,
    *,
    require_project_full_pass: bool = False,
) -> Mapping[str, Any] | None:
    """Validate an accepted decision and its formal receipt as one contract."""

    decision = _record(value)
    if decision is None or decision.get("schema_version") != VERIFY_DECISION_SCHEMA:
        return None
    if (
        not isinstance(decision.get("success"), bool)
        or not isinstance(decision.get("accepted"), bool)
        or not _bounded_string(decision.get("status"), limit=128, nonempty=True)
        or not _bounded_string(decision.get("resolution"), limit=128)
        or not isinstance(decision.get("project_full_executed"), bool)
    ):
        return None
    receipt = validate_formal_verification_receipt(
        decision.get("formal_verification_receipt"),
        decision=decision,
        require_project_full_pass=require_project_full_pass,
    )
    if receipt is None:
        return None
    if decision.get("accepted") is True:
        smell_guard = _record(decision.get("smell_guard"))
        build_test = _record(decision.get("build_test_guard"))
        checkpoint = _record(decision.get("checkpoint"))
        artifacts = _record(decision.get("artifacts"))
        artifact_index = _record(decision.get("artifact_index"))
        test_changes = _record(decision.get("test_changes"))
        if (
            decision.get("status") != "PASS"
            or decision.get("success") is not True
            or decision.get("resolution") != "resolved"
            or smell_guard is None
            or smell_guard.get("success") is not True
            or smell_guard.get("failure_count") != 0
            or build_test is None
            or build_test.get("success") is not True
            or checkpoint is None
            or checkpoint.get("accepted") is not True
            or checkpoint.get("resolution") != "resolved"
            or checkpoint.get("verify_status") != "PASS"
            or checkpoint.get("build_test_success") is not True
            or test_changes is None
            or test_changes.get("success") is not True
            or artifacts is None
            or artifact_index is None
        ):
            return None
        receipt_refs = _record(receipt.get("artifact_refs")) or {}
        for name, path in receipt_refs.items():
            indexed = _record(artifact_index.get(name))
            if (
                artifacts.get(name) != path
                or indexed is None
                or indexed.get("path") != path
                or not isinstance(indexed.get("bytes"), int)
                or isinstance(indexed.get("bytes"), bool)
                or int(indexed.get("bytes")) < 0
            ):
                return None
        if require_project_full_pass and (
            decision.get("project_full_executed") is not True
            or build_test.get("verification_mode") != "project_full"
            or build_test.get("project_full_executed") is not True
        ):
            return None
    return decision
