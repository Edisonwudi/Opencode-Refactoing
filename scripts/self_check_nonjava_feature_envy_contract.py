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

from smell_core.guards import run_smell_guards  # noqa: E402
from smell_core.guards.context import GuardRunContext  # noqa: E402
from smell_core.feature_envy import feature_envy_metric_budget  # noqa: E402
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

PYTHON_ALIAS_GAMED = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1


def total_price(order):
    up = order.unit_price
    q = order.quantity
    tr = order.tax_rate
    sf = order.shipping_fee
    dc = order.discount
    subtotal = up * q
    tax = subtotal * tr
    shipping = sf
    discount = dc
    total = subtotal + tax + shipping - discount
    return total
"""
PYTHON_BARE_ALIAS_GAMED = """\
class Order:
    def __init__(self):
        self.unit_price = 10
        self.quantity = 2
        self.tax_rate = 5
        self.shipping_fee = 3
        self.discount = 1
        self.tracking_info = "T-1"


def total_price(order):
    ti = order.tracking_info
    up, q = order.unit_price, order.quantity
    tr = order.tax_rate
    sf = order.shipping_fee
    dc = order.discount
    label = ti
    subtotal = up * q
    tax = subtotal + tr
    total = subtotal + tax + sf - dc
    return label, total
"""
C_ALIAS_GAMED = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

static int total_price(struct Order *order) {
    int up = order->unit_price;
    int q = order->quantity;
    int tr = order->tax_rate;
    int sf = order->shipping_fee;
    int dc = order->discount;
    int subtotal = up * q;
    int tax = subtotal * tr;
    int shipping = sf;
    int discount = dc;
    int total = subtotal + tax + shipping - discount;
    return total;
}
"""
C_BARE_ALIAS_GAMED = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

static int total_price(struct Order *order) {
    int up = order->unit_price;
    int q = order->quantity;
    int tr = order->tax_rate;
    int sf = order->shipping_fee;
    int dc = order->discount;
    int total = up * q;
    total += tr;
    total += sf;
    total -= dc;
    return total;
}
"""

PYTHON_DENOMINATOR_BEFORE = """\
class Calculator:
    def total_price(self, order):
        subtotal = order.unit_price * order.quantity
        tax = subtotal * order.tax_rate
        shipping = order.shipping_fee
        discount = order.discount
        return subtotal + tax + shipping - discount
"""
PYTHON_DENOMINATOR_GAMED = """\
class Calculator:
    def total_price(self, order):
        subtotal = order.unit_price * order.quantity
        tax = subtotal * order.tax_rate
        shipping = order.shipping_fee
        discount = order.discount
        unrelated = self.a + self.b + self.c + self.d + self.e
        return subtotal + tax + shipping - discount + (unrelated * 0)
"""
PYTHON_WRONG_SELECTOR = """\
class Calculator:
    def total_price(self, customer, order):
        first = customer.unit_price + customer.quantity
        second = customer.tax_rate + customer.shipping_fee
        selected = order.discount
        padding = 1
        return first + second + selected + padding
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
        assert profile["version"].endswith("/feature_envy/v5"), profile
        assert profile["definition"] == (
            "tree-sitter-explicit-target-access-and-ratio-predicate-v2"
        ), profile
        assert profile["ratio_denominator_contract"] == (
            "baseline-capped-current-member-access-v1"
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
            == "complete-target-boundary-with-frozen-recovery-no-additions-v2"
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


def _alias_folding_anti_gaming_cases() -> dict[str, tuple[float, float]]:
    cases = (
        (
            "python/feature_envy[gamed]",
            "python",
            "demo.py",
            PYTHON_BEFORE,
            PYTHON_ALIAS_GAMED,
            "demo.py:method=total_price|line=10",
        ),
        (
            "python/feature_envy[bare-gamed]",
            "python",
            "demo.py",
            PYTHON_BEFORE,
            PYTHON_BARE_ALIAS_GAMED,
            "demo.py:method=total_price|line=10",
        ),
        (
            "c/feature_envy[gamed]",
            "c",
            "demo.c",
            C_BEFORE,
            C_ALIAS_GAMED,
            "demo.c:method=total_price|line=3",
        ),
        (
            "c/feature_envy[bare-gamed]",
            "c",
            "demo.c",
            C_BEFORE,
            C_BARE_ALIAS_GAMED,
            "demo.c:method=total_price|line=3",
        ),
    )
    results: dict[str, tuple[float, float]] = {}
    for label, language, filename, before, gamed_after, location in cases:
        with tempfile.TemporaryDirectory(
            prefix=f"feature-envy-alias-{language}-"
        ) as temp:
            project = Path(temp)
            path = _commit(project, filename, before)
            baseline = _bridge(
                project,
                "capture-baseline",
                language=language,
                location=location,
                receiver="order",
            )
            assert baseline["success"] is True, (label, baseline)
            before_value = float(
                baseline["metrics"]["objectives"]["expected_receiver_access"]
            )
            assert before_value > 0, (label, baseline)

            path.write_text(gamed_after, encoding="utf-8")
            gamed = _bridge(
                project,
                "verify",
                language=language,
                location=location,
                receiver="order",
            )
            assert gamed["success"] is False, (label, gamed)
            delta = gamed["checkpoint"]["delta"]
            assert delta["has_production_diff"] is True, (label, delta)
            assert delta["metric_progress"] is False, (label, delta)
            after_value = float(
                delta["objectives"]["expected_receiver_access"]["after"]
            )
            assert after_value >= before_value, (label, delta)
            results[label] = (before_value, after_value)
    return results


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
        config = SimpleNamespace(
            locations=locations,
            language="python",
            smell="feature_envy",
            profile=SimpleNamespace(guards=[{"type": "feature_envy"}]),
        )
        missing_checkpoint = run_smell_guards(config)
        assert len(missing_checkpoint) == 1, missing_checkpoint
        assert missing_checkpoint[0]["success"] is False, missing_checkpoint
        assert (
            missing_checkpoint[0]["details"]["reason"]
            == "BASELINE_CHECKPOINT_MISSING"
        ), missing_checkpoint
        missing_target = run_smell_guards(
            config,
            GuardRunContext(
                checkpoint_required=True,
                checkpoint_smell="feature_envy",
                checkpoint={
                    "required": True,
                    "smell": "feature_envy",
                    "current_metrics": {
                        "ok": True,
                        "candidate_count": 0,
                        "finding_present": False,
                        "target_missing": True,
                        "target_identity_collision": False,
                        "target_patch_identity_ok": False,
                    },
                    "delta": {
                        "metric_progress": False,
                        "reason": "TARGET_NOT_LOCATED",
                    },
                },
            ),
        )
        assert len(missing_target) == 1, missing_target
        assert missing_target[0]["success"] is False, missing_target
        assert missing_target[0]["details"]["reason"] == "TARGET_NOT_LOCATED", missing_target


def _exact_budget_and_denominator_contract() -> None:
    access_boundary = feature_envy_metric_budget(
        method_loc=8,
        receiver_access=3,
        total_member_access=3,
        ratio_denominator_cap=3,
    )
    assert access_boundary["finding_present"] is False, access_boundary
    assert access_boundary["receiver_access_passing_max"] == 3, access_boundary
    assert access_boundary["receiver_access_required_reduction"] == 0, access_boundary
    assert access_boundary["receiver_ratio"] == 1.0, access_boundary
    assert access_boundary["required_receiver_access_reduction"] == 0, access_boundary

    ratio_boundary = feature_envy_metric_budget(
        method_loc=8,
        receiver_access=4,
        total_member_access=7,
        ratio_denominator_cap=7,
    )
    assert ratio_boundary["finding_present"] is False, ratio_boundary
    assert ratio_boundary["receiver_ratio"] == 0.571429, ratio_boundary
    assert ratio_boundary["receiver_ratio_required_access_reduction"] == 0, ratio_boundary
    assert ratio_boundary["required_receiver_access_reduction"] == 0, ratio_boundary

    both_active = feature_envy_metric_budget(
        method_loc=8,
        receiver_access=4,
        total_member_access=6,
        ratio_denominator_cap=6,
    )
    assert both_active["finding_present"] is True, both_active
    assert both_active["receiver_access_required_reduction"] == 1, both_active
    # 4/6 -> 3/5 is exactly 0.6 and therefore still a finding; two access
    # removals are required to cross the strict ratio boundary.
    assert both_active["receiver_ratio_required_access_reduction"] == 2, both_active
    assert both_active["required_receiver_access_reduction"] == 1, both_active
    assert (
        both_active["receiver_pass_when"]
        == "receiver_access <= 3 OR receiver_ratio < 0.6"
    ), both_active
    assert (
        both_active["finding_when"]
        == "method_loc >= 5 AND receiver_access >= 4 AND receiver_ratio >= 0.6"
    ), both_active

    with tempfile.TemporaryDirectory(prefix="feature-envy-denominator-") as temp:
        project = Path(temp)
        path = _commit(project, "demo.py", PYTHON_DENOMINATOR_BEFORE)
        baseline = _bridge(
            project,
            "capture-baseline",
            language="python",
            location="demo.py:method=total_price|line=2",
            receiver="order",
        )
        assert baseline["success"] is True, baseline
        baseline_metrics = baseline["metrics"]
        # Alias folding preserves the receiver provenance of subtotal/tax, so
        # this fixture has seven effective receiver accesses at baseline.
        assert baseline_metrics["ratio_denominator_cap"] == 7, baseline_metrics
        assert baseline_metrics["dominant_receiver_ratio"] == 1.0, baseline_metrics
        assert baseline_metrics["feature_envy_budget"]["finding_present"] is True, baseline_metrics

        path.write_text(PYTHON_DENOMINATOR_GAMED, encoding="utf-8")
        verified = _bridge(
            project,
            "verify",
            language="python",
            location="demo.py:method=total_price|line=2",
            receiver="order",
        )
        assert verified["success"] is False, verified
        current = verified["checkpoint"]["current_metrics"]
        assert current["total_member_access"] == 12, current
        assert current["raw_dominant_receiver_ratio"] == 0.583333, current
        assert current["ratio_denominator_cap"] == 7, current
        assert current["ratio_denominator_growth_ignored"] == 5, current
        assert current["dominant_receiver_ratio"] == 1.0, current
        budget = current["feature_envy_budget"]
        assert budget["receiver_access"] == 7, budget
        assert budget["receiver_access_finding_min"] == 4, budget
        assert budget["receiver_access_passing_max"] == 3, budget
        assert budget["receiver_access_required_reduction"] == 4, budget
        assert budget["receiver_ratio_finding_min"] == 0.6, budget
        assert budget["receiver_ratio_required_access_reduction"] == 3, budget
        assert budget["required_receiver_access_reduction"] == 3, budget
        assert budget["finding_present"] is True, budget


def _explicit_receiver_must_own_baseline_finding() -> None:
    with tempfile.TemporaryDirectory(prefix="feature-envy-selector-") as temp:
        project = Path(temp)
        _commit(project, "demo.py", PYTHON_WRONG_SELECTOR)
        baseline = _bridge(
            project,
            "capture-baseline",
            language="python",
            location="demo.py:method=total_price|line=2",
            receiver="order",
        )
        assert baseline["success"] is False, baseline
        rendered = json.dumps(baseline, sort_keys=True)
        assert "BASELINE_FINDING_NOT_FOUND" in rendered, baseline


def _selected_target_parser_recovery_is_frozen() -> None:
    """A macro-shaped recovery remains bounded baseline evidence."""

    before = """\
#define API_EXPORT
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };

API_EXPORT int total_price(struct Order *order) {
    int subtotal = order->unit_price * order->quantity;
    int tax = subtotal * order->tax_rate;
    int shipping = order->shipping_fee;
    int discount = order->discount;
    return subtotal + tax + shipping - discount;
}
"""
    with tempfile.TemporaryDirectory(prefix="feature-envy-parser-recovery-") as raw:
        project = Path(raw)
        _commit(project, "demo.c", before)
        baseline = _bridge(
            project,
            "capture-baseline",
            language="c",
            location="demo.c:method=total_price|line=4",
            receiver="order",
        )
        assert baseline["success"] is True, baseline
        metrics = baseline["metrics"]
        assert metrics["parser_recovery_required"] is True, metrics
        assert metrics["target_file_parseable"] is False, metrics
        assert metrics["target_declaration_boundary_complete"] is True, metrics
        assert list(metrics["target_syntax_issue_witnesses"]), metrics


def _conditional_declarator_recovery_keeps_exact_boundary() -> None:
    """Conditional alternatives may share one body without widening the target."""

    before = """\
struct Order { int unit_price, quantity, tax_rate, shipping_fee, discount; };
int total_price(struct Order *order) {
    int value = 0;
    for (int i = 0; i < 2; i++) {
        if (i) {
#ifdef USE_UNIT_PRICE
            if (order->unit_price) {
#else
            if (order->quantity) {
                value += order->unit_price;
#endif
                if (order->tax_rate) { value += order->shipping_fee; }
            } else { value += order->discount; }
        }
    }
    return value + order->quantity;
}
int unrelated(void) { return 0; }
"""
    with tempfile.TemporaryDirectory(prefix="feature-envy-conditional-recovery-") as raw:
        project = Path(raw)
        _commit(project, "demo.c", before)
        baseline = _bridge(
            project,
            "capture-baseline",
            language="c",
            location="demo.c:method=total_price|line=2",
            receiver="order",
        )
        assert baseline["success"] is True, baseline
        metrics = baseline["metrics"]
        assert metrics["target_declaration_boundary_complete"] is True, metrics
        identity = metrics["finding_identity"]
        assert identity["begin_line"] == 2, identity


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
    _exact_budget_and_denominator_contract()
    _explicit_receiver_must_own_baseline_finding()
    _selected_target_parser_recovery_is_frozen()
    _conditional_declarator_recovery_keeps_exact_boundary()
    alias_gaming = _alias_folding_anti_gaming_cases()
    rendered = " ".join(
        f"{language}={before}->{after}/reanchor{reanchors}"
        for language, (before, after, reanchors) in positives.items()
    )
    print(
        "nonjava-feature-envy-contract PASS "
        f"{rendered} negatives={len(negatives)} "
        "baseline_collision=1 current_collision=1 ordinary_fail_closed=2 "
        "exact_budget=3 denominator_gaming=1 selector_mismatch_rejected=1"
    )
    rendered_alias_gaming = " ".join(
        f"{name}={before:g}->{after:g}"
        for name, (before, after) in alias_gaming.items()
    )
    print(
        "nonjava-envy-alias-folding PASS "
        f"gamed_verify_failed={len(alias_gaming)} {rendered_alias_gaming}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
