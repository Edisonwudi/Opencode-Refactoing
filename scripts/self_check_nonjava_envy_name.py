#!/usr/bin/env python3
"""End-to-end checks for non-Java feature_envy and mysterious_name checkpoints.

Each case builds a temporary project, captures the immutable checkpoint
baseline on the smelly source, asserts the unchanged source fails verify, and
asserts the refactored source passes with a strictly decreased objective.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
CONFIG = ROOT / "runtime" / "python" / "smell_core" / "defaults" / "refactor.yaml"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), text=True, capture_output=True, check=False)


def _bridge(
    project: Path,
    command: str,
    language: str,
    smell: str,
    location: str,
    evidence: str,
) -> dict[str, object]:
    target_context: dict[str, str] = {}
    if smell == "feature_envy":
        match = re.search(
            r"(?:^|;\s*)(?:envied_receiver|envied_type)=([^;]+)",
            evidence,
        )
        if match:
            target_context["receiver_type"] = match.group(1).strip()
    elif smell == "mysterious_name":
        kind = re.search(r"(?:^|;\s*)kind=([^;]+)", evidence)
        name = re.search(r"(?:^|;\s*)name=([^;]+)", evidence)
        if kind:
            target_context["symbol_kind"] = kind.group(1).strip()
        if name:
            target_context["symbol_name"] = name.group(1).strip()
    args = [
        sys.executable,
        str(BRIDGE),
        command,
        "--config",
        str(CONFIG),
        "--project-root",
        str(project),
        "--language",
        language,
        "--smell",
        smell,
        "--location",
        location,
        "--smell-evidence",
        evidence,
    ]
    if target_context:
        args.extend([
            "--target-context-json",
            json.dumps(target_context, separators=(",", ":"), sort_keys=True),
        ])
    if command == "verify":
        args.append("--skip-build-test")
    result = _run(args, project)
    if result.returncode not in {0, 1}:
        raise AssertionError(f"{language}/{smell} {command}: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _case(
    language: str,
    smell: str,
    filename: str,
    before: str,
    after: str,
    location: str,
    evidence: str,
    objective: str,
) -> tuple[float, float]:
    with tempfile.TemporaryDirectory(prefix=f"nonjava-{language}-{smell}-") as temp_dir:
        project = Path(temp_dir)
        source = project / filename
        source.write_text(before, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", filename]):
            result = _run(command, project)
            if result.returncode != 0:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project)
        if result.returncode != 0:
            raise AssertionError(result.stderr)

        baseline = _bridge(project, "capture-baseline", language, smell, location, evidence)
        assert baseline["success"] is True, baseline
        if smell == "feature_envy" and language != "java":
            profile = baseline["metrics"]["detector_profile"]
            assert profile["reject_same_owner_receiver_relocation"] is False, profile
        before_value = float(baseline["metrics"]["objectives"][objective])
        assert before_value > 0, baseline
        unchanged = _bridge(project, "verify", language, smell, location, evidence)
        assert unchanged["success"] is False, unchanged

        source.write_text(after, encoding="utf-8")
        repaired = _bridge(project, "verify", language, smell, location, evidence)
        assert repaired["success"] is True, repaired
        delta = repaired["checkpoint"]["delta"]
        assert delta["has_production_diff"] is True and delta["metric_progress"] is True, repaired
        after_value = float(delta["objectives"][objective]["after"])
        assert after_value < before_value, delta
        return before_value, after_value


def _case_expect_fail(
    language: str,
    smell: str,
    filename: str,
    before: str,
    gamed_after: str,
    location: str,
    evidence: str,
    objective: str,
) -> tuple[float, float]:
    """Anti-gaming case: the metric-gaming rewrite must NOT pass verify.

    Alias folding attributes cached-field reads back to the original receiver,
    so the checkpoint objective must not decrease and verify must fail.
    """
    with tempfile.TemporaryDirectory(prefix=f"nonjava-{language}-{smell}-gamed-") as temp_dir:
        project = Path(temp_dir)
        source = project / filename
        source.write_text(before, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", filename]):
            result = _run(command, project)
            if result.returncode != 0:
                raise AssertionError(result.stderr)
        result = _run([
            "git", "-c", "user.name=checkpoint-self-check", "-c",
            "user.email=checkpoint@example.invalid", "commit", "-qm", "baseline",
        ], project)
        if result.returncode != 0:
            raise AssertionError(result.stderr)

        baseline = _bridge(project, "capture-baseline", language, smell, location, evidence)
        assert baseline["success"] is True, baseline
        before_value = float(baseline["metrics"]["objectives"][objective])
        assert before_value > 0, baseline

        source.write_text(gamed_after, encoding="utf-8")
        gamed = _bridge(project, "verify", language, smell, location, evidence)
        assert gamed["success"] is False, gamed
        delta = gamed["checkpoint"]["delta"]
        assert delta["has_production_diff"] is True and delta["metric_progress"] is False, gamed
        after_value = float(delta["objectives"][objective]["after"])
        assert after_value >= before_value, delta
        return before_value, after_value


PYTHON_ENVY_BEFORE = """\
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
PYTHON_ENVY_AFTER = """\
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
PYTHON_ENVY_CHEAT_AFTER = """\
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
PYTHON_ENVY_BARE_CHEAT_AFTER = """\
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
PYTHON_NAME_BEFORE = """\
def proc(d, tmp):
    q = d + tmp
    return q
"""
PYTHON_NAME_AFTER = """\
def proc(discount, total):
    result = discount + total
    return result
"""
PYTHON_NAME_SCOPED_BEFORE = """\
def proc(d, tmp):
    q = d + tmp
    return q


def other(d):
    return d * 2
"""
PYTHON_NAME_SCOPED_AFTER = """\
def proc(discount, tmp):
    q = discount + tmp
    return q


def other(d):
    return d * 2
"""
C_ENVY_BEFORE = """\
struct Order {
    int unit_price;
    int quantity;
    int tax_rate;
    int shipping_fee;
    int discount;
};

static int total_price(struct Order *order) {
    int subtotal = order->unit_price * order->quantity;
    int tax = subtotal * order->tax_rate;
    int shipping = order->shipping_fee;
    int discount = order->discount;
    int total = subtotal + tax + shipping - discount;
    return total;
}
"""
C_ENVY_AFTER = """\
struct Order {
    int unit_price;
    int quantity;
    int tax_rate;
    int shipping_fee;
    int discount;
};

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
C_ENVY_CHEAT_AFTER = """\
struct Order {
    int unit_price;
    int quantity;
    int tax_rate;
    int shipping_fee;
    int discount;
};

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
C_ENVY_BARE_CHEAT_AFTER = """\
struct Order {
    int unit_price;
    int quantity;
    int tax_rate;
    int shipping_fee;
    int discount;
};

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


def main() -> int:
    results = {
        "python/feature_envy": _case(
            "python",
            "feature_envy",
            "demo.py",
            PYTHON_ENVY_BEFORE,
            PYTHON_ENVY_AFTER,
            "demo.py:method=total_price|line=10",
            "envied_receiver=order; foreign_access=5",
            "expected_receiver_access",
        ),
        "python/mysterious_name": _case(
            "python",
            "mysterious_name",
            "demo.py",
            PYTHON_NAME_BEFORE,
            PYTHON_NAME_AFTER,
            "demo.py:method=proc|line=1",
            "kind=param; name=d; reason=too_short; len=1",
            "target_suspicious_name_present",
        ),
        "c/feature_envy": _case(
            "c",
            "feature_envy",
            "demo.c",
            C_ENVY_BEFORE,
            C_ENVY_AFTER,
            "demo.c:method=total_price|line=9",
            "envied_type=Order; foreign_access=5",
            "expected_receiver_access",
        ),
        "python/mysterious_name_scoped": _case(
            "python",
            "mysterious_name",
            "demo.py",
            PYTHON_NAME_SCOPED_BEFORE,
            PYTHON_NAME_SCOPED_AFTER,
            "demo.py:method=proc|line=1",
            "kind=param; name=d; reason=too_short; len=1",
            "target_suspicious_name_present",
        ),
    }
    rendered = " ".join(f"{name}={before:g}->{after:g}" for name, (before, after) in results.items())
    print(f"nonjava-envy-name-self-check PASS unchanged_pass=0 {rendered}")
    gamed = {
        "python/feature_envy[gamed]": _case_expect_fail(
            "python",
            "feature_envy",
            "demo.py",
            PYTHON_ENVY_BEFORE,
            PYTHON_ENVY_CHEAT_AFTER,
            "demo.py:method=total_price|line=10",
            "envied_receiver=order; foreign_access=5",
            "expected_receiver_access",
        ),
        "python/feature_envy[bare-gamed]": _case_expect_fail(
            "python",
            "feature_envy",
            "demo.py",
            PYTHON_ENVY_BEFORE,
            PYTHON_ENVY_BARE_CHEAT_AFTER,
            "demo.py:method=total_price|line=10",
            "envied_receiver=order; foreign_access=5",
            "expected_receiver_access",
        ),
        "c/feature_envy[gamed]": _case_expect_fail(
            "c",
            "feature_envy",
            "demo.c",
            C_ENVY_BEFORE,
            C_ENVY_CHEAT_AFTER,
            "demo.c:method=total_price|line=9",
            "envied_type=Order; foreign_access=5",
            "expected_receiver_access",
        ),
        "c/feature_envy[bare-gamed]": _case_expect_fail(
            "c",
            "feature_envy",
            "demo.c",
            C_ENVY_BEFORE,
            C_ENVY_BARE_CHEAT_AFTER,
            "demo.c:method=total_price|line=9",
            "envied_type=Order; foreign_access=5",
            "expected_receiver_access",
        ),
    }
    rendered_gamed = " ".join(f"{name}={before:g}->{after:g}" for name, (before, after) in gamed.items())
    print(f"nonjava-envy-alias-folding PASS gamed_verify_failed={len(gamed)} {rendered_gamed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
