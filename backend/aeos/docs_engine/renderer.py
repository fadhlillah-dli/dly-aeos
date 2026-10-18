"""Deterministic Product 1 renderer (master §27, §28, §29, §41).

``render_document_set`` turns an approved PlanVersion's content plus the project identity into the
Product 1 document set as canonical Markdown with section anchors. It is a pure function (no DB,
no network, no LLM): planning output is DATA that is laid into the §27 sections; nothing is
invented. Specifically:

* sections without plan content are marked "documentation pending" (listed in
  ``pending_sections``) — never filled with made-up text;
* ``DYNAMIC`` sections *reference* the live source (issue tracker, verifier evidence, CI) instead of
  copying a status snapshot; ``HUMAN_GOVERNED`` sections are left for the authorised human;
* nodes whose §28 timing has not come (Release Plans, Operations, Incidents, Retirement, VA/PT ...)
  are NOT STARTED / NOT APPLICABLE with a reason and the accountable owner — no PT/VA/UAT/restore
  results and no approvals are ever fabricated;
* every document has an ``input_hash`` over exactly the plan slice it uses: when the stored hash
  matches, the document is not re-rendered at all (delta rendering, §28 "tidak diregenerasi penuh
  pada setiap edit"); ``content_hash`` over the Markdown drives the publication delta.

``sync_document_records`` persists the result as ``DocumentRecord`` upserts plus
``DocumentRevision`` snapshots. Only APPROVED versions may be synced.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import ContentType
from aeos.core.errors import Conflict
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.docs_engine import product1 as p1
from aeos.docs_engine.plan_content import FeatureView, PlanView, as_list, as_text, build_view, item_text
from aeos.docs_engine.plan_content import _first as pick
from aeos.docs_engine.visuals import STATUS_RENDERED, VisualArtifact
from aeos.models.publishing import DocumentRecord, DocumentRevision

RENDERER_VERSION = "1"

# DocumentRecord.status values
ST_DRAFT = "DRAFT"
ST_APPROVED = "APPROVED"
ST_PUBLISHED = "PUBLISHED"
ST_SUPERSEDED = "SUPERSEDED"
ST_NOT_STARTED = "NOT_STARTED"
ST_NOT_APPLICABLE = "NOT_APPLICABLE"

# Section render states
SEC_FILLED = "FILLED"
SEC_PENDING = "PENDING"  # documentation pending
SEC_LIVE = "LIVE_REFERENCE"
SEC_HUMAN = "HUMAN_REQUIRED"
SEC_NOT_STARTED = "NOT_STARTED"
SEC_NOT_APPLICABLE = "NOT_APPLICABLE"

PENDING_TEXT = "_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._"
HUMAN_TEXT = ("_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). "
              "AEOS dan model tidak mengisi bagian ini._")

TRACKER_BY_DOMAIN = {"PERSONAL": "GitHub Issues", "OFFICE": "Jira"}
DOCS_BY_DOMAIN = {"PERSONAL": "Google Docs (Google Drive folder proyek)", "OFFICE": "Confluence (project folder)"}


# ---------------------------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class RepoInfo:
    provider: str
    full_name: str
    url: str = ""
    default_branch: str = "main"


@dataclass(frozen=True)
class ProjectIdentity:
    id: str
    key: str
    name: str
    domain: str
    data_classification: str = "INTERNAL"
    lifecycle: str = "ACTIVE"
    description: str = ""
    owner_name: str | None = None
    is_platform: bool = False
    project_kind: str = "NEW"
    repositories: tuple[RepoInfo, ...] = ()

    def slice(self) -> dict[str, Any]:
        return {
            "id": self.id, "key": self.key, "name": self.name, "domain": self.domain,
            "classification": self.data_classification, "lifecycle": self.lifecycle,
            "description": self.description, "owner": self.owner_name, "platform": self.is_platform,
            "kind": self.project_kind,
            "repos": [(r.provider, r.full_name, r.url, r.default_branch) for r in self.repositories],
        }


@dataclass(frozen=True)
class VersionIdentity:
    id: str
    plan_id: str
    version: int
    manifest_hash: str
    approved: bool
    approval_decision_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskInfo:
    id: str
    task_key: str
    title: str
    goal: str = ""
    feature_key: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    base_commit: str | None = None

    def slice(self) -> dict[str, Any]:
        return {"key": self.task_key, "title": self.title, "goal": self.goal, "feature": self.feature_key,
                "ac": list(self.acceptance_criteria), "deps": list(self.depends_on), "base": self.base_commit}


# ---------------------------------------------------------------------------------------------
# Outputs


@dataclass
class RenderedSection:
    key: str
    title: str
    anchor: str
    content_type: str
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "anchor": self.anchor, "content_type": self.content_type,
                "status": self.status, "note": self.note}


@dataclass
class RenderedDocument:
    logical_doc_id: str
    node: str
    title: str
    kind: str  # DOC | FOLDER
    doc_type: str
    parent_logical_id: str | None
    timing: str
    status: str
    status_reason: str
    owner: str
    order_index: int
    input_hash: str
    template_key: str | None = None
    content_type: str = ContentType.AI_MAINTAINED.value
    body_markdown: str | None = None  # None when reused (input hash unchanged)
    content_hash: str | None = None
    sections: list[RenderedSection] = field(default_factory=list)
    pending_sections: list[str] = field(default_factory=list)
    links: dict[str, Any] = field(default_factory=dict)
    visual_keys: list[str] = field(default_factory=list)
    reused: bool = False

    @property
    def is_folder(self) -> bool:
        return self.kind == "FOLDER"


@dataclass
class RenderResult:
    project_id: str
    plan_version_id: str
    approved: bool
    product_title: str
    documents: list[RenderedDocument]
    visuals: list[VisualArtifact] = field(default_factory=list)

    def by_id(self) -> dict[str, RenderedDocument]:
        return {d.logical_doc_id: d for d in self.documents}


# ---------------------------------------------------------------------------------------------
# Markdown helpers


def _bullets(items: Iterable[Any]) -> str:
    lines = [f"- {item_text(x)}" for x in items if item_text(x)]
    return "\n".join(lines)


def _numbered(items: Iterable[Any], prefix: str = "") -> str:
    lines = []
    for i, x in enumerate([x for x in items if item_text(x)], start=1):
        lines.append(f"{i}. {prefix}{item_text(x)}")
    return "\n".join(lines)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(v: str) -> str:
        return (v or "—").replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _join(*parts: str | None) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _live(source: str, what: str, *, status: str | None = None) -> str:
    lead = f"**{status}** — " if status else ""
    return (f"> {lead}{what}\n>\n> Sumber live: {source}. Status dinamis dibaca dari sumber tersebut dan diproyeksikan "
            f"di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.")


class _Doc:
    """Section values for one document; enforces the content-type rules."""

    def __init__(self, spec: p1.DocTypeSpec, logical_id: str):
        self.spec = spec
        self.logical_id = logical_id
        self.values: dict[str, tuple[str, str]] = {}

    def put(self, key: str, text: str | None) -> None:
        sec = self.spec.section(key)
        if sec.content_type == ContentType.HUMAN_GOVERNED:
            raise ValueError(f"{key} is HUMAN_GOVERNED and cannot be filled by the renderer")
        if sec.content_type == ContentType.DYNAMIC:
            raise ValueError(f"{key} is DYNAMIC: use live() to reference the source")
        text = (text or "").strip()
        self.values[key] = (text, SEC_FILLED) if text else (PENDING_TEXT, SEC_PENDING)

    def live(self, key: str, text: str) -> None:
        sec = self.spec.section(key)
        if sec.content_type != ContentType.DYNAMIC:
            raise ValueError(f"{key} is {sec.content_type}, not DYNAMIC")
        self.values[key] = (text.strip(), SEC_LIVE)

    def human(self, key: str, reference: str = "") -> None:
        sec = self.spec.section(key)
        if sec.content_type != ContentType.HUMAN_GOVERNED:
            raise ValueError(f"{key} is not HUMAN_GOVERNED")
        self.values[key] = (_join(HUMAN_TEXT, reference), SEC_HUMAN)

    def not_started(self, key: str, reason: str, *, applicable: bool = True) -> None:
        label = "NOT STARTED" if applicable else "NOT APPLICABLE"
        self.values[key] = (f"> **{label}** — {reason}", SEC_NOT_STARTED if applicable else SEC_NOT_APPLICABLE)


def _anchor(logical_id: str, section_key: str) -> str:
    return f"{logical_id.replace('/', '--')}--{section_key.replace('_', '-')}"


def _compose(doc: RenderedDocument, builder: _Doc, *, header_title: str, metadata: list[tuple[str, str]],
             intro: str = "") -> None:
    lines = [f"# {header_title}", "", f"<!-- aeos:logical_id={doc.logical_doc_id} renderer={RENDERER_VERSION} -->", ""]
    lines += [_table(["Metadata", "Nilai"], [[k, v] for k, v in metadata]), ""]
    if intro:
        lines += [intro.strip(), ""]
    sections: list[RenderedSection] = []
    pending: list[str] = []
    for spec in builder.spec.sections:
        text, status = builder.values.get(spec.key, (PENDING_TEXT, SEC_PENDING))
        if spec.content_type == ContentType.HUMAN_GOVERNED and status not in (SEC_NOT_STARTED, SEC_NOT_APPLICABLE):
            text, status = builder.values.get(spec.key, (HUMAN_TEXT, SEC_HUMAN))
        anchor = _anchor(doc.logical_doc_id, spec.key)
        lines += [f"## {spec.title}", "",
                  f"<!-- aeos:section={spec.key} anchor={anchor} content_type={spec.content_type.value} -->",
                  f"_Content type: {spec.content_type.value}_", "", text, ""]
        sections.append(RenderedSection(spec.key, spec.title, anchor, spec.content_type.value, status, spec.note))
        if status == SEC_PENDING:
            pending.append(spec.title)
    doc.body_markdown = "\n".join(lines).rstrip() + "\n"
    doc.sections = sections
    doc.pending_sections = pending
    counts = Counter(s.content_type.value for s in builder.spec.sections)
    doc.content_type = counts.most_common(1)[0][0] if counts else ContentType.AI_MAINTAINED.value
    doc.content_hash = content_hash({"markdown": doc.body_markdown})


# ---------------------------------------------------------------------------------------------
# Renderer


class _Ctx:
    def __init__(self, project: ProjectIdentity, version: VersionIdentity, view: PlanView, tasks: list[TaskInfo],
                 visuals: list[VisualArtifact], previous: dict[str, str]):
        self.project = project
        self.version = version
        self.view = view
        self.tasks = tasks
        self.visuals = visuals
        self.previous = previous
        self.docs: list[RenderedDocument] = []
        self.order = 0
        self.title = p1.product_title(as_text(view.product.get("name")) or project.name)

    # -- owners: a role is never assigned to an invented person (§27 "owner yang sah")
    def owner(self, role: str) -> str:
        team = self.view.team
        key = role.lower().replace(" ", "_")
        name = as_text(team.get(key)) if team else ""
        if name:
            return f"{name} ({role})"
        if self.project.owner_name:
            return f"{self.project.owner_name} (pemilik proyek; {role} belum ditetapkan)"
        return f"UNASSIGNED ({role} belum ditetapkan)"

    def tracker(self) -> str:
        tracker = TRACKER_BY_DOMAIN.get(self.project.domain, "issue tracker")
        repo = self.code_repo()
        if self.project.domain == "PERSONAL" and repo:
            return f"{tracker} ({repo.full_name})"
        return tracker

    def code_repo(self) -> RepoInfo | None:
        want = "GITHUB" if self.project.domain == "PERSONAL" else "BITBUCKET"
        for r in self.project.repositories:
            if r.provider.upper() == want:
                return r
        return self.project.repositories[0] if self.project.repositories else None

    def tasks_for(self, feature_key: str) -> list[TaskInfo]:
        return [t for t in self.tasks if (t.feature_key or "") == feature_key]

    def visuals_for(self, *kinds: str) -> list[VisualArtifact]:
        return [v for v in self.visuals if v.kind in kinds]

    def metadata(self, doc: RenderedDocument) -> list[tuple[str, str]]:
        v = self.version
        src = f"plan `{v.plan_id}` v{v.version} (`{v.manifest_hash[:23]}`)"
        if not v.approved:
            src += " — PREVIEW, BELUM DISETUJUI"
        return [
            ("Logical doc ID", f"`{doc.logical_doc_id}`"),
            ("Project / domain", f"{self.project.name} (`{self.project.key}`) / {self.project.domain}"),
            ("Owner", doc.owner),
            ("Status (saat render)", f"{doc.status}" + (f" — {doc.status_reason}" if doc.status_reason else "")),
            ("Source plan / version", src),
            ("External revision / last synced", "dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis "
                                                 "ke isi dokumen"),
            ("Last reviewed", "belum direview manusia (dicatat di AEOS)"),
            ("Links", "; ".join(f"{k}: {', '.join(map(str, vals))}" for k, vals in sorted(doc.links.items())
                                if vals and isinstance(vals, list) and k != "task_ids") or "—"),
            ("Issue / evidence", "dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen"),
        ]

    # -- registration

    def add(self, *, logical_id: str, node: str, title: str, kind: str, doc_type: str, parent: str | None,
            timing: p1.Timing, status: str, reason: str, owner: str, slice_: dict[str, Any],
            template_key: str | None = None, links: dict[str, Any] | None = None,
            build: Callable[[RenderedDocument], None] | None = None) -> RenderedDocument:
        self.order += 1
        input_hash = content_hash({"renderer": RENDERER_VERSION, "structure": p1.STRUCTURE_VERSION,
                                   "logical_id": logical_id, "status": status, "reason": reason, "owner": owner,
                                   "approved": self.version.approved, "slice": slice_})
        doc = RenderedDocument(
            logical_doc_id=logical_id, node=node, title=title, kind=kind, doc_type=doc_type, parent_logical_id=parent,
            timing=timing.value, status=status, status_reason=reason, owner=owner, order_index=self.order,
            input_hash=input_hash, template_key=template_key, links=links or {},
        )
        if kind == "FOLDER":
            doc.content_type = ContentType.STATIC.value
            doc.content_hash = content_hash({"folder": logical_id, "title": title})
        elif self.previous.get(logical_id) == input_hash:
            doc.reused = True  # delta: unchanged input → no re-render
        elif build is not None:
            build(doc)
        self.docs.append(doc)
        return doc


def _visual_block(v: VisualArtifact) -> str:
    head = (f"**Diagram: {v.title}** — {v.kind_label}; status desain (DESIGN), bukan bukti runtime. "
            f"Sumber yang disyaratkan: {v.required_source}.")
    if v.status == STATUS_RENDERED:
        img = f"![{v.title}](visual:{v.key})"
    else:
        img = f"> **Visual pending** — {v.reason or 'belum dirender'}. Source Mermaid disimpan agar dapat di-diff."
    return _join(head, img, f"```mermaid\n{v.source.strip()}\n```")


def _visuals_text(ctx: _Ctx, *kinds: str) -> str:
    return _join(*[_visual_block(v) for v in ctx.visuals_for(*kinds)])


def _feature_slice(f: FeatureView, tasks: list[TaskInfo]) -> dict[str, Any]:
    return {"feature": f.raw, "key": f.key, "tasks": [t.slice() for t in tasks]}


def _future_doc(ctx: _Ctx, node: p1.Node, parent: str | None, *, applicable: bool, reason: str) -> None:
    spec = p1.DOC_TYPES[node.doc_type or ""]
    status = ST_NOT_STARTED if applicable else ST_NOT_APPLICABLE
    owner = ctx.owner(node.owner_role)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for s in spec.sections:
            b.not_started(s.key, reason, applicable=applicable)
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))
        doc.pending_sections = []

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type,
            parent=parent, timing=node.timing, status=status, reason=reason, owner=owner,
            slice_={"project": ctx.project.slice(), "applicable": applicable}, build=build)


def _folder(ctx: _Ctx, node: p1.Node, parent: str | None, *, has_items: bool, reason: str = "") -> None:
    status = ST_APPROVED if has_items else ST_NOT_STARTED
    if not ctx.version.approved:
        status = ST_DRAFT if has_items else ST_NOT_STARTED
    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="FOLDER", doc_type="FOLDER", parent=parent,
            timing=node.timing, status=status, reason="" if has_items else reason, owner=ctx.owner(node.owner_role),
            slice_={"title": node.title, "has_items": has_items})


# --- builders per document type -------------------------------------------------------------


def _product_home(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    v, pr = ctx.view, ctx.project
    spec = p1.DOC_TYPES["PRODUCT_HOME"]
    features = [(f.key, f.title, f.priority, f.milestone) for f in v.features]
    slice_ = {"project": pr.slice(), "product": v.product, "summary": v.summary, "team": v.team, "stack": v.stack,
              "criticality": as_text(v.get("criticality")), "features": features,
              "milestones": [as_text(m.get("title")) for m in v.milestones], "risks": len(v.risks),
              "services": [item_text(c) for c in as_list(v.architecture.get("services"))]}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("identity", _join(_table(["Atribut", "Nilai"], [
            ["Nama produk", ctx.title],
            ["Project ID", f"`{pr.id}`"],
            ["Project key", f"`{pr.key}`"],
            ["Domain", pr.domain + " (dipilih manusia; tidak diubah oleh model)"],
            ["Jenis proyek", pr.project_kind + (" — proyek platform DLY AEOS (self-build)" if pr.is_platform else "")],
            ["Lifecycle", pr.lifecycle],
        ]), as_text(v.summary) or pr.description))
        team_rows = [[role, as_text(v.team.get(role.lower().replace(" ", "_"))) or "belum ditetapkan"]
                     for role in ("Product Owner", "Engineering Lead", "Tech Lead")]
        b.put("ownership", _join(_table(["Peran", "Nama"], team_rows),
                                 f"Pemilik proyek di AEOS: {pr.owner_name}" if pr.owner_name else ""))
        repo = ctx.code_repo()
        services = [item_text(c) for c in as_list(v.architecture.get("services"))]
        b.put("repository", _table(["Jenis", "Referensi"], [
            ["Repository", f"{repo.full_name} ({repo.provider})" if repo else "belum terhubung"],
            ["Issue tracker", ctx.tracker()],
            ["Dokumentasi resmi", DOCS_BY_DOMAIN.get(pr.domain, "—")],
            ["Service", ", ".join(services) if services else "belum didefinisikan di rencana"],
        ]))
        b.put("classification", _table(["Atribut", "Nilai"], [
            ["Criticality", as_text(v.get("criticality")) or "belum ditetapkan"],
            ["Data classification", pr.data_classification],
        ]))
        b.put("stack", _bullets(v.stack))
        links = [f"- {n.title}: `{n.key}`" for n, _p in p1.iter_nodes() if n.kind == p1.NodeKind.DOCUMENT]
        if repo and repo.url:
            links.append(f"- Repository: {repo.url}")
        b.live("quick_links", _join("\n".join(links), "Tautan eksternal (Google Docs/Confluence/issue) ditambahkan oleh "
                                                      "publisher sebagai ExternalReference setelah publikasi."))
        b.live("current_development", _live(ctx.tracker(), "Pekerjaan berjalan (issue/work item) diproyeksikan dari "
                                                          "tracker; status Done di tracker tidak membuka dependency "
                                                          "runtime tanpa bukti penerimaan."))
        feat_rows = [[f"`{k}`", t, p or "—", m or "—"] for k, t, p, m in features]
        b.live("active_work", _join(_table(["Feature", "Judul", "Prioritas", "Milestone"], feat_rows)
                                    if feat_rows else "Belum ada fitur di rencana yang disetujui.",
                                    _live(ctx.tracker(), "Status epic/fitur/release dibaca dari tracker dan "
                                                         "Release Plans.")))
        b.live("open_risks", _join(f"Rencana yang disetujui mencatat {len(v.risks)} risiko awal (lihat Initial Risk "
                                   "Assessment)." if v.risks else "Rencana tidak mencatat risiko awal.",
                                   _live("AEOS verifier/security review dan tracker",
                                         "Temuan security terbuka hanya dari review/evidence nyata.")))
        b.put("sdlc_lifecycle", _join(
            f"Lifecycle proyek: **{pr.lifecycle}**.",
            _table(["Tahap SDLC (§28)", "Dokumen"], [[r["timing"], r["documents"]] for r in p1.TIMING_TABLE]),
        ))
        b.human("governance_links", "Referensi: AEOS Control Center → Keputusan (Decision registry) untuk approval "
                                    "rencana, publikasi, merge dan risk acceptance.")
        intro = f"**{ctx.title}**"
        _compose(doc, b, header_title=f"{node.title} — {ctx.title}", metadata=ctx.metadata(doc), intro=intro)

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Product Owner"), slice_=slice_,
            links={"requirement": [f"02-requirements/prd-{f.key}" for f in v.features]}, build=build)


def _ok_status(ctx: _Ctx) -> str:
    return ST_APPROVED if ctx.version.approved else ST_DRAFT


def _product_overview(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    v = ctx.view
    spec = p1.DOC_TYPES["PRODUCT_OVERVIEW"]
    slice_ = {"summary": v.summary, "vision": v.vision, "objectives": v.objectives, "in": v.scope_in,
              "out": v.scope_out, "team": v.team, "stakeholders": as_list(v.get("stakeholders")),
              "milestones": v.milestones, "features": [(f.key, f.title, f.milestone) for f in v.features],
              "components": as_list(v.architecture.get("components")), "services": as_list(v.architecture.get("services")),
              "data": v.data}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", v.summary)
        b.put("vision_objectives", _join(f"**Vision:** {v.vision}" if v.vision else "",
                                         _bullets(v.objectives) if v.objectives else ""))
        b.put("scope", _join("**In scope**\n\n" + _bullets(v.scope_in) if v.scope_in else "",
                             "**Out of scope / non-goals**\n\n" + _bullets(v.scope_out) if v.scope_out else ""))
        team_rows = [[k.replace("_", " ").title(), as_text(x)] for k, x in sorted(v.team.items()) if as_text(x)]
        b.put("team_ownership", _table(["Peran", "Nama"], team_rows) if team_rows else
              (f"Pemilik proyek: {ctx.project.owner_name}" if ctx.project.owner_name else ""))
        b.put("stakeholders", _bullets(as_list(v.get("stakeholders"))))
        roadmap_rows = []
        for m in v.milestones:
            mkey = as_text(m.get("milestone_key") or m.get("key"))
            feats = [f.title for f in v.features if f.milestone and f.milestone == mkey]
            roadmap_rows.append([as_text(m.get("title")) or mkey, ", ".join(feats) or "—",
                                 as_text(m.get("exit_evidence")) or "—"])
        b.put("roadmap", _table(["Milestone", "Fitur", "Exit evidence"], roadmap_rows) if roadmap_rows else "")
        comps = as_list(v.architecture.get("components")) + as_list(v.architecture.get("services"))
        b.put("system_inventory", _bullets(comps))
        b.put("data_overview", _join(as_text(v.data.get("overview") or v.data.get("summary")),
                                     _bullets(as_list(v.data.get("types")))))
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Product Owner"), slice_=slice_,
            build=build)


def _prd(ctx: _Ctx, f: FeatureView, parent: str) -> None:
    spec = p1.DOC_TYPES["PRD"]
    tasks = ctx.tasks_for(f.key)
    logical_id = f"{parent}/prd-{f.key}"
    rfc_ids = [f"03-rfcs/rfc-{f.key}"] if f.needs_rfc else []
    tdd_ids = [f"03-tdds/tdd-{f.key}"] if f.needs_tdd else []
    slice_ = {**_feature_slice(f, tasks), "rfc": rfc_ids, "tdd": tdd_ids}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", f.summary)
        b.put("problem", as_text(f.get("problem")))
        b.put("background", as_text(f.get("background")))
        goals, non_goals = as_list(f.get("goals")), as_list(f.get("non_goals"))
        b.put("goals_non_goals", _join("**Goals**\n\n" + _bullets(goals) if goals else "",
                                       "**Non-goals**\n\n" + _bullets(non_goals) if non_goals else ""))
        b.put("business_requirements", _bullets(as_list(f.get("business_requirements"))))
        fr = as_list(f.get("functional_requirements", "requirements"))
        b.put("functional_requirements", _numbered(fr) if fr else as_text(f.get("requirement")))
        b.put("non_functional_requirements", _bullets(as_list(f.get("non_functional", "non_functional_requirements",
                                                                    "nfr"))))
        b.put("user_stories", _bullets(as_list(f.get("user_stories"))))
        ac_parts = [_numbered(f.acceptance, "AC: ")] if f.acceptance else []
        for t in tasks:
            if t.acceptance_criteria:
                ac_parts.append(f"**TaskSpec `{t.task_key}` — {t.title}**\n\n" + _bullets(t.acceptance_criteria))
        b.put("acceptance_criteria", _join(*ac_parts))
        b.put("user_customer_impact", as_text(f.get("user_impact", "customer_impact", "impact")))
        deps = [f"Fitur `{d}`" for d in f.depends_on]
        for t in tasks:
            deps += [f"Task `{t.task_key}` bergantung pada `{d}`" for d in t.depends_on]
        b.put("dependencies", _bullets(deps))
        b.put("assumptions", _bullets(as_list(f.get("assumptions"))))
        b.put("risks", _bullets(as_list(f.get("risks"))))
        b.put("success_metrics", _bullets(as_list(f.get("success_metrics", "metrics"))))
        b.put("rollout_considerations", as_text(f.get("rollout", "rollout_considerations")))
        rel = [f"- TaskSpec `{t.task_key}` — {t.title}" for t in tasks]
        rel += [f"- RFC `{x}`" for x in rfc_ids] + [f"- TDD `{x}`" for x in tdd_ids]
        rel.append(f"- Test Plan `06-testing/test-plan-{f.key}`")
        b.live("related_work", _join("\n".join(rel), _live(ctx.tracker(), "Issue/work item per TaskSpec dibuat "
                                                                          "publisher dan ditautkan sebagai "
                                                                          "ExternalReference.")))
        _compose(doc, b, header_title=f"PRD — {f.title}", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="02", title=f"PRD — {f.title}", kind="DOC", doc_type="PRD", parent=parent,
            timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Product Owner") if not f.owner else f"{f.owner} (Feature Owner)", slice_=slice_,
            template_key="prd",
            links={"requirement": [f.key], "tasks": [t.task_key for t in tasks], "task_ids": [t.id for t in tasks],
                   "commit": sorted({t.base_commit for t in tasks if t.base_commit}),
                   "related": rfc_ids + tdd_ids + [f"06-testing/test-plan-{f.key}"]},
            build=build)


def _architecture_overview(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    a = ctx.view.architecture
    spec = p1.DOC_TYPES["ARCHITECTURE_OVERVIEW"]
    slice_ = {"architecture": {k: x for k, x in a.items() if k != "diagrams"},
              "visuals": [(x.key, x.kind, x.source_hash, x.status) for x in ctx.visuals]}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("context", _join(as_text(a.get("context")), _visuals_text(ctx, "context")))
        other = [x for x in ctx.visuals if x.kind not in
                 ("context", "component", "erd", "data_flow", "trust_boundary", "sequence", "cicd", "deployment")]
        b.put("high_level_architecture", _join(as_text(pick(a, "high_level", "overview", "summary")),
                                               *[_visual_block(x) for x in other]))
        b.put("component_architecture", _join(_bullets(as_list(a.get("components"))), _visuals_text(ctx, "component")))
        b.put("data_architecture", _join(as_text(pick(a, "data", "data_architecture")), _visuals_text(ctx, "erd")))
        b.put("data_flow", _join(as_text(a.get("data_flow")), _visuals_text(ctx, "data_flow", "trust_boundary")))
        b.put("external_integrations", _bullets(as_list(pick(a, "integrations", "external_integrations"))))
        b.put("api_landscape", _join(_bullets(as_list(pick(a, "apis", "api_landscape"))), _visuals_text(ctx, "sequence")))
        b.put("infrastructure", _join(as_text(a.get("infrastructure")), _visuals_text(ctx, "cicd", "deployment")))
        b.put("security_architecture", as_text(pick(a, "security", "security_architecture")))
        b.put("principles", _bullets(as_list(a.get("principles"))))
        decisions = as_list(a.get("decisions"))
        b.human("decisions", ("Usulan keputusan dari rencana (DATA, bukan approval):\n\n" + _bullets(decisions))
                if decisions else "")
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Tech Lead"), slice_=slice_,
            links={"visuals": [x.key for x in ctx.visuals]}, build=build)


def _rfc_or_tdd(ctx: _Ctx, kind: str, key: str, title: str, data: dict[str, Any], parent: str,
                feature: FeatureView | None) -> None:
    spec = p1.DOC_TYPES[kind]
    prefix = "rfc" if kind == "RFC" else "tdd"
    logical_id = f"{parent}/{prefix}-{key}"
    slice_ = {"data": data, "feature": feature.raw if feature else None}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for s in spec.sections:
            if s.content_type == ContentType.HUMAN_GOVERNED:
                b.human(s.key)
            elif s.content_type == ContentType.DYNAMIC:
                b.live(s.key, _live("AEOS", "Status dibaca dari sumber live."))
            else:
                b.put(s.key, as_text(data.get(s.key)) if not isinstance(data.get(s.key), list)
                      else _bullets(data[s.key]))
        _compose(doc, b, header_title=f"{spec.title} — {title}", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="03", title=f"{spec.title} — {title}", kind="DOC", doc_type=kind,
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Tech Lead"), slice_=slice_, template_key=prefix,
            links={"requirement": [feature.key] if feature else []}, build=build)


def _security_overview(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    sec = ctx.view.security
    spec = p1.DOC_TYPES["SECURITY_OVERVIEW"]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("ownership", ctx.owner("Security Lead"))
        authn, authz = as_text(pick(sec, "authn", "authentication")), as_text(pick(sec, "authz", "authorization"))
        b.put("authn_authz", _join(f"**Authn:** {authn}" if authn else "", f"**Authz:** {authz}" if authz else ""))
        b.put("encryption", as_text(sec.get("encryption")))
        b.put("secrets", as_text(sec.get("secrets")))
        b.put("logs_monitoring", as_text(pick(sec, "logging", "logs_monitoring", "monitoring")))
        b.put("controls", _bullets(as_list(sec.get("controls"))))
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Security Lead"),
            slice_={"security": {k: x for k, x in sec.items() if k not in ("threats", "threat_model")},
                    "team": ctx.view.team, "owner": ctx.project.owner_name}, build=build)


def _initial_risk_assessment(ctx: _Ctx, parent: str) -> None:
    spec = p1.DOC_TYPES["RISK_ASSESSMENT"]
    risks = ctx.view.risks
    logical_id = f"{parent}/initial-risk-assessment"

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("context_scope", _join("Initial risk/security assessment saat project creation (§28), dari rencana yang "
                                     "disetujui. Ini identifikasi awal, bukan hasil pengujian security.",
                                     as_text(ctx.view.summary)))
        rows = []
        for i, r in enumerate(risks, start=1):
            if isinstance(r, dict):
                rows.append([as_text(r.get("id") or r.get("key")) or f"R{i}",
                             as_text(r.get("title") or r.get("risk") or r.get("description")),
                             as_text(r.get("likelihood")) or "—", as_text(r.get("impact") or r.get("severity")) or "—",
                             as_text(r.get("owner")) or "—"])
            else:
                rows.append([f"R{i}", as_text(r), "—", "—", "—"])
        b.put("risk_register", _table(["ID", "Risiko", "Likelihood", "Impact", "Owner"], rows) if rows else "")
        b.put("likelihood_impact", "Rating mengikuti nilai di register; ambang risiko kanonik (MCI) UNVERIFIED — tidak "
                                   "dikarang." if rows else "")
        mitig = [f"{as_text(r.get('title') or r.get('risk'))}: {as_text(r.get('mitigation'))}" for r in risks
                 if isinstance(r, dict) and as_text(r.get("mitigation"))]
        b.put("mitigations", _bullets(mitig))
        b.human("residual_risk")
        b.put("risk_owner", ctx.owner("Product Owner"))
        b.human("acceptance", "Risk acceptance hanya melalui Decision manusia (kind RISK_ACCEPTANCE).")
        _compose(doc, b, header_title="Initial Risk Assessment", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title="Initial Risk Assessment", kind="DOC", doc_type="RISK_ASSESSMENT",
            parent=parent, timing=p1.Timing.PROJECT_CREATION, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Product Owner"), slice_={"risks": risks, "summary": ctx.view.summary,
                                                     "team": ctx.view.team, "owner": ctx.project.owner_name},
            template_key="risk-assessment", build=build)


def _threat_model_input(ctx: _Ctx) -> tuple[dict[str, Any], list[Any]] | None:
    sec = ctx.view.security
    tm = sec.get("threat_model") if isinstance(sec.get("threat_model"), dict) else {}
    threats = as_list(sec.get("threats")) or as_list(tm.get("threats"))
    if not threats and not tm:
        return None
    return tm, threats


def _threat_model(ctx: _Ctx, parent: str) -> None:
    data = _threat_model_input(ctx)
    if data is None:
        return
    tm, threats = data
    spec = p1.DOC_TYPES["THREAT_MODEL"]
    logical_id = f"{parent}/threat-model-initial"

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for key in ("assets", "actors", "trust_boundaries", "entry_points", "attack_scenarios", "mitigations"):
            b.put(key, _bullets(as_list(tm.get(key))))
        b.put("threats", _bullets(threats))
        tb = _visuals_text(ctx, "trust_boundary", "data_flow")
        if tb:
            b.put("trust_boundaries", _join(_bullets(as_list(tm.get("trust_boundaries"))), tb))
        _compose(doc, b, header_title="Threat Model — Initial", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title="Threat Model — Initial", kind="DOC", doc_type="THREAT_MODEL",
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Security Lead"), slice_={"tm": tm, "threats": threats,
                                                     "visuals": [x.source_hash for x in ctx.visuals]},
            template_key="threat-model", build=build)


def _privacy_assessment(ctx: _Ctx, f: FeatureView, parent: str) -> None:
    spec = p1.DOC_TYPES["PRIVACY_ASSESSMENT"]
    privacy = f.raw.get("privacy") if isinstance(f.raw.get("privacy"), dict) else {}
    logical_id = f"{parent}/dpia-{f.key}"

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for s in spec.sections:
            if s.content_type == ContentType.HUMAN_GOVERNED:
                b.human(s.key)
            else:
                val = privacy.get(s.key)
                b.put(s.key, _bullets(val) if isinstance(val, list) else as_text(val))
        _compose(doc, b, header_title=f"Privacy Assessment / DPIA — {f.title}", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title=f"Privacy Assessment / DPIA — {f.title}", kind="DOC",
            doc_type="PRIVACY_ASSESSMENT", parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx),
            reason="", owner=ctx.owner("Privacy Officer"), slice_={"privacy": privacy, "feature": f.key},
            template_key="privacy-assessment-dpia", links={"requirement": [f.key]}, build=build)


def _data_inventory(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    d = ctx.view.data
    spec = p1.DOC_TYPES["DATA_INVENTORY"]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("data_types", _bullets(as_list(pick(d, "types", "data_types"))))
        b.put("classification", f"Klasifikasi proyek: **{ctx.project.data_classification}**")
        b.put("owner", as_text(d.get("owner")) or ctx.owner("Privacy Officer"))
        for key in ("storage", "access", "retention", "disposal"):
            val = d.get(key)
            b.put(key, _bullets(val) if isinstance(val, list) else as_text(val))
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Privacy Officer"),
            slice_={"data": d, "classification": ctx.project.data_classification, "team": ctx.view.team,
                    "owner": ctx.project.owner_name}, build=build)


def _security_decisions(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    spec = p1.DOC_TYPES["SECURITY_DECISIONS"]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.human("decision_log", "Sumber: AEOS Decision registry (kind POLICY/EXCEPTION/RISK_ACCEPTANCE untuk proyek ini).")
        b.human("exceptions")
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Security Lead"),
            slice_={"team": ctx.view.team, "owner": ctx.project.owner_name}, build=build)


def _development(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    dev = ctx.view.development
    spec = p1.DOC_TYPES["DEVELOPMENT_GUIDELINES"]
    aliases = {"development_guidelines": ("guidelines", "development_guidelines"),
               "coding_standards": ("coding_standards", "coding"), "secure_coding": ("secure_coding",),
               "branching": ("branching", "branch_strategy"), "pr_review": ("pr_review", "review"),
               "dependency_management": ("dependency_management", "dependencies", "open_source"),
               "dev_environment": ("dev_environment", "environment"), "secrets": ("secrets",),
               "ci_cd_requirements": ("ci_cd", "ci_cd_requirements", "ci")}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for key, keys in aliases.items():
            val = next((dev[k] for k in keys if dev.get(k)), None)
            b.put(key, _bullets(val) if isinstance(val, list) else as_text(val))
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner("Engineering Lead"),
            slice_={"development": dev, "team": ctx.view.team, "owner": ctx.project.owner_name}, build=build)


def _test_plan(ctx: _Ctx, f: FeatureView, parent: str) -> None:
    t = ctx.view.testing
    ft = f.raw.get("testing") if isinstance(f.raw.get("testing"), dict) else {}
    spec = p1.DOC_TYPES["TEST_PLAN"]
    tasks = ctx.tasks_for(f.key)
    logical_id = f"{parent}/test-plan-{f.key}"

    def lookup(*keys: str) -> Any:
        for src in (ft, t):
            for k in keys:
                if src.get(k):
                    return src[k]
        return None

    def txt(*keys: str) -> str:
        val = lookup(*keys)
        return _bullets(val) if isinstance(val, list) else as_text(val)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", _join(f"Test Plan untuk fitur **{f.title}** (`{f.key}`).", f.summary))
        b.put("objectives", txt("objectives"))
        scope_items = [f"TaskSpec `{x.task_key}` — {x.title}" for x in tasks]
        b.put("scope", _join(_bullets(scope_items) if scope_items else "", txt("scope")))
        b.put("test_strategy", txt("strategy", "test_strategy"))
        b.put("approach", txt("approach"))
        b.put("test_levels", txt("levels", "test_levels"))
        b.put("test_types", txt("types", "test_types"))
        b.put("environment", txt("environment"))
        b.put("test_data", txt("data", "test_data"))
        b.put("tools", txt("tools"))
        acs = list(f.acceptance) + [ac for x in tasks for ac in x.acceptance_criteria]
        scenarios = lookup("scenarios")
        b.put("scenarios", _bullets(scenarios) if isinstance(scenarios, list) else
              (_join("Skenario minimum diturunkan dari acceptance criteria yang disetujui (bukan hasil uji):",
                     _numbered(acs)) if acs else ""))
        case_rows = [[f"TC-{i}", item_text(ac), "NOT STARTED"] for i, ac in enumerate(acs, start=1)]
        b.put("test_cases", _table(["Case", "Acceptance criterion", "Hasil"], case_rows) if case_rows else "")
        b.put("functional_testing", txt("functional"))
        b.put("regression_testing", txt("regression"))
        b.put("performance_testing", txt("performance"))
        b.put("security_testing", txt("security"))
        b.live("uat", _live("Sign-off manusia + EvidenceArtifact", "UAT belum dilakukan.", status="NOT STARTED"))
        b.live("execution_results", _live("AEOS independent verifier (EvidenceArtifact tepercaya)",
                                          "Belum ada eksekusi. Hasil hanya dari verifier independen; tidak ada "
                                          "PASS yang dinyatakan oleh dokumen atau model.", status="NOT STARTED"))
        b.live("defect_management", _live(ctx.tracker(), "Defect dicatat sebagai issue/work item."))
        b.put("exit_criteria", txt("exit_criteria"))
        b.human("final_assessment")
        b.human("sign_off")
        b.live("evidence_references", _live("AEOS Evidence & Decisions", "Referensi evidence ditautkan setelah verifier "
                                                                          "menjalankan suite."))
        _compose(doc, b, header_title=f"Test Plan — {f.title}", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="06", title=f"Test Plan — {f.title}", kind="DOC", doc_type="TEST_PLAN",
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("QA Lead"), slice_={**_feature_slice(f, tasks), "testing": t},
            template_key="test-plan",
            links={"requirement": [f.key, f"02-requirements/prd-{f.key}"], "tasks": [x.task_key for x in tasks],
                   "task_ids": [x.id for x in tasks]},
            build=build)


def _template_doc(ctx: _Ctx, tpl: p1.TemplateSpec, parent: str) -> None:
    body = p1.load_template(tpl.key)
    logical_id = f"{parent}/{tpl.key}"

    def build(doc: RenderedDocument) -> None:
        spec = p1.DOC_TYPES[tpl.doc_type]
        doc.body_markdown = body
        doc.content_type = ContentType.STATIC.value
        doc.sections = [RenderedSection(s.key, s.title, _anchor(logical_id, s.key), s.content_type.value, SEC_FILLED)
                        for s in spec.sections]
        doc.content_hash = content_hash({"markdown": body})

    ctx.add(logical_id=logical_id, node="TPL", title=f"Template — {tpl.title}", kind="DOC", doc_type="TEMPLATE",
            parent=parent, timing=p1.Timing.STATIC_TEMPLATE, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Product Owner"), slice_={"template": p1.load_template(tpl.key)}, template_key=tpl.key,
            build=build)


# ---------------------------------------------------------------------------------------------


def render_document_set(*, project: ProjectIdentity, version: VersionIdentity, content: dict[str, Any] | None,
                        tasks: list[TaskInfo] | None = None, visuals: list[VisualArtifact] | None = None,
                        previous_input_hashes: dict[str, str] | None = None) -> RenderResult:
    """Render the Product 1 set for one plan version (pure; see module docstring)."""
    view = build_view(content)
    ctx = _Ctx(project, version, view, list(tasks or []), list(visuals or []), dict(previous_input_hashes or {}))
    lifecycle = project.lifecycle.upper()
    root = "root"
    ctx.add(logical_id=root, node="ROOT", title=project.name, kind="FOLDER", doc_type="FOLDER", parent=None,
            timing=p1.Timing.PROJECT_CREATION, status=_ok_status(ctx), reason="", owner=ctx.owner("Product Owner"),
            slice_={"name": project.name, "title": ctx.title})

    for node in p1.TREE:
        if node.key == "00-product-home":
            _product_home(ctx, node, root)
        elif node.key == "01-product-overview":
            _product_overview(ctx, node, root)
        elif node.key == "02-requirements":
            _folder(ctx, node, root, has_items=bool(view.features),
                    reason="Belum ada fitur pada rencana yang disetujui.")
            for f in view.features:
                _prd(ctx, f, node.key)
        elif node.key == "03-architecture-design":
            _folder(ctx, node, root, has_items=True)
            for child in node.children:
                if child.key == "03-architecture-overview":
                    _architecture_overview(ctx, child, node.key)
                elif child.key in ("03-rfcs", "03-tdds"):
                    kind = "RFC" if child.key == "03-rfcs" else "TDD"
                    entries: list[tuple[str, str, dict[str, Any], FeatureView | None]] = []
                    for i, raw in enumerate(as_list(view.get("rfcs" if kind == "RFC" else "tdds"))):
                        if isinstance(raw, dict):
                            key = as_text(raw.get("key") or raw.get("id")) or f"{kind.lower()}-{i + 1}"
                            entries.append((key, as_text(raw.get("title")) or key, raw, None))
                    for f in view.features:
                        flag = f.needs_rfc if kind == "RFC" else f.needs_tdd
                        data = f.raw.get(kind.lower()) if isinstance(f.raw.get(kind.lower()), dict) else {}
                        if flag:
                            entries.append((f.key, f.title, data, f))
                    _folder(ctx, child, node.key, has_items=bool(entries),
                            reason=f"Belum ada {kind} yang diperlukan pada rencana yang disetujui (dibuat bila "
                                   "diperlukan saat feature approval).")
                    for key, title, data, feat in entries:
                        _rfc_or_tdd(ctx, kind, key, title, data, child.key, feat)
        elif node.key == "04-security-privacy":
            _folder(ctx, node, root, has_items=True)
            for child in node.children:
                if child.key == "04-security-overview":
                    _security_overview(ctx, child, node.key)
                elif child.key == "04-security-assessments":
                    _folder(ctx, child, node.key, has_items=True)
                    _initial_risk_assessment(ctx, child.key)
                elif child.key == "04-threat-models":
                    _folder(ctx, child, node.key, has_items=_threat_model_input(ctx) is not None,
                            reason="Threat model dibuat saat feature approval bila rencana memuat threat/assessment.")
                    _threat_model(ctx, child.key)
                elif child.key == "04-vulnerability-assessments":
                    _folder(ctx, child, node.key, has_items=False,
                            reason="Belum ada vulnerability assessment yang dijalankan; hasil VA hanya dari "
                                   "pemeriksaan nyata (tidak dikarang).")
                elif child.key == "04-penetration-tests":
                    _folder(ctx, child, node.key, has_items=False,
                            reason="Belum ada penetration test; hasil PT hanya dari pengujian nyata dengan "
                                   "otorisasi (tidak dikarang).")
                elif child.key == "04-privacy-assessments":
                    pd = [f for f in view.features if f.personal_data]
                    unknown = any(f.personal_data is None for f in view.features)
                    _folder(ctx, child, node.key, has_items=bool(pd),
                            reason="Tidak ada fitur yang dinyatakan memproses data pribadi" +
                                   ("; status data pribadi sebagian fitur belum diketahui." if unknown else "."))
                    for f in pd:
                        _privacy_assessment(ctx, f, child.key)
                elif child.key == "04-data-inventory":
                    _data_inventory(ctx, child, node.key)
                elif child.key == "04-security-privacy-decisions":
                    _security_decisions(ctx, child, node.key)
        elif node.key == "05-development":
            _development(ctx, node, root)
        elif node.key == "06-testing":
            _folder(ctx, node, root, has_items=bool(view.features),
                    reason="Test Plan dibuat per fitur saat feature approval; belum ada fitur.")
            for f in view.features:
                _test_plan(ctx, f, node.key)
        elif node.key == "07-release-deployment":
            _folder(ctx, node, root, has_items=False,
                    reason="Release Plan (termasuk Release Notes) dibuat saat release candidate (§28).")
        elif node.key == "08-operations":
            _folder(ctx, node, root, has_items=True)
            for child in node.children:
                if child.kind == p1.NodeKind.FOLDER:
                    names = ", ".join(t for _k, t in p1.SUGGESTED_RUNBOOKS)
                    _folder(ctx, child, node.key, has_items=False,
                            reason=f"Runbook ({names}) dibuat sesuai scope saat release candidate.")
                else:
                    _future_doc(ctx, child, node.key, applicable=True,
                                reason="Dibuat saat production validation (§28); status hanya berdasarkan evidence "
                                       f"runtime. Owner: {ctx.owner(child.owner_role)}.")
        elif node.key == "09-incidents-problems":
            _folder(ctx, node, root, has_items=True)
            for child in node.children:
                _folder(ctx, child, node.key, has_items=False,
                        reason="Dibuat ketika insiden/problem nyata terjadi; tidak ada insiden tercatat.")
        elif node.key == "10-changes":
            _folder(ctx, node, root, has_items=False, reason="Change record dibuat per perubahan saat implementation/PR.")
        elif node.key == "11-improvements":
            _folder(ctx, node, root, has_items=False, reason="Improvement dicatat ketika ada peluang yang diajukan.")
        elif node.key == "12-retirement":
            retiring = lifecycle in ("RETIRING", "RETIRED")
            _future_doc(ctx, node, root, applicable=retiring,
                        reason=("Lifecycle proyek RETIRING: rencana closure disiapkan pemilik." if retiring else
                                "Lifecycle proyek belum retirement (§28)") + f" Owner: {ctx.owner(node.owner_role)}.")
        elif node.key == "templates":
            _folder(ctx, node, root, has_items=True)
            for tpl in p1.TEMPLATES:
                _template_doc(ctx, tpl, node.key)

    return RenderResult(project_id=project.id, plan_version_id=version.id, approved=version.approved,
                        product_title=ctx.title, documents=ctx.docs, visuals=ctx.visuals)


# ---------------------------------------------------------------------------------------------
# Persistence (DocumentRecord upserts with delta detection)


@dataclass
class DeltaReport:
    created: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"created": self.created, "changed": self.changed, "unchanged": self.unchanged,
                "superseded": self.superseded}


def previous_input_hashes(session: Session, project_id: str) -> dict[str, str]:
    rows = session.execute(select(DocumentRecord.logical_doc_id, DocumentRecord.input_hash)
                           .where(DocumentRecord.project_id == project_id)).all()
    return {lid: h for lid, h in rows if h}


def sync_document_records(session: Session, result: RenderResult, *, domain: str) -> DeltaReport:
    """Upsert DocumentRecords from an APPROVED render. Unchanged documents are not touched."""
    if not result.approved:
        raise Conflict("only an APPROVED plan version updates DocumentRecords; use a local preview instead",
                       code="PLAN_NOT_APPROVED")
    now = utcnow()
    report = DeltaReport()
    existing = {r.logical_doc_id: r for r in session.scalars(
        select(DocumentRecord).where(DocumentRecord.project_id == result.project_id))}
    seen: set[str] = set()
    for doc in result.documents:
        seen.add(doc.logical_doc_id)
        rec = existing.get(doc.logical_doc_id)
        if rec is not None and (doc.reused or (rec.input_hash == doc.input_hash and rec.content_hash ==
                                                 doc.content_hash)):
            if rec.status == ST_SUPERSEDED:  # re-appeared in a newer approved version
                rec.status = doc.status
                rec.status_reason = doc.status_reason
                report.changed.append(doc.logical_doc_id)
            else:
                report.unchanged.append(doc.logical_doc_id)
            rec.order_index = doc.order_index
            continue
        if rec is None:
            rec = DocumentRecord(project_id=result.project_id, domain=domain, logical_doc_id=doc.logical_doc_id,
                                 node=doc.node, title=doc.title)
            session.add(rec)
            report.created.append(doc.logical_doc_id)
            changed_content = True
        else:
            changed_content = rec.content_hash != doc.content_hash
            if changed_content:
                report.changed.append(doc.logical_doc_id)
            else:
                report.unchanged.append(doc.logical_doc_id)
        rec.node, rec.title, rec.kind = doc.node, doc.title, doc.kind
        rec.doc_type, rec.parent_logical_id, rec.template_key = doc.doc_type, doc.parent_logical_id, doc.template_key
        rec.timing, rec.owner, rec.content_type = doc.timing, doc.owner, doc.content_type
        rec.order_index, rec.input_hash = doc.order_index, doc.input_hash
        rec.pending_sections = list(doc.pending_sections)
        rec.sections = [s.to_dict() for s in doc.sections]
        rec.links = {**(rec.links or {}), **doc.links}
        rec.mode = "LIVE"
        if changed_content:
            # A published document with new content goes back to APPROVED (approved, not yet published).
            rec.status = doc.status
            rec.status_reason = doc.status_reason
            rec.content_hash = doc.content_hash
            rec.body_markdown = doc.body_markdown or ""
            rec.source_plan_version_id = result.plan_version_id
            rec.rendered_at = now
            session.flush()
            if doc.kind == "DOC":
                for old in session.scalars(select(DocumentRevision).where(
                        DocumentRevision.document_id == rec.id, DocumentRevision.kind == "RENDERED",
                        DocumentRevision.status == "CURRENT")):
                    old.status = "SUPERSEDED"
                session.add(DocumentRevision(
                    document_id=rec.id, project_id=rec.project_id, domain=domain, logical_doc_id=rec.logical_doc_id,
                    kind="RENDERED", status="CURRENT", plan_version_id=result.plan_version_id,
                    content_hash=doc.content_hash, body_markdown=doc.body_markdown or "",
                ))
    for lid, rec in existing.items():
        if lid not in seen and rec.status != ST_SUPERSEDED:
            rec.status = ST_SUPERSEDED
            rec.status_reason = f"tidak ada di PlanVersion {result.plan_version_id} yang disetujui"
            report.superseded.append(lid)
    session.flush()
    return report
