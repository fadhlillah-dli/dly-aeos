"""Owner-gated LIVE check of the DOCX -> Google Docs import fidelity (master §29: "fidelity, ukuran,
heading/table/link harus diuji"; §51 P2).

It writes ONE document into a Drive folder the owner names, through the publisher's real Google adapter with
the owner's own consented OAuth token (``aeos publish google-connect``, scope drive.file), then reads it back with
``documents.get`` and requires CONSISTENT headings, tables, links and embedded image. Nothing runs unless the
owner opts in explicitly:

    DLY_AEOS_NETWORK_TESTS=1
    DLY_AEOS_GOOGLE_FIDELITY_FOLDER=<Drive folder id created by AEOS for tests>

The document is left in that folder (AEOS has no delete operation); its URL is printed for the owner.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.network, pytest.mark.integration]

FOLDER = os.environ.get("DLY_AEOS_GOOGLE_FIDELITY_FOLDER", "")
MD = ("# Fidelity Check\n\nA [link](https://example.org/aeos-fidelity).\n\n## Table\n\n| A | B |\n|---|---|\n"
      "| 1 | 2 |\n\n![Pixel](visual:px)\n\n### End\n\ndone\n")


def _png(path: Path) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))
    return path


@pytest.mark.skipif(os.environ.get("DLY_AEOS_NETWORK_TESTS") != "1" or not FOLDER,
                    reason="owner opt-in: DLY_AEOS_NETWORK_TESTS=1 and DLY_AEOS_GOOGLE_FIDELITY_FOLDER=<folder id>")
def test_live_docx_import_keeps_headings_tables_links_and_images(tmp_path):
    from aeos.core.errors import AeosError
    from aeos.docs_engine.docx import markdown_to_docx
    from aeos.publisher.providers import google as gg
    from aeos.publisher.providers.base import OpContext, ProviderBlocked

    png = _png(tmp_path / "px.png")
    docx = markdown_to_docx(MD, title="AEOS fidelity check", logical_id="fidelity-check",
                            image_resolver=lambda key: png if key == "px" else None)
    try:
        prov = gg.GoogleDriveDocsProvider(root_folder_id=FOLDER)
        prov.auth.header()
    except (AeosError, ProviderBlocked) as exc:  # no consented/refreshable token in the secret store
        pytest.skip(f"Google OAuth not connected: {getattr(exc, 'code', type(exc).__name__)}")
    ctx = OpContext(operation_id="live", publication_id="live-fidelity", project_id="fidelity", project_key="fidelity",
                    project_name="Fidelity", domain="PERSONAL", provider="google-drive", operation="create_doc",
                    logical_id="fidelity-check", idempotency_key="sha256:" + "f" * 64, title="AEOS fidelity check",
                    target={}, payload={}, parent_external_id=FOLDER, attachments={"document.docx": docx})
    try:
        res = prov.execute(ctx)
    finally:
        prov.close()
    print(f"fidelity document: {res.external_url}")
    fid = res.response["fidelity"]
    assert fid["status"] == "CONSISTENT", fid
