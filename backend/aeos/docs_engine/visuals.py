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

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aeos.core.hashing import sha256_text
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.docs_engine.plan_content import DiagramView

DETECT_TIMEOUT_S = 20
RENDER_TIMEOUT_S = 90

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
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=DETECT_TIMEOUT_S,
                              check=False)
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
            proc = subprocess.run(
                [mmdc.path, "-i", str(src), "-o", str(target), "-b", "white", "--quiet"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
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


def screenshot_manifest(*, project_id: str, commit: str | None, environment: str | None,
                        routes: list[str] | None = None) -> dict[str, Any]:
    """§29 screenshot contract. Capture is NOT-PROVEN in this build: a manifest records what must be
    captured (running app at an exact commit/environment, without secrets/PII) and stays NOT_PROVEN
    until an approved collector attaches the artefacts as evidence."""
    return {
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
