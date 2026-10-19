"""Read-only, tolerant view over approved ``PlanVersion.content`` (§13, §14).

The planning module owns the content schema; the renderer only *reads* it. Keys are looked up with
a small set of aliases so a schema evolution does not silently drop content — anything missing is
reported as "documentation pending" rather than invented (§27, §41).

**Schema v1 seam.** Every approved PlanVersion is validated against
``aeos.planning.contracts.plan_content_schema`` (``product_home``, ``overview``, ``requirements[]``,
``architecture{overview, diagrams}``, ``security_privacy{overview, initial_risks, threat_model,
data_inventory}``, ``development_baseline``, ``testing{test_plans}``, ``release{release_plans}``,
``operations``, ...). ``normalise_schema_v1`` maps that layout onto the renderer's internal layout
below (one place, so a schema change is one edit here); ``build_view`` applies it automatically.
Values the schema marks as unknown (``UNKNOWN``) are treated as absent — never as a name.

Internal (legacy-tolerant) keys, all optional:

``product{name, vision, objectives[], summary}``, ``summary|overview|description``, ``problem``,
``background``, ``vision``, ``objectives[]``, ``scope{in[], out[]}|in_scope[]|out_of_scope[]``,
``non_goals[]``, ``team{product_owner, engineering_lead, tech_lead, ...}``, ``stakeholders[]``,
``stack[]|{}``, ``criticality``, ``data_classification``, ``features[]``, ``milestones[]``,
``architecture{context, high_level, components[], data, data_flow, integrations[], apis[],
infrastructure, security, principles[], decisions[]}``, ``diagrams[]|visuals[]``, ``risks[]``,
``security{authn, authz, encryption, secrets, logging, controls[], threats[]}``, ``data{types[],
storage, retention, ...}``, ``development{...}``, ``testing{strategy, levels[], types[], tools[]}``,
``rfcs[]``, ``tdds[]``, ``assumptions[]``, ``unknowns[]``, ``constraints[]``,
``non_functional[]``, ``success_metrics[]``.

Feature keys: ``feature_key|key|id``, ``title|name``, ``summary|description|requirement``,
``problem``, ``goals[]``, ``non_goals[]``, ``business_requirements[]``,
``functional_requirements[]|requirements[]``, ``non_functional[]``, ``user_stories[]``,
``acceptance|acceptance_criteria[]``, ``impact``, ``depends_on[]``, ``assumptions[]``, ``risks[]``,
``success_metrics[]``, ``rollout``, ``priority``, ``milestone|milestone_key``, ``owner``,
``needs_rfc``, ``needs_tdd``, ``rfc{}``, ``tdd{}``, ``personal_data``, ``privacy{}``,
``security_assessment{scope, requirements[], risks[], mitigations[]}|needs_security_assessment``,
``threat_model{assets[], actors[], ...}|threats[]``, ``testing{}``.

Schema-v1-only internal keys added by the normaliser: ``home_summary``, ``team_ownership``,
``roadmap[]``, ``system_inventory[]``, ``data_overview``, ``home{repository, issue_tracker,
quick_links[], current_development, active_features[], open_risks[], sdlc_lifecycle,
governance_links[], data_classification}``, ``data{items[], overview}``, ``test_plans[]``,
``release_plans[]``, ``operations{...}``, ``plan_tasks[]``; per feature ``prd_title``,
``prd_dependencies[]``, ``related_work[]``, ``rfc_refs[]``, ``tdd_refs[]``, ``risky_tasks[]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aeos.core.ids import slugify

SCHEMA_V1_MARKERS = ("product_home", "security_privacy", "development_baseline")
_UNKNOWN_VALUES = frozenset({"unknown", "n/a", "na", "-", "—", "tbd", "none", "belum diketahui", "tidak diketahui"})
_RISKY_TASK_CLASSES = ("R2", "R3", "R4")


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def known(v: Any) -> str:
    """Text of *v*, or "" when the plan marks it unknown (schema: "UNKNOWN unless provided")."""
    t = as_text(v)
    return "" if t.lower() in _UNKNOWN_VALUES else t


def feature_slug(raw: Any) -> str:
    """The key a feature's documents use (``prd-<slug>``); TaskSpec/PRD/test-plan references to a feature are
    compared through this, so ``F1`` in a task and ``f1`` in a logical id are the same feature."""
    text = as_text(raw)
    if not text or not re.search(r"[A-Za-z0-9]", text):
        return ""
    return slugify(text, max_len=40)


def as_list(v: Any) -> list[Any]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, tuple):
        return [x for x in v if x not in (None, "")]
    return [v]


def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        for k in ("text", "description", "summary", "title", "name", "value"):
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k].strip()
        return ", ".join(f"{k}: {as_text(x)}" for k, x in v.items() if as_text(x))
    if isinstance(v, list):
        return "; ".join(as_text(x) for x in v if as_text(x))
    return str(v)


def item_text(v: Any) -> str:
    """One list item rendered as text: dict items keep their title + description."""
    if isinstance(v, dict):
        title = as_text(_first(v, "title", "name", "key", "id", default=""))
        desc = as_text(_first(v, "description", "summary", "text", "detail", "responsibility", "mitigation",
                              default=""))
        if title and desc and desc != title:
            return f"**{title}** — {desc}"
        return title or desc or as_text(v)
    return as_text(v)


@dataclass
class FeatureView:
    key: str
    title: str
    raw: dict[str, Any]

    def get(self, *keys: str, default: Any = None) -> Any:
        return _first(self.raw, *keys, default=default)

    @property
    def summary(self) -> str:
        return as_text(self.get("summary", "description", "requirement", "overview"))

    @property
    def acceptance(self) -> list[Any]:
        return as_list(self.get("acceptance_criteria", "acceptance", "ac"))

    @property
    def depends_on(self) -> list[str]:
        return [as_text(x) for x in as_list(self.get("depends_on", "dependencies"))]

    @property
    def milestone(self) -> str:
        return as_text(self.get("milestone_key", "milestone"))

    @property
    def priority(self) -> str:
        return as_text(self.get("priority"))

    @property
    def owner(self) -> str:
        return as_text(self.get("owner"))

    @property
    def needs_rfc(self) -> bool:
        return bool(self.get("needs_rfc") or isinstance(self.raw.get("rfc"), dict))

    @property
    def needs_tdd(self) -> bool:
        return bool(self.get("needs_tdd") or isinstance(self.raw.get("tdd"), dict))

    @property
    def personal_data(self) -> bool | None:
        v = self.raw.get("personal_data")
        return None if v is None else bool(v)


@dataclass
class DiagramView:
    key: str
    title: str
    kind: str
    source: str
    language: str = "mermaid"
    verification_source: str = ""  # what the plan says the diagram must be checked against (§29)


@dataclass
class PlanView:
    raw: dict[str, Any]
    features: list[FeatureView] = field(default_factory=list)
    diagrams: list[DiagramView] = field(default_factory=list)

    def get(self, *keys: str, default: Any = None) -> Any:
        return _first(self.raw, *keys, default=default)

    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name)
        return v if isinstance(v, dict) else {}

    @property
    def product(self) -> dict[str, Any]:
        return self.section("product")

    @property
    def schema_v1(self) -> bool:
        return bool(self.raw.get("_schema_v1"))

    @property
    def summary(self) -> str:
        # Only text values: a dict (e.g. the schema's ``overview`` object) is never dumped as "key: value" prose.
        for v in (_first(self.product, "summary", "description"), self.raw.get("summary"), self.raw.get("overview"),
                  self.raw.get("description"), self.raw.get("idea")):
            if isinstance(v, (str, int, float)) and as_text(v):
                return as_text(v)
        return ""

    @property
    def vision(self) -> str:
        return as_text(_first(self.product, "vision") or self.get("vision"))

    @property
    def objectives(self) -> list[Any]:
        return as_list(_first(self.product, "objectives", "goals") or self.get("objectives", "goals"))

    @property
    def scope_in(self) -> list[Any]:
        scope = self.raw.get("scope")
        if isinstance(scope, dict):
            return as_list(_first(scope, "in", "in_scope", "included"))
        if isinstance(scope, list):
            return scope
        return as_list(self.get("in_scope"))

    @property
    def scope_out(self) -> list[Any]:
        scope = self.raw.get("scope")
        if isinstance(scope, dict):
            return as_list(_first(scope, "out", "out_of_scope", "excluded"))
        return as_list(self.get("out_of_scope", "non_goals"))

    @property
    def team(self) -> dict[str, Any]:
        t = self.raw.get("team") or self.raw.get("owners") or self.raw.get("ownership")
        return t if isinstance(t, dict) else {}

    @property
    def milestones(self) -> list[dict[str, Any]]:
        return [m if isinstance(m, dict) else {"title": as_text(m)} for m in as_list(self.get("milestones"))]

    @property
    def risks(self) -> list[Any]:
        return as_list(self.get("risks"))

    @property
    def architecture(self) -> dict[str, Any]:
        return self.section("architecture")

    @property
    def security(self) -> dict[str, Any]:
        return self.section("security")

    @property
    def data(self) -> dict[str, Any]:
        return self.section("data")

    @property
    def development(self) -> dict[str, Any]:
        return self.section("development")

    @property
    def testing(self) -> dict[str, Any]:
        return self.section("testing")

    @property
    def stack(self) -> list[str]:
        v = self.get("stack", "tech_stack", "technology")
        if isinstance(v, dict):
            return [f"{k}: {as_text(x)}" for k, x in v.items() if as_text(x)]
        return [as_text(x) for x in as_list(v)]

    @property
    def home(self) -> dict[str, Any]:
        return self.section("home")

    @property
    def test_plans(self) -> list[dict[str, Any]]:
        return [t for t in as_list(self.raw.get("test_plans")) if isinstance(t, dict)]

    @property
    def release_plans(self) -> list[dict[str, Any]]:
        return [r for r in as_list(self.raw.get("release_plans")) if isinstance(r, dict)]

    @property
    def operations(self) -> dict[str, Any]:
        return self.section("operations")

    @property
    def plan_tasks(self) -> list[dict[str, Any]]:
        return [t for t in as_list(self.raw.get("plan_tasks")) if isinstance(t, dict)]

    def feature(self, raw_key: Any) -> FeatureView | None:
        slug = feature_slug(raw_key)
        return next((f for f in self.features if f.key == slug), None)

    def test_plan_index(self) -> list[tuple[str, dict[str, Any], list[str]]]:
        """(logical key, test plan, covered feature keys) for every Test Plan document.

        Schema v1 ``testing.test_plans[]`` give one Test Plan each (``test-plan-<key>``); a feature that no
        plan covers keeps the per-feature plan derived from its acceptance criteria (``test-plan-<feature>``).
        The renderer and the publisher's issue links both use this, so a link never points at a missing doc."""
        used: set[str] = set()
        out: list[tuple[str, dict[str, Any], list[str]]] = []
        covered: set[str] = set()
        feature_keys = {f.key for f in self.features}
        for i, tp in enumerate(self.test_plans):
            base = feature_slug(_first(tp, "key", "title", default="")) or f"tp-{i + 1}"
            key, n = base, 2
            while key in used or key in feature_keys:
                key = f"{base}-{n}"
                n += 1
            used.add(key)
            feats = [s for s in (feature_slug(x) for x in as_list(tp.get("feature_keys"))) if s]
            covered.update(feats)
            out.append((key, tp, feats))
        for f in self.features:
            if f.key not in covered:
                out.append((f.key, {}, [f.key]))
        return out

    def test_plan_ids_for(self, feature_key: str) -> list[str]:
        return [f"06-testing/test-plan-{k}" for k, _tp, feats in self.test_plan_index() if feature_key in feats]


def _feature_key(raw: dict[str, Any], index: int) -> str:
    key = as_text(_first(raw, "feature_key", "key", "id", default=""))
    if not key:
        key = slugify(as_text(_first(raw, "title", "name", default=f"feature-{index + 1}")))
    return slugify(key, max_len=40)


# ---------------------------------------------------------------------------------------------
# Schema v1 → internal layout (the §27/§28 seam)


def is_schema_v1(content: dict[str, Any]) -> bool:
    return content.get("schema_version") == "1" or any(isinstance(content.get(k), dict) for k in SCHEMA_V1_MARKERS)


def _obj(d: dict[str, Any], key: str) -> dict[str, Any]:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _texts(v: Any) -> list[str]:
    return [t for t in (as_text(x) for x in as_list(v)) if t]


def _dedupe(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for x in items:
        k = as_text(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _non_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


_REF_RE = {"rfc": re.compile(r"^\s*RFC\b", re.I), "tdd": re.compile(r"^\s*TDD\b", re.I)}


def _prd_fields(prd: dict[str, Any], feature: dict[str, Any]) -> dict[str, Any]:
    related = _texts(prd.get("related_work"))
    return _non_empty({
        "prd_title": as_text(prd.get("title")),
        "summary": as_text(prd.get("overview")) or as_text(feature.get("requirement")),
        "problem": prd.get("problem"), "background": prd.get("background"),
        "goals": prd.get("goals"), "non_goals": prd.get("non_goals"),
        "business_requirements": prd.get("business_requirements"),
        "functional_requirements": prd.get("functional_requirements"),
        "non_functional_requirements": prd.get("non_functional_requirements"),
        "user_stories": prd.get("user_stories"), "user_impact": prd.get("user_impact"),
        "prd_dependencies": prd.get("dependencies"), "assumptions": prd.get("assumptions"),
        "risks": prd.get("risks"), "success_metrics": prd.get("success_metrics"),
        "rollout_considerations": prd.get("rollout_considerations"), "related_work": related,
        "acceptance": _dedupe(as_list(feature.get("acceptance")) + as_list(prd.get("acceptance_criteria"))),
        "rfc_refs": [x for x in related if _REF_RE["rfc"].match(x)],
        "tdd_refs": [x for x in related if _REF_RE["tdd"].match(x)],
    })


def _feature_flags(key: str, raw: dict[str, Any], plan_tasks: list[dict[str, Any]],
                   controls: list[str]) -> dict[str, Any]:
    """Needs derived from approved plan DATA (never guessed): a feature whose tasks carry risk class R2+ needs a
    per-change security assessment (§27 04, §28 feature approval); an RFC/TDD the PRD references is needed."""
    out: dict[str, Any] = {}
    risky = [t for t in plan_tasks if feature_slug(t.get("feature_key")) == key
             and as_text(t.get("risk_class")).upper() in _RISKY_TASK_CLASSES]
    if risky:
        out["risky_tasks"] = [{"key": as_text(t.get("key")), "title": as_text(t.get("title")),
                               "risk_class": as_text(t.get("risk_class")).upper()} for t in risky]
        out["needs_security_assessment"] = True
        out["security_assessment"] = _non_empty({
            "scope": [f"TaskSpec `{x['key']}` — {x['title']} (risk class {x['risk_class']})"
                      for x in out["risky_tasks"]],
            "requirements": [f"Kontrol baseline proyek: {c}" for c in controls],
            "risks": raw.get("risks"),
        })
    if raw.get("rfc_refs"):
        out["needs_rfc"] = True
    if raw.get("tdd_refs"):
        out["needs_tdd"] = True
    return out


def normalise_schema_v1(content: dict[str, Any]) -> dict[str, Any]:
    """Map a schema-v1 ``PlanVersion.content`` onto the renderer's internal layout. Pure; nothing invented:
    every value comes from the approved plan, ``UNKNOWN`` stays absent."""
    c = content
    ph, ov = _obj(c, "product_home"), _obj(c, "overview")
    arch = _obj(c, "architecture")
    ao = _obj(arch, "overview")
    sp = _obj(c, "security_privacy")
    spo, tm = _obj(sp, "overview"), _obj(sp, "threat_model")
    dev, testing, release, ops = (_obj(c, "development_baseline"), _obj(c, "testing"), _obj(c, "release"),
                                  _obj(c, "operations"))
    plan_tasks = [t for t in as_list(c.get("tasks")) if isinstance(t, dict)]
    controls = _texts(spo.get("controls"))

    prds: dict[str, dict[str, Any]] = {}
    for p in as_list(c.get("requirements")):
        if isinstance(p, dict):
            k = feature_slug(p.get("feature_key")) or feature_slug(p.get("title"))
            if k and k not in prds:
                prds[k] = p
    features: list[dict[str, Any]] = []
    joined: set[str] = set()
    for f in as_list(c.get("features")):
        raw = dict(f) if isinstance(f, dict) else {"title": as_text(f)}
        k = feature_slug(_first(raw, "feature_key", "key", "id", "title", default=""))
        if k in prds:
            raw.update(_prd_fields(prds[k], raw))
            joined.add(k)
        raw.update(_feature_flags(k, raw, plan_tasks, controls))
        features.append(raw)
    for k, prd in prds.items():  # a PRD whose feature entry is missing is still a needed PRD — never dropped
        if k in joined:
            continue
        raw = {"key": as_text(prd.get("feature_key")) or k, "title": as_text(prd.get("title")) or k,
               "prd_without_feature": True}
        raw.update(_prd_fields(prd, raw))
        raw.update(_feature_flags(k, raw, plan_tasks, controls))
        features.append(raw)

    threats = [t for t in as_list(tm.get("threats")) if isinstance(t, dict)]

    def keyed(t: dict[str, Any], field_: str) -> str:
        val, key = as_text(t.get(field_)), as_text(t.get("key"))
        return f"**{key}** — {val}" if key and val else val

    threat_model = _non_empty({
        "assets": tm.get("assets"), "actors": tm.get("actors"), "trust_boundaries": tm.get("trust_boundaries"),
        "entry_points": tm.get("entry_points"),
        "threats": [x for x in (keyed(t, "threat") for t in threats) if x],
        "attack_scenarios": [x for x in (keyed(t, "scenario") for t in threats) if x],
        "mitigations": [x for x in (keyed(t, "mitigation") for t in threats) if x],
    })
    team = _non_empty({"product_owner": known(ph.get("product_owner")),
                       "engineering_lead": known(ph.get("engineering_lead")), "tech_lead": known(ph.get("tech_lead")),
                       "security_lead": known(spo.get("ownership")), "operations_owner": known(ops.get("ownership"))})
    out: dict[str, Any] = {
        "_schema_v1": True,
        "schema_version": c.get("schema_version"),
        "product": _non_empty({"name": as_text(ph.get("name")), "vision": as_text(ov.get("vision")),
                               "objectives": ov.get("objectives")}),
        "summary": as_text(ov.get("overview")) or as_text(ph.get("summary")),
        "home_summary": as_text(ph.get("summary")),
        "scope": {"in": as_list(ov.get("scope_in")), "out": as_list(ov.get("scope_out"))},
        "team": team,
        "team_ownership": as_text(ov.get("team_ownership")),
        "stakeholders": as_list(ov.get("stakeholders")),
        "roadmap": as_list(ov.get("roadmap")),
        "system_inventory": as_list(ov.get("system_inventory")),
        "data_overview": as_text(ov.get("data_overview")),
        "criticality": known(ph.get("criticality")),
        "stack": as_list(ph.get("stack")),
        "home": _non_empty({
            "repository": known(ph.get("repository")), "issue_tracker": known(ph.get("issue_tracker")),
            "quick_links": as_list(ph.get("quick_links")), "current_development": as_text(ph.get("current_development")),
            "active_features": [s for s in (feature_slug(x) for x in as_list(ph.get("active_features"))) if s],
            "open_risks": _texts(ph.get("open_risks")), "sdlc_lifecycle": as_text(ph.get("sdlc_lifecycle")),
            "governance_links": as_list(ph.get("governance_links")),
            "data_classification": as_text(ph.get("data_classification")),
        }),
        "milestones": as_list(c.get("milestones")),
        "features": features,
        "architecture": _non_empty({
            "context": ao.get("context"), "high_level": ao.get("high_level_architecture"),
            "components": ao.get("component_architecture"), "data": ao.get("data_architecture"),
            "data_flow": ao.get("data_flow"), "integrations": ao.get("external_integrations"),
            "apis": ao.get("api_landscape"), "infrastructure": ao.get("infrastructure"),
            "security": ao.get("security_architecture"), "principles": ao.get("principles"),
            "decisions": ao.get("decisions"),
            "services": [known(ph.get("service"))] if known(ph.get("service")) else [],
            "diagrams": as_list(arch.get("diagrams")),
        }),
        "risks": [r for r in as_list(sp.get("initial_risks")) if r],
        "security": _non_empty({
            "owner": known(spo.get("ownership")), "authn_authz": spo.get("authn_authz"),
            "encryption": spo.get("encryption"), "secrets": spo.get("secrets"),
            "logging": spo.get("logging_monitoring"), "controls": spo.get("controls"),
            "threat_model": threat_model, "open_questions": sp.get("open_questions"),
        }),
        "data": _non_empty({"items": [d for d in as_list(sp.get("data_inventory")) if isinstance(d, dict)],
                            "overview": as_text(ov.get("data_overview"))}),
        "development": _non_empty({
            "guidelines": dev.get("guidelines"), "coding_standards": dev.get("coding_standards"),
            "secure_coding": dev.get("secure_coding"), "branching": dev.get("branching"),
            "pr_review": dev.get("pr_review"), "dependency_management": dev.get("dependency_management"),
            "dev_environment": dev.get("dev_environment"), "secrets": dev.get("secrets_handling"),
            "ci_cd_requirements": dev.get("ci_cd_requirements"),
        }),
        "testing": {},
        "test_plans": as_list(testing.get("test_plans")),
        "release_plans": as_list(release.get("release_plans")),
        "operations": ops,
        "plan_tasks": plan_tasks,
        "assumptions": as_list(c.get("assumptions")),
        "unknowns": as_list(c.get("unknowns")),
    }
    return out


def build_view(content: dict[str, Any] | None) -> PlanView:
    content = content if isinstance(content, dict) else {}
    if is_schema_v1(content):
        content = normalise_schema_v1(content)
    view = PlanView(raw=content)
    seen: set[str] = set()
    for i, f in enumerate(as_list(content.get("features"))):
        raw = f if isinstance(f, dict) else {"title": as_text(f)}
        key = _feature_key(raw, i)
        base, n = key, 2
        while key in seen:  # duplicate keys never collapse two features into one document
            key = f"{base}-{n}"
            n += 1
        seen.add(key)
        title = as_text(_first(raw, "title", "name", default=key))
        view.features.append(FeatureView(key=key, title=title, raw=raw))
    arch = view.architecture
    diagrams = as_list(content.get("diagrams")) + as_list(content.get("visuals")) + as_list(arch.get("diagrams"))
    dseen: set[str] = set()
    for i, d in enumerate(diagrams):
        if not isinstance(d, dict):
            continue
        source = as_text(_first(d, "mermaid", "source", "code", default=""))
        if not source:
            continue
        key = slugify(as_text(_first(d, "key", "id", "title", default=f"diagram-{i + 1}")), max_len=40)
        if key in dseen:
            key = f"{key}-{i + 1}"
        dseen.add(key)
        view.diagrams.append(DiagramView(
            key=key, title=as_text(_first(d, "title", "name", default=key)),
            kind=as_text(_first(d, "kind", "type", default="context")).lower(),
            source=source, language=as_text(_first(d, "language", default="mermaid")).lower(),
            verification_source=as_text(_first(d, "verification_source", "verify_against", default="")),
        ))
    return view
