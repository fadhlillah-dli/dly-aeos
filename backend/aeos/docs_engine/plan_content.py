"""Read-only, tolerant view over approved ``PlanVersion.content`` (§13, §14).

The planning module owns the content schema; the renderer only *reads* it. Keys are looked up with
a small set of aliases so a schema evolution does not silently drop content — anything missing is
reported as "documentation pending" rather than invented (§27, §41).

Consumed keys (all optional):

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
``needs_rfc``, ``needs_tdd``, ``rfc{}``, ``tdd{}``, ``personal_data``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aeos.core.ids import slugify


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


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
    def summary(self) -> str:
        return as_text(_first(self.product, "summary", "description") or self.get("summary", "overview", "description",
                                                                                   "idea"))

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


def _feature_key(raw: dict[str, Any], index: int) -> str:
    key = as_text(_first(raw, "feature_key", "key", "id", default=""))
    if not key:
        key = slugify(as_text(_first(raw, "title", "name", default=f"feature-{index + 1}")))
    return slugify(key, max_len=40)


def build_view(content: dict[str, Any] | None) -> PlanView:
    content = content if isinstance(content, dict) else {}
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
        ))
    return view
