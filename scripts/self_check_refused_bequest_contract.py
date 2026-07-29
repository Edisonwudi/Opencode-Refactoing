#!/usr/bin/env python3
"""Route-independent Refused Bequest compatibility contract checks.

The fixtures use generic hierarchy names and exercise more than one legal
capability-split topology.  They intentionally do not mirror any delivery
project, sample id, or production method name.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.checkpoint_contract import evaluate_checkpoint_contract  # noqa: E402
from smell_core.java.semantic_detector import build_refused_bequest_impact_map  # noqa: E402


BASELINE = """\
abstract class CapabilityRoot {
    CapabilityRoot(int seed) {}
    public String label() { return "root"; }
    protected int marker() { return 1; }
    abstract void mutate();
}

final class RejectingLeaf extends CapabilityRoot {
    public RejectingLeaf(int seed) { super(seed); }
    public String ownView() { return label(); }
    @Override void mutate() { throw new UnsupportedOperationException(); }
}

final class WorkingBranch extends CapabilityRoot {
    WorkingBranch(int seed) { super(seed); }
    @Override void mutate() {}
}
"""


NARROW_INTERFACE = """\
interface MutableCapability {
    void mutate();
}

abstract class CapabilityRoot {
    CapabilityRoot(int seed) {}
    public String label() { return "root"; }
    protected int marker() { return 1; }
}

final class RejectingLeaf extends CapabilityRoot {
    public RejectingLeaf(int seed) { super(seed); }
    public String ownView() { return label(); }
}

final class WorkingBranch extends CapabilityRoot implements MutableCapability {
    WorkingBranch(int seed) { super(seed); }
    @Override public void mutate() {}
}
"""


INTERMEDIATE_BASE = """\
interface MutableCapability {
    void mutate();
}

abstract class VisibleIdentity {
    VisibleIdentity(int seed) {}
    public String label() { return "root"; }
    protected int marker() { return 1; }
}

abstract class CapabilityRoot extends VisibleIdentity implements MutableCapability {
    CapabilityRoot(int seed) { super(seed); }
}

final class RejectingLeaf extends VisibleIdentity {
    public RejectingLeaf(int seed) { super(seed); }
    public String ownView() { return label(); }
}

final class WorkingBranch extends CapabilityRoot {
    WorkingBranch(int seed) { super(seed); }
    @Override public void mutate() {}
}
"""


API_LOSS = """\
interface MutableCapability {
    void mutate();
}

abstract class CapabilityRoot implements MutableCapability {
}

final class RejectingLeaf {
    public RejectingLeaf() {}
    public String ownView() { return "detached"; }
}

final class WorkingBranch extends CapabilityRoot {
    @Override public void mutate() {}
}
"""


INHERITED_SHEDDING = """\
interface MutableCapability {
    void mutate();
}

abstract class CapabilityRoot implements MutableCapability {
}

final class RejectingLeaf {
    public RejectingLeaf(int seed) {}
    public String ownView() { return "detached"; }
}

final class WorkingBranch extends CapabilityRoot {
    @Override public void mutate() {}
}
"""


VISIBILITY_NARROWING = """\
interface MutableCapability {
    void mutate();
}

abstract class VisibleIdentity {
    VisibleIdentity(int seed) {}
    protected String label() { return "root"; }
    protected int marker() { return 1; }
}

abstract class CapabilityRoot implements MutableCapability {
}

final class RejectingLeaf extends VisibleIdentity {
    public RejectingLeaf(int seed) { super(seed); }
    protected String ownView() { return label(); }
}

final class WorkingBranch extends CapabilityRoot {
    @Override public void mutate() {}
}
"""


def contract_snapshot(project: Path, source_text: str) -> dict:
    source = project / "src" / "main" / "java" / "Hierarchy.java"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(source_text, encoding="utf-8")
    impact = build_refused_bequest_impact_map(
        project,
        target_file=source,
        method="mutate",
        line=8,
        reported_parent="CapabilityRoot",
        target_parameter_count=0,
        target_class_name="RejectingLeaf",
    )
    assert impact["ok"] is True, impact
    contract = impact["target_contract"]
    assert contract["ok"] is True, contract
    return contract


def evaluate(before: dict, after: dict):
    return evaluate_checkpoint_contract(
        {
            "ok": True,
            "objectives": {"rejection_signals": 1.0},
            "contract_snapshot": before,
        },
        {
            "ok": True,
            "objectives": {"rejection_signals": 0.0},
            "contract_snapshot": after,
        },
        has_production_diff=True,
        smell="refused_bequest",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="refused-contract-") as raw:
        project = Path(raw)
        baseline = contract_snapshot(project, BASELINE)

        narrow = evaluate(baseline, contract_snapshot(project, NARROW_INTERFACE))
        assert narrow.metric_progress is True, narrow
        assert narrow.semantic_contract_preserved is True, narrow

        intermediate = evaluate(baseline, contract_snapshot(project, INTERMEDIATE_BASE))
        assert intermediate.metric_progress is True, intermediate
        assert intermediate.semantic_contract_preserved is True, intermediate
        assert intermediate.semantic_contract_delta["superclass_changed"] is True, intermediate

        inherited_shedding = evaluate(
            baseline,
            contract_snapshot(project, INHERITED_SHEDDING),
        )
        assert inherited_shedding.metric_progress is True, inherited_shedding
        assert inherited_shedding.semantic_contract_preserved is True, inherited_shedding
        assert any(
            item.startswith("missing_inherited_method:label(")
            for item in inherited_shedding.semantic_contract_delta["review_signals"]
        ), inherited_shedding.semantic_contract_delta

        api_loss = evaluate(baseline, contract_snapshot(project, API_LOSS))
        assert api_loss.metric_progress is False, api_loss
        assert api_loss.reason == "SEMANTIC_CONTRACT_REGRESSION", api_loss
        regressions = api_loss.semantic_contract_delta["regressions"]
        assert any(item.startswith("missing_constructor:RejectingLeaf(") for item in regressions), regressions
        assert any(
            item.startswith("missing_inherited_method:label(")
            for item in api_loss.semantic_contract_delta["review_signals"]
        ), api_loss.semantic_contract_delta

        narrowed = evaluate(baseline, contract_snapshot(project, VISIBILITY_NARROWING))
        assert narrowed.metric_progress is False, narrowed
        assert narrowed.reason == "SEMANTIC_CONTRACT_REGRESSION", narrowed
        assert any(
            item.startswith("narrowed_declared_method:ownView(")
            for item in narrowed.semantic_contract_delta["regressions"]
        ), narrowed.semantic_contract_delta

    print(
        "refused bequest contract self-check: PASS "
        "legal_routes=3 rejected_regressions=2 sample_specific_rules=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
