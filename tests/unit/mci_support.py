"""Isolated MCI reference fixtures (§02, §24, §34).

The live ``policy/mci-reference.yaml`` is the OWNER's file: once the owner supplies MCI_V2 it says VERIFIED, so a
test that asserts the live UNAVAILABLE/UNVERIFIED state would turn CI red exactly when the owner does the right
thing. The canonical document itself lives only on the owner's machine (``<data dir>/policy/canonical/`` or the
aeos.yaml ``mci_source_path``; the repository is public). Behaviour tests therefore build their own repository root
(a copy of the real top-level ``policy/*.yaml`` with a reference written by the test) AND their own local canonical
directory under ``tmp_path`` - they never read the owner's data dir or aeos.yaml. Only state-independent facts are
asserted against the live reference (compliance NOT-PROVEN, pack mappings UNVERIFIED without a decision,
thresholds None).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from aeos.core.hashing import file_sha256
from aeos.core.paths import repo_root

UNAVAILABLE = ("mci:\n  name: MCI_V2_SOURCE_OF_TRUTH\n  status: UNAVAILABLE\n  version: null\n  content_hash: null\n"
               "  location: null\n  verified_at: null\n  note: 'test fixture: not supplied'\npacks: [ARCH, QA]\n")
SOURCE_NAME = "MCI_V2_SOURCE_OF_TRUTH.md"
LOCATION = f"local:{SOURCE_NAME}"
SOURCE_REF = f"mci:{SOURCE_NAME}"  # the logical context-pack ref of the local source (never a host path)


def fake_repo(tmp_path: Path, reference: str = UNAVAILABLE, *, name: str = "repo") -> Path:
    """A repository root with the real top-level policy files and *reference* as ``policy/mci-reference.yaml``."""
    root = tmp_path / name
    (root / "policy").mkdir(parents=True)
    for p in sorted((repo_root() / "policy").glob("*.yaml")):
        shutil.copyfile(p, root / "policy" / p.name)
    (root / "policy" / "mci-reference.yaml").write_text(reference, encoding="utf-8")
    return root


def local_dir(tmp_path: Path) -> Path:
    """The test's stand-in for ``<data dir>/policy/canonical`` (outside the fake repository)."""
    return tmp_path / "local-data" / "policy" / "canonical"


def write_source(directory: Path, text: str = "# MCI_V2 test placeholder (not the real document)\n",
                 name: str = SOURCE_NAME) -> str:
    """Write a placeholder MCI source into *directory* and return its sha256 (``sha256:<hex>``)."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return file_sha256(path)


def write_mapping(directory: Path, text: str) -> Path:
    """Write the owner's local per-pack mapping (``mci-pack-mapping.yaml``) into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mci-pack-mapping.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def verified_reference(content_hash: str, *, location: str = LOCATION, packs: str = "packs: [ARCH, QA]\n") -> str:
    return ("mci:\n  name: MCI_V2_SOURCE_OF_TRUTH\n  status: VERIFIED\n  version: 'test-2'\n"
            f"  content_hash: '{content_hash}'\n  location: {location}\n  verified_at: '2026-10-26'\n" + packs)


def isolate(monkeypatch, root: Path, local: Path | None = None, override: Path | None = None) -> Path:
    """Point the policy module (snapshot) and the knowledge policy context at the fake repository *root* and the
    local canonical directory *local* (default ``local_dir(root.parent)``); *override* plays aeos.yaml
    ``mci_source_path``. Returns the local directory."""
    from aeos.knowledge import packs
    from aeos.policy import snapshot

    local = local if local is not None else local_dir(root.parent)
    monkeypatch.setattr(snapshot, "repo_root", lambda: root)
    monkeypatch.setattr(packs, "repo_root", lambda: root)
    monkeypatch.setattr(snapshot, "mci_source_settings", lambda settings=None: (local, override))
    return local
