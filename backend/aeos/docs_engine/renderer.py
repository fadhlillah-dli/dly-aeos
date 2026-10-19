"""Deterministic Product 1 renderer (master §27, §28, §29, §41).

``render_document_set`` turns an approved PlanVersion's content plus the project identity into the
Product 1 document set as canonical Markdown with section anchors. It is a pure function (no DB,
no network, no LLM): planning output is DATA that is laid into the §27 sections; nothing is
invented. Specifically:

* sections without plan content are marked "documentation pending" (listed in
  ``pending_sections``) — never filled with made-up text;
* ``DYNAMIC`` sections *reference* the live source (issue tracker, verifier evidence, CI) instead of
  copying a status snapshot; ``HUMAN_GOVERNED`` sections are left for the authorised human;
* nodes whose §28 timing has not come are NOT STARTED / NOT APPLICABLE with a reason and the
  accountable owner — no PT/VA/UAT/restore results and no approvals are ever fabricated. When the
  approved plan or a real record provides them, they are rendered: Release Plans and runbooks from
  the plan, Operations documents as DRAFT proposals until trusted production-validation evidence,
  change records from broker-opened PRs (DRAFT until ACCEPTED), improvements from project
  ChangeProposals, VA/PT documents from trusted result evidence, the Retirement plan from the
  read-only retirement checklist (``RenderSignals``, gathered by ``docs_engine.service``);
* every document has an ``input_hash`` over exactly the plan slice it uses: when the stored hash
  matches, the document is not re-rendered at all (delta rendering, §28 "tidak diregenerasi penuh
  pada setiap edit"); ``content_hash`` over the Markdown drives the publication delta.

``sync_document_records`` persists the result as ``DocumentRecord`` upserts plus
``DocumentRevision`` snapshots. Only APPROVED versions may be synced.
"""

from __future__ import annotations

import re
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
from aeos.core.ids import slugify
from aeos.core.timeutil import utcnow
from aeos.docs_engine import product1 as p1
from aeos.docs_engine.plan_content import (
    FeatureView,
    PlanView,
    as_list,
    as_text,
    build_view,
    feature_slug,
    item_text,
    known,
)
from aeos.docs_engine.plan_content import _first as pick
from aeos.docs_engine.visuals import STATUS_RENDERED, VisualArtifact
from aeos.models.publishing import DocumentRecord, DocumentRevision

RENDERER_VERSION = "3"  # part of every input_hash: a renderer change re-renders (never reuses stale bodies)

# Trusted EvidenceArtifact kinds whose real results create §27 04 VA / PT documents.
VA_EVIDENCE_KINDS = ("VA_REPORT", "VULNERABILITY_ASSESSMENT")
PT_EVIDENCE_KINDS = ("PT_REPORT", "PENETRATION_TEST")

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

_CLASS_ORDER = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}

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


@dataclass(frozen=True)
class EvidenceRef:
    """A trusted, project-scoped EvidenceArtifact a document may *reference* (never copy a verdict from)."""

    id: str
    kind: str
    claim: str = ""
    origin: str = ""
    evidence_level: str = ""
    commit: str | None = None
    environment: str | None = None
    created_at: str | None = None
    summary: tuple[tuple[str, str], ...] = ()  # small redacted key/value summary (scope, method, tools ...)

    def get(self, key: str) -> str:
        return next((v for k, v in self.summary if k == key), "")

    def slice(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "claim": self.claim, "level": self.evidence_level,
                "commit": self.commit, "env": self.environment, "summary": list(self.summary)}


@dataclass(frozen=True)
class ChangeInfo:
    """One implementation change (TaskSpec + the PR the broker opened + its acceptance), §27 10. / §28 PR."""

    task_id: str
    task_key: str
    title: str
    goal: str = ""
    feature_key: str | None = None
    risk_class: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    pr_id: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str = ""
    repo: str = ""
    head_ref: str = ""
    base_ref: str = ""
    accepted_head_sha: str | None = None
    merge_commit_sha: str | None = None
    merged: bool = False
    acceptance_id: str = ""
    acceptance_status: str = ""
    evidence_ids: tuple[str, ...] = ()
    bundle_evidence_id: str | None = None
    rollback_notes: tuple[str, ...] = ()
    contract_deltas: tuple[str, ...] = ()

    def slice(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class ImprovementInfo:
    """A project-scoped ChangeProposal shown as an §27 11. Improvement (status lives in the proposal flow)."""

    id: str
    title: str
    kind: str = ""
    status: str = ""
    problem: str = ""
    expected_benefit: str = ""
    risk: str = ""
    owner: str = ""
    source_refs: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()

    def slice(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


@dataclass
class RenderSignals:
    """Records outside the plan that §28 timings depend on, gathered read-only by the service layer."""

    evidence: list[EvidenceRef] = field(default_factory=list)
    changes: list[ChangeInfo] = field(default_factory=list)
    improvements: list[ImprovementInfo] = field(default_factory=list)
    retirement: dict[str, Any] | None = None

    def evidence_of(self, *kinds: str) -> list[EvidenceRef]:
        return [e for e in self.evidence if e.kind in kinds]


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


def _live(source: str, what: str) -> str:
    """A DYNAMIC section: the live source and what to look up there — never a status word or snapshot (§28)."""
    body = "\n".join(f"> {line}" if line.strip() else ">" for line in what.strip().splitlines())
    return (f"{body}\n>\n> Sumber live: {source}. Status dinamis dibaca dari sumber tersebut dan diproyeksikan "
            f"di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.")


def _refs(label: str, ids: Iterable[str]) -> str:
    ids = [i for i in ids if i]
    return f"{label}: " + ", ".join(f"`{i}`" for i in ids) if ids else ""


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
                 visuals: list[VisualArtifact], previous: dict[str, str], signals: RenderSignals | None = None):
        self.project = project
        self.version = version
        self.view = view
        self.tasks = tasks
        self.visuals = visuals
        self.previous = previous
        self.signals = signals or RenderSignals()
        self.docs: list[RenderedDocument] = []
        self.order = 0
        self.title = p1.product_title(as_text(view.product.get("name")) or project.name)

    def changes_for(self, feature_key: str) -> list[ChangeInfo]:
        return [c for c in self.signals.changes if feature_slug(c.feature_key) == feature_key]

    def commit_links(self, feature_key: str) -> dict[str, list[str]]:
        """§27 "link ke commit": accepted head + merge commit of the feature's changes (observations)."""
        changes = self.changes_for(feature_key)
        return {k: v for k, v in {
            "accepted_head": sorted({c.accepted_head_sha for c in changes if c.accepted_head_sha}),
            "merge_commit": sorted({c.merge_commit_sha for c in changes if c.merge_commit_sha}),
            "pull_request": sorted({c.pr_url for c in changes if c.pr_url}),
        }.items() if v}

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
        # TaskSpec.feature_key is the plan's raw key ("F1"); the document key is its slug ("f1").
        return [t for t in self.tasks if feature_slug(t.feature_key) == feature_key]

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
            # A review is a human record of one exact content hash in AEOS; writing it into the body would
            # change the hash it refers to (and a stale "belum direview" would be false after a review).
            ("Last reviewed", "dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen"),
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
    check = f"Verifikasi diagram: **{v.verification_status}**" + (
        f" — {'; '.join(v.verification_notes)}" if v.verification_notes else "")
    if v.verification_source:
        check += f" (rencana: periksa terhadap {v.verification_source})"
    if v.status == STATUS_RENDERED:
        img = f"![{v.title}](visual:{v.key})"
    else:
        img = f"> **Visual pending** — {v.reason or 'belum dirender'}. Source Mermaid disimpan agar dapat di-diff."
    return _join(head, check, img, f"```mermaid\n{v.source.strip()}\n```")


def _visual_slice(ctx: _Ctx) -> list[Any]:
    return [(x.key, x.kind, x.source_hash, x.status, x.verification_status, list(x.verification_notes),
             x.verification_source) for x in ctx.visuals]


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
    home = v.home
    features = [(f.key, f.title, f.priority, f.milestone) for f in v.features]
    releases = [(as_text(r.get("version")), as_text(r.get("title"))) for r in v.release_plans]
    risk_keys = [as_text(r.get("key") or r.get("id")) for r in v.risks if isinstance(r, dict)]
    screenshots = [e for e in ctx.signals.evidence_of("SCREENSHOT") if e.commit]  # §29: real app at a commit
    slice_ = {"project": pr.slice(), "product": v.product, "summary": v.summary, "home_summary": v.get("home_summary"),
              "team": v.team, "stack": v.stack, "criticality": as_text(v.get("criticality")), "features": features,
              "milestones": [as_text(m.get("title")) for m in v.milestones], "risks": len(v.risks),
              "risk_keys": risk_keys, "releases": releases, "home": home,
              "screenshots": [e.slice() for e in screenshots],
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
        ]), as_text(v.get("home_summary")) or as_text(v.summary) or pr.description))
        team_rows = [[role, as_text(v.team.get(role.lower().replace(" ", "_"))) or "belum ditetapkan"]
                     for role in ("Product Owner", "Engineering Lead", "Tech Lead")]
        b.put("ownership", _join(_table(["Peran", "Nama"], team_rows),
                                 f"Pemilik proyek di AEOS: {pr.owner_name}" if pr.owner_name else ""))
        repo = ctx.code_repo()
        services = [item_text(c) for c in as_list(v.architecture.get("services"))]
        rows = [
            ["Repository", f"{repo.full_name} ({repo.provider})" if repo else "belum terhubung"],
            ["Issue tracker", ctx.tracker()],
            ["Dokumentasi resmi", DOCS_BY_DOMAIN.get(pr.domain, "—")],
            ["Service", ", ".join(services) if services else "belum didefinisikan di rencana"],
        ]
        if home.get("repository"):
            rows.append(["Repository (referensi di rencana)", as_text(home["repository"])])
        if home.get("issue_tracker"):
            rows.append(["Issue tracker (referensi di rencana)", as_text(home["issue_tracker"])])
        b.put("repository", _table(["Jenis", "Referensi"], rows))
        crit = as_text(v.get("criticality"))
        cls_rows = [["Criticality", crit or "UNKNOWN (belum ditetapkan di rencana)"],
                    ["Data classification (record proyek, floor)", pr.data_classification]]
        if home.get("data_classification"):
            cls_rows.append(["Data classification (usulan rencana)", as_text(home["data_classification"]) +
                             " — record proyek tetap berlaku sebagai batas bawah"])
        b.put("classification", _table(["Atribut", "Nilai"], cls_rows))
        b.put("stack", _bullets(v.stack))
        links = [f"- {n.title}: `{n.key}`" for n, _p in p1.iter_nodes() if n.kind == p1.NodeKind.DOCUMENT]
        if repo and repo.url:
            links.append(f"- Repository: {repo.url}")
        plan_links = as_list(home.get("quick_links"))
        shots = [f"`{e.id}` — {e.claim or 'screenshot'}" + (f" (commit `{e.commit[:12]}`)" if e.commit else "")
                 for e in screenshots]
        b.live("quick_links", _join("\n".join(links),
                                    ("**Tautan dari rencana**\n\n" + _bullets(plan_links)) if plan_links else "",
                                    ("**Screenshot aplikasi (EvidenceArtifact tepercaya, commit tercatat)**\n\n" +
                                     _bullets(shots)) if shots else "",
                                    "Tautan eksternal (Google Docs/Confluence/issue) ditambahkan oleh publisher sebagai "
                                    "ExternalReference setelah publikasi."))
        note = as_text(home.get("current_development"))
        b.live("current_development", _join(
            f"Catatan rencana saat disetujui (bukan status live): {note}" if note else "",
            _live(ctx.tracker(), "Pekerjaan berjalan (issue/work item) diproyeksikan dari tracker; status Done di "
                                 "tracker tidak membuka dependency runtime tanpa bukti penerimaan.")))
        active = set(as_list(home.get("active_features")))
        feat_rows = [[f"`{k}`", t, p or "—", m or "—"] + (["ya" if k in active else "—"] if active else [])
                     for k, t, p, m in features]
        headers = ["Feature", "Judul", "Prioritas", "Milestone"] + (["Aktif (rencana)"] if active else [])
        rel_rows = [[ver or "—", title or "—", f"`07-release-deployment/release-plan-{k}`"]
                    for (ver, title), (k, _r) in zip(releases, _release_index(v), strict=False)]
        b.live("active_work", _join(_table(headers, feat_rows) if feat_rows else
                                    "Belum ada fitur di rencana yang disetujui.",
                                    _table(["Release", "Judul", "Release Plan"], rel_rows) if rel_rows else "",
                                    _live(ctx.tracker(), "Status epic/fitur/release dibaca dari tracker dan "
                                                         "Release Plans.")))
        open_keys = [k for k in as_list(home.get("open_risks")) if k]
        risk_line = (f"Rencana yang disetujui mencatat {len(v.risks)} risiko awal" +
                     (f" ({', '.join(f'`{k}`' for k in risk_keys if k)})" if any(risk_keys) else "") +
                     " — lihat Initial Risk Assessment.") if v.risks else "Rencana tidak mencatat risiko awal."
        b.live("open_risks", _join(risk_line,
                                   _refs("Risiko terbuka menurut rencana", open_keys),
                                   _live("AEOS verifier/security review dan tracker",
                                         "Temuan security terbuka hanya dari review/evidence nyata.")))
        sdlc_note = as_text(home.get("sdlc_lifecycle"))
        b.put("sdlc_lifecycle", _join(
            f"Lifecycle proyek: **{pr.lifecycle}**.",
            f"Catatan rencana: {sdlc_note}" if sdlc_note else "",
            _table(["Tahap SDLC (§28)", "Dokumen"], [[r["timing"], r["documents"]] for r in p1.TIMING_TABLE]),
        ))
        gov = as_list(home.get("governance_links"))
        b.human("governance_links", _join(
            "Referensi: AEOS Control Center → Keputusan (Decision registry) untuk approval rencana, publikasi, merge "
            "dan risk acceptance.",
            ("Tautan governance yang diusulkan rencana (DATA, bukan approval):\n\n" + _bullets(gov)) if gov else ""))
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
              "data": v.data, "team_ownership": v.get("team_ownership"), "roadmap": as_list(v.get("roadmap")),
              "inventory": as_list(v.get("system_inventory")), "data_overview": v.get("data_overview"),
              "assumptions": as_list(v.get("assumptions")), "unknowns": as_list(v.get("unknowns"))}

    def statements(items: list[Any], extra: str) -> list[str]:
        out = []
        for x in items:
            if isinstance(x, dict):
                text, more = as_text(x.get("statement")), as_text(x.get(extra))
                if text:
                    out.append(f"{text} ({extra}: {more})" if more else text)
            elif as_text(x):
                out.append(as_text(x))
        return out

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", v.summary)
        b.put("vision_objectives", _join(f"**Vision:** {v.vision}" if v.vision else "",
                                         _bullets(v.objectives) if v.objectives else ""))
        assumptions = statements(as_list(v.get("assumptions")), "source")
        unknowns = statements(as_list(v.get("unknowns")), "impact")
        b.put("scope", _join("**In scope**\n\n" + _bullets(v.scope_in) if v.scope_in else "",
                             "**Out of scope / non-goals**\n\n" + _bullets(v.scope_out) if v.scope_out else "",
                             "**Asumsi rencana**\n\n" + _bullets(assumptions) if assumptions else "",
                             "**Unknowns rencana (belum diputuskan)**\n\n" + _bullets(unknowns) if unknowns else ""))
        team_rows = [[k.replace("_", " ").title(), as_text(x)] for k, x in sorted(v.team.items()) if as_text(x)]
        b.put("team_ownership", _join(as_text(v.get("team_ownership")),
                                      _table(["Peran", "Nama"], team_rows) if team_rows else
                                      (f"Pemilik proyek: {ctx.project.owner_name}" if ctx.project.owner_name else "")))
        b.put("stakeholders", _bullets(as_list(v.get("stakeholders"))))
        roadmap_rows = []
        for m in v.milestones:
            mkey = as_text(m.get("milestone_key") or m.get("key"))
            feats = [f.title for f in v.features if f.milestone and f.milestone == mkey]
            roadmap_rows.append([as_text(m.get("title")) or mkey, ", ".join(feats) or "—",
                                 as_text(m.get("exit_evidence")) or "—"])
        b.put("roadmap", _join(_bullets(as_list(v.get("roadmap"))),
                               _table(["Milestone", "Fitur", "Exit evidence"], roadmap_rows) if roadmap_rows else ""))
        inventory = as_list(v.get("system_inventory")) + as_list(v.architecture.get("services"))
        if not inventory:
            inventory = as_list(v.architecture.get("components"))
        b.put("system_inventory", _bullets(inventory))
        items = [d for d in as_list(v.data.get("items")) if isinstance(d, dict)]
        types = [f"{as_text(d.get('data_type'))} ({as_text(d.get('classification')) or 'klasifikasi belum ada'})"
                 for d in items if as_text(d.get("data_type"))]
        b.put("data_overview", _join(as_text(v.data.get("overview") or v.data.get("summary")),
                                     _bullets(as_list(v.data.get("types")) or types)))
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
    test_plan_ids = ctx.view.test_plan_ids_for(f.key)
    commits = ctx.commit_links(f.key)
    slice_ = {**_feature_slice(f, tasks), "rfc": rfc_ids, "tdd": tdd_ids, "test_plans": test_plan_ids,
              "commits": commits}

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
        req = as_text(f.get("requirement"))
        req_line = f"**Requirement (rencana):** {req}" if req and req != f.summary and req not in map(as_text, fr) else ""
        b.put("functional_requirements", _join(req_line, _numbered(fr)) if fr else req)
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
        deps += [as_text(d) for d in as_list(f.get("prd_dependencies")) if as_text(d)]
        for t in tasks:
            deps += [f"Task `{t.task_key}` bergantung pada `{d}`" for d in t.depends_on]
        b.put("dependencies", _bullets(deps))
        b.put("assumptions", _bullets(as_list(f.get("assumptions"))))
        b.put("risks", _bullets(as_list(f.get("risks"))))
        b.put("success_metrics", _bullets(as_list(f.get("success_metrics", "metrics"))))
        b.put("rollout_considerations", as_text(f.get("rollout", "rollout_considerations")))
        rel = [f"- TaskSpec `{t.task_key}` — {t.title}" for t in tasks]
        rel += [f"- RFC `{x}`" for x in rfc_ids] + [f"- TDD `{x}`" for x in tdd_ids]
        rel += [f"- Test Plan `{x}`" for x in test_plan_ids]
        rel += [f"- Rencana: {as_text(x)}" for x in as_list(f.get("related_work")) if as_text(x)]
        rel += [f"- Change record `10-changes/change-{feature_slug(c.task_key)}`" for c in ctx.changes_for(f.key)]
        b.live("related_work", _join("\n".join(rel), _live(ctx.tracker(), "Issue/work item per TaskSpec dibuat "
                                                                          "publisher dan ditautkan sebagai "
                                                                          "ExternalReference.")))
        prd_title = as_text(f.get("prd_title"))
        intro = _join(f"Judul PRD di rencana: **{prd_title}**" if prd_title and prd_title != f.title else "",
                      "_PRD tanpa entri fitur di rencana — tetap didokumentasikan, tidak dibuang._"
                      if f.get("prd_without_feature") else "")
        _compose(doc, b, header_title=f"PRD — {f.title}", metadata=ctx.metadata(doc), intro=intro)

    ctx.add(logical_id=logical_id, node="02", title=f"PRD — {f.title}", kind="DOC", doc_type="PRD", parent=parent,
            timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Product Owner") if not f.owner else f"{f.owner} (Feature Owner)", slice_=slice_,
            template_key="prd",
            links={"requirement": [f.key], "tasks": [t.task_key for t in tasks], "task_ids": [t.id for t in tasks],
                   "commit": sorted({t.base_commit for t in tasks if t.base_commit}), **commits,
                   "related": rfc_ids + tdd_ids + test_plan_ids},
            build=build)


def _architecture_overview(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    a = ctx.view.architecture
    spec = p1.DOC_TYPES["ARCHITECTURE_OVERVIEW"]
    slice_ = {"architecture": {k: x for k, x in a.items() if k != "diagrams"}, "visuals": _visual_slice(ctx),
              "features": [f.key for f in ctx.view.features]}

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("context", _join(as_text(a.get("context")), _visuals_text(ctx, "context")))
        other = [x for x in ctx.visuals if x.kind not in
                 ("context", "component", "erd", "data_flow", "trust_boundary", "sequence", "cicd", "deployment")]
        # Only text: the schema's ``architecture.overview`` object is mapped field by field, never dumped here.
        high = pick(a, "high_level", "summary")
        b.put("high_level_architecture", _join(as_text(high) if not isinstance(high, dict) else "",
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
            links={"visuals": [x.key for x in ctx.visuals],
                   "requirement": [f"02-requirements/prd-{f.key}" for f in ctx.view.features]}, build=build)


def _rfc_or_tdd(ctx: _Ctx, kind: str, key: str, title: str, data: dict[str, Any], parent: str,
                feature: FeatureView | None) -> None:
    spec = p1.DOC_TYPES[kind]
    prefix = "rfc" if kind == "RFC" else "tdd"
    logical_id = f"{parent}/{prefix}-{key}"
    slice_ = {"data": data, "feature": feature.raw if feature else None}
    refs = [as_text(x) for x in as_list(feature.get(f"{prefix}_refs"))] if feature else []

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
        intro = _join(f"{spec.title} ini diperlukan karena dirujuk oleh PRD `02-requirements/prd-{feature.key}` "
                      "(rencana yang disetujui):" if refs and feature else "", _bullets(refs))
        _compose(doc, b, header_title=f"{spec.title} — {title}", metadata=ctx.metadata(doc), intro=intro)

    ctx.add(logical_id=logical_id, node="03", title=f"{spec.title} — {title}", kind="DOC", doc_type=kind,
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Tech Lead"), slice_=slice_, template_key=prefix,
            links={"requirement": [feature.key] if feature else []}, build=build)


def _security_overview(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    sec = ctx.view.security
    spec = p1.DOC_TYPES["SECURITY_OVERVIEW"]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        owner = known(sec.get("owner"))
        b.put("ownership", f"{owner} (Security Lead / ownership menurut rencana)" if owner else ctx.owner("Security Lead"))
        authn, authz = as_text(pick(sec, "authn", "authentication")), as_text(pick(sec, "authz", "authorization"))
        both = as_text(sec.get("authn_authz"))
        b.put("authn_authz", _join(both, f"**Authn:** {authn}" if authn else "", f"**Authz:** {authz}" if authz else ""))
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

    questions = [as_text(q) for q in as_list(ctx.view.security.get("open_questions")) if as_text(q)]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("context_scope", _join("Initial risk/security assessment saat project creation (§28), dari rencana yang "
                                     "disetujui. Ini identifikasi awal, bukan hasil pengujian security.",
                                     as_text(ctx.view.summary),
                                     ("**Pertanyaan terbuka security/privacy (rencana)**\n\n" + _bullets(questions))
                                     if questions else ""))
        rows = []
        for i, r in enumerate(risks, start=1):
            if isinstance(r, dict):
                title = as_text(r.get("title") or r.get("risk"))
                desc = as_text(r.get("description"))
                rows.append([as_text(r.get("id") or r.get("key")) or f"R{i}",
                             f"{title} — {desc}" if title and desc and desc != title else (title or desc),
                             as_text(r.get("likelihood")) or "—", as_text(r.get("impact") or r.get("severity")) or "—",
                             known(r.get("owner")) or "—"])
            else:
                rows.append([f"R{i}", as_text(r), "—", "—", "—"])
        b.put("risk_register", _table(["ID", "Risiko", "Likelihood", "Impact", "Owner"], rows) if rows else "")
        b.put("likelihood_impact", "Rating mengikuti nilai di register; ambang risiko kanonik (MCI) UNVERIFIED — tidak "
                                   "dikarang." if rows else "")
        mitig = [f"{as_text(r.get('key') or r.get('id') or r.get('title') or r.get('risk'))}: "
                 f"{as_text(r.get('mitigation'))}" for r in risks if isinstance(r, dict) and as_text(r.get("mitigation"))]
        b.put("mitigations", _bullets(mitig))
        residual = [f"{as_text(r.get('key') or r.get('id') or r.get('title'))}: {as_text(r.get('residual_risk'))}"
                    for r in risks if isinstance(r, dict) and as_text(r.get("residual_risk"))]
        b.human("residual_risk", ("Usulan residual risk dari rencana (DATA — bukan risk acceptance):\n\n" +
                                  _bullets(residual)) if residual else "")
        owners = sorted({known(r.get("owner")) for r in risks if isinstance(r, dict) and known(r.get("owner"))})
        b.put("risk_owner", _join(ctx.owner("Product Owner"),
                                  ("Owner per risiko menurut rencana: " + ", ".join(owners)) if owners else ""))
        b.human("acceptance", "Risk acceptance hanya melalui Decision manusia (kind RISK_ACCEPTANCE).")
        _compose(doc, b, header_title="Initial Risk Assessment", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title="Initial Risk Assessment", kind="DOC", doc_type="RISK_ASSESSMENT",
            parent=parent, timing=p1.Timing.PROJECT_CREATION, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Product Owner"), slice_={"risks": risks, "summary": ctx.view.summary,
                                                     "team": ctx.view.team, "owner": ctx.project.owner_name,
                                                     "questions": questions},
            template_key="risk-assessment", build=build)


def _threat_model_input(ctx: _Ctx) -> tuple[dict[str, Any], list[Any]] | None:
    sec = ctx.view.security
    tm = sec.get("threat_model") if isinstance(sec.get("threat_model"), dict) else {}
    threats = as_list(sec.get("threats")) or as_list(tm.get("threats"))
    if not threats and not tm:
        return None
    return tm, threats


def _feature_threat_model_input(f: FeatureView) -> tuple[dict[str, Any], list[Any]] | None:
    tm = f.raw.get("threat_model") if isinstance(f.raw.get("threat_model"), dict) else {}
    threats = as_list(f.raw.get("threats")) or as_list(tm.get("threats"))
    if not threats and not tm:
        return None
    return tm, threats


def _threat_model_doc(ctx: _Ctx, *, logical_id: str, title: str, parent: str, tm: dict[str, Any],
                      threats: list[Any], with_visuals: bool, links: dict[str, Any] | None = None) -> None:
    spec = p1.DOC_TYPES["THREAT_MODEL"]
    tb = _visuals_text(ctx, "trust_boundary", "data_flow") if with_visuals else ""

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        for key in ("assets", "actors", "trust_boundaries", "entry_points", "attack_scenarios", "mitigations"):
            b.put(key, _bullets(as_list(tm.get(key))))
        b.put("threats", _bullets(threats))
        if tb:
            b.put("trust_boundaries", _join(_bullets(as_list(tm.get("trust_boundaries"))), tb))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title=title, kind="DOC", doc_type="THREAT_MODEL",
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Security Lead"),
            slice_={"tm": tm, "threats": threats, "visuals": _visual_slice(ctx) if with_visuals else []},
            template_key="threat-model", links=links, build=build)


def _threat_model(ctx: _Ctx, parent: str) -> None:
    data = _threat_model_input(ctx)
    if data is None:
        return
    tm, threats = data
    _threat_model_doc(ctx, logical_id=f"{parent}/threat-model-initial", title="Threat Model — Initial",
                      parent=parent, tm=tm, threats=threats, with_visuals=True)


def _feature_threat_model(ctx: _Ctx, f: FeatureView, parent: str) -> None:
    """§28 feature approval "threat/assessment": a threat model per feature when the approved plan gives one."""
    data = _feature_threat_model_input(f)
    if data is None:
        return
    tm, threats = data
    _threat_model_doc(ctx, logical_id=f"{parent}/threat-model-{f.key}", title=f"Threat Model — {f.title}",
                      parent=parent, tm=tm, threats=threats, with_visuals=False, links={"requirement": [f.key]})


def _security_assessment_input(f: FeatureView) -> dict[str, Any] | None:
    raw = f.raw.get("security_assessment")
    if isinstance(raw, dict):
        return raw
    return {} if f.get("needs_security_assessment") else None


def _security_assessment(ctx: _Ctx, f: FeatureView, parent: str) -> None:
    """§27 "Assessments per change" at feature approval (§28). Findings come only from real reviews/scans
    (DYNAMIC → evidence); residual risk and approval are HUMAN_GOVERNED — never filled by AEOS."""
    data = _security_assessment_input(f)
    if data is None:
        return
    spec = p1.DOC_TYPES["SECURITY_ASSESSMENT"]
    logical_id = f"{parent}/security-assessment-{f.key}"

    def text(key: str) -> str:
        val = data.get(key)
        return _bullets(val) if isinstance(val, list) else as_text(val)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("scope", _join(f"Perubahan: fitur **{f.title}** (`{f.key}`).", text("scope")))
        b.put("requirements", text("requirements"))
        b.put("risks", text("risks"))
        task_keys = [t.task_key for t in ctx.tasks_for(f.key)]
        b.live("findings", _live("AEOS security review / scanner (EvidenceArtifact tepercaya)",
                                 _join("Temuan hanya dari review/scan nyata untuk perubahan ini — tidak dikarang oleh "
                                       "dokumen atau model.", _refs("Cari temuan untuk TaskSpec", task_keys))))
        b.put("mitigations", text("mitigations"))
        b.human("residual_risk")
        b.human("approval", "Approval security hanya melalui Decision manusia.")
        _compose(doc, b, header_title=f"Security Assessment — {f.title}", metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title=f"Security Assessment — {f.title}", kind="DOC",
            doc_type="SECURITY_ASSESSMENT", parent=parent, timing=p1.Timing.FEATURE_APPROVAL,
            status=_ok_status(ctx), reason="", owner=ctx.owner("Security Lead"),
            slice_={"assessment": data, "feature": f.key, "title": f.title, "team": ctx.view.team,
                    "owner": ctx.project.owner_name, "tasks": [t.task_key for t in ctx.tasks_for(f.key)]},
            template_key="security-assessment", links={"requirement": [f.key]}, build=build)


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

    items = [x for x in as_list(d.get("items")) if isinstance(x, dict) and as_text(x.get("data_type"))]
    floor = ctx.project.data_classification

    def per_item(field_: str) -> str:
        return _bullets([f"**{as_text(x.get('data_type'))}** — {as_text(x.get(field_))}" for x in items
                         if as_text(x.get(field_))])

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        if items:
            rows = [[as_text(x.get(k)) or "—" for k in ("data_type", "classification", "owner", "storage", "access",
                                                         "retention", "disposal")] for x in items]
            b.put("data_types", _table(["Data type", "Classification", "Owner", "Storage", "Access", "Retention",
                                        "Disposal"], rows))
        else:
            b.put("data_types", _bullets(as_list(pick(d, "types", "data_types"))))
        cls_lines = [f"Klasifikasi proyek (floor, record AEOS): **{floor}**"]
        for x in items:
            c = as_text(x.get("classification"))
            if not c:
                continue
            below = _CLASS_ORDER.get(c.upper(), 99) < _CLASS_ORDER.get(floor.upper(), 0)
            cls_lines.append(f"- {as_text(x.get('data_type'))}: {c}" +
                             (f" — di bawah floor proyek; {floor} yang berlaku" if below else ""))
        b.put("classification", "\n".join(cls_lines))
        owners = [f"- {as_text(x.get('data_type'))}: {known(x.get('owner'))}" for x in items if known(x.get("owner"))]
        b.put("owner", _join(as_text(d.get("owner")) or ctx.owner("Privacy Officer"), "\n".join(owners)))
        for key in ("storage", "access", "retention", "disposal"):
            if items:
                b.put(key, per_item(key))
                continue
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
               "dev_environment": ("dev_environment", "environment"), "secrets": ("secrets", "secrets_handling"),
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


_SCENARIO_GROUPS = {
    "functional_testing": ("functional", "integration", "negative", "edge", "accessibility"),
    "regression_testing": ("regression",),
    "performance_testing": ("performance",),
    "security_testing": ("security",),
}


def _scenario_line(s: dict[str, Any]) -> str:
    head = " — ".join(x for x in (f"**{as_text(s.get('key'))}**" if as_text(s.get("key")) else "",
                                   as_text(s.get("title"))) if x)
    kind = as_text(s.get("type"))
    parts = [f"{head} ({kind})" if kind else head]
    steps = [as_text(x) for x in as_list(s.get("steps")) if as_text(x)]
    if steps:
        parts.append("langkah: " + "; ".join(steps))
    if as_text(s.get("expected_result")):
        parts.append("expected: " + as_text(s.get("expected_result")))
    refs = [as_text(x) for x in as_list(s.get("acceptance_criteria_refs")) if as_text(x)]
    if refs:
        parts.append("AC: " + ", ".join(refs))
    return "; ".join(p for p in parts if p)


def _test_plan(ctx: _Ctx, key: str, tp: dict[str, Any], feature_keys: list[str], parent: str) -> None:
    """One Test Plan: a schema-v1 ``testing.test_plans[]`` entry, or (``tp`` empty) the per-feature plan derived
    from the approved acceptance criteria. Results, UAT and sign-off are never written here (§27, §28)."""
    spec = p1.DOC_TYPES["TEST_PLAN"]
    feats = [f for f in ctx.view.features if f.key in feature_keys]
    f = feats[0] if feats and not tp else None
    legacy = ctx.view.testing
    ft = f.raw.get("testing") if f is not None and isinstance(f.raw.get("testing"), dict) else {}
    ts = tp.get("test_strategy") if isinstance(tp.get("test_strategy"), dict) else {}
    tasks = [t for fk in feature_keys for t in ctx.tasks_for(fk)]
    logical_id = f"{parent}/test-plan-{key}"
    scenarios = [s for s in as_list(tp.get("scenarios")) if isinstance(s, dict)]
    title = (f"Test Plan — {as_text(tp.get('title'))}" if as_text(tp.get("title")) else
             f"Test Plan — {', '.join(x.title for x in feats) or key}")

    def lookup(*keys: str) -> Any:
        for src in (ft, legacy):
            for k in keys:
                if src.get(k):
                    return src[k]
        return None

    def txt(*keys: str) -> str:
        val = lookup(*keys)
        return _bullets(val) if isinstance(val, list) else as_text(val)

    def lst(val: Any) -> str:
        return _bullets(val) if isinstance(val, list) else as_text(val)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        feat_line = ", ".join(f"**{x.title}** (`{x.key}`)" for x in feats)
        acs = [ac for x in feats for ac in x.acceptance] + [ac for x in tasks for ac in x.acceptance_criteria]
        scope_items = [f"TaskSpec `{x.task_key}` — {x.title}" for x in tasks]
        if not tp:
            b.put("overview", _join(f"Test Plan untuk fitur {feat_line}." if feat_line else "",
                                    f.summary if f is not None else ""))
            b.put("objectives", txt("objectives"))
            b.put("scope", _join(_bullets(scope_items) if scope_items else "", txt("scope")))
            b.put("test_strategy", txt("strategy", "test_strategy"))
            b.put("approach", txt("approach"))
            b.put("test_levels", txt("levels", "test_levels"))
            b.put("test_types", txt("types", "test_types"))
            b.put("environment", txt("environment"))
            b.put("test_data", txt("data", "test_data"))
            b.put("tools", txt("tools"))
            planned = lookup("scenarios")
            b.put("scenarios", _bullets(planned) if isinstance(planned, list) else
                  (_join("Skenario minimum diturunkan dari acceptance criteria yang disetujui (bukan hasil uji):",
                         _numbered(acs)) if acs else ""))
            case_rows = [[f"TC-{i}", item_text(ac)] for i, ac in enumerate(acs, start=1)]
            b.put("test_cases", _table(["Case", "Acceptance criterion"], case_rows) if case_rows else "")
            for sec, legacy_key in (("functional_testing", "functional"), ("regression_testing", "regression"),
                                    ("performance_testing", "performance"), ("security_testing", "security")):
                b.put(sec, txt(legacy_key))
            b.put("exit_criteria", txt("exit_criteria"))
            uat_lines: list[str] = []
            defect_note = ""
            evidence_req: list[str] = []
        else:
            b.put("overview", _join(f"Test Plan untuk fitur {feat_line}." if feat_line else "",
                                    as_text(tp.get("overview"))))
            b.put("objectives", lst(as_list(tp.get("objectives"))))
            b.put("scope", _join(_bullets(scope_items) if scope_items else "", as_text(tp.get("scope"))))
            summary = [f"{label}: {', '.join(map(as_text, as_list(ts.get(k))))}" for label, k in
                       (("Approach", "approach"), ("Levels", "levels"), ("Types", "types"),
                        ("Environments", "environments"), ("Data", "data"), ("Tools", "tools"))
                       if as_list(ts.get(k))]
            b.put("test_strategy", _bullets(summary))
            b.put("approach", as_text(ts.get("approach")))
            b.put("test_levels", lst(as_list(ts.get("levels"))))
            b.put("test_types", lst(as_list(ts.get("types"))))
            b.put("environment", lst(as_list(ts.get("environments"))))
            b.put("test_data", as_text(ts.get("data")))
            b.put("tools", lst(as_list(ts.get("tools"))))
            b.put("scenarios", _bullets([_scenario_line(s) for s in scenarios]))
            case_rows = [[as_text(s.get("key")) or f"TC-{i}", as_text(s.get("title")) or "—",
                          as_text(s.get("expected_result")) or "—",
                          ", ".join(map(as_text, as_list(s.get("acceptance_criteria_refs")))) or "—"]
                         for i, s in enumerate(scenarios, start=1)]
            if not case_rows:
                case_rows = [[f"TC-{i}", item_text(ac), "—", "—"] for i, ac in enumerate(acs, start=1)]
            b.put("test_cases", _table(["Case", "Skenario / criterion", "Expected result", "AC refs"], case_rows)
                  if case_rows else "")
            for sec, kinds in _SCENARIO_GROUPS.items():
                b.put(sec, _bullets([_scenario_line(s) for s in scenarios if as_text(s.get("type")) in kinds]))
            exit_c, plan_acs = as_list(tp.get("exit_criteria")), as_list(tp.get("acceptance_criteria"))
            b.put("exit_criteria", _join(_bullets(exit_c),
                                         ("**Acceptance criteria yang harus terbukti**\n\n" + _bullets(plan_acs))
                                         if plan_acs else ""))
            uat_lines = [_scenario_line(s) for s in scenarios if as_text(s.get("type")) == "uat"]
            defect_note = as_text(tp.get("defect_management"))
            evidence_req = [as_text(x) for x in as_list(tp.get("evidence_requirements")) if as_text(x)]
        b.live("uat", _live("Sign-off manusia + EvidenceArtifact", _join(
            ("Skenario UAT yang direncanakan:\n\n" + _bullets(uat_lines)) if uat_lines else "",
            "Pelaksanaan dan hasil UAT dicatat oleh manusia yang berwenang dengan evidence; dokumen tidak menyatakan "
            "hasil UAT.")))
        b.live("execution_results", _live("AEOS independent verifier (EvidenceArtifact tepercaya)", _join(
            "Hasil hanya dari verifier independen; dokumen ini dan model tidak menyatakan hasil uji.",
            _refs("Cari hasil untuk TaskSpec", [x.task_key for x in tasks]))))
        b.live("defect_management", _live(ctx.tracker(), _join(f"Proses (rencana): {defect_note}" if defect_note else "",
                                                               "Defect dicatat sebagai issue/work item.")))
        b.human("final_assessment")
        b.human("sign_off")
        b.live("evidence_references", _live("AEOS Evidence & Decisions", _join(
            ("Evidence yang disyaratkan rencana:\n\n" + _bullets(evidence_req)) if evidence_req else "",
            "Referensi evidence ditautkan setelah verifier menjalankan suite.")))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    commits: dict[str, list[str]] = {}
    for fk in feature_keys:
        for k, vals in ctx.commit_links(fk).items():
            commits[k] = sorted(set(commits.get(k, [])) | set(vals))
    ctx.add(logical_id=logical_id, node="06", title=title, kind="DOC", doc_type="TEST_PLAN",
            parent=parent, timing=p1.Timing.FEATURE_APPROVAL, status=_ok_status(ctx), reason="",
            owner=ctx.owner("QA Lead"),
            slice_={"plan": tp, "features": [_feature_slice(x, ctx.tasks_for(x.key)) for x in feats],
                    "testing": legacy, "commits": commits},
            template_key="test-plan",
            links={"requirement": [*feature_keys, *[f"02-requirements/prd-{k}" for k in feature_keys]],
                   "tasks": [x.task_key for x in tasks], "task_ids": [x.id for x in tasks], **commits},
            build=build)


# --- §27 07. Release & Deployment -------------------------------------------------------------


def _release_index(view: PlanView) -> list[tuple[str, dict[str, Any]]]:
    used: set[str] = set()
    return [(_unique_key(as_text(r.get("version") or r.get("key")) or f"release-{i + 1}", used,
                         fallback=f"release-{i + 1}"), r) for i, r in enumerate(view.release_plans)]


def _release_plan(ctx: _Ctx, key: str, rp: dict[str, Any], parent: str) -> None:
    """Release Plan per version from ``release.release_plans[]`` (Release Notes is a section, U-08). Testing/security/
    UAT status, readiness and post-release validation are DYNAMIC references; sign-off is HUMAN (§27, §28)."""
    spec = p1.DOC_TYPES["RELEASE_PLAN"]
    logical_id = f"{parent}/release-plan-{key}"
    version = as_text(rp.get("version"))
    title = f"Release Plan — {version or key}" + (f" ({as_text(rp.get('title'))})" if as_text(rp.get("title")) else "")
    included = [ctx.view.feature(x) for x in as_list(rp.get("features"))]
    feats = [f for f in included if f is not None]
    tasks = [t for f in feats for t in ctx.tasks_for(f.key)]
    test_plans = sorted({tp for f in feats for tp in ctx.view.test_plan_ids_for(f.key)})

    def feature_label(x: Any) -> str:
        f = ctx.view.feature(x)
        return f"{f.title} (`{f.key}`)" if f is not None else as_text(x)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", as_text(rp.get("overview")))
        b.put("objectives", "")  # the schema has no release objectives: documentation pending, never invented
        b.put("scope", as_text(rp.get("scope")))
        groups = [("Features", [feature_label(x) for x in as_list(rp.get("features"))]),
                  ("Enhancements", as_list(rp.get("enhancements"))), ("Fixes", as_list(rp.get("fixes"))),
                  ("Breaking changes", as_list(rp.get("breaking_changes")))]
        b.put("changes", _join(*[f"**{label}**\n\n" + _bullets(items) for label, items in groups if items]))
        related = [as_text(x) for x in as_list(rp.get("related_requirements")) if as_text(x)]
        b.live("related_requirements_architecture", _live("AEOS DocumentRecord (Product 1)", _join(
            _bullets([f"PRD `02-requirements/prd-{f.key}`" for f in feats] + ["`03-architecture-overview`"]),
            ("Requirement terkait (rencana):\n\n" + _bullets(related)) if related else "")))
        b.live("testing_status", _live("AEOS independent verifier + CI", _join(
            _refs("Test Plan", test_plans), _refs("TaskSpec", [t.task_key for t in tasks]))))
        b.live("security_status", _live("AEOS security review / scanner (EvidenceArtifact tepercaya)",
                                        "Temuan dan status security per perubahan dibaca dari review/evidence nyata."))
        b.live("uat_status", _live("Sign-off manusia + EvidenceArtifact", "UAT hanya dari pelaksanaan nyata dengan "
                                                                          "evidence; dokumen tidak menyatakan hasil."))
        known_issues = [as_text(x) for x in as_list(rp.get("known_issues")) if as_text(x)]
        risks = [as_text(x) for x in as_list(rp.get("risks")) if as_text(x)]
        b.live("known_issues_risks", _live(ctx.tracker(), _join(
            ("Known issues tercatat di rencana:\n\n" + _bullets(known_issues)) if known_issues else "",
            ("Risiko tercatat di rencana:\n\n" + _bullets(risks)) if risks else "",
            "Issue terbuka dibaca live dari tracker.")))
        notes = rp.get("release_notes") if isinstance(rp.get("release_notes"), dict) else {}
        b.put("release_notes", _join(
            as_text(notes.get("summary")),
            ("**Highlights**\n\n" + _bullets(as_list(notes.get("highlights")))) if as_list(notes.get("highlights")) else "",
            f"**Upgrade notes:** {as_text(notes.get('upgrade_notes'))}" if as_text(notes.get("upgrade_notes")) else "",
            ("**Known limitations**\n\n" + _bullets(as_list(notes.get("known_limitations"))))
            if as_list(notes.get("known_limitations")) else ""))
        b.put("deployment", as_text(rp.get("deployment")))
        b.put("migration", as_text(rp.get("migration")))
        b.put("rollback", as_text(rp.get("rollback")))
        readiness = [as_text(x) for x in as_list(rp.get("production_readiness")) if as_text(x)]
        b.live("production_readiness", _live("AEOS gates / Operations", _join(
            ("Kriteria kesiapan (rencana; pemenuhannya dibaca live):\n\n" + _bullets(readiness)) if readiness else "",
            "Kesiapan produksi hanya dari evidence; dokumen tidak menyatakan siap/tidak siap.")))
        b.human("sign_off", "Sign-off rilis hanya oleh manusia yang berwenang (Decision).")
        checks = [as_text(x) for x in as_list(rp.get("post_release_validation")) if as_text(x)]
        b.live("post_release_validation", _live("AEOS Operations (production evidence)", _join(
            ("Pemeriksaan pasca-rilis yang direncanakan:\n\n" + _bullets(checks)) if checks else "",
            "Hasil validasi hanya dari evidence runtime/production.")))
        b.live("evidence", _live("AEOS Evidence & Decisions", "Snapshot release candidate (commit + content hash "
                                                              "dokumen) dicatat AEOS sebagai DocumentRevision "
                                                              "RELEASE_CANDIDATE; evidence verifier ditautkan di sana."))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="07", title=title, kind="DOC", doc_type="RELEASE_PLAN", parent=parent,
            timing=p1.Timing.RELEASE_CANDIDATE, status=_ok_status(ctx), reason="", owner=ctx.owner("Release Manager"),
            slice_={"release": rp, "features": [(f.key, f.title) for f in feats], "tasks": [t.task_key for t in tasks],
                    "test_plans": test_plans},
            template_key="release-plan",
            links={"requirement": [f.key for f in feats], "related": test_plans,
                   "release": [x for x in dict.fromkeys([version, as_text(rp.get("key"))]) if x] or [key]},
            build=build)


# --- §27 08. Operations -------------------------------------------------------------------------


def _ops_status(ctx: _Ctx) -> tuple[str, str, list[EvidenceRef]]:
    """Operations documents carry proposals from the plan; they become APPROVED only after trusted
    production-validation evidence exists (§28 "status hanya berdasarkan evidence")."""
    ev = ctx.signals.evidence_of("PRODUCTION_VALIDATION")
    if ev and ctx.version.approved:
        return ST_APPROVED, f"production validation evidence: {', '.join(e.id for e in ev[:5])}", ev
    return (ST_DRAFT, "Usulan operasional dari rencana yang disetujui; status APPROVED hanya setelah evidence "
                      "production validation (§28).", ev)


def _ops_doc(ctx: _Ctx, node: p1.Node, parent: str, fill: Callable[[_Doc], bool]) -> None:
    """Render an 08 document from ``operations`` proposals; NOT STARTED (with reason) when the plan has none."""
    ops = ctx.view.operations
    spec = p1.DOC_TYPES[node.doc_type or ""]
    probe = _Doc(spec, node.key)
    has = fill(probe)
    if not has:
        _future_doc(ctx, node, parent, applicable=True,
                    reason="Dibuat saat production validation (§28); status hanya berdasarkan evidence runtime. "
                           f"Owner: {ctx.owner(node.owner_role)}.")
        return
    status, reason, ev = _ops_status(ctx)

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        fill(b)
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc),
                 intro="_Usulan dari rencana (AI_MAINTAINED proposal); bukan bukti operasi berjalan._")

    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=status, reason=reason, owner=ctx.owner(node.owner_role),
            slice_={"ops": ops, "evidence": [e.slice() for e in ev], "kind": node.doc_type,
                    "restore": [e.slice() for e in ctx.signals.evidence_of("RESTORE_TEST")]},
            build=build)


def _ops_fillers(ctx: _Ctx) -> dict[str, Callable[[_Doc], bool]]:
    ops = ctx.view.operations
    restore = ctx.signals.evidence_of("RESTORE_TEST")

    def lst(key: str) -> list[str]:
        return [as_text(x) for x in as_list(ops.get(key)) if as_text(x)]

    slo = [x for x in as_list(ops.get("sli_slo_proposals")) if isinstance(x, dict) and as_text(x.get("indicator"))]

    def slo_line(x: dict[str, Any]) -> str:
        return (f"**{as_text(x.get('indicator'))}** — objective (usulan): {as_text(x.get('objective')) or '—'}" +
                (f"; alasan: {as_text(x.get('rationale'))}" if as_text(x.get("rationale")) else ""))

    def match(*words: str) -> list[dict[str, Any]]:
        return [x for x in slo if any(w in as_text(x.get("indicator")).lower() for w in words)]

    def overview(b: _Doc) -> bool:
        b.put("production_architecture", as_text(ops.get("overview")))
        b.put("ownership", known(ops.get("ownership")) or ctx.owner("Operations Owner"))
        b.put("dependencies", _bullets(lst("dependencies")))
        b.put("support", "")  # not in the plan schema: documentation pending
        crit = known(ops.get("criticality"))
        b.put("criticality", f"{crit} (usulan rencana)" if crit else "UNKNOWN (belum ditetapkan di rencana)")
        b.put("sli_slo_sla", _bullets([slo_line(x) for x in slo]))
        return bool(as_text(ops.get("overview")) or lst("dependencies") or slo or known(ops.get("ownership")))

    def monitoring(b: _Doc) -> bool:
        b.put("metrics", _bullets(lst("monitoring")))
        b.live("dashboards", _live("Sistem monitoring produksi", "Dashboard dirujuk setelah production validation."))
        b.live("coverage", _live("Sistem monitoring produksi", "Cakupan monitoring dibaca dari sistem monitoring."))
        b.put("thresholds", _bullets([slo_line(x) for x in slo]))
        return bool(lst("monitoring"))

    def logging(b: _Doc) -> bool:
        text = as_text(ops.get("logging"))
        b.put("application_logs", text)
        for k in ("audit_logs", "security_logs", "retention", "access"):
            b.put(k, "")  # the plan schema has one logging statement: the rest is documentation pending
        return bool(text)

    def alerting(b: _Doc) -> bool:
        b.put("alert_rules", _bullets(lst("alerting")))
        for k in ("severity", "notification", "escalation"):
            b.put(k, "")
        return bool(lst("alerting"))

    def slo_doc(b: _Doc) -> bool:
        avail = match("avail", "uptime")
        lat = match("latency", "response", "p95", "p99", "duration")
        err = match("error", "failure", "success")
        other = [x for x in slo if x not in avail + lat + err]
        b.put("availability", _join(_bullets([slo_line(x) for x in avail]),
                                    ("**Indikator lain yang diusulkan**\n\n" + _bullets([slo_line(x) for x in other]))
                                    if other else ""))
        b.put("latency", _bullets([slo_line(x) for x in lat]))
        b.put("error_rate", _bullets([slo_line(x) for x in err]))
        b.live("error_budget", _live("AEOS Operations (SLI dari evidence runtime)",
                                     "Error budget dan pengukuran hanya dari data SLI nyata."))
        return bool(slo)

    def backup(b: _Doc) -> bool:
        bk = ops.get("backup") if isinstance(ops.get("backup"), dict) else {}
        for k in ("scope", "frequency", "retention", "restore_procedure"):
            b.put(k, as_text(bk.get(k)))
        b.put("storage", "")
        b.live("restore_testing", _live("EvidenceArtifact RESTORE_TEST (tepercaya)", _join(
            _refs("Restore test tercatat", [e.id for e in restore]),
            "Tanpa evidence restore test = belum teruji; dokumen tidak menyatakan restore berhasil.")))
        return any(as_text(bk.get(k)) for k in ("scope", "frequency", "retention", "restore_procedure"))

    def maintenance(b: _Doc) -> bool:
        b.put("jobs", _bullets(lst("maintenance")))
        b.put("checks", "")
        b.put("routine_activities", "")
        return bool(lst("maintenance"))

    return {"OPERATIONAL_OVERVIEW": overview, "MONITORING": monitoring, "LOGGING": logging, "ALERTING": alerting,
            "SLO": slo_doc, "BACKUP_RESTORE": backup, "MAINTENANCE": maintenance}


def _runbook(ctx: _Ctx, key: str, rb: dict[str, Any], parent: str) -> None:
    spec = p1.DOC_TYPES["RUNBOOK"]
    logical_id = f"{parent}/runbook-{key}"
    title = f"Runbook — {as_text(rb.get('title')) or key}"

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("purpose_trigger", as_text(rb.get("purpose")))
        for k in ("prerequisites", "procedure", "verification", "rollback", "escalation"):
            b.put(k, "")  # written by the Operations Owner at release candidate; never invented
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="08", title=title, kind="DOC", doc_type="RUNBOOK", parent=parent,
            timing=p1.Timing.RELEASE_CANDIDATE, status=_ok_status(ctx), reason="",
            owner=ctx.owner("Operations Owner"), slice_={"runbook": rb}, template_key="runbook", build=build)


# --- §27 04. VA / PT from real results ------------------------------------------------------------


def _assessment_from_evidence(ctx: _Ctx, ev: EvidenceRef, parent: str, doc_type: str) -> None:
    """A VA/PT document exists only for a real, trusted result artefact. Findings/retest/evidence are DYNAMIC
    references to that artefact; rules of engagement stay HUMAN (§27 "hanya dari hasil nyata")."""
    spec = p1.DOC_TYPES[doc_type]
    prefix = "va" if doc_type == "VULNERABILITY_ASSESSMENT" else "pt"
    logical_id = f"{parent}/{prefix}-{feature_slug(ev.id)[:12] or 'x'}"
    label = "Vulnerability Assessment" if prefix == "va" else "Penetration Test"
    title = f"{label} — {ev.claim[:80] or ev.id}"

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("scope", ev.get("scope"))
        if prefix == "va":
            b.put("method_tools", ev.get("method") or ev.get("tools"))
        else:
            b.human("rules_of_engagement", "Rules of engagement dan otorisasi pengujian dicatat oleh manusia.")
            b.live("retest", _live(f"EvidenceArtifact `{ev.id}` dan retest berikutnya", "Status retest dibaca dari "
                                                                                       "evidence."))
        b.live("findings", _live(f"EvidenceArtifact `{ev.id}` ({ev.kind}, {ev.evidence_level or '—'}, origin "
                                 f"{ev.origin or '—'})", "Temuan dibaca dari artefak hasil nyata; tidak disalin atau "
                                                         "diringkas sebagai status di dokumen."))
        b.put("remediation", ev.get("remediation"))
        b.live("evidence", _live("AEOS Evidence", _join(_refs("EvidenceArtifact", [ev.id]),
                                                        f"Commit: `{ev.commit}`" if ev.commit else "",
                                                        f"Environment: {ev.environment}" if ev.environment else "")))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="04", title=title, kind="DOC", doc_type=doc_type, parent=parent,
            timing=p1.Timing.RELEASE_CANDIDATE, status=_ok_status(ctx), reason="", owner=ctx.owner("Security Lead"),
            slice_={"evidence": ev.slice()}, links={"evidence": [ev.id]}, build=build)


# --- §27 10. Changes / 11. Improvements ------------------------------------------------------------


def _change_record(ctx: _Ctx, c: ChangeInfo, parent: str) -> None:
    """Change record per implementation PR (§28 Implementation/PR): DRAFT until the acceptance is ACCEPTED,
    back to DRAFT when it goes STALE; implementation/testing/validation/evidence are DYNAMIC references and
    the approval (merge) is HUMAN — AEOS never merges (U-09)."""
    spec = p1.DOC_TYPES["CHANGE_RECORD"]
    key = feature_slug(c.task_key) or feature_slug(c.task_id)
    logical_id = f"{parent}/change-{key}"
    title = f"Change — {c.task_key}: {c.title}"[:200]
    f = ctx.view.feature(c.feature_key) if c.feature_key else None
    accepted = c.acceptance_status == "ACCEPTED"
    status = ST_APPROVED if (accepted and ctx.version.approved) else ST_DRAFT
    reason = "" if status == ST_APPROVED else (
        f"acceptance {c.acceptance_status or 'belum ada'} — difinalkan setelah implementasi diterima (ACCEPTED)")

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("description", _join(f"**{c.title}**", c.goal))
        b.put("reason", _join(f"Mengimplementasikan fitur **{f.title}** (`{f.key}`), PRD "
                              f"`02-requirements/prd-{f.key}`." if f is not None else "",
                              f.summary if f is not None else ""))
        b.put("scope", _join(("**Path yang diizinkan TaskSpec**\n\n" + _bullets([f"`{p}`" for p in c.allowed_paths]))
                             if c.allowed_paths else "",
                             ("**Acceptance criteria**\n\n" + _bullets(c.acceptance_criteria))
                             if c.acceptance_criteria else "",
                             ("**Delta kontrak (path kontrak yang berubah)**\n\n" +
                              _bullets([f"`{p}`" for p in c.contract_deltas])) if c.contract_deltas else ""))
        b.put("risk", f"Risk class TaskSpec: {c.risk_class}" if c.risk_class else "")
        b.put("impact", as_text(f.get("user_impact")) if f is not None else "")
        b.live("implementation", _live(f"{c.repo or 'repository'} pull request", _join(
            f"PR: {c.pr_url}" if c.pr_url else (f"PR #{c.pr_number}" if c.pr_number else ""),
            f"Branch `{c.head_ref}` → `{c.base_ref}`" if c.head_ref else "",
            f"Accepted head: `{c.accepted_head_sha}`" if c.accepted_head_sha else "")))
        b.live("testing", _live("AEOS independent verifier (EvidenceArtifact tepercaya)",
                                _refs("Evidence verifier", c.evidence_ids) or "Hasil uji dibaca dari verifier."))
        b.human("approval", _join("Merge ke branch utama adalah keputusan manusia (U-09); AEOS tidak melakukan merge.",
                                  f"Observasi provider: merge commit `{c.merge_commit_sha}` (observasi, bukan otoritas)."
                                  if c.merge_commit_sha else ""))
        b.put("rollback", _bullets(c.rollback_notes))
        b.live("validation", _live("AEOS acceptance record", _refs("AcceptanceRecord", [c.acceptance_id]) or
                                   "Validasi dibaca dari acceptance record."))
        b.live("evidence", _live("AEOS Evidence", _refs("EvidenceArtifact", [*([c.bundle_evidence_id]
                                                                              if c.bundle_evidence_id else []),
                                                                            *c.evidence_ids])
                                 or "Evidence ditautkan saat tersedia."))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    links = {"requirement": [f.key] if f is not None else [], "tasks": [c.task_key], "task_ids": [c.task_id],
             **{k: v for k, v in {"accepted_head": [c.accepted_head_sha] if c.accepted_head_sha else [],
                                  "merge_commit": [c.merge_commit_sha] if c.merge_commit_sha else [],
                                  "pull_request": [c.pr_url] if c.pr_url else []}.items() if v}}
    ctx.add(logical_id=logical_id, node="10", title=title, kind="DOC", doc_type="CHANGE_RECORD", parent=parent,
            timing=p1.Timing.IMPLEMENTATION_PR, status=status, reason=reason, owner=ctx.owner("Change Owner"),
            slice_={"change": c.slice(), "feature": f.raw if f is not None else None}, links=links, build=build)


def _improvement(ctx: _Ctx, imp: ImprovementInfo, parent: str) -> None:
    spec = p1.DOC_TYPES["IMPROVEMENT_RECORD"]
    logical_id = f"{parent}/improvement-{feature_slug(imp.id)[:12] or 'x'}"
    title = f"Improvement — {imp.title}"[:200]

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("opportunity", _join(f"**{imp.title}**" + (f" ({imp.kind})" if imp.kind else ""), imp.problem))
        b.put("current_state", imp.problem)
        b.put("proposal", _join(imp.title, ("Sumber yang diusulkan:\n\n" + _bullets(imp.source_refs))
                                if imp.source_refs else ""))
        b.put("expected_benefit", imp.expected_benefit)
        b.put("risk", imp.risk)
        b.put("owner", imp.owner or ctx.owner("Product Owner"))
        b.live("implementation", _live("AEOS Change Proposal / TaskSpec", _refs("TaskSpec", imp.task_ids) or
                                       f"Status implementasi dibaca dari ChangeProposal `{imp.id}`."))
        b.live("result", _live("AEOS Change Proposal / Decision", _refs("Decision", imp.decision_ids) or
                               f"Hasil dibaca dari ChangeProposal `{imp.id}`."))
        b.live("metrics", _live("AEOS Evidence", _refs("EvidenceArtifact", imp.evidence_ids) or
                                "Metrik hanya dari evidence tercatat."))
        _compose(doc, b, header_title=title, metadata=ctx.metadata(doc))

    ctx.add(logical_id=logical_id, node="11", title=title, kind="DOC", doc_type="IMPROVEMENT_RECORD", parent=parent,
            timing=p1.Timing.IMPLEMENTATION_PR, status=_ok_status(ctx), reason="", owner=ctx.owner("Product Owner"),
            slice_={"improvement": imp.slice()}, links={"proposal": [imp.id]}, build=build)


# --- §27 12. Retirement ---------------------------------------------------------------------------------


def _retirement(ctx: _Ctx, node: p1.Node, parent: str) -> None:
    """When the lifecycle truly retires, the plan is the read-only operations retirement checklist (what exists,
    which owner action remains). Execution/validation/evidence are DYNAMIC; final approval is HUMAN (§28, §33)."""
    checklist = ctx.signals.retirement or {}
    items = {i.get("key"): i for i in as_list(checklist.get("items")) if isinstance(i, dict)}
    spec = p1.DOC_TYPES["RETIREMENT_PLAN"]

    def item(key: str) -> str:
        i = items.get(key)
        if not i:
            return ""
        detail = i.get("detail") or {}
        names = [f"{k}: {', '.join(map(str, v))}" for k, v in detail.items() if isinstance(v, list) and v]
        return _join(f"**{as_text(i.get('title'))}** — tindakan pemilik: {as_text(i.get('owner_action'))}",
                     _bullets(names))

    def build(doc: RenderedDocument) -> None:
        b = _Doc(spec, doc.logical_doc_id)
        b.put("overview", f"Retirement & decommission proyek **{ctx.project.name}** (lifecycle "
                          f"{ctx.project.lifecycle}). Tidak ada data, resource atau backup yang dihapus otomatis (§33).")
        b.put("scope", _bullets([as_text(i.get("title")) for i in items.values()]))
        b.put("impact", item("consumers"))
        b.put("data_closure", item("data"))
        b.put("access_closure", item("credentials"))
        b.put("integration_closure", item("consumers"))
        b.put("infrastructure_closure", item("jobs"))
        b.put("security_closure", item("credentials"))
        data_items = [x for x in as_list(ctx.view.data.get("items")) if isinstance(x, dict)]
        b.put("privacy_closure", _join(item("legal_hold"), _bullets(
            [f"{as_text(x.get('data_type'))} — disposal (rencana): {as_text(x.get('disposal')) or '—'}"
             for x in data_items])))
        b.put("backup_closure", item("backups"))
        b.put("communication", item("communication"))
        b.live("execution", _live("AEOS Operations → Retirement checklist", "Status tiap butir dibaca live dari "
                                                                           "checklist read-only."))
        b.live("validation", _live("AEOS Operations → Retirement checklist", "Verifikasi akhir pemilik dibaca dari "
                                                                            "Decision RETIREMENT."))
        b.live("evidence", _live("AEOS Evidence & Decisions", "Evidence eksekusi closure ditautkan saat tercatat."))
        b.human("final_approval", "Final approval retirement hanya melalui Decision manusia (kind RETIREMENT).")
        _compose(doc, b, header_title=node.title, metadata=ctx.metadata(doc))

    slice_ = {"items": [{k: v for k, v in i.items() if k in ("key", "title", "owner_action", "detail")}
                        for i in items.values()],
              "data": ctx.view.data.get("items"), "lifecycle": ctx.project.lifecycle}
    # live counts in the checklist (open tasks, attachments ...) are not part of the hash: only names/actions are
    for i in slice_["items"]:
        i["detail"] = {k: v for k, v in (i.get("detail") or {}).items() if isinstance(v, list)}
    ctx.add(logical_id=node.key, node=node.number, title=node.title, kind="DOC", doc_type=spec.doc_type, parent=parent,
            timing=node.timing, status=_ok_status(ctx), reason="", owner=ctx.owner(node.owner_role), slice_=slice_,
            template_key="retirement-decommission", build=build)


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


def _unique_key(raw_key: str, used: set[str], *, fallback: str = "item") -> str:
    """Plan content is DATA: keys become logical ids and path segments, so they are slugged (no separators,
    no traversal) and de-duplicated (two RFCs/TDDs never collapse into one document)."""
    base = slugify(raw_key, max_len=40) if re.search(r"[a-z0-9]", raw_key.lower()) else fallback
    key, n = base, 2
    while key in used:
        key = f"{base}-{n}"
        n += 1
    used.add(key)
    return key


def render_document_set(*, project: ProjectIdentity, version: VersionIdentity, content: dict[str, Any] | None,
                        tasks: list[TaskInfo] | None = None, visuals: list[VisualArtifact] | None = None,
                        previous_input_hashes: dict[str, str] | None = None,
                        signals: RenderSignals | None = None) -> RenderResult:
    """Render the Product 1 set for one plan version (pure; see module docstring)."""
    view = build_view(content)
    ctx = _Ctx(project, version, view, list(tasks or []), list(visuals or []), dict(previous_input_hashes or {}),
               signals)
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
                    used: set[str] = set()
                    # Feature-bound documents first: their key is the (already unique) feature key that
                    # the PRD links to; plan-level RFC/TDD entries are de-duplicated against them.
                    for f in view.features:
                        flag = f.needs_rfc if kind == "RFC" else f.needs_tdd
                        data = f.raw.get(kind.lower()) if isinstance(f.raw.get(kind.lower()), dict) else {}
                        if flag:
                            entries.append((_unique_key(f.key, used), f.title, data, f))
                    for i, raw in enumerate(as_list(view.get("rfcs" if kind == "RFC" else "tdds"))):
                        if isinstance(raw, dict):
                            raw_key = as_text(raw.get("key") or raw.get("id")) or f"{kind.lower()}-{i + 1}"
                            entries.append((_unique_key(raw_key, used, fallback=f"{kind.lower()}-{i + 1}"),
                                            as_text(raw.get("title")) or raw_key, raw, None))
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
                    for f in view.features:
                        _security_assessment(ctx, f, child.key)
                elif child.key == "04-threat-models":
                    has_tm = _threat_model_input(ctx) is not None or any(
                        _feature_threat_model_input(f) is not None for f in view.features)
                    _folder(ctx, child, node.key, has_items=has_tm,
                            reason="Threat model dibuat saat feature approval bila rencana memuat threat/assessment.")
                    _threat_model(ctx, child.key)
                    for f in view.features:
                        _feature_threat_model(ctx, f, child.key)
                elif child.key == "04-vulnerability-assessments":
                    va = ctx.signals.evidence_of(*VA_EVIDENCE_KINDS)
                    _folder(ctx, child, node.key, has_items=bool(va),
                            reason="Belum ada vulnerability assessment yang dijalankan; hasil VA hanya dari "
                                   "pemeriksaan nyata (EvidenceArtifact tepercaya, tidak dikarang).")
                    for ev in va:
                        _assessment_from_evidence(ctx, ev, child.key, "VULNERABILITY_ASSESSMENT")
                elif child.key == "04-penetration-tests":
                    pt = ctx.signals.evidence_of(*PT_EVIDENCE_KINDS)
                    _folder(ctx, child, node.key, has_items=bool(pt),
                            reason="Belum ada penetration test; hasil PT hanya dari pengujian nyata dengan "
                                   "otorisasi (EvidenceArtifact tepercaya, tidak dikarang).")
                    for ev in pt:
                        _assessment_from_evidence(ctx, ev, child.key, "PENETRATION_TEST")
                elif child.key == "04-privacy-assessments":
                    pd = [f for f in view.features if f.personal_data]
                    unknown = any(f.personal_data is None for f in view.features)
                    _folder(ctx, child, node.key, has_items=bool(pd),
                            reason="Tidak ada fitur yang dinyatakan memproses data pribadi" +
                                   ("; status data pribadi sebagian fitur belum dinyatakan di rencana (schema rencana "
                                    "v1 tidak memiliki penanda data pribadi per fitur) — DPIA dibuat bila manusia/"
                                    "rencana menyatakannya." if unknown else "."))
                    for f in pd:
                        _privacy_assessment(ctx, f, child.key)
                elif child.key == "04-data-inventory":
                    _data_inventory(ctx, child, node.key)
                elif child.key == "04-security-privacy-decisions":
                    _security_decisions(ctx, child, node.key)
        elif node.key == "05-development":
            _development(ctx, node, root)
        elif node.key == "06-testing":
            index = view.test_plan_index()
            _folder(ctx, node, root, has_items=bool(index),
                    reason="Test Plan dibuat per fitur/release saat feature approval; belum ada fitur atau test plan.")
            for key, tp, feats in index:
                _test_plan(ctx, key, tp, feats, node.key)
        elif node.key == "07-release-deployment":
            releases = _release_index(view)
            _folder(ctx, node, root, has_items=bool(releases),
                    reason="Release Plan (termasuk Release Notes) dibuat saat release candidate (§28); rencana yang "
                           "disetujui belum memuat release plan.")
            for key, rp in releases:
                _release_plan(ctx, key, rp, node.key)
        elif node.key == "08-operations":
            _folder(ctx, node, root, has_items=True)
            fillers = _ops_fillers(ctx)
            for child in node.children:
                if child.kind == p1.NodeKind.FOLDER:
                    names = ", ".join(t for _k, t in p1.SUGGESTED_RUNBOOKS)
                    used: set[str] = set()
                    books = [(_unique_key(as_text(rb.get("key") or rb.get("title")) or f"runbook-{i + 1}", used,
                                          fallback=f"runbook-{i + 1}"), rb)
                             for i, rb in enumerate(as_list(view.operations.get("runbooks"))) if isinstance(rb, dict)]
                    _folder(ctx, child, node.key, has_items=bool(books),
                            reason=f"Runbook ({names}) dibuat sesuai scope saat release candidate; rencana belum "
                                   "memuat runbook.")
                    for key, rb in books:
                        _runbook(ctx, key, rb, child.key)
                else:
                    _ops_doc(ctx, child, node.key, fillers[child.doc_type or ""])
        elif node.key == "09-incidents-problems":
            _folder(ctx, node, root, has_items=True)
            for child in node.children:
                _folder(ctx, child, node.key, has_items=False,
                        reason="Dibuat ketika insiden/problem nyata terjadi dan tercatat; AEOS belum memiliki record "
                               "insiden/problem produk untuk proyek ini (tidak dikarang).")
        elif node.key == "10-changes":
            changes = sorted(ctx.signals.changes, key=lambda c: c.task_key)
            _folder(ctx, node, root, has_items=bool(changes),
                    reason="Change record dibuat per perubahan saat implementation/PR; belum ada PR untuk proyek ini.")
            seen_changes: set[str] = set()
            for c in changes:
                if c.task_key in seen_changes:
                    continue  # one change record per TaskSpec (the newest PR was listed first)
                seen_changes.add(c.task_key)
                _change_record(ctx, c, node.key)
        elif node.key == "11-improvements":
            imps = sorted(ctx.signals.improvements, key=lambda i: i.id)
            _folder(ctx, node, root, has_items=bool(imps),
                    reason="Improvement dicatat ketika ada peluang yang diajukan (ChangeProposal proyek).")
            for imp in imps:
                _improvement(ctx, imp, node.key)
        elif node.key == "12-retirement":
            retiring = lifecycle in ("RETIRING", "RETIRED")
            if retiring and ctx.signals.retirement:
                _retirement(ctx, node, root)
            else:
                _future_doc(ctx, node, root, applicable=retiring,
                            reason=("Lifecycle proyek RETIRING: checklist retirement belum tersedia; rencana closure "
                                    "disiapkan pemilik." if retiring else "Lifecycle proyek belum retirement (§28)")
                            + f" Owner: {ctx.owner(node.owner_role)}.")
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
