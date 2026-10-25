"""Visuals for Product 1 documents (master §29).

* Technical diagrams come from diffable source (Mermaid) in the approved plan and are rendered to
  PNG/SVG with mermaid-cli (``mmdc``) when that optional component is installed. Detection and
  rendering are bounded by timeouts. When ``mmdc`` is missing or fails, the source is kept and the
  visual is reported as ``VISUAL_PENDING`` — never replaced by a fake/placeholder image.
* Diagrams are design artefacts (DESIGN / ILLUSTRATIVE) and are never used as runtime proof.
* Screenshots must come from the running app at a known commit/environment without secrets/PII.
  This module defines the manifest; the trusted collector ``aeos.docs_engine.screenshots`` captures
  them, and manifests stay ``NOT_PROVEN`` until it stored SCREENSHOT evidence for the project.

Optional binary override: environment variable ``AEOS_MMDC_BINARY`` (path to mmdc/mmdc.cmd).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from aeos.core.hashing import sha256_text
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.docs_engine.plan_content import DiagramView

DETECT_TIMEOUT_S = 20
RENDER_TIMEOUT_S = 90


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    procs: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        procs = parent.children(recursive=True)
    procs.append(parent)
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    with contextlib.suppress(psutil.Error):
        psutil.wait_procs(procs, timeout=5)


def run_bounded(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run *argv* (no shell) with a timeout that kills the whole process tree.

    ``mmdc`` is an npm shim on Windows (``mmdc.cmd`` -> node -> headless Chromium): ``subprocess.run``'s
    timeout would kill only the shim and leave node/Chromium running. Raises ``subprocess.TimeoutExpired``."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)

# §29 table: which source a visual must come from and how it may be used.
VISUAL_KINDS: dict[str, dict[str, str]] = {
    "context": {"label": "Context/component diagram", "source": "Contract/manifest dan review arsitektur"},
    "component": {"label": "Context/component diagram", "source": "Contract/manifest dan review arsitektur"},
    "sequence": {"label": "Sequence diagram", "source": "Alur API dan pengujian integration"},
    "erd": {"label": "ERD", "source": "Schema/migration yang dirujuk, bukan tabel yang dihalusinasikan"},
    "trust_boundary": {"label": "Trust boundary/data flow", "source": "Threat model dan inventory data"},
    "data_flow": {"label": "Trust boundary/data flow", "source": "Threat model dan inventory data"},
    "cicd": {"label": "CI/CD/deployment topology", "source": "Pipeline/IaC dan target environment yang diverifikasi"},
    "deployment": {"label": "CI/CD/deployment topology",
                   "source": "Pipeline/IaC dan target environment yang diverifikasi"},
    "wireframe": {"label": "Wireframe/journey", "source": "Proposal UX; diberi status desain"},
    "journey": {"label": "Wireframe/journey", "source": "Proposal UX; diberi status desain"},
    "screenshot": {"label": "Screenshot",
                   "source": "Aplikasi berjalan pada commit/environment tertentu; tanpa secret/PII yang tidak perlu"},
    "evidence_chart": {"label": "Evidence chart",
                       "source": "Test/metric data yang tercatat; bukan ilustrasi untuk menyatakan PASS"},
}

# Why a model-authored diagram stays UNVERIFIED until a real check exists (§29 table, evidence honesty).
_UNVERIFIED_NOTE: dict[str, str] = {
    "context": "Dari rencana (model-authored); tidak ada InstallManifest/inventory yang dirujuk untuk proyek ini, "
               "jadi node belum dibandingkan — review arsitektur manusia lewat approval rencana saja.",
    "component": "Dari rencana (model-authored); tidak ada InstallManifest/inventory yang dirujuk untuk proyek ini, "
                 "jadi node belum dibandingkan — review arsitektur manusia lewat approval rencana saja.",
    "sequence": "Dari rencana (model-authored); tidak ada route contract yang dirujuk untuk proyek ini; VERIFIED hanya "
                "bila ditautkan ke evidence integration test — belum ada.",
    "erd": "ERD dari rencana (PROPOSAL); belum diturunkan dari schema/migration yang dirujuk.",
    "trust_boundary": "Dari rencana; dicek silang hanya terhadap label trust boundary di threat model rencana.",
    "data_flow": "Dari rencana; dicek silang terhadap trust boundary threat model dan data inventory rencana.",
    "cicd": "Topologi dari rencana; dicek silang hanya terhadap environment dengan target deployment terdaftar.",
    "deployment": "Topologi dari rencana; dicek silang hanya terhadap environment dengan target deployment terdaftar.",
    "journey": "Proposal UX (status desain), bukan perilaku aplikasi yang terverifikasi.",
    "wireframe": "Proposal UX (status desain), bukan perilaku aplikasi yang terverifikasi.",
}

STATUS_RENDERED = "RENDERED"
STATUS_PENDING = "VISUAL_PENDING"
STATUS_FAILED = "FAILED"
EVIDENCE_ROLE = "ILLUSTRATIVE_NOT_RUNTIME_PROOF"


@dataclass
class MmdcInfo:
    available: bool
    path: str | None = None
    version: str | None = None
    status: str = "NOT_INSTALLED"  # NOT_INSTALLED|AVAILABLE|FAILED|TIMEOUT
    reason: str = ""
    checked_at: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class VisualArtifact:
    key: str
    title: str
    kind: str
    source: str
    source_hash: str
    status: str
    reason: str = ""
    png_path: str | None = None
    svg_path: str | None = None
    source_path: str | None = None
    renderer: str | None = None
    kind_label: str = ""
    required_source: str = ""
    design_status: str = "DESIGN"
    evidence_role: str = EVIDENCE_ROLE
    # §29 "diverifikasi terhadap ...": what the plan says to check against, and what AEOS could actually check.
    verification_source: str = ""
    verification_status: str = "UNVERIFIED"  # UNVERIFIED | CONSISTENT | MISMATCH
    verification_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mmdc_candidate() -> str | None:
    override = os.environ.get("AEOS_MMDC_BINARY")
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("mmdc")


def detect_mmdc(binary: str | None = None) -> MmdcInfo:
    """Detect mermaid-cli. Optional component: absence is a normal, reported state."""
    path = binary or mmdc_candidate()
    if not path:
        return MmdcInfo(available=False, reason="mermaid-cli (mmdc) tidak ditemukan; visual tetap sebagai source "
                                                "Mermaid dan berstatus visual pending. Opsional: npm install -g "
                                                "@mermaid-js/mermaid-cli")
    try:
        proc = run_bounded([path, "--version"], timeout=DETECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return MmdcInfo(available=False, path=path, status="TIMEOUT", reason="mmdc --version timeout")
    except OSError as exc:
        return MmdcInfo(available=False, path=path, status="FAILED", reason=f"mmdc tidak dapat dijalankan: {exc}")
    if proc.returncode != 0:
        return MmdcInfo(available=False, path=path, status="FAILED",
                        reason=redact_text((proc.stderr or proc.stdout or "")[:300]))
    return MmdcInfo(available=True, path=path, version=(proc.stdout or "").strip()[:64], status="AVAILABLE")


def _kind_meta(kind: str) -> dict[str, str]:
    return VISUAL_KINDS.get(kind, {"label": kind or "diagram", "source": "Source diagram terversi yang direview"})


def _source_artifact(diagram: DiagramView) -> VisualArtifact:
    meta = _kind_meta(diagram.kind)
    return VisualArtifact(
        key=diagram.key, title=diagram.title, kind=diagram.kind, source=diagram.source,
        source_hash="sha256:" + sha256_text(diagram.source), status=STATUS_PENDING, kind_label=meta["label"],
        required_source=meta["source"],
        design_status="DESIGN" if diagram.kind not in ("screenshot", "evidence_chart") else "NOT_PROVEN",
        verification_source=diagram.verification_source,
        verification_notes=[_UNVERIFIED_NOTE.get(diagram.kind, "Diagram dari rencana (model-authored); belum "
                                                                "diverifikasi terhadap sumbernya.")],
    )


def source_only(diagrams: list[DiagramView]) -> list[VisualArtifact]:
    """The plan's diagrams as VISUAL_PENDING artifacts carrying only their source: nothing is written or rendered
    (pure; used where only the document text matters, e.g. the §41 readiness check)."""
    return [_source_artifact(d) for d in diagrams]


def render_diagram(diagram: DiagramView, out_dir: Path, *, mmdc: MmdcInfo | None = None,
                   timeout: int = RENDER_TIMEOUT_S) -> VisualArtifact:
    """Render one Mermaid diagram to PNG+SVG under *out_dir* (content-addressed by source hash)."""
    art = _source_artifact(diagram)
    short = art.source_hash[7:23]
    if diagram.language != "mermaid":
        art.reason = f"bahasa diagram {diagram.language!r} tidak didukung renderer; source disimpan"
        return art
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / f"{diagram.key}-{short}.mmd"
    png = out_dir / f"{diagram.key}-{short}.png"
    svg = out_dir / f"{diagram.key}-{short}.svg"
    src.write_text(diagram.source, encoding="utf-8")
    art.source_path = str(src)
    if png.is_file() and svg.is_file() and png.stat().st_size > 0:
        art.status, art.png_path, art.svg_path, art.renderer = STATUS_RENDERED, str(png), str(svg), "mmdc (cache)"
        return art
    mmdc = mmdc if mmdc is not None else detect_mmdc()
    if not mmdc.available or not mmdc.path:
        art.reason = mmdc.reason or "mmdc tidak tersedia"
        return art
    for target in (png, svg):
        try:
            proc = run_bounded([mmdc.path, "-i", str(src), "-o", str(target), "-b", "white", "--quiet"],
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            art.status, art.reason = STATUS_FAILED, f"mmdc timeout setelah {timeout}s; visual pending"
            return art
        except OSError as exc:
            art.status, art.reason = STATUS_FAILED, f"mmdc gagal dijalankan: {exc}"
            return art
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            art.status = STATUS_FAILED
            art.reason = "mmdc gagal: " + redact_text((proc.stderr or proc.stdout or "").strip()[:300])
            return art
    art.status, art.png_path, art.svg_path = STATUS_RENDERED, str(png), str(svg)
    art.renderer = f"mmdc {mmdc.version or ''}".strip()
    return art


def render_visuals(diagrams: list[DiagramView], out_dir: Path, *, enabled: bool = True,
                   mmdc: MmdcInfo | None = None) -> list[VisualArtifact]:
    if not diagrams:
        return []
    if not enabled:
        info = MmdcInfo(available=False, status="DISABLED", reason="rendering visual dinonaktifkan untuk preview ini")
    else:
        info = mmdc if mmdc is not None else detect_mmdc()
    return [render_diagram(d, out_dir, mmdc=info) for d in diagrams]


# ---------------------------------------------------------------------------------------------
# Cross-checks that AEOS can really perform (§29 "diverifikasi terhadap ..."). Anything else stays UNVERIFIED.

_ERD_REL = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*[|}o]{1,2}[-.]{2}[|{o]{1,2}\s*([A-Za-z_][\w-]*)")
_ERD_ENTITY = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*\{\s*$")


def erd_entities(source: str) -> set[str]:
    """Entity names of a Mermaid ``erDiagram`` (relationship lines and ``NAME {`` blocks)."""
    out: set[str] = set()
    for line in source.splitlines():
        if line.strip().lower().startswith("erdiagram"):
            continue
        m = _ERD_REL.match(line)
        if m:
            out.update({m.group(1), m.group(2)})
            continue
        m = _ERD_ENTITY.match(line)
        if m:
            out.add(m.group(1))
    return out


_ERD_ATTR = re.compile(r"^\s*[A-Za-z_][\w()\[\],]*\s+([A-Za-z_][\w]*)")


def erd_attributes(source: str) -> dict[str, set[str]]:
    """Attribute names per entity block of a Mermaid ``erDiagram`` (``type name [PK|FK] ["comment"]``)."""
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in source.splitlines():
        m = _ERD_ENTITY.match(line)
        if m:
            current = m.group(1)
            out.setdefault(current, set())
            continue
        if current is not None:
            if line.strip().startswith("}"):
                current = None
                continue
            a = _ERD_ATTR.match(line)
            if a:
                out[current].add(a.group(1))
    return out


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


_FLOW_LABELLED = re.compile(r"([A-Za-z_][\w-]*)\s*(\[\[|\[\(|\(\(|\[|\(|\{\{|\{|>)\s*\"?([^\]\)\}\"]{1,120}?)\"?\s*"
                            r"(\]\]|\)\]|\)\)|\]|\)|\}\}|\})")
_FLOW_SKIP = ("%%", "flowchart", "graph", "classdef", "class ", "style ", "linkstyle", "click ", "end", "direction",
              "subgraph")
_FLOW_ARROW = re.compile(r"<?[-.=]{2,}>?|&|\|[^|]*\|")
_API_PATH = re.compile(r"(/api/[A-Za-z0-9_./{}:-]+)")


def flow_nodes(source: str) -> dict[str, str]:
    """Node id -> label of a Mermaid flowchart/graph (``A[Label]``, ``B((x))``, bare ``A --> B``); subgraph
    titles, styles and edge labels are not nodes."""
    nodes: dict[str, str] = {}
    for line in source.splitlines():
        s = line.strip()
        if not s or s.lower().startswith(_FLOW_SKIP):
            continue
        for m in _FLOW_LABELLED.finditer(s):
            nodes[m.group(1)] = m.group(3).strip()
        bare = _FLOW_LABELLED.sub(lambda m: f" {m.group(1)} ", s)
        for token in _FLOW_ARROW.sub(" ", bare).split():
            if re.fullmatch(r"[A-Za-z_][\w-]*", token):
                nodes.setdefault(token, token)
    return nodes


def _route_matches(path: str, templates: set[str]) -> bool:
    segs = [s for s in path.rstrip("/.").split("/") if s]
    for t in templates:
        tsegs = [s for s in t.split("/") if s]
        if len(tsegs) == len(segs) and all(a == b or (a.startswith("{") and a.endswith("}")) or
                                           (b.startswith("{") and b.endswith("}")) for a, b in zip(tsegs, segs,
                                                                                                    strict=True)):
            return True
    return False


def _labels_present(labels: list[str], source: str) -> list[str]:
    src = _norm_label(source)
    return [x for x in labels if _norm_label(x) and _norm_label(x) not in src]


def verify_visuals(visuals: list[VisualArtifact], *, trust_boundaries: list[str] | None = None,
                   known_tables: set[str] | None = None, known_components: set[str] | None = None,
                   known_routes: set[str] | None = None, data_items: list[str] | None = None,
                   deployment_environments: list[str] | None = None,
                   known_columns: dict[str, set[str]] | None = None) -> list[VisualArtifact]:
    """Annotate visuals with the checks AEOS can run deterministically (labels/names, never runtime proof):

    * ``erd`` — entity names vs the referenced schema's table names (``known_tables``; for the platform
      project these come from the SQLAlchemy metadata at the running commit). Tables the schema does not
      have are reported as MISMATCH (never silently accepted); without a referenced schema → UNVERIFIED.
    * ``trust_boundary``/``data_flow`` — every trust boundary named in the plan's threat model, and (data flow)
      every data type of the plan's data inventory, must appear in the diagram source; missing ones → MISMATCH.
    * ``context``/``component`` — every node must name a registered component (``known_components``: the
      platform's InstallManifest/inventory ids and names); unknown nodes → MISMATCH.
    * ``sequence`` — every ``/api/...`` path in the diagram must exist in the route contract (``known_routes``);
      unknown paths → MISMATCH. The message order is not proven (integration-test evidence is separate).
    * ``deployment``/``cicd`` — every environment with a registered deployment target must appear.
    Without a reference to compare against, a diagram stays UNVERIFIED with the reason in ``verification_notes``."""
    boundaries = [b for b in (trust_boundaries or []) if b.strip()]
    data_names = [d for d in (data_items or []) if d.strip()]
    envs = [e for e in (deployment_environments or []) if e.strip()]
    for v in visuals:
        if v.kind in ("context", "component") and known_components:
            known = {_norm_label(c) for c in known_components}
            nodes = flow_nodes(v.source)
            unknown = sorted({label for nid, label in nodes.items()
                              if _norm_label(label) not in known and _norm_label(nid) not in known})
            if not nodes:
                v.verification_notes = ["Diagram tanpa node yang dapat dibaca; tidak dapat dibandingkan dengan "
                                        "InstallManifest."]
            elif unknown:
                v.verification_status = "MISMATCH"
                v.verification_notes = ["Node tidak terdaftar di InstallManifest/inventory (tidak diterima sebagai "
                                        f"fakta): {', '.join(unknown[:20])}"]
            else:
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(nodes)} node cocok dengan komponen InstallManifest/inventory "
                                        "(cek nama; relasi belum dibandingkan)."]
            continue
        if v.kind == "sequence" and known_routes:
            paths = sorted(set(_API_PATH.findall(v.source)))
            unknown = [p for p in paths if not _route_matches(p, known_routes)]
            if not paths:
                v.verification_notes = ["Sequence tanpa path /api yang dapat dibandingkan dengan route contract; "
                                        "tetap UNVERIFIED."]
            elif unknown:
                v.verification_status = "MISMATCH"
                v.verification_notes = ["Path API tidak ada di route contract (contracts/api/routes.json): "
                                        + ", ".join(unknown[:20])]
            else:
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(paths)} path API ada di route contract; urutan pesan belum "
                                        "dibuktikan oleh evidence integration test."]
            continue
        if v.kind in ("deployment", "cicd") and envs:
            missing = _labels_present(envs, v.source)
            if missing:
                v.verification_status = "MISMATCH"
                v.verification_notes = ["Environment dengan target deployment terdaftar tidak muncul di diagram: "
                                        + ", ".join(missing)]
            else:
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(envs)} environment target deployment terdaftar muncul di "
                                        "diagram (cek label; pipeline/IaC belum dibandingkan)."]
            continue
        if v.kind == "data_flow" and (boundaries or data_names):
            notes, bad = [], False
            if boundaries:
                miss_b = _labels_present(boundaries, v.source)
                bad = bad or bool(miss_b)
                notes.append("Trust boundary di threat model tidak muncul di diagram: " + ", ".join(miss_b) if miss_b
                             else f"Semua {len(boundaries)} trust boundary threat model muncul di diagram.")
            if data_names:
                miss_d = _labels_present(data_names, v.source)
                bad = bad or bool(miss_d)
                notes.append("Data dari data inventory tidak muncul di diagram: " + ", ".join(miss_d) if miss_d
                             else f"Semua {len(data_names)} jenis data di data inventory muncul di diagram.")
            v.verification_status = "MISMATCH" if bad else "CONSISTENT"
            v.verification_notes = [*notes, "Cek label, bukan verifikasi runtime."]
            continue
        if v.kind == "erd" and known_tables is not None:
            entities = erd_entities(v.source)
            tables = {_norm_label(t) for t in known_tables}
            unknown = sorted(e for e in entities if _norm_label(e) not in tables)
            if not entities:
                v.verification_status = "UNVERIFIED"
                v.verification_notes = ["ERD tanpa entitas yang dapat dibaca; tidak dapat dibandingkan dengan schema."]
            elif unknown:
                v.verification_status = "MISMATCH"
                v.verification_notes = [f"Entitas tidak ada di schema yang dirujuk (tidak diterima sebagai fakta): "
                                        f"{', '.join(unknown[:20])}"]
            else:
                cols = {_norm_label(t): {_norm_label(c) for c in cs} for t, cs in (known_columns or {}).items()}
                bad_cols = sorted(f"{e}.{a}" for e, attrs in erd_attributes(v.source).items()
                                  if _norm_label(e) in cols for a in attrs if _norm_label(a) not in cols[_norm_label(e)])
                if bad_cols:
                    v.verification_status = "MISMATCH"
                    v.verification_notes = ["Kolom tidak ada di tabel schema yang dirujuk (tidak diterima sebagai "
                                            f"fakta): {', '.join(bad_cols[:20])}"]
                else:
                    v.verification_status = "CONSISTENT"
                    v.verification_notes = [f"Semua {len(entities)} entitas ada di schema yang dirujuk (nama tabel)"
                                            + ("; atribut yang ditulis cocok dengan kolom tabel" if known_columns
                                               else "; kolom belum dibandingkan") + "; relasi belum dibandingkan."]
        elif v.kind in ("trust_boundary", "data_flow") and boundaries:
            src = _norm_label(v.source)
            missing = [b for b in boundaries if _norm_label(b) and _norm_label(b) not in src]
            if missing:
                v.verification_status = "MISMATCH"
                v.verification_notes = ["Trust boundary di threat model tidak muncul di diagram: " + ", ".join(missing)]
            else:
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(boundaries)} trust boundary threat model muncul di diagram "
                                        "(cek label, bukan verifikasi runtime)."]
    return visuals


def evidence_chart(*, title: str, unit: str, source: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    """§29 evidence chart: drawn only from recorded data, every point bound to its record id and time; a missing
    value is an UNKNOWN gap (never plotted as 0) and the chart never states PASS/FAIL (that is the record's job).
    ``points``: ``{"ref": record id, "at": ISO time, "value": float | None}`` oldest first."""
    bound = [p for p in points if p.get("ref") and p.get("at")]
    measured = [p for p in bound if isinstance(p.get("value"), (int, float))]
    out: dict[str, Any] = {"kind": "evidence_chart", "title": title, "unit": unit, "source": source,
                           "evidence_role": "RECORDED_DATA_NOT_A_VERDICT", "points": bound,
                           "unknown_points": len(bound) - len(measured), "unbound_points_dropped": len(points) - len(bound),
                           "refs": [p["ref"] for p in measured]}
    if not measured:
        out.update(status="NO_DATA", mermaid=None, reason="Belum ada nilai tercatat; tidak ada grafik yang dikarang.")
        return out
    labels = ", ".join(f'"{str(p["at"])[5:16]}"' for p in measured)
    values = ", ".join(f"{float(p['value']):g}" for p in measured)
    safe_title = re.sub(r'["\n]', " ", title)[:80]
    out.update(status="EVIDENCE_BOUND", reason=f"{len(measured)} nilai tercatat dari {source}.",
               mermaid=f'xychart-beta\n  title "{safe_title}"\n  x-axis [{labels}]\n  y-axis "{unit}"\n'
                       f"  line [{values}]")
    return out


def screenshot_manifest(*, project_id: str, commit: str | None, environment: str | None,
                        routes: list[str] | None = None,
                        evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """§29 screenshot contract. A manifest stays NOT_PROVEN unless trusted SCREENSHOT EvidenceArtifacts
    (collector origin, recorded commit/environment) exist for the project; those are referenced by id — never
    generated or copied here. They are produced by the trusted collector ``aeos.docs_engine.screenshots``
    (``aeos docs screenshot``: registered deployment target, commit proven by its health endpoint first, page
    text checked for secrets/PII before the image is stored)."""
    items = [e for e in (evidence or []) if e.get("kind") == "SCREENSHOT" and e.get("trusted")
             and e.get("origin") != "worker" and (e.get("environment") or {}).get("commit")]
    out: dict[str, Any] = {
        "kind": "screenshot",
        "project_id": project_id,
        "commit": commit,
        "environment": environment,
        "routes": list(routes or []),
        "requirements": [
            "Aplikasi berjalan pada commit/environment yang tercatat",
            "Tanpa secret/PII yang tidak perlu (redaction diperiksa sebelum disimpan)",
            "Disimpan sebagai EvidenceArtifact oleh collector tepercaya, bukan oleh worker",
        ],
        "status": "NOT_PROVEN",
        "reason": "Belum ada screenshot tepercaya untuk proyek ini. Collector: `aeos docs screenshot <project_id> "
                  "--environment staging|production --commit <sha> --route /...` (target deployment terdaftar; "
                  "commit dibuktikan lewat health endpoint; teks halaman diperiksa untuk secret/PII).",
        "items": [],
        "evidence_role": "SCREENSHOT_REQUIRES_COLLECTOR",
    }
    if items:
        out["status"] = "EVIDENCE_REFERENCED"
        out["reason"] = "Screenshot dirujuk dari EvidenceArtifact tepercaya (commit/environment tercatat)."
        out["items"] = [{"evidence_id": e.get("id"), "commit": (e.get("environment") or {}).get("commit"),
                         "environment": (e.get("environment") or {}).get("name"), "claim": e.get("claim", "")}
                        for e in items]
    return out
