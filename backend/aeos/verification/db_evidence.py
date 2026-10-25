"""DB/Data Engineer evidence (§16 "Uji migration/rollback/restore; credential migration dipisah runtime", §31
"migration identity ... backup/restore"; W6-046).

``db_credentials`` - the migration credential is separate from the runtime credential:

* the project's registered deployment targets (``deployment-target`` integrations, §31 G6/G8) must each name a
  migration credential *reference* (``config.migration_credential_ref``) and a runtime credential reference
  (``config.runtime_credential_ref``, else the integration's ``credential_ref``) that differ - references only,
  never values (a value-shaped entry FAILs);
* a static scan of the candidate's runtime configuration files (env files, settings, compose/k8s/helm, ``config/``)
  FAILs when one of them names a migration credential reference.

Owner configuration decides the references; nothing here invents them. With no deployment target registered the
check is NOT_APPLICABLE (local SQLite runtime: no runtime credential exists to separate) and says so; a target
without both references is NOT_PROVEN until the owner names them.

The migration round-trip with seeded backup/restore and rollback-with-data is ``packs/migration_roundtrip.py``.
"""

from __future__ import annotations

from typing import Any

from aeos.core.redaction import contains_secret
from aeos.verification import scope
from aeos.verification.steps import StepContext, StepEvidence, StepResult

DEPLOYMENT_TARGET = "deployment-target"
MIGRATION_REF_KEY = "migration_credential_ref"
RUNTIME_REF_KEY = "runtime_credential_ref"
RUNTIME_CONFIG_PATTERNS: tuple[str, ...] = (
    "**/.env*", "config/**", "**/settings*.py", "**/docker-compose*.y*ml", "**/compose*.y*ml", "**/k8s/**",
    "**/kubernetes/**", "**/helm/**", "**/charts/**", "**/deploy/**", "**/Procfile", "**/app.yaml", "**/fly.toml",
    "**/render.yaml", "**/*.tfvars",
)
DB_ROLES = frozenset({"db-data-engineer"})
MAX_SCAN_BYTES = 512 * 1024
MAX_SCAN_FILES = 500


def _targets(session: Any, project: Any) -> list[Any]:
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration

    return list(session.scalars(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.provider == DEPLOYMENT_TARGET)))


def runtime_config_files(snapshot: Any, limit: int = MAX_SCAN_FILES) -> list[str]:
    """Repository-relative runtime configuration files of the candidate snapshot (bounded)."""
    out: list[str] = []
    for p in sorted(snapshot.rglob("*")):
        if len(out) >= limit:
            break
        if not p.is_file() or "node_modules" in p.parts or ".git" in p.parts:
            continue
        rel = p.relative_to(snapshot).as_posix()
        if scope.first_match(rel, RUNTIME_CONFIG_PATTERNS, case_insensitive=True):
            out.append(rel)
    return out


def _ref(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def run(ctx: StepContext) -> StepResult | None:
    if not ctx.categories.get("migration") and ctx.role not in DB_ROLES:
        return None
    if ctx.session is None or ctx.project is None:
        return None
    rows = _targets(ctx.session, ctx.project)
    reasons: list[str] = []
    gaps: list[str] = []
    targets: list[dict[str, Any]] = []
    migration_refs: list[str] = []
    for r in rows:
        cfg = r.config or {}
        mig = _ref(cfg.get(MIGRATION_REF_KEY))
        rt = _ref(cfg.get(RUNTIME_REF_KEY)) or _ref(r.credential_ref)
        entry = {"integration_id": r.id, "purpose": r.purpose, "migration_ref": mig, "runtime_ref": rt}
        for label, ref in (("migration", mig), ("runtime", rt)):
            if ref is not None and contains_secret(ref):
                reasons.append(f"deployment target {r.purpose}: the {label} credential entry looks like a secret "
                               "value, not a reference")
                entry[f"{label}_ref"] = "[REDACTED]"
        if mig is None or rt is None:
            gaps.append(f"deployment target {r.purpose} does not name both {MIGRATION_REF_KEY} and a runtime "
                        f"credential reference")
        elif mig == rt:
            reasons.append(f"deployment target {r.purpose}: migration and runtime use the same credential reference "
                           f"({mig})")
        if mig is not None and not contains_secret(mig):
            migration_refs.append(mig)
        targets.append(entry)
    leaks: list[dict[str, Any]] = []
    scanned = 0
    for path in runtime_config_files(ctx.snapshot) if migration_refs else []:
        scanned += 1
        text = ctx.snapshot.joinpath(*path.split("/")).read_bytes()[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        for ref in migration_refs:
            if ref in text:
                leaks.append({"path": path, "ref": ref})
    for leak in leaks:
        reasons.append(f"runtime configuration {leak['path']} references the migration credential {leak['ref']}")
    content = {"suite": "db-credential-split", "targets": targets, "runtime_config_leaks": leaks,
               "runtime_config_files_scanned": scanned, "scanned_patterns": list(RUNTIME_CONFIG_PATTERNS),
               "rule": "migration credential reference != runtime credential reference; runtime config never names "
                       "the migration credential"}
    ev = StepEvidence("CONFIG_SNAPSHOT", f"Migration/runtime credential split: {len(rows)} deployment target(s), "
                                         f"{len(leaks)} runtime-config reference(s) to a migration credential",
                      content, "E1", "db_credentials")
    details = {"targets": targets, "leaks": leaks[:20]}
    if reasons:
        return StepResult("db_credentials", "FAIL", reasons[0], details, [ev], reasons=reasons)
    if not rows:
        msg = ("migration/runtime credential split NOT_APPLICABLE: no deployment target is registered for this "
               "project (local runtime only; nothing to separate yet)")
        return StepResult("db_credentials", "NOT_APPLICABLE", msg, details, [ev], not_proven=[msg])
    if gaps:
        msg = "; ".join(gaps[:3]) + " - credential split NOT-PROVEN until the owner names both references"
        return StepResult("db_credentials", "NOT_PROVEN", msg, details, [ev], not_proven=[msg])
    return StepResult("db_credentials", "PASS", f"{len(rows)} deployment target(s) name distinct migration and runtime "
                                                "credential references; no runtime config names a migration credential",
                      details, [ev])
