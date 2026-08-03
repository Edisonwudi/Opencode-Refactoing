#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.java.semantic_detector import (  # noqa: E402
    build_refused_bequest_impact_map,
    run_java_semantic_detector,
)
from smell_core.checkpoint_adapters import (  # noqa: E402
    _compact_refused_bequest_impact_map,
)


SOURCE = """\
abstract class Page {
    protected int pageId;

    abstract void setChild(Page child);

    protected int pageId() {
        return pageId;
    }
}

final class Leaf extends Page {
    @Override
    void setChild(Page child) {
        throw new UnsupportedOperationException();
    }
}

final class Branch extends Page {
    @Override
    void setChild(Page child) {
        this.child = child;
    }

    private Page child;
}

final class Caller {
    private Page root;
    private Unrelated unrelated;

    void attach(Page page, Page child) {
        Page local = page;
        page.setChild(child);
        local.setChild(child);
        root.setChild(child);
        lookup().setChild(child);
        unrelated.setChild(child);
    }

    Page lookup() {
        return root;
    }
}

final class Unrelated {
    void setChild(Page child) {
    }
}
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="refused-bequest-plan-") as tmp:
        project = Path(tmp)
        source = project / "src" / "main" / "java" / "Hierarchy.java"
        source.parent.mkdir(parents=True)
        source.write_text(SOURCE, encoding="utf-8")
        detection = run_java_semantic_detector(project)
        assert detection.ok and detection.project_model is not None, detection
        payload = build_refused_bequest_impact_map(
            project,
            target_file=source,
            method="setChild",
            line=7,
            reported_parent="Page",
            target_parameter_count=1,
            target_class_name="Leaf",
            project_model=detection.project_model,
        )

    assert payload["ok"] is True, payload
    assert payload["target"]["class"].endswith("Leaf"), payload["target"]
    declarations = payload["contract_declarations"]
    assert any(
        item["owner"].endswith("Page") and item["body_kind"] == "abstract_or_bodyless"
        for item in declarations
    ), declarations
    roles = {item["class"].rsplit(".", 1)[-1]: item["role"] for item in payload["implementers"]}
    assert roles["Leaf"].startswith("refusing_target:rejecting_or_stub"), roles
    assert roles["Branch"] == "real_implementer", roles
    call_sites = payload["production_call_sites"]
    assert len(call_sites) == 4, call_sites
    resolutions = {item["receiver"]: item["receiver_resolution"] for item in call_sites}
    assert resolutions["page"] == "parameter", resolutions
    assert resolutions["local"] == "local_variable", resolutions
    assert resolutions["root"] == "field", resolutions
    assert resolutions["lookup()"] == "method_return", resolutions
    assert payload["unresolved_receiver_call_sites"] == 0, payload
    assert payload["excluded_unrelated_same_name_calls"] == 1, payload
    inherited_surface = payload["inherited_surface_at_risk"]
    assert inherited_surface[0]["owner"].endswith("Page"), inherited_surface
    assert {"name": "pageId", "type": "int"} in inherited_surface[0]["state_fields"], inherited_surface
    assert any(
        item["signature"].startswith("pageId(")
        for item in inherited_surface[0]["non_target_methods"]
    ), inherited_surface
    contract = payload["target_contract"]
    assert contract["ok"] is True, contract
    assert contract["direct_superclass"].endswith("Page"), contract
    assert any(
        item["api_key"].startswith("pageId(")
        for item in contract["visible_non_target_methods"]
    ), contract
    assert all(
        not item["api_key"].startswith("setChild(")
        for item in contract["visible_non_target_methods"]
    ), contract
    compact = _compact_refused_bequest_impact_map(payload)
    assert compact["analysis_ok"] is True, compact
    assert len(compact["contract_declarations"]) == len(declarations), compact
    assert len(compact["implementers"]) == len(payload["implementers"]), compact
    assert len(compact["production_call_sites"]) == len(call_sites), compact
    assert compact["remaining_work_count"] == (
        len(declarations) + len(payload["implementers"]) + len(call_sites)
    ), compact
    assert "target_contract" not in compact, compact
    print("refused bequest plan self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
