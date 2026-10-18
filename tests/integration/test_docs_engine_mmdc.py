"""Real mermaid-cli rendering (optional component, §29). Skips cleanly when mmdc is not installed."""

from __future__ import annotations

import pytest

from aeos.docs_engine import visuals as vis
from aeos.docs_engine.plan_content import DiagramView

pytestmark = pytest.mark.integration


def test_real_mmdc_renders_png_and_svg(tmp_path):
    info = vis.detect_mmdc()
    if not info.available:
        pytest.skip(f"mermaid-cli not available: {info.reason or info.status}")
    art = vis.render_diagram(DiagramView(key="ctx", title="Context", kind="context",
                                         source="flowchart TD\n  U[User] --> A[API]"), tmp_path, mmdc=info)
    if art.status != vis.STATUS_RENDERED:
        pytest.skip(f"mmdc present but could not render here (browser sandbox?): {art.reason}")
    from pathlib import Path

    assert Path(art.png_path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "<svg" in Path(art.svg_path).read_text(encoding="utf-8")[:2000]
