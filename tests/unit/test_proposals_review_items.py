"""§39 review-package items derived from records instead of boilerplate (audit items: UPGRADE delta, ADD
overlap, recorded shortcomings, docs/CI/rollback impact, plain-language trade-offs, actual/estimate costs,
advisories, distribution/terms)."""

from __future__ import annotations

from sqlalchemy import select

from aeos.cost import facade as cost
from aeos.models.inventory import ComponentReference, ComponentRelation
from aeos.models.inventory_ext import DriftFinding
from aeos.models.research import ResearchEntry
from aeos.models.routing import ModelRoute
from aeos.proposals import review as R
from aeos.research import advisories
from tests.unit.proposal_fixtures import component, create, owner_plan, restore_evidence, review


def _entry(db, key):
    return db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == key))


def test_upgrade_review_lists_version_publisher_license_terms_and_schema_delta(db):
    component(db)
    db.get(ComponentReference, "opencode").upstream_identity = "github:anomalyco/opencode"
    db.get(ComponentReference, "opencode").license = "MIT"
    oc = _entry(db, "tool:opencode")
    oc.component_id = "opencode"
    oc.upstream = {"owner": "anomalyco-new", "license_spdx": "Apache-2.0",
                   "dependency_diff": {"base": "1.3.0", "head": "1.4.0", "checked_at": "2026-10-19T00:00:00",
                                       "manifests": [{"path": "package.json", "status": "CHANGED"}]}}
    oc.data_terms = "Tidak mengirim data proyek"
    db.commit()
    p = review(db, create(db, kind="UPGRADE", candidate_pin="1.4.0"))
    delta = p.review_package["upgrade_delta"]["components"][0]
    assert delta["version"] == {"from": "1.3.0", "to": "1.4.0", "changed": True}
    assert delta["publisher"]["to"] == "anomalyco-new" and delta["publisher"]["changed"] is True
    assert delta["license"] == {"from": "MIT", "to": "Apache-2.0", "changed": True}
    assert delta["terms"]["to"] == "Tidak mengirim data proyek"
    assert delta["schema"]["status"] == "DEPENDENCY_DIFF" and delta["schema"]["changed_files"] == ["package.json"]
    other = review(db, create(db, kind="UPGRADE", candidate_pin="9.9.9"))
    assert other.review_package["upgrade_delta"]["components"][0]["schema"]["status"] == "UNKNOWN"
    assert review(db, create(db, kind="ADD", sources=["https://github.com/a/b"])).review_package["upgrade_delta"] is None


def test_add_review_shows_existing_components_with_the_same_capability(db):
    component(db)  # opencode is a worker-engine
    db.add(ComponentReference(component_id="goose", kind="worker_engine", name="Goose"))
    db.commit()
    p = review(db, create(db, kind="ADD", sources=["https://github.com/anomalyco/opencode"]))
    assert p.review_package["summary"]["decision_question"] == R.KIND_QUESTIONS["ADD"]
    assert R.KIND_QUESTIONS["ADD"].startswith("Capability baru apa yang dibutuhkan")
    overlap = {c["component_id"] for c in p.review_package["baseline"]["overlap"]}
    assert {"opencode", "goose"} <= overlap


def test_baseline_lists_recorded_shortcomings_of_the_old_component(db):
    component(db)
    oc = _entry(db, "tool:opencode")
    oc.component_id = "opencode"
    db.add(DriftFinding(component_id="opencode", drift_class="resolved-version-mismatch", summary="pin 1.3.0, terpasang 1.2.9",
                        domain_scope="PLATFORM"))
    db.add(DriftFinding(component_id="opencode", drift_class="installed-but-disabled", summary="office only",
                        domain_scope="OFFICE"))
    db.commit()
    p = review(db, create(db, kind="UPGRADE", candidate_pin="1.4.0"))
    issues = p.review_package["baseline"]["components"][0]["known_issues"]
    sources = {i["source"] for i in issues}
    assert sources == {"research_registry", "inventory_drift"}
    assert any("1.2.9" in i["issue"] for i in issues) and not any("office only" in i["issue"] for i in issues)


def test_impact_docs_ci_and_rollback_are_derived_not_assumed(db):
    component(db)
    db.add_all([
        ComponentReference(component_id="github-actions", kind="integration", name="GitHub Actions", category="code/docs/CI"),
        ComponentReference(component_id="decision-registry", kind="platform_module", name="Decision registry"),
        ComponentReference(component_id="google-docs", kind="integration", name="Google Docs", category="code/docs/CI"),
        ComponentRelation(source_id="opencode", target_id="decision-registry", relation="reads", lens="DESIGN"),
    ])
    db.commit()
    p = review(db, create(db, kind="UPGRADE", candidate_pin="1.4.0"))
    impact = p.review_package["impact"]
    assert impact["ci"]["status"] == "UNKNOWN", "a 'decision-*' target is not CI"
    assert impact["docs"]["status"] == "UNKNOWN"
    assert impact["rollback"]["status"] == "UNKNOWN" and impact["rollback"]["targets"], "an old pin is a target, not proof"
    db.add_all([ComponentRelation(source_id="opencode", target_id="github-actions", relation="verifies", lens="DESIGN"),
                ComponentRelation(source_id="opencode", target_id="google-docs", relation="publishes_to", lens="DESIGN")])
    db.commit()
    owner_plan(db, p)
    impact = p.review_package["impact"]
    assert impact["ci"]["status"] == "KNOWN" and "github-actions" in impact["ci"]["items"][0]
    assert impact["docs"]["status"] == "KNOWN" and "google-docs" in impact["docs"]["items"][0]
    assert impact["rollback"]["status"] == "KNOWN", "a restore-tested backup is a tested rollback reference"


def test_impact_lists_tool_schema_and_prompt_context_of_a_route_component(db):
    db.add(ComponentReference(component_id="zen-route", kind="model_provider", name="Zen route",
                              access={"identity_ref": "workers/opencode-zen"}))
    db.add(ModelRoute(route_key="zen-route", pool="WORKER", provider="opencode-zen", model_id="m", tool_set=["edit", "bash"],
                      prompt_revision="p2", context_strategy="trimmed"))
    db.commit()
    p = review(db, create(db, kind="UPGRADE", existing_component_ids=["zen-route"], candidate_pin="2"))
    impact = p.review_package["impact"]
    assert impact["schema_api_tools"]["status"] == "KNOWN" and "edit" in impact["schema_api_tools"]["items"][0]
    assert impact["hooks_prompts_memory"]["status"] == "KNOWN"
    assert any("prompt_revision p2" in i for i in impact["hooks_prompts_memory"]["items"])
    assert impact["identity"]["status"] == "KNOWN" and impact["resource_hardware"]["status"] == "UNKNOWN"


def test_plain_language_is_candidate_specific_and_masks_unmeasured_percentages(db):
    db.add(ResearchEntry(name="TokenSaver", identity="github:acme/tokensaver", category="metering",
                         relevance="Mengurangi konteks command output hingga 60-90% token", lifecycle="DISCOVERED",
                         limitations="Menambah dependency CLI baru"))
    db.commit()
    p = review(db, create(db, kind="ADD", sources=["https://github.com/acme/tokensaver"]))
    text = p.review_package["summary"]["plain_language"]
    joined = " ".join(text)
    assert any(t.startswith("TokenSaver: Mengurangi konteks command output") and "tetapi menambah dependency" in t for t in text)
    assert "Menambah dependency CLI baru" in joined
    assert "%" not in joined.replace("Persentase", ""), "no percentage without a measurement method"
    assert "belum terbukti mengurangi kebutuhan review Claude" in joined
    assert text[-1] == "Persentase penghematan/keunggulan tidak ditampilkan tanpa metode pengukuran dan baseline."


def test_cost_distinguishes_actual_ledger_and_route_estimate(db):
    component(db)
    db.add(ModelRoute(route_key="opencode-zen/new-free", pool="WORKER", provider="opencode-zen", model_id="m2",
                      is_free="YES", pricing={"input_per_mtok": 0, "output_per_mtok": 0}))
    db.add(ComponentReference(component_id="opencode-zen/new-free", kind="model_provider", name="Zen free"))
    db.commit()
    cost.record_usage(db, provider="opencode", category="WORKER_API", source="provider", cost_usd=0.25, domain="PERSONAL",
                      input_tokens=1000, output_tokens=500)
    cost.record_usage(db, provider="opencode", category="WORKER_API", source="provider", cost_usd=None, domain="PERSONAL")
    cost.record_usage(db, provider="opencode", category="WORKER_API", source="provider", cost_usd=99, domain="OFFICE")
    db.commit()
    p = review(db, create(db, kind="REPLACE", candidate_pin="1.0.0", candidate_component_ids=["opencode-zen/new-free"]))
    c = p.review_package["cost"]
    assert c["cash"]["basis"] == "ESTIMATE" and c["cash"]["value"] == 0.0 and "gratis" in c["cash"]["note"]
    actual = c["baseline_cash_30d"]
    assert actual["basis"] == "ACTUAL" and actual["value"] == 0.25 and actual["unknown_entries"] == 1, \
        "OFFICE usage never enters a PERSONAL package; UNKNOWN costs are not summed"
    assert c["tokens"]["basis"] == "ACTUAL" and c["tokens"]["quantity"] == 1500


def test_advisory_affecting_the_pin_blocks_admission_and_is_flagged(db):
    up = {"advisories": {"status": "OK", "checked_at": "2026-10-19T00:00:00", "items": [
        {"ghsa_id": "GHSA-1", "severity": "high", "vulnerabilities": [{"vulnerable_version_range": ">= 1.0.0, < 1.4.1"}]}]}}
    assert advisories.summary(up, "1.4.0")["status"] == "AFFECTED"
    assert advisories.summary(up, "v1.4.1")["status"] == "NOT_AFFECTED"
    assert advisories.summary(up, "sha256:abc")["status"] == "UNKNOWN"
    assert advisories.summary({}, "1.4.0")["status"] == "NOT_CHECKED"
    db.add(ResearchEntry(name="Widget", identity="github:acme/widget", identity_status="CHECKED", lifecycle="SOURCE_VERIFIED",
                         upstream=up, distribution="npm: @acme/widget", data_terms="tanpa telemetri"))
    db.commit()
    p = review(db, create(db, kind="ADD", sources=["https://github.com/acme/widget"], candidate_pin="1.4.0"))
    src = p.review_package["source_identity"][0]
    assert "ADVISORIES_AFFECTED" in src["flags"] and src["distribution"] == "npm: @acme/widget"
    assert "TERMS_NOT_CHECKED" in src["flags"]
    gate = R.source_identity_gate(db, p)
    assert gate["status"] == "BLOCKED" and gate["blocking"][0]["status"] == "ADVISORY_AFFECTED"
    assert p.review_package["security_privacy"]["dimensions"]["advisories"]["status"] == "BLOCKED"


def test_replacement_plan_reports_unknown_owner_instead_of_a_literal(db):
    component(db)
    db.get(ComponentReference, "opencode").owner = ""
    db.commit()
    p = review(db, create(db, kind="REPLACE", candidate_pin="1.4.0"))
    assert p.review_package["replacement_plan"]["operations_owner"] == "UNKNOWN"
    ev = restore_evidence(db)
    from aeos.proposals import plan

    plan.update(db, p, {"operations_owner": "tim-platform", "replacement": {
        "cutover_order": "workforce dulu", "backup_restore_evidence_id": ev.id, "coexistence_safe": False}},
        actor_user_id="owner")
    db.commit()
    rp = p.review_package["replacement_plan"]
    assert rp["operations_owner"] == ["tim-platform"] and rp["cutover_order"] == "workforce dulu"
    assert rp["coexistence_safe"] is False and ev.id in rp["backup_and_restore_evidence"]
