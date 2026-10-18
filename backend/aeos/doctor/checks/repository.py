"""§08 Repository: repo/path/base commit, branch/dirty state, lockfiles, hooks and configs that load."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import file_fingerprint
from aeos.doctor.model import CheckContext, Outcome, Remedy, npm_cmd, shell_chain
from aeos.inventory import scope as scope_reader
from aeos.inventory.probes import github_slug_from_remote, strip_url_credentials


def _git(ctx: CheckContext, *args: str) -> str | None:
    git = ctx.runner.which("git")
    if not git:
        return None
    rr = ctx.runner.run([git, "-C", str(ctx.repo), *args], timeout=10.0, cwd=ctx.repo)
    return rr.stdout.strip() if rr.ok else None


@check("repo.root", "REPOSITORY", "Root repository platform", component_id="dly-aeos-platform-repo",
       ttl_seconds=86400, severity_fail="HIGH")
def repo_root_check(ctx: CheckContext) -> Outcome:
    markers = {"pyproject.toml": (ctx.repo / "pyproject.toml").is_file(),
               "backend/aeos": (ctx.repo / "backend" / "aeos").is_dir(),
               "AGENTS.md": (ctx.repo / "AGENTS.md").is_file(),
               "install-manifest": (ctx.repo / "install-manifest").is_dir()}
    missing = [k for k, v in markers.items() if not v]
    if missing:
        return Outcome("FAILED", f"Root repo tidak lengkap: {', '.join(missing)} tidak ada.", evidence=markers,
                       error_code="REPO_INCOMPLETE", target_identity="<repo>")
    return Outcome("VERIFIED", "Root repository platform ditemukan (pyproject, backend, AGENTS.md, manifest).",
                   evidence=markers, target_identity="<repo>")


@check("repo.origin", "REPOSITORY", "Remote origin = repo platform", component_id="dly-aeos-platform-repo",
       ttl_seconds=3600, invalidators=("repo:origin",), severity_fail="MEDIUM", observation="configuration")
def repo_origin(ctx: CheckContext) -> Outcome:
    expected = ctx.settings.platform_repo_slug
    if not ctx.runner.which("git"):
        return Outcome("UNKNOWN", "git tidak ditemukan; remote tidak dapat dibaca.", error_code="GIT_MISSING")
    remote = _git(ctx, "remote", "get-url", "origin")
    if remote is None:
        return Outcome("MISSING", "Remote 'origin' belum dikonfigurasi.", evidence={"expected": expected},
                       error_code="NO_ORIGIN", remedy=Remedy(
                           operation="Tambahkan remote origin ke repo platform yang dipilih eksplisit",
                           command=f"git remote add origin https://github.com/{expected}.git",
                           target=f"github:{expected}", rollback="git remote remove origin",
                           evidence_to_collect="git remote get-url origin"))
    slug = github_slug_from_remote(remote)
    evidence = {"remote": strip_url_credentials(remote), "observed": slug, "expected": expected}
    if slug and slug.lower() == expected.lower():
        return Outcome("VERIFIED", f"Remote origin cocok dengan github:{expected}.", evidence=evidence,
                       target_identity=f"github:{slug}")
    return Outcome("FAILED", f"Remote origin ({slug or 'bukan GitHub'}) tidak sama dengan platform_repo_slug {expected}; "
                   "repo bernama sama tidak dianggap target yang sama (§37).", evidence=evidence,
                   error_code="ORIGIN_MISMATCH", target_identity=f"github:{slug}" if slug else None)


@check("repo.state", "REPOSITORY", "Branch, base commit dan status dirty", component_id="dly-aeos-platform-repo",
       ttl_seconds=900, invalidators=("repo:head",))
def repo_state(ctx: CheckContext) -> Outcome:
    if not ctx.runner.which("git"):
        return Outcome("UNKNOWN", "git tidak ditemukan.", error_code="GIT_MISSING")
    head = _git(ctx, "rev-parse", "HEAD")
    if head is None:
        return Outcome("FAILED", "Bukan git working tree atau HEAD tidak dapat dibaca.", error_code="NOT_A_REPO")
    branch = _git(ctx, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(ctx, "status", "--porcelain") or ""
    dirty = len([ln for ln in porcelain.splitlines() if ln.strip()])
    evidence = {"branch": branch, "base_commit": head, "dirty_files": dirty}
    summary = f"Branch {branch} @ {head[:12]}; " + (f"{dirty} file belum di-commit." if dirty else "bersih.")
    return Outcome("VERIFIED", summary, evidence=evidence, severity="LOW" if dirty else "INFO",
                   component_version=head[:12], target_identity=f"{branch}@{head[:12]}")


@check("repo.lockfiles", "REPOSITORY", "Lockfile dependency", component_id="dly-aeos-platform-repo",
       ttl_seconds=3600, invalidators=("repo:lockfile",), ok={"VERIFIED"},
       fingerprint=file_fingerprint("frontend/package-lock.json"))
def repo_lockfiles(ctx: CheckContext) -> Outcome:
    npm = (ctx.repo / "frontend" / "package-lock.json").is_file()
    py = (ctx.repo / "requirements.lock").is_file()
    evidence = {"frontend/package-lock.json": npm, "requirements.lock": py}
    if npm and py:
        return Outcome("VERIFIED", "Lockfile frontend dan Python tersedia.", evidence=evidence)
    missing = [k for k, v in evidence.items() if not v]
    # ADR-0093: one universal lock compiled from pyproject (no shell redirection: PowerShell 5.1 would write
    # UTF-16); the frontend lock comes from npm in frontend/.
    cmd = ("uv pip compile pyproject.toml --universal --extra dev -o requirements.lock" if not py
           else shell_chain("cd frontend", npm_cmd("install --package-lock-only --no-audit --no-fund")))
    return Outcome("MISSING", f"Lockfile belum ada: {', '.join(missing)} (versi hanya berupa range).", evidence=evidence,
                   error_code="LOCKFILE_MISSING", remedy=Remedy(
                       operation="Buat lockfile dari lingkungan yang sudah diuji lalu commit lewat PR",
                       command=cmd, target=", ".join(missing), privilege="pengguna lokal",
                       rollback="hapus file lockfile baru", evidence_to_collect="hash lockfile di commit"))


@check("repo.hooks", "REPOSITORY", "Hooks dan konfigurasi yang akan dimuat", component_id="dly-aeos-platform-repo",
       ttl_seconds=3600)
def repo_hooks(ctx: CheckContext) -> Outcome:
    hooks_path = _git(ctx, "rev-parse", "--git-path", "hooks")
    custom = _git(ctx, "config", "--get", "core.hooksPath")
    active: list[str] = []
    if hooks_path:
        hp = Path(hooks_path)
        hp = hp if hp.is_absolute() else ctx.repo / hp
        if hp.is_dir():
            active = sorted(p.name for p in hp.iterdir() if p.is_file() and not p.name.endswith(".sample"))
    reads, servers = scope_reader.read_mcp_configs(ctx.repo)
    project_configs = [r.source for r in reads if r.status == "READ"]
    hooks_cfg = [r.source for r in reads if r.status == "READ" and (r.detail or {}).get("hooks_configured")]
    evidence: dict[str, Any] = {"git_hooks": active, "core_hooks_path": custom, "project_agent_configs": project_configs,
                                "mcp_servers": [s["name"] for s in servers], "agent_hooks_in": hooks_cfg}
    risky = bool(project_configs or custom)
    summary = (f"{len(active)} git hook aktif; konfigurasi agent/MCP proyek: {', '.join(project_configs) or 'tidak ada'}.")
    if risky:
        summary += " Claude non-bare akan memuatnya; bridge wajib berjalan dari workspace pengawas (§12)."
    return Outcome("VERIFIED", summary, evidence=evidence, severity="MEDIUM" if risky else "INFO")


@check("repo.install_manifest", "REPOSITORY", "InstallManifest valid dan termuat", component_id="install-manifest",
       ttl_seconds=3600, invalidators=("manifest",), ok={"VERIFIED", "CONFIGURED"}, severity_fail="HIGH",
       fingerprint=file_fingerprint("install-manifest/manifest.yaml"))
def repo_install_manifest(ctx: CheckContext) -> Outcome:
    from aeos.core.errors import ValidationFailed
    from aeos.doctor.model import aeos_cmd
    from aeos.inventory.manifest import current_manifest, load_manifest

    try:
        load = load_manifest(root=ctx.repo)
    except ValidationFailed as exc:
        errs = (exc.details or {}).get("errors") or []
        return Outcome("FAILED", f"InstallManifest tidak valid: {exc.message}", evidence={"errors": errs[:20]},
                       error_code=exc.code, remedy=Remedy(
                           operation="Perbaiki manifest lewat PR yang direview (protected path)",
                           target="install-manifest/manifest.yaml", privilege="reviewer manusia",
                           evidence_to_collect="validasi schema + semantik lulus"))
    evidence: dict[str, Any] = {"components": len(load.data.get("components") or []), "content_hash": load.content_hash,
                                "warnings": load.warnings}
    if ctx.session is None:
        return Outcome("CONFIGURED", f"Manifest valid ({evidence['components']} komponen); database belum ada sehingga "
                       "belum dimuat.", evidence=evidence, component_version=load.content_hash[:19])
    rec = current_manifest(ctx.session)
    evidence["loaded_hash"] = rec.content_hash if rec else None
    if rec is None or rec.content_hash != load.content_hash:
        return Outcome("CONFIGURED", "Manifest valid tetapi versi file berbeda dari yang termuat; muat ulang setelah review.",
                       evidence=evidence, severity="MEDIUM", component_version=load.content_hash[:19],
                       remedy=Remedy(operation="Muat manifest ke katalog desain", command=aeos_cmd("seed"),
                                     source="install-manifest/manifest.yaml", privilege="pengguna lokal",
                                     rollback="checkout manifest sebelumnya lalu seed",
                                     evidence_to_collect="hash termuat = hash file"))
    return Outcome("VERIFIED", f"Manifest valid dan termuat ({evidence['components']} komponen).", evidence=evidence,
                   component_version=load.content_hash[:19])
