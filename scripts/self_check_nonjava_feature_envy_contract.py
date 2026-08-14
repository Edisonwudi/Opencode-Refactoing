#!/usr/bin/env python3
"""Adversarial checks for the non-Java Feature Envy target contract."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
BRIDGE = RUNTIME_PYTHON / "bridge" / "smell_bridge.py"
CONFIG = RUNTIME_PYTHON / "smell_core" / "defaults" / "refactor.yaml"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.guards import _run_generic_feature_envy_guard  # noqa: E402
from smell_core.guards.context import GuardRunContext  # noqa: E402
from smell_core.location import parse_locations  # noqa: E402


PYTHON_BEFORE = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1


def total_price(order):
    subtotal = order.unit_price * order.quantity
    tax = subtotal * order.tax_rate
    shipping = order.shipping_fee
    discount = order.discount
    total = subtotal + tax + shipping - discount
    return total
"""
PYTHON_AFTER = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1

    def total_price(self):
        subtotal = self.unit_price * self.quantity
        tax = subtotal * self.tax_rate
        shipping = self.shipping_fee
        discount = self.discount
        total = subtotal + tax + shipping - discount
        return total


def total_price(order):
    return order.total_price()
"""

C_BEFORE = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

static int total_price(struct Order *order) {
    int subtotal = order->unit_price * order->quantity;
    int tax = subtotal * order->tax_rate;
    int shipping = order->shipping_fee;
    int discount = order->discount;
    return subtotal + tax + shipping - discount;
}
"""
C_AFTER = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

static int order_total(struct Order *order) {
    int subtotal = order->unit_price * order->quantity;
    int tax = subtotal * order->tax_rate;
    int shipping = order->shipping_fee;
    int discount = order->discount;
    return subtotal + tax + shipping - discount;
}

static int total_price(struct Order *order) {
    return order_total(order);
}
"""

CPP_BEFORE = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

static int total_price(Order *order) {
    int subtotal = order->unit_price * order->quantity;
    int tax = subtotal * order->tax_rate;
    int shipping = order->shipping_fee;
    int discount = order->discount;
    int total = subtotal + tax + shipping - discount;
    return total;
}
"""
CPP_AFTER = """\
struct Order {
    int unit_price, quantity, tax_rate, shipping_fee, discount;
    int total_price() const {
        int subtotal = unit_price * quantity;
        int tax = subtotal * tax_rate;
        int shipping = shipping_fee;
        int applied_discount = discount;
        int total = subtotal + tax + shipping - applied_discount;
        return total;
    }
};

static int total_price(Order *order) {
    return order->total_price();
}
"""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def _bridge(
    project: Path,
    command: str,
    *,
    language: str,
    location: str,
    receiver: str | None,
    evidence: str = "envied_receiver=wrong_audit_value; foreign_access=999",
) -> dict[str, object]:
    args = [
        sys.executable,
        str(BRIDGE),
        command,
        "--output-detail",
        "audit",
        "--config",
        str(CONFIG),
        "--project-root",
        str(project),
        "--language",
        language,
        "--smell",
        "feature_envy",
        "--location",
        location,
        "--smell-evidence",
        evidence,
    ]
    if receiver is not None:
        args.extend([
            "--target-context-json",
            json.dumps({"receiver_type": receiver}, separators=(",", ":")),
        ])
    if command == "verify":
        args.append("--skip-build-test")
    proc = _run(args, project)
    if not proc.stdout.strip():
        raise AssertionError(f"bridge emitted no JSON: rc={proc.returncode} stderr={proc.stderr}")
    payload = json.loads(proc.stdout)
    payload["_returncode"] = proc.returncode
    return payload


def _commit(project: Path, filename: str, source: str) -> Path:
    path = project / filename
    path.write_text(source, encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", filename]):
        proc = _run(command, project)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
    proc = _run([
        "git", "-c", "user.name=feature-envy-check",
        "-c", "user.email=feature-envy@example.invalid",
        "commit", "-qm", "baseline",
    ], project)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return path


def _positive_case(
    language: str,
    filename: str,
    before: str,
    after: str,
    location: str,
    receiver: str,
) -> tuple[int, int, int]:
    with tempfile.TemporaryDirectory(prefix=f"feature-envy-{language}-") as temp:
        project = Path(temp)
        path = _commit(project, filename, before)
        baseline = _bridge(
            project,
            "capture-baseline",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert baseline["success"] is True, baseline
        metrics = baseline["metrics"]
        profile = metrics["detector_profile"]
        assert profile["version"].endswith("/feature_envy/v3"), profile
        assert (
            profile["definition"]
            == "tree-sitter-explicit-target-dominant-receiver-root-v1"
        ), profile
        assert (
            profile["metric"]
            == "alias-folded-expected-receiver-member-access"
        ), profile
        assert profile["receiver_identity"] == "canonical-root-identifier", profile
        assert profile["source_discovery"] == "forbidden", profile
        assert (
            profile["candidate_evaluation"]
            == "one-explicit-target-declaration-only"
        ), profile
        assert (
            profile["source_parseability_contract"]
            == "explicit-target-declaration-subtree-no-errors-v1"
        ), profile
        assert (
            profile["declaration_uniqueness_contract"]
            == "same-file-owner-name-full-parameter-fingerprint-exactly-one-v1"
        ), profile
        assert (
            profile["alias_folding"]
            == "simple-local-alias-root-provenance-v1"
        ), profile
        assert profile["finding_min_receiver_access"] == 4, profile
        assert profile["finding_min_receiver_ratio"] == 0.6, profile
        for java_only_field in (
            "type_resolution",
            "catalog_identity_schema",
            "finding_min_exclusive",
        ):
            assert java_only_field not in profile, profile
        assert (
            profile["target_patch_identity_contract"]
            == "feature-envy-wrapper-same-hunk-owner-name-signature-bijection-v1"
        ), profile
        # Deliberately wrong evidence must not change the explicit receiver.
        assert metrics["expected_receiver_name"] == receiver, metrics
        before_access = int(metrics["expected_receiver_access"])
        assert before_access >= 4, metrics

        path.write_text(after, encoding="utf-8")
        verified = _bridge(
            project,
            "verify",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert verified["success"] is True, verified
        current = verified["checkpoint"]["current_metrics"]
        assert current["target_missing"] is False, current
        assert current["target_patch_identity_ok"] is True, current
        assert current["scope_files"] == [filename], current
        assert current["finding_present"] is False, current
        after_access = int(current["expected_receiver_access"])
        assert after_access < before_access, current
        return before_access, after_access, len(current.get("wrapper_reanchors") or [])


def _negative_case(
    label: str,
    *,
    language: str,
    filename: str,
    before: str,
    after: str,
    location: str,
    receiver: str,
    expected_errors: set[str],
) -> str:
    with tempfile.TemporaryDirectory(prefix=f"feature-envy-negative-{label}-") as temp:
        project = Path(temp)
        path = _commit(project, filename, before)
        baseline = _bridge(
            project,
            "capture-baseline",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert baseline["success"] is True, baseline
        path.write_text(after, encoding="utf-8")
        verified = _bridge(
            project,
            "verify",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert verified["success"] is False, verified
        current = verified["checkpoint"]["current_metrics"]
        assert current["target_missing"] is True, current
        error = str(current.get("error") or "")
        assert error in expected_errors, current
        return error


def _baseline_identity_collision_case(
    label: str,
    *,
    language: str,
    filename: str,
    before: str,
    after: str,
    location: str,
    receiver: str,
) -> str:
    """A duplicate declaration identity must never produce a usable c000."""
    with tempfile.TemporaryDirectory(prefix=f"feature-envy-collision-{label}-") as temp:
        project = Path(temp)
        path = _commit(project, filename, before)
        baseline = _bridge(
            project,
            "capture-baseline",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert baseline["success"] is False, baseline
        assert "target_identity_collision" in str(baseline.get("error") or ""), baseline

        # Reproduce the exploit shape: delete the envied declaration and
        # format the same-signature decoy.  Since c000 failed closed, this
        # current tree cannot be accepted as continuity of the first target.
        path.write_text(after, encoding="utf-8")
        verified = _bridge(
            project,
            "verify",
            language=language,
            location=location,
            receiver=receiver,
        )
        assert verified["success"] is False, verified
        return "target_identity_collision"


def _ordinary_guard_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="feature-envy-ordinary-") as temp:
        project = Path(temp)
        (project / "demo.py").write_text(PYTHON_BEFORE, encoding="utf-8")
        locations = parse_locations("demo.py:method=total_price|line=10", project)
        config = SimpleNamespace(locations=locations, language="python")
        missing_checkpoint = _run_generic_feature_envy_guard(config, {}, None)
        assert missing_checkpoint["success"] is False, missing_checkpoint
        assert (
            missing_checkpoint["details"]["error"]
            == "feature_envy_checkpoint_contract_required"
        ), missing_checkpoint
        missing_target = _run_generic_feature_envy_guard(
            config,
            {},
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="feature_envy",
                current_metrics={
                    "ok": True,
                    "finding_present": False,
                    "target_missing": True,
                    "target_identity_collision": False,
                    "target_patch_identity_ok": False,
                },
            ),
        )
        assert missing_target["success"] is False, missing_target


def main() -> int:
    positives = {
        "python": _positive_case(
            "python", "demo.py", PYTHON_BEFORE, PYTHON_AFTER,
            "demo.py:method=total_price|line=10", "order",
        ),
        "c": _positive_case(
            "c", "demo.c", C_BEFORE, C_AFTER,
            "demo.c:method=total_price|line=3", "order",
        ),
        "cpp": _positive_case(
            "cpp", "demo.cpp", CPP_BEFORE, CPP_AFTER,
            "demo.cpp:method=total_price|line=3", "order",
        ),
    }
    assert sum(value[2] for value in positives.values()) >= 1, positives

    same_signature_before = PYTHON_BEFORE + """\


def total_price(order):
    return order
"""
    same_signature_after = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1


def total_price( order ):
    return order
"""
    same_hunk_decoys = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1


def extracted(order):
    return order.unit_price + order.quantity


def total_price( order ):
    return order


def total_price( order ):
    return order
"""
    filler = "\n".join(f"FILLER_{index} = {index}" for index in range(30))
    cross_hunk_before = PYTHON_BEFORE + "\n\n" + filler + "\n"
    cross_hunk_after = (
        PYTHON_BEFORE.split("\ndef total_price(order):", 1)[0]
        + "\n\n" + filler
        + "\n\n\ndef total_price(order):\n    return order\n"
    )
    owner_before = """\
struct Order { int a, b, c, d; };
struct Checkout {
    int total(Order *order) {
        int first = order->a;
        int second = order->b;
        int third = order->c;
        int fourth = order->d;
        return first + second + third + fourth;
    }
};
"""
    owner_after = """\
struct Order { int a, b, c, d; };
struct OtherCheckout {
    int total(Order *order) {
        int first = order->a;
        int second = order->b;
        int third = order->c;
        int fourth = order->d;
        return first + second + third + fourth;
    }
};
"""
    negatives = {
        "same_signature_decoy_deletion": _baseline_identity_collision_case(
            "same-signature",
            language="python", filename="demo.py",
            before=same_signature_before, after=same_signature_after,
            location="demo.py:method=total_price|line=10", receiver="order",
        ),
        "same_hunk_multiple_decoys": _negative_case(
            "same-hunk-decoys",
            language="python", filename="demo.py",
            before=PYTHON_BEFORE, after=same_hunk_decoys,
            location="demo.py:method=total_price|line=10", receiver="order",
            expected_errors={"target_identity_collision"},
        ),
        "cross_hunk_reanchor": _negative_case(
            "cross-hunk",
            language="python", filename="demo.py",
            before=cross_hunk_before, after=cross_hunk_after,
            location="demo.py:method=total_price|line=10", receiver="order",
            expected_errors={"frozen_target_patch_identity_unresolved"},
        ),
        "owner_change": _negative_case(
            "owner-change",
            language="cpp", filename="demo.cpp",
            before=owner_before, after=owner_after,
            location="demo.cpp:method=total|line=4", receiver="order",
            expected_errors={"frozen_target_declaration_missing"},
        ),
    }

    with tempfile.TemporaryDirectory(prefix="feature-envy-missing-selector-") as temp:
        project = Path(temp)
        _commit(project, "demo.py", PYTHON_BEFORE)
        missing = _bridge(
            project,
            "capture-baseline",
            language="python",
            location="demo.py:method=total_price|line=10",
            receiver=None,
        )
        assert missing["success"] is False, missing
        assert "missing_explicit_receiver_selector" in str(missing.get("error") or ""), missing

    _ordinary_guard_fail_closed()
    rendered = " ".join(
        f"{language}={before}->{after}/reanchor{reanchors}"
        for language, (before, after, reanchors) in positives.items()
    )
    print(
        "nonjava-feature-envy-contract PASS "
        f"{rendered} negatives={len(negatives)} "
        "baseline_collision=1 current_collision=1 ordinary_fail_closed=2"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
