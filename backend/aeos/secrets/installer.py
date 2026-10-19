"""OpenBao user-level install from the official GitHub release (§07 bootstrap order 3, §08 remedy).

The plan is shown before anything happens (source, version, asset, checksum file, target, privilege,
cost, rollback) and an explicit confirmation is required. Integrity: SHA256 of the asset must match
``checksums.txt`` of the same release. Authenticity (GPG ``.gpgsig`` / sigstore bundle) is NOT
verified here — that limitation is printed in the plan and recorded as NOT-PROVEN. Never
system-wide, never elevated, no package manager, no PATH changes.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from aeos.core.config import Settings

GITHUB_API = "https://api.github.com/repos/openbao/openbao/releases"
RELEASES_PAGE = "https://github.com/openbao/openbao/releases"
ALLOWED_HOSTS = frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
MAX_BINARY_BYTES = 600 * 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?$")


class InstallError(RuntimeError):
    pass


def platform_target() -> tuple[str, str]:
    os_name = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise InstallError(f"unsupported CPU architecture {machine!r}")
    return os_name, arch


def asset_name(version: str, os_name: str, arch: str) -> str:
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"openbao_{version}_{os_name}_{arch}.{ext}"


def binary_name() -> str:
    return "bao.exe" if sys.platform == "win32" else "bao"


def install_dir(settings: Settings) -> Path:
    return settings.data_dir / "bin"


@dataclass
class Release:
    tag: str
    version: str
    assets: dict[str, str]
    html_url: str
    published_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _check_host(url: str) -> None:
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise InstallError(f"refusing download from non-official host {host!r}")


def manifest_pin(root: Path | None = None) -> str | None:
    """The owner-approved OpenBao version pin from the reviewed install manifest (§07), or None."""
    import yaml

    from aeos.core.paths import repo_root

    path = (root or repo_root()) / "install-manifest" / "manifest.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    for comp in data.get("components") or []:
        if isinstance(comp, dict) and comp.get("component_id") == "openbao":
            pin = comp.get("approved_pin")
            if isinstance(pin, dict):
                pin = pin.get("version")
            return str(pin).lstrip("v") if pin and _VERSION.match(str(pin).lstrip("v")) else None
    return None


def install_command(root: Path | None = None) -> str:
    """The ``aeos`` sub-command that installs OpenBao as the CLI accepts it right now: plain ``vault install``
    only when the manifest carries an approved pin; otherwise the owner must name a reviewed exact version
    (remedies must never suggest a command that is refused)."""
    return "vault install" if manifest_pin(root) else "vault install --version <X.Y.Z>"


def resolve_release(client: httpx.Client, version: str | None = None) -> Release:
    if version is not None and not _VERSION.match(version.lstrip("v")):
        raise InstallError(f"invalid version {version!r}")
    url = f"{GITHUB_API}/latest" if version is None else f"{GITHUB_API}/tags/v{version.lstrip('v')}"
    r = client.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=TIMEOUT)
    if r.status_code == 404:
        raise InstallError(f"OpenBao release {version or 'latest'} not found")
    r.raise_for_status()
    data = r.json()
    tag = str(data.get("tag_name") or "")
    ver = tag.lstrip("v")
    if not _VERSION.match(ver):
        raise InstallError(f"unexpected release tag {tag!r}")
    assets = {str(a["name"]): str(a["browser_download_url"]) for a in data.get("assets") or []
              if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")}
    return Release(tag=tag, version=ver, assets=assets, html_url=str(data.get("html_url") or RELEASES_PAGE),
                   published_at=data.get("published_at"))


def install_plan(settings: Settings, release: Release) -> dict[str, Any]:
    os_name, arch = platform_target()
    name = asset_name(release.version, os_name, arch)
    if name not in release.assets:
        raise InstallError(f"release {release.tag} has no asset {name}")
    if "checksums.txt" not in release.assets:
        raise InstallError(f"release {release.tag} has no checksums.txt")
    target = install_dir(settings) / binary_name()
    return {
        "operation": "install OpenBao server binary (user level)",
        "source": release.html_url, "version": release.version, "published_at": release.published_at,
        "asset": name, "asset_url": release.assets[name], "checksums_url": release.assets["checksums.txt"],
        "target": str(target), "privilege": "current user only (no admin/sudo, no PATH change, no service)",
        "cost": "none", "network": "github.com release download only",
        "verification": "SHA256 vs checksums.txt of the same release",
        "not_verified": "GPG/sigstore signature of checksums.txt (authenticity NOT-PROVEN)",
        "rollback": f"delete {target} (and {settings.data_dir / 'openbao'} if the server was initialised)",
        "evidence": "inventory observation openbao/installation with version + sha256",
    }


def _download(client: httpx.Client, url: str) -> bytes:
    _check_host(url)
    buf = io.BytesIO()
    with client.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as r:
        _check_host(str(r.url))
        r.raise_for_status()
        for chunk in r.iter_bytes():
            buf.write(chunk)
            if buf.tell() > MAX_DOWNLOAD_BYTES:
                raise InstallError("download exceeds the size limit")
    return buf.getvalue()


def parse_checksums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def _extract_binary(archive: bytes, asset: str) -> bytes:
    wanted = {"bao", "bao.exe"}
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if base in wanted and not info.is_dir():
                    if info.file_size > MAX_BINARY_BYTES:
                        raise InstallError("binary in archive exceeds the size limit")
                    return zf.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            for member in tf.getmembers():
                base = member.name.rsplit("/", 1)[-1]
                if base in wanted and member.isfile():
                    if member.size > MAX_BINARY_BYTES:
                        raise InstallError("binary in archive exceeds the size limit")
                    fh = tf.extractfile(member)
                    if fh is None:
                        break
                    return fh.read()
    raise InstallError("bao binary not found in the release archive")


def install(settings: Settings, client: httpx.Client, plan: dict[str, Any]) -> dict[str, Any]:
    checksums = parse_checksums(_download(client, plan["checksums_url"]).decode("utf-8", errors="replace"))
    expected = checksums.get(plan["asset"])
    if not expected:
        raise InstallError(f"{plan['asset']} is not listed in checksums.txt")
    archive = _download(client, plan["asset_url"])
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        raise InstallError(f"SHA256 mismatch for {plan['asset']}: expected {expected}, got {actual} - nothing installed")
    binary = _extract_binary(archive, plan["asset"])
    target = Path(plan["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_bytes(binary)
    if sys.platform != "win32":
        os.chmod(tmp, stat.S_IRWXU)  # owner-only rwx: user-level install
    tmp.replace(target)
    return {"installed": str(target), "version": plan["version"], "archive_sha256": actual,
            "binary_sha256": hashlib.sha256(binary).hexdigest(), "verification": "sha256-checksums",
            "signature_verified": False}
