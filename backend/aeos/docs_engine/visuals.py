"""Visuals for Product 1 documents (master §29).

* Technical diagrams come from diffable source (Mermaid) in the approved plan and are rendered to
  PNG/SVG with mermaid-cli (``mmdc``) when that optional component is installed. Detection and
  rendering are bounded by timeouts. When ``mmdc`` is missing or fails, the source is kept and the
  visual is reported as ``VISUAL_PENDING`` — never replaced by a fake/placeholder image.
* Diagrams are design artefacts (DESIGN / ILLUSTRATIVE) and are never used as runtime proof.
* Screenshots must come from the running app at a known commit/environment without secrets/PII.
  This module defines the manifest; capture itself is NOT-PROVEN here (no browser automation is
  installed/approved), so manifests stay ``NOT_PROVEN`` until a collector fills them.

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
    "context": "Dari rencana (model-authored); belum dibandingkan dengan contract/manifest — review arsitektur manusia "
               "lewat approval rencana saja.",
    "component": "Dari rencana (model-authored); belum dibandingkan dengan contract/manifest — review arsitektur "
                 "manusia lewat approval rencana saja.",
    "sequence": "Dari rencana (model-authored); VERIFIED hanya bila ditautkan ke evidence integration test — belum ada.",
    "erd": "ERD dari rencana (PROPOSAL); belum diturunkan dari schema/migration yang dirujuk.",
    "trust_boundary": "Dari rencana; dicek silang hanya terhadap label trust boundary di threat model rencana.",
    "data_flow": "Dari rencana; dicek silang hanya terhadap label trust boundary di threat model rencana.",
    "cicd": "Topologi dari rencana; belum diverifikasi terhadap pipeline/IaC dan target environment.",
    "deployment": "Topologi dari rencana; belum diverifikasi terhadap pipeline/IaC dan target environment.",
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


def render_diagram(diagram: DiagramView, out_dir: Path, *, mmdc: MmdcInfo | None = None,
                   timeout: int = RENDER_TIMEOUT_S) -> VisualArtifact:
    """Render one Mermaid diagram to PNG+SVG under *out_dir* (content-addressed by source hash)."""
    meta = _kind_meta(diagram.kind)
    source_hash = "sha256:" + sha256_text(diagram.source)
    short = source_hash[7:23]
    art = VisualArtifact(
        key=diagram.key, title=diagram.title, kind=diagram.kind, source=diagram.source, source_hash=source_hash,
        status=STATUS_PENDING, kind_label=meta["label"], required_source=meta["source"],
        design_status="DESIGN" if diagram.kind not in ("screenshot", "evidence_chart") else "NOT_PROVEN",
        verification_source=diagram.verification_source,
        verification_notes=[_UNVERIFIED_NOTE.get(diagram.kind, "Diagram dari rencana (model-authored); belum "
                                                                "diverifikasi terhadap sumbernya.")],
    )
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


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def verify_visuals(visuals: list[VisualArtifact], *, trust_boundaries: list[str] | None = None,
                   known_tables: set[str] | None = None) -> list[VisualArtifact]:
    """Annotate visuals with the checks AEOS can run deterministically:

    * ``erd`` — entity names vs the referenced schema's table names (``known_tables``; for the platform
      project these come from the SQLAlchemy metadata at the running commit). Tables the schema does not
      have are reported as MISMATCH (never silently accepted); without a referenced schema → UNVERIFIED.
    * ``trust_boundary``/``data_flow`` — every trust boundary named in the plan's threat model must appear in
      the diagram source; missing ones → MISMATCH.
    Everything else stays UNVERIFIED with the reason in ``verification_notes``."""
    boundaries = [b for b in (trust_boundaries or []) if b.strip()]
    for v in visuals:
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
                v.verification_status = "CONSISTENT"
                v.verification_notes = [f"Semua {len(entities)} entitas ada di schema yang dirujuk (nama tabel); "
                                        "kolom/relasi belum dibandingkan."]
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


def screenshot_manifest(*, project_id: str, commit: str | None, environment: str | None,
                        routes: list[str] | None = None,
                        evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """§29 screenshot contract. AEOS itself has no approved browser-automation collector in this build, so a
    manifest stays NOT_PROVEN unless trusted SCREENSHOT EvidenceArtifacts (collector origin, recorded commit/
    environment) exist for the project; those are referenced by id — never generated or copied here."""
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
        "reason": "Capture screenshot belum diimplementasikan/diizinkan pada build ini (tidak ada browser "
                  "automation yang disetujui).",
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
