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
import itertools
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
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
    verification_status: str = "UNVERIFIED"  # UNVERIFIED | CONSISTENT | MISMATCH | VERIFIED (sequence + tests)
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


norm_label = _norm_label  # shared with docs_engine.references (one normalisation for both sides of a check)


def erd_relations(source: str) -> list[tuple[str, str]]:
    """Relationship lines of a Mermaid ``erDiagram`` as (entity, entity) pairs, in source order."""
    out: list[tuple[str, str]] = []
    for line in source.splitlines():
        m = _ERD_REL.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


@dataclass
class VisualReferences:
    """What a diagram is compared with (§29 "Sumber / verifikasi"), gathered read-only by
    ``aeos.docs_engine.references`` from the project's own records and its checkout at a recorded commit.

    ``None`` means "not known for this project": that check stays UNVERIFIED and ``missing[<name>]`` says why.
    Names are kept as read; comparisons normalise both sides with :func:`norm_label`."""

    source: str = ""  # where the references were read (commit), for the record
    component_source: str = "InstallManifest/inventory"
    schema_source: str = "schema yang dirujuk"
    ci_source: str = "pipeline CI"
    deploy_source: str = "IaC/deployment yang terdeteksi"
    tables: set[str] | None = None
    columns: dict[str, set[str]] | None = None
    foreign_keys: set[tuple[str, str]] | None = None  # (table, referenced table)
    table_aliases: dict[str, str] = field(default_factory=dict)  # mapped class / model name -> table name
    components: set[str] | None = None
    component_ids: dict[str, str] = field(default_factory=dict)  # id / name / friendly name -> component id
    relations: set[tuple[str, str]] | None = None  # (source component id, target component id), DESIGN lens
    routes: set[str] | None = None
    route_prefix: str = ""  # only paths under it are covered by the route contract ("/api/" for the platform)
    route_source: str = "route contract"
    ci_jobs: set[str] | None = None  # CI job ids/names, triggers, stages and job environments
    deploy_refs: set[str] | None = None  # IaC/deploy kinds, compose services, chart/manifest names
    # integration test key (junit form) -> (evidence id, accepted head commit): trusted verifier PASS at that head
    passed_tests: dict[str, tuple[str, str]] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)  # reference name -> why it is not known


# --- flowchart nodes/edges, sequence calls and the integration tests a diagram names -----------------------

_NODE_ID = r"[A-Za-z_]\w*(?:-\w+)*"  # a hyphen inside an id, never the start of a link ("A-->B" is two nodes)
_FLOW_LABELLED = re.compile(rf"({_NODE_ID})\s*(\[\[|\[\(|\(\(|\[|\(|\{{\{{|\{{|>)\s*\"?([^\]\)\}}\"]{{1,120}}?)\"?\s*"
                            r"(\]\]|\)\]|\)\)|\]|\)|\}\}|\})")
_FLOW_SKIP = re.compile(r"^(?:%%|(?:flowchart|graph|classdef|class|style|linkstyle|click|direction|subgraph)\b|"
                        r"end\s*$)", re.IGNORECASE)
_FLOW_ARROW = re.compile(r"<?[-.=]{2,}>?|&|\|[^|]*\|")
_API_PATH = re.compile(r"(/api/[A-Za-z0-9_./{}:-]+)")
# link text forms ("A -- text --> B", "A -. text .-> B", "A == text ==> B") and "|label|" are not nodes
_EDGE_TEXT = (re.compile(r"(?<![-.=])--\s+[^-|>\s][^>]*?\s+-->"), re.compile(r"-\.\s+[^.]+?\s+\.->"),
              re.compile(r"==\s+[^=]+?\s+==>"))
_EDGE_LABEL = re.compile(r"\|[^|]*\|")
_EDGE_SPLIT = re.compile(r"\s*(?:(?<=\s)[ox]|<)?(?:-{2,}|={2,}|-\.+-|~{3,})(?:>|[ox](?=\s))?\s*")


def _flow_line(line: str) -> str | None:
    """A flowchart statement with link texts/labels removed; None for directives, comments and subgraph lines."""
    s = line.strip()
    if not s or _FLOW_SKIP.match(s):
        return None
    for pat in _EDGE_TEXT:
        s = pat.sub(" --> ", s)
    return _EDGE_LABEL.sub(" ", s)


def flow_nodes(source: str) -> dict[str, str]:
    """Node id -> label of a Mermaid flowchart/graph (``A[Label]``, ``B((x))``, bare ``A --> B``); subgraph
    titles, styles and link texts/labels are not nodes."""
    nodes: dict[str, str] = {}
    for line in source.splitlines():
        s = _flow_line(line)
        if s is None:
            continue
        for m in _FLOW_LABELLED.finditer(s):
            nodes[m.group(1)] = m.group(3).strip()
        bare = _FLOW_LABELLED.sub(lambda m: f" {m.group(1)} ", s)
        for part in _EDGE_SPLIT.split(bare):
            for token in _FLOW_ARROW.sub(" ", part).split():
                if re.fullmatch(_NODE_ID, token):
                    nodes.setdefault(token, token)
    return nodes


def flow_edges(source: str) -> list[tuple[str, str]]:
    """(node id, node id) pairs of a Mermaid flowchart's links (``A --> B``, ``A -->|x| B``, ``A -- x --> B``,
    ``A -.-> B``, ``A ==> B``, chains ``A --> B --> C`` and ``A & B --> C``); direction is kept as written."""
    edges: list[tuple[str, str]] = []
    for line in source.splitlines():
        s = _flow_line(line)
        if s is None:
            continue
        parts = _EDGE_SPLIT.split(_FLOW_LABELLED.sub(lambda m: f" {m.group(1)} ", s))
        groups = [[t for t in (x.strip() for x in p.split("&")) if re.fullmatch(_NODE_ID, t)] for p in parts]
        for left, right in itertools.pairwise(groups):
            edges.extend((a, b) for a in left for b in right if a != b)
    return edges


_HTTP_CALL = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9_./{}:-]*)")


def sequence_paths(source: str, prefix: str = "") -> list[str]:
    """API paths a sequence diagram uses: every ``/api/...`` path and every ``METHOD /path`` call; with *prefix*
    only the paths the route contract covers (the platform contract covers ``/api/``)."""
    found = set(_API_PATH.findall(source)) | {p for p in _HTTP_CALL.findall(source) if p}
    paths = sorted({p.rstrip(".,;") or "/" for p in found})
    return [p for p in paths if not prefix or p.startswith(prefix)]


_TEST_NODE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.py::[\w:.\[\]-]+)")


def named_tests(diagram_source: str, verification_source: str = "") -> list[str]:
    """Integration-test node ids a diagram names (``%% test: tests/integration/test_x.py::test_y`` lines in the
    Mermaid source, or node ids in the plan's ``verification_source``) in junit key form."""
    text = "\n".join([*(ln for ln in diagram_source.splitlines() if ln.strip().startswith("%%")),
                      verification_source or ""])
    return sorted({junit_key(m) for m in _TEST_NODE.findall(text)})


def junit_key(nodeid: str) -> str:
    """``tests/x/test_a.py::TestB::test_c[p]`` -> ``tests.x.test_a.TestB::test_c[p]``: the case key the trusted verifier
    stores in its TEST_REPORT (same rule as ``aeos.verification.junit.nodeid_to_key``; a different form simply never
    matches, so the diagram stays not VERIFIED)."""
    bracket = nodeid.find("[")
    param = nodeid[bracket:] if bracket != -1 else ""
    parts = (nodeid[:bracket] if bracket != -1 else nodeid).split("::")
    parts[-1] = parts[-1] + param
    module = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    module = module.replace("\\", "/").replace("/", ".")
    if len(parts) == 1:
        return module
    classes = parts[1:-1]
    return f"{'.'.join([module, *classes]) if classes else module}::{parts[-1]}"


def _singular(norm: str) -> str:
    if norm.endswith("ies") and len(norm) > 3:
        return norm[:-3] + "y"
    if norm.endswith(("sses", "xes", "ches", "shes")):
        return norm[:-2]
    return norm[:-1] if norm.endswith("s") and not norm.endswith("ss") else norm


def _relation_supported(a: str, b: str, refs: VisualReferences) -> str | None:
    """How the referenced schema supports an ERD relation between tables *a* and *b* (normalised table names):
    ``FK`` (declared foreign key), ``JUNCTION`` (a table with foreign keys to both), ``COLUMN`` (an ``<entity>_id``
    column without a constraint) or None."""
    fks = {(_norm_label(x), _norm_label(y)) for x, y in (refs.foreign_keys or set())}
    if (a, b) in fks or (b, a) in fks:
        return "FK"
    if any((t, a) in fks and (t, b) in fks for t, _ in fks):
        return "JUNCTION"
    cols = {_norm_label(t): {_norm_label(c) for c in cs} for t, cs in (refs.columns or {}).items()}
    for child, parent in ((a, b), (b, a)):
        if {_singular(parent) + "id", parent + "id"} & cols.get(child, set()):
            return "COLUMN"
    return None


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


_REFERENCE_OF_KIND = {"context": "components", "component": "components", "sequence": "routes", "erd": "tables",
                      "cicd": "ci_jobs", "deployment": "deploy_refs"}


def _unknown_nodes(nodes: dict[str, str], allowed: set[str]) -> list[str]:
    return sorted({label for nid, label in nodes.items()
                   if _norm_label(label) not in allowed and _norm_label(nid) not in allowed})


def _check_components(v: VisualArtifact, refs: VisualReferences) -> None:
    if not refs.components:
        return
    where = refs.component_source
    nodes = flow_nodes(v.source)
    unknown = _unknown_nodes(nodes, {_norm_label(c) for c in refs.components})
    if not nodes:
        v.verification_notes = [f"Diagram tanpa node yang dapat dibaca; tidak dapat dibandingkan dengan {where}."]
        return
    if unknown:
        v.verification_status = "MISMATCH"
        v.verification_notes = [f"Node tidak terdaftar di {where} (tidak diterima sebagai fakta): "
                                f"{', '.join(unknown[:20])}"]
        return
    if refs.relations is None:
        v.verification_status = "CONSISTENT"
        reason = refs.missing.get("relations")
        v.verification_notes = [f"Semua {len(nodes)} node cocok dengan komponen {where} (cek nama; relasi belum "
                                "dibandingkan" + (f": {reason}" if reason else "") + ")."]
        return
    ids = {_norm_label(k): cid for k, cid in refs.component_ids.items()}

    def component_of(nid: str) -> str | None:
        return ids.get(_norm_label(nodes.get(nid, nid))) or ids.get(_norm_label(nid))

    registered = {frozenset(p) for p in refs.relations}
    edges = flow_edges(v.source)
    bad = []
    for a, b in edges:
        ca, cb = component_of(a), component_of(b)
        if ca and cb and ca != cb and frozenset((ca, cb)) not in registered:
            bad.append(f"{nodes.get(a, a)} -> {nodes.get(b, b)}")
    if bad:
        v.verification_status = "MISMATCH"
        v.verification_notes = [f"Relasi tidak terdaftar di {where} (tidak diterima sebagai fakta): "
                                f"{', '.join(sorted(set(bad))[:20])}"]
        return
    v.verification_status = "CONSISTENT"
    v.verification_notes = [f"Semua {len(nodes)} node dan {len(edges)} relasi cocok dengan komponen dan relasi "
                            f"{where} (relasi DESIGN dari manifest, arah tidak dibedakan)."]


def _check_sequence(v: VisualArtifact, refs: VisualReferences) -> None:
    paths = sequence_paths(v.source, refs.route_prefix)
    tests = named_tests(v.source, v.verification_source)
    notes: list[str] = []
    routes_ok = False
    if refs.routes:
        unknown = [p for p in paths if not _route_matches(p, refs.routes)]
        if unknown:
            v.verification_status = "MISMATCH"
            v.verification_notes = [f"Path API tidak ada di route contract ({refs.route_source}): "
                                    + ", ".join(unknown[:20])]
            return
        if paths:
            routes_ok = True
            notes.append(f"Semua {len(paths)} path API ada di route contract ({refs.route_source})"
                         + ("" if tests else "; urutan pesan belum dibuktikan oleh evidence integration test."))
        else:
            notes.append("Sequence tanpa path API yang dapat dibandingkan dengan route contract.")
    elif paths:
        notes.append("Path API belum dibandingkan: " + refs.missing.get("routes", "route contract tidak diketahui."))
    if not tests:
        if routes_ok:
            v.verification_status = "CONSISTENT"
            v.verification_notes = notes
        elif notes:
            v.verification_notes = [*notes, "VERIFIED hanya bila integration test yang dirujuk (`%% test: <node id>`) "
                                            "PASS pada accepted head."]
        return
    passed = [k for k in tests if k in refs.passed_tests]
    missing = [k for k in tests if k not in refs.passed_tests]
    if missing:
        notes.append("Integration test yang dirujuk belum PASS pada accepted head (TEST_REPORT verifier tepercaya): "
                     + ", ".join(missing[:20]))
        v.verification_status = "CONSISTENT" if routes_ok else "UNVERIFIED"
        v.verification_notes = notes
        return
    proof = ", ".join(f"{k} (evidence {refs.passed_tests[k][0]} @ {refs.passed_tests[k][1][:12]})" for k in passed[:10])
    notes.append(f"Urutan pesan dibuktikan oleh {len(passed)} integration test yang PASS pada accepted head: {proof}.")
    # the route half must be proven too: without a route contract a passing test does not make the paths facts
    v.verification_status = "VERIFIED" if routes_ok or not paths else "CONSISTENT"
    v.verification_notes = notes


def _check_topology(v: VisualArtifact, refs: VisualReferences, envs: list[str]) -> None:
    notes: list[str] = []
    checked = bad = False
    if envs:
        checked = True
        missing = _labels_present(envs, v.source)
        bad = bool(missing)
        notes.append("Environment dengan target deployment terdaftar tidak muncul di diagram: " + ", ".join(missing)
                     if missing else f"Semua {len(envs)} environment target deployment terdaftar muncul di diagram"
                                     " (cek label).")
    ref = refs.ci_jobs if v.kind == "cicd" else refs.deploy_refs
    if ref:
        checked = True
        allowed = {_norm_label(x) for x in (*ref, *envs, *(refs.ci_jobs or ()))}
        if v.kind == "deployment":
            allowed |= {_norm_label(x) for x in refs.components or ()}
        where = refs.ci_source if v.kind == "cicd" else refs.deploy_source
        nodes = flow_nodes(v.source)
        unknown = _unknown_nodes(nodes, allowed)
        if not nodes:
            notes.append(f"Diagram tanpa node yang dapat dibaca; tidak dapat dibandingkan dengan {where}.")
        elif unknown:
            bad = True
            notes.append(f"Node tidak ada di {where} (tidak diterima sebagai fakta): {', '.join(unknown[:20])}")
        else:
            notes.append(f"Semua {len(nodes)} node cocok dengan {where}.")
    elif checked:
        reason = refs.missing.get(_REFERENCE_OF_KIND[v.kind])
        notes.append("Pipeline/IaC belum dibandingkan" + (f": {reason}" if reason else "."))
    if checked:
        v.verification_status = "MISMATCH" if bad else "CONSISTENT"
        v.verification_notes = notes


def _check_erd(v: VisualArtifact, refs: VisualReferences) -> None:
    if refs.tables is None:
        return
    where = refs.schema_source
    entities = erd_entities(v.source)
    alias = {_norm_label(k): _norm_label(t) for k, t in refs.table_aliases.items()}
    tables = {_norm_label(t) for t in refs.tables} | set(alias)
    unknown = sorted(e for e in entities if _norm_label(e) not in tables)
    if not entities:
        v.verification_status = "UNVERIFIED"
        v.verification_notes = ["ERD tanpa entitas yang dapat dibaca; tidak dapat dibandingkan dengan schema."]
        return
    if unknown:
        v.verification_status = "MISMATCH"
        v.verification_notes = [f"Entitas tidak ada di {where} (tidak diterima sebagai fakta): "
                                f"{', '.join(unknown[:20])}"]
        return
    cols = {_norm_label(t): {_norm_label(c) for c in cs} for t, cs in (refs.columns or {}).items()}
    bad_cols = sorted(f"{e}.{a}" for e, attrs in erd_attributes(v.source).items()
                      if _norm_label(e) in cols for a in attrs if _norm_label(a) not in cols[_norm_label(e)])
    if bad_cols:
        v.verification_status = "MISMATCH"
        v.verification_notes = [f"Kolom tidak ada di tabel {where} (tidak diterima sebagai fakta): "
                                f"{', '.join(bad_cols[:20])}"]
        return
    rels = erd_relations(v.source)
    head = (f"Semua {len(entities)} entitas ada di {where} (nama tabel)"
            + ("; atribut yang ditulis cocok dengan kolom tabel" if refs.columns else "; kolom belum dibandingkan"))
    if refs.foreign_keys is None:
        v.verification_status = "CONSISTENT"
        v.verification_notes = [head + "; relasi belum dibandingkan."]
        return

    def table_of(e: str) -> str:
        n = _norm_label(e)
        return alias.get(n, n)

    support = {(a, b): _relation_supported(table_of(a), table_of(b), refs) for a, b in rels}
    unsupported = sorted({f"{a} - {b}" for (a, b), how in support.items() if how is None})
    if unsupported:
        v.verification_status = "MISMATCH"
        v.verification_notes = [f"Relasi tidak didukung {where} (tanpa foreign key, tabel penghubung atau kolom "
                                f"<entitas>_id; tidak diterima sebagai fakta): {', '.join(unsupported[:20])}"]
        return
    kinds = sorted({how for how in support.values() if how})
    v.verification_status = "CONSISTENT"
    v.verification_notes = [head + (f"; {len(rels)} relasi didukung schema ({', '.join(kinds)})" if rels
                                    else "; ERD tanpa garis relasi") + "."]


def verify_visuals(visuals: list[VisualArtifact], *, trust_boundaries: list[str] | None = None,
                   known_tables: set[str] | None = None, known_components: set[str] | None = None,
                   known_routes: set[str] | None = None, data_items: list[str] | None = None,
                   deployment_environments: list[str] | None = None,
                   known_columns: dict[str, set[str]] | None = None,
                   refs: VisualReferences | None = None) -> list[VisualArtifact]:
    """Annotate visuals with the checks AEOS can run deterministically (labels/names, never runtime proof).

    References come from ``refs`` (built by ``aeos.docs_engine.references`` from the project's own records and its
    checkout at a recorded commit); the ``known_*`` keywords fill a reference ``refs`` does not carry.

    * ``erd`` — entity names vs the referenced schema's tables (platform: SQLAlchemy metadata; product: Prisma
      schema / SQL / Alembic migrations of the checkout), written attributes vs its columns and relationship lines
      vs its foreign keys (or a junction table / ``<entity>_id`` column). Anything else → MISMATCH.
    * ``trust_boundary``/``data_flow`` — every trust boundary named in the plan's threat model, and (data flow)
      every data type of the plan's data inventory, must appear in the diagram source; missing ones → MISMATCH.
    * ``context``/``component`` — every node must name a registered component (platform: InstallManifest/inventory;
      product: compose services / package / chart names of the checkout) and every link a registered relation
      (platform: the manifest's DESIGN relations); unknown nodes or links → MISMATCH.
    * ``sequence`` — every API path must exist in the route contract (platform: contracts/api/routes.json; product:
      the OpenAPI files of the checkout); VERIFIED only when every integration test the diagram names PASSed in a
      trusted verifier report at an accepted head.
    * ``deployment``/``cicd`` — every environment with a registered deployment target must appear, and every node
      must be a CI job/stage of the checkout's pipeline (cicd) or a detected IaC/deploy reference (deployment).
    Without a reference to compare against, a diagram stays UNVERIFIED with the reason in ``verification_notes``."""
    refs = replace(refs) if refs is not None else VisualReferences()
    if refs.tables is None and known_tables is not None:
        refs.tables = set(known_tables)
    if refs.columns is None and known_columns is not None:
        refs.columns = dict(known_columns)
    if refs.components is None and known_components is not None:
        refs.components = set(known_components)
    if refs.routes is None and known_routes is not None:
        refs.routes = set(known_routes)
    boundaries = [b for b in (trust_boundaries or []) if b.strip()]
    data_names = [d for d in (data_items or []) if d.strip()]
    envs = [e for e in (deployment_environments or []) if e.strip()]
    for v in visuals:
        if v.kind in ("context", "component"):
            _check_components(v, refs)
        elif v.kind == "sequence":
            _check_sequence(v, refs)
        elif v.kind in ("deployment", "cicd"):
            _check_topology(v, refs, envs)
        elif v.kind == "erd":
            _check_erd(v, refs)
        elif v.kind == "data_flow" and (boundaries or data_names):
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
        elif v.kind == "trust_boundary" and boundaries:
            src = _norm_label(v.source)
            missing = [b for b in boundaries if _norm_label(b) and _norm_label(b) not in src]
            if missing:
                v.verification_status = "MISMATCH"
                v.verification_notes = ["Trust boundary di threat model tidak muncul di diagram: " + ", ".join(missing)]
            else:
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(boundaries)} trust boundary threat model muncul di diagram "
                                        "(cek label, bukan verifikasi runtime)."]
        reason = refs.missing.get(_REFERENCE_OF_KIND.get(v.kind, ""))
        if v.verification_status == "UNVERIFIED" and reason and reason not in v.verification_notes:
            v.verification_notes = [*v.verification_notes, reason]  # why no reference exists for this project
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
