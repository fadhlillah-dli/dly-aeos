"""§29 / §16 diagrams checked against their required sources (labels and names; never runtime proof):
context/component vs InstallManifest/inventory components, sequence vs the API route contract, data flow vs the
data inventory (and threat-model trust boundaries), deployment vs registered deployment targets."""

from __future__ import annotations

from aeos.docs_engine import visuals as vis
from aeos.docs_engine.plan_content import DiagramView


def _visual(kind: str, source: str) -> vis.VisualArtifact:
    import tempfile
    from pathlib import Path

    view = DiagramView(key=kind, title=kind, kind=kind, language="mermaid", source=source, verification_source="")
    # render_visuals keeps the Mermaid source next to the (disabled) render: never in the working directory
    return vis.render_visuals([view], Path(tempfile.mkdtemp(prefix="aeos-vis-test-")), enabled=False)[0]


def test_flowchart_nodes_are_parsed_without_edge_labels_or_subgraph_titles():
    src = ("flowchart LR\n  subgraph Host [Mesin lokal]\n  ui[Control Center] -->|REST| api((dly-aeos-control-plane))\n"
           "  api --> db[(SQLite)]\n  api -.-> vault\n  end\n  classDef x fill:#fff\n")
    nodes = vis.flow_nodes(src)
    assert nodes == {"ui": "Control Center", "api": "dly-aeos-control-plane", "db": "SQLite", "vault": "vault"}


def test_component_diagram_is_checked_against_registered_components():
    known = {"dly-aeos-control-plane", "SQLite metadata store", "openbao", "Control Center"}
    ok = _visual("component", "flowchart LR\n  a[Control Center] --> b[dly-aeos-control-plane] --> openbao")
    bad = _visual("context", "flowchart LR\n  a[Control Center] --> k[Kafka cluster]")
    vis.verify_visuals([ok, bad], known_components=known)
    assert ok.verification_status == "CONSISTENT"
    assert bad.verification_status == "MISMATCH" and "Kafka cluster" in bad.verification_notes[0]
    untouched = _visual("context", "flowchart LR\n  a --> b")
    vis.verify_visuals([untouched])
    assert untouched.verification_status == "UNVERIFIED", "no reference -> never claimed consistent"


def test_sequence_paths_must_exist_in_the_route_contract():
    routes = {"/api/plans", "/api/plans/{plan_id}", "/api/plans/{plan_id}/approve"}
    ok = _visual("sequence", "sequenceDiagram\n  UI->>API: GET /api/plans\n  UI->>API: POST /api/plans/42/approve")
    bad = _visual("sequence", "sequenceDiagram\n  UI->>API: POST /api/plans/42/merge")
    none = _visual("sequence", "sequenceDiagram\n  A->>B: hello")
    vis.verify_visuals([ok, bad, none], known_routes=routes)
    assert ok.verification_status == "CONSISTENT" and "integration test" in ok.verification_notes[0]
    assert bad.verification_status == "MISMATCH" and "/api/plans/42/merge" in bad.verification_notes[0]
    assert none.verification_status == "UNVERIFIED"


def test_data_flow_must_show_every_data_inventory_item_and_trust_boundary():
    ok = _visual("data_flow", "flowchart LR\n  u[Data tugas] --> b[Browser boundary]")
    bad = _visual("data_flow", "flowchart LR\n  u[Data tugas] --> x[API]")
    vis.verify_visuals([ok, bad], trust_boundaries=["Browser boundary"], data_items=["Data tugas"])
    assert ok.verification_status == "CONSISTENT"
    vis.verify_visuals([bad], trust_boundaries=[], data_items=["Data tugas", "Email pengguna"])
    assert bad.verification_status == "MISMATCH" and any("Email pengguna" in n for n in bad.verification_notes)


def test_deployment_topology_must_name_every_registered_target_environment():
    ok = _visual("deployment", "flowchart LR\n  ci[CI] --> s[staging] --> p[production]")
    bad = _visual("deployment", "flowchart LR\n  ci[CI] --> p[production]")
    vis.verify_visuals([ok, bad], deployment_environments=["staging", "production"])
    assert ok.verification_status == "CONSISTENT"
    assert bad.verification_status == "MISMATCH" and "staging" in bad.verification_notes[0]


def test_erd_attributes_are_compared_with_the_real_columns():
    from aeos.core.db import Base
    from aeos.docs_engine import service
    from aeos.models.portfolio import Project

    platform = Project(project_key="p", name="DLY AEOS", domain="PERSONAL", is_platform=True)
    cols = service.known_schema_columns(platform)
    assert cols is not None and "project_key" in cols["projects"]
    good = _visual("erd", "erDiagram\n  projects {\n    string id PK\n    string project_key\n  }\n"
                          "  projects ||--o{ plans : has")
    bad = _visual("erd", "erDiagram\n  projects {\n    string id PK\n    string budget_owner\n  }")
    vis.verify_visuals([good, bad], known_tables=set(Base.metadata.tables), known_columns=cols)
    assert good.verification_status == "CONSISTENT" and "kolom tabel" in good.verification_notes[0]
    assert bad.verification_status == "MISMATCH" and "projects.budget_owner" in bad.verification_notes[0]
    assert vis.erd_attributes("erDiagram\n  t {\n    int a PK \"komentar\"\n    varchar(20) b\n  }") == {"t": {"a", "b"}}


def test_platform_render_uses_the_real_inventory_and_route_contract(settings, db, monkeypatch):
    from aeos.docs_engine import service
    from aeos.inventory import seed as inv_seed
    from aeos.models.portfolio import Project

    inv_seed.run(db)
    db.commit()
    p = Project(project_key="dly-aeos-test", name="DLY AEOS", domain="PERSONAL", data_classification="INTERNAL",
                is_platform=True)
    db.add(p)
    db.commit()
    names = service.known_component_names(db, p)
    assert names and "dly-aeos-control-plane" in names and "claude-code" in names
    routes = service.known_route_paths(p)
    assert routes and "/api/projects/{project_id}" in routes
    product = Project(project_key="prod-x", name="X", domain="PERSONAL", data_classification="INTERNAL")
    assert service.known_component_names(db, product) is None and service.known_route_paths(product) is None


def test_platform_runbooks_come_from_the_reviewed_repository_runbooks():
    """§27 08. Runbooks: restart / DB recovery procedures for DLY AEOS itself are the reviewed docs/runbooks;
    a product project without such a source keeps them pending (never invented)."""
    from aeos.docs_engine import renderer as r
    from aeos.docs_engine import service
    from aeos.models.portfolio import Project
    from tests.unit.test_docs_engine_schema_seam import _render, _section_text
    from tests.unit.test_publisher_support import schema_sample_content

    platform = Project(project_key="p", name="DLY AEOS", domain="PERSONAL", is_platform=True)
    books = service.repo_runbooks(platform)
    by = {b.key: b for b in books}
    assert {"restart-recovery", "backup-restore", "incident-containment"} <= set(by)
    assert "aeos serve" in by["restart-recovery"].get("steps") and by["restart-recovery"].get("verification")
    assert service.repo_runbooks(Project(project_key="x", name="X", domain="PERSONAL")) == []

    content = schema_sample_content()  # its plan lists a "restart" runbook
    ids = _render(content, signals=r.RenderSignals(runbooks=books)).by_id()
    restart = ids["08-runbooks/runbook-restart"]
    assert "aeos serve" in _section_text(restart, "procedure")
    assert "docs/runbooks/restart-recovery.md" in _section_text(restart, "purpose_trigger")
    assert "Procedure" not in restart.pending_sections and "Verification" not in restart.pending_sections
    assert "08-runbooks/runbook-backup-restore" in ids, "a reviewed repo runbook the plan does not list is rendered"
    assert "08-runbooks/runbook-restart-recovery" not in ids, "already covered by the plan's restart runbook"
    product = _render(content).by_id()["08-runbooks/runbook-restart"]
    assert "Procedure" in product.pending_sections


def test_platform_operations_sections_come_from_the_controllers_own_rules():
    """§27 08: Logs (audit/security/retention/access), Alerts (severity/notification/escalation), Backup storage,
    Maintenance checks/routines and Support for DLY AEOS itself come from its alert catalog, retention policy,
    backup layout and Doctor registry; a product project keeps them pending (its plan schema has no field)."""
    from aeos.docs_engine import renderer as r
    from aeos.docs_engine import service
    from aeos.models.portfolio import Project
    from aeos.operations import alerts
    from tests.unit.test_docs_engine_schema_seam import _render, _section_text
    from tests.unit.test_publisher_support import schema_sample_content

    platform = Project(project_key="p", name="DLY AEOS", domain="PERSONAL", is_platform=True)
    facts = service.platform_ops_facts(platform)
    assert "emergency_stop`: CRITICAL" in facts["alerting.severity"] and "stale_leases`: HIGH" in facts["alerting.severity"]
    assert "retention.yaml" in facts["logging.retention"] and "<data>/backups" in facts["backup.storage"]
    assert "Readiness Doctor" in facts["maintenance.checks"]
    assert service.platform_ops_facts(Project(project_key="x", name="X", domain="PERSONAL")) == {}
    assert alerts.rule_severity("stale_leases") == "HIGH" and alerts.rule_severity("unknown") == "MEDIUM"

    content = schema_sample_content()
    ids = _render(content, signals=r.RenderSignals(platform_ops=facts)).by_id()
    logs, alerts_doc = ids["08-logs"], ids["08-alerts"]
    assert not logs.pending_sections and not alerts_doc.pending_sections
    assert "audit_events" in _section_text(logs, "audit_logs")
    assert "CRITICAL" in _section_text(alerts_doc, "severity")
    assert "Storage" not in ids["08-backup-restore"].pending_sections
    assert {"Checks", "Routine Activities"}.isdisjoint(ids["08-maintenance"].pending_sections)
    assert "Support" not in ids["08-operational-overview"].pending_sections
    product = _render(content).by_id()
    assert {"Audit Logs", "Security Logs", "Retention", "Access"} <= set(product["08-logs"].pending_sections)
    assert "Support" in product["08-operational-overview"].pending_sections


def test_product_operations_sections_come_from_the_plan_schema_revision_3():
    """§27 08 for a PRODUCT project: support, audit/security logs, retention, access, alert severity/notification/
    escalation, backup storage, maintenance checks and routine activities are plan fields (schema revision 3), so a
    plan that states them fills the sections; older content is upgraded with empty fields (still pending)."""
    from aeos.planning import contracts
    from aeos.planning.service import schema_errors
    from tests.unit.test_docs_engine_schema_seam import _render, _section_text
    from tests.unit.test_publisher_support import schema_sample_content

    content = schema_sample_content()
    content["operations"].update({
        "support": "Tim kecil, kanal email support@example.org, jam kerja WIB",
        "log_policy": {"audit_logs": "Audit login dan perubahan data", "security_logs": "Login gagal dan lockout",
                       "retention": "90 hari", "access": "Hanya admin aplikasi"},
        "alert_policy": {"severity": "P1 down, P2 degradasi", "notification": "Email ke on-call",
                         "escalation": "On-call -> pemilik dalam 30 menit"},
        "maintenance_checks": ["Cek ruang disk mingguan"], "routine_activities": ["Rotasi kunci per kuartal"]})
    content["operations"]["backup"]["storage"] = "Bucket terenkripsi terpisah"
    assert schema_errors("plan_content", content) == []
    ids = _render(content).by_id()
    assert "support@example.org" in _section_text(ids["08-operational-overview"], "support")
    assert not ids["08-logs"].pending_sections and "lockout" in _section_text(ids["08-logs"], "security_logs")
    assert not ids["08-alerts"].pending_sections and "30 menit" in _section_text(ids["08-alerts"], "escalation")
    assert "Bucket terenkripsi" in _section_text(ids["08-backup-restore"], "storage")
    assert {"Checks", "Routine Activities"}.isdisjoint(ids["08-maintenance"].pending_sections)

    # content stored before revision 3 (no new fields) is upgraded with EMPTY values and stays valid
    old = schema_sample_content()
    for key in contracts.OPERATIONS_ADDITIVE:
        old["operations"].pop(key)
    old["operations"]["backup"].pop("storage")
    assert schema_errors("plan_content", old), "the revision-2 shape is not valid without the upgrade"
    upgraded = contracts.upgrade_content(old)
    assert schema_errors("plan_content", upgraded) == []
    assert upgraded["operations"]["support"] == "" and upgraded["operations"]["backup"]["storage"] == ""
    assert "support" not in old["operations"], "the stored content itself is never mutated"
    assert contracts.upgrade_content(upgraded) == upgraded
    assert "cicd" in contracts.DIAGRAM_KINDS


def test_every_rendered_document_links_to_a_requirement_commit_or_evidence():
    """§27 "setiap dokumen ditautkan ke requirement/commit/issue/evidence yang relevan"."""
    from aeos.docs_engine import renderer as r
    from tests.unit.test_docs_engine_schema_seam import _render
    from tests.unit.test_publisher_support import schema_sample_content

    change = r.ChangeInfo(task_id="t1", task_key="T1", title="API", feature_key="F1", pr_url="https://github.com/o/r/pull/1",
                          accepted_head_sha="a" * 40, merge_commit_sha="b" * 40, merged=True, acceptance_status="ACCEPTED")
    va = r.EvidenceRef(id="ev-va-1", kind="VA_REPORT", origin="collector", commit="c" * 40)
    res = _render(schema_sample_content(), signals=r.RenderSignals(evidence=[va], changes=[change]))
    unlinked = [d.logical_doc_id for d in res.documents
                if d.kind == "DOC" and d.doc_type != "TEMPLATE" and d.status not in ("NOT_STARTED", "NOT_APPLICABLE")
                and not d.links]
    assert not unlinked, unlinked
    ids = res.by_id()
    assert ids["05-development"].links["merge_commit"] == ["b" * 40]
    assert ids["05-development"].links["pull_request"] == ["https://github.com/o/r/pull/1"]
    assert ids["01-product-overview"].links["requirement"]
    assert "ev-va-1" in ids["04-security-overview"].links["evidence"]
    assert ids["04-data-inventory"].links["related"] == ["04-threat-models/threat-model-initial"]
    assert "04-threat-models/threat-model-initial" in ids
    assert ids["08-runbooks/runbook-restart"].links["related"] == ["08-operational-overview"]
    assert "b" * 40 in ids["05-development"].body_markdown, "links are shown in the metadata table"
    assert ids["01-product-overview"].links["requirement"] == ["02-requirements"], "a stable requirement-set link"
