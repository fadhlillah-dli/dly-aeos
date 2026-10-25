"""§08 Evidence/review: collector/verifier independence, artifact permissions, trace redaction, acceptance suite,
acceptance invalidation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aeos.core.enums import Domain
from aeos.core.redaction import REDACTED, redact, redact_text
from aeos.doctor.checks import check
from aeos.doctor.checks._common import module_present
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd

# Well-known SIDs that must never hold write access to a private data partition (locale-independent).
BROAD_SIDS = {"S-1-1-0": "Everyone", "S-1-5-32-545": "BUILTIN\\Users", "S-1-5-11": "Authenticated Users",
              "S-1-5-7": "Anonymous", "S-1-5-32-546": "BUILTIN\\Guests"}
WRITE_RIGHTS = ("FullControl", "Modify", "Write", "CreateFiles", "AppendData", "WriteData", "ChangePermissions",
                "TakeOwnership")
ACL_SCRIPT = (
    "$ErrorActionPreference='Stop'; $acl = Get-Acl -LiteralPath $env:AEOS_ACL_PATH; "
    "@($acl.Access | ForEach-Object { $sid = try { $_.IdentityReference.Translate("
    "[System.Security.Principal.SecurityIdentifier]).Value } catch { [string]$_.IdentityReference }; "
    "[pscustomobject]@{sid=$sid; rights=[string]$_.FileSystemRights; type=[string]$_.AccessControlType; "
    "inherited=[bool]$_.IsInherited} }) | ConvertTo-Json -Compress"
)


def _windows() -> bool:
    # One platform source for every check (sys.platform), so remedies match the OS they are shown on.
    return sys.platform.startswith("win")


# Generic/specific access-mask bits that allow writing (Get-Acl prints inherit-only generic ACEs as a number,
# e.g. "268435456" = GENERIC_ALL, "-536805376" = GENERIC_EXECUTE|GENERIC_READ|...).
WRITE_MASK = (0x10000000 | 0x40000000 | 0x00000002 | 0x00000004 | 0x00040000 | 0x00080000)


def _grants_write(rights: str) -> bool:
    text = rights.strip()
    try:
        mask = int(text)
    except ValueError:
        return any(w in text for w in WRITE_RIGHTS)
    return bool((mask & 0xFFFFFFFF) & WRITE_MASK)


def windows_acl(ctx: CheckContext, path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """(broad write ACEs, error) for *path* via Get-Acl with SIDs (no localized group names). ``None`` = unread."""
    ps = ctx.runner.which("powershell") or ctx.runner.which("pwsh")
    if not ps:
        return None, "powershell tidak ditemukan"
    env = {**dict(ctx.env), "AEOS_ACL_PATH": str(path)}
    rr = ctx.runner.run([ps, "-NoProfile", "-NonInteractive", "-Command", ACL_SCRIPT], timeout=20.0, env=env,
                        cwd=ctx.probe_cwd)
    if not rr.ok:
        return None, f"Get-Acl gagal ({rr.error or rr.returncode})"
    try:
        data = json.loads(rr.stdout or "[]")
    except json.JSONDecodeError:
        return None, "output Get-Acl tidak dapat dibaca"
    entries = data if isinstance(data, list) else [data]
    broad = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rights = str(e.get("rights") or "")
        if str(e.get("type")) == "Allow" and e.get("sid") in BROAD_SIDS and _grants_write(rights):
            broad.append({"sid": e["sid"], "principal": BROAD_SIDS[e["sid"]], "rights": rights,
                          "inherited": bool(e.get("inherited"))})
    return broad, None


@check("evidence.acceptance_suite", "EVIDENCE_REVIEW", "Suite acceptance tepercaya", component_id="verification-review",
       ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"}, severity_fail="MEDIUM")
def evidence_acceptance_suite(ctx: CheckContext) -> Outcome:
    d = ctx.repo / "tests" / "acceptance"
    if not d.is_dir():
        return Outcome("MISSING", "tests/acceptance tidak ada.", error_code="ACCEPTANCE_SUITE_MISSING")
    files = sorted(p.name for p in d.glob("test_*.py"))
    evidence = {"path": "<repo>/tests/acceptance", "test_files": files, "protected": True, "executed_here": False}
    if not files:
        return Outcome("MISSING", "Suite acceptance tepercaya belum berisi test; acceptance tidak dapat dibuktikan.",
                       evidence=evidence, error_code="ACCEPTANCE_SUITE_EMPTY", remedy=Remedy(
                           operation="Tambahkan test acceptance lewat PR yang direview manusia (protected path)",
                           target="tests/acceptance", privilege="reviewer manusia",
                           evidence_to_collect="evidence.acceptance_suite CONFIGURED"))
    return Outcome("CONFIGURED", f"{len(files)} file test acceptance (read-only bagi worker); belum dijalankan oleh Doctor.",
                   evidence=evidence)


@check("evidence.dir_permissions", "EVIDENCE_REVIEW", "Izin direktori evidence per domain", component_id="evidence-store",
       ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH")
def evidence_dir_permissions(ctx: CheckContext) -> Outcome:
    layout = ctx.settings.layout
    roots = {d.value: layout.domain_root(d) for d in Domain}
    missing = [k for k, p in roots.items() if not p.is_dir()]
    if missing:
        return Outcome("MISSING", f"Partisi data domain belum ada: {', '.join(missing)}.", error_code="EVIDENCE_DIR_MISSING",
                       remedy=Remedy(operation="Buat layout data dir", command=aeos_cmd("db upgrade"), target="<data>",
                                     evidence_to_collect="evidence.dir_permissions"))
    evidence: dict[str, Any] = {"partitions": {k: f"<data>/{k.lower()}" for k in roots}, "separate_partitions": True}
    for k, p in roots.items():
        test = p / ".aeos-perm-probe"
        try:
            test.write_bytes(b"ok")
            test.unlink()
        except OSError as exc:
            return Outcome("FAILED", f"Partisi {k} tidak dapat ditulis ({type(exc).__name__}).", evidence=evidence,
                           error_code="EVIDENCE_DIR_NOT_WRITABLE")
        if not _windows() and p.stat().st_mode & 0o007:
            evidence.setdefault("other_access", []).append(k)
    if evidence.get("other_access"):
        return Outcome("FAILED", f"Partisi dapat diakses pengguna lain: {', '.join(evidence['other_access'])}.",
                       evidence=evidence, error_code="EVIDENCE_DIR_TOO_OPEN", remedy=Remedy(
                           operation="Batasi izin partisi evidence", command="chmod -R o-rwx <data dir>",
                           privilege="pengguna lokal", evidence_to_collect="tanpa akses other"))
    if _windows():
        broad: list[dict[str, Any]] = []
        errors: list[str] = []
        for k, p in roots.items():
            found, err = windows_acl(ctx, p)
            if err:
                errors.append(f"{k}: {err}")
            for f in found or []:
                broad.append({"partition": k, **f})
        evidence["acl_checked"] = not errors
        evidence["acl_errors"] = errors
        evidence["broad_write_aces"] = broad
        if broad:
            return Outcome("FAILED", "Partisi evidence dapat ditulis grup luas (" + ", ".join(
                sorted({b["principal"] for b in broad})) + ").", evidence=evidence, error_code="EVIDENCE_DIR_ACL_TOO_OPEN",
                remedy=Remedy(operation="Hapus hak tulis grup luas dari data dir (pertahankan pemilik, SYSTEM, "
                              "Administrators)", command="icacls <data dir> /remove:g *S-1-1-0 *S-1-5-32-545 *S-1-5-11 /T",
                              privilege="pengguna lokal/admin OS", rollback="icacls <data dir> /reset /T",
                              evidence_to_collect="evidence.dir_permissions VERIFIED"))
        if errors:
            return Outcome("CONFIGURED", "Partisi PERSONAL/OFFICE terpisah dan dapat ditulis; ACL Windows tidak dapat "
                           "dibaca (" + "; ".join(errors[:2]) + ").", evidence=evidence)
        return Outcome("VERIFIED", "Partisi PERSONAL/OFFICE terpisah, dapat ditulis; ACL Windows tanpa hak tulis "
                       "Everyone/Users/Authenticated Users.", evidence=evidence)
    return Outcome("VERIFIED", "Partisi PERSONAL/OFFICE terpisah, dapat ditulis, tanpa akses pengguna lain.",
                   evidence=evidence)


def verifier_independence_self_test(repo: Path) -> dict[str, Any]:
    """Controlled self-test of the verifier's trust separation (no worker run, no network):

    * a synthetic candidate ``backend/`` that ships ``pytest`` and ``json`` shadow modules is detected as shadowing
      the trusted harness/stdlib (the candidate can never replace the grader);
    * a candidate inside the trusted controller checkout is refused (TRUST_ZONE_OVERLAP), a sibling is not;
    * the trusted acceptance suite is read from the controller checkout (outside the candidate) and hashed."""
    import tempfile

    from aeos.verification.suite import trusted_suite_hash
    from aeos.verification.verifier import _Blocked, _Verifier, shadowed_modules

    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="aeos-doctor-verifier-") as tmp:
        cand = Path(tmp) / "candidate" / "backend"
        (cand / "pytest").mkdir(parents=True)
        (cand / "pytest" / "__init__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (cand / "json.py").write_text("", encoding="utf-8")
        shadows = shadowed_modules(cand, repo / "backend")
        out["shadow_detected"] = {"pytest", "json"} <= set(shadows)
        trusted = repo.resolve()
        try:
            _Verifier._check_separation(trusted / "inner-candidate", trusted)
            out["overlap_refused"] = False
        except _Blocked as exc:
            out["overlap_refused"] = exc.code == "TRUST_ZONE_OVERLAP"
        try:
            _Verifier._check_separation(Path(tmp).resolve() / "candidate", trusted)
            out["sibling_allowed"] = True
        except _Blocked:
            out["sibling_allowed"] = False
    try:
        out["trusted_suite_hash"] = trusted_suite_hash(repo)
    except Exception as exc:  # no trusted suite: independence cannot be shown
        out["trusted_suite_hash"] = None
        out["suite_error"] = type(exc).__name__
    out["ok"] = bool(out["shadow_detected"] and out["overlap_refused"] and out["sibling_allowed"]
                     and out["trusted_suite_hash"])
    return out


def _verifier_runner_view(ctx: CheckContext) -> dict[str, Any]:
    """Which runner the verifier executes candidate code in (§20 isolated runner). CONTAINER only on a host with
    a proven container boundary + the verifier image; otherwise WEAK, whose residual risk is reported, never
    hidden. Fail closed: any error is reported as WEAK with the reason."""
    domain = Domain.PERSONAL.value  # OFFICE is always BLOCKED by the worker runtime; verifier re-checks per domain
    if ctx.session is None:  # bootstrap layer without a database: the runner state cannot be read
        return {"isolation": "WEAK", "runner": "process", "reasons": ["no database: verifier runner state not readable"],
                "residual_risks": ["verifier isolation NOT-PROVEN: runner state could not be read (fail closed)"]}
    try:
        from aeos.verification import facade as verification

        st = verification.verifier_runner_status(ctx.session, domain=domain)
    except Exception as exc:
        return {"isolation": "WEAK", "runner": "process", "reasons": [f"verifier runner unreadable ({type(exc).__name__})"],
                "residual_risks": ["verifier isolation NOT-PROVEN: runner state could not be read (fail closed)"]}
    isolation = str(st.get("isolation") or "WEAK")
    view: dict[str, Any] = {"isolation": isolation, "runner": st.get("runner"), "domain": domain,
                            "reasons": st.get("reasons") or [], "remedies": st.get("remedies") or [],
                            "image": st.get("image"), "containment": st.get("containment") or {}}
    if isolation != "CONTAINER":
        view["residual_risks"] = [
            "Verifier isolation WEAK: candidate code the verifier executes (trusted suite, candidate tests, "
            "migration round-trip) runs as the controller's OS user with a scrubbed, redirected environment; no "
            "container boundary proven on this host, so filesystem/egress are not OS-enforced (ADR-0051). Writes "
            "to the controller checkout/interpreter and the DB trust tables during a verification are detected and "
            "QUARANTINED; reads by absolute path or the OS credential API are not prevented.",
            *([f"verifier container runner not available: {r}" for r in (st.get('reasons') or [])[:3]]),
        ]
    return view


@check("evidence.verifier", "EVIDENCE_REVIEW", "Verifier/collector independen tersedia",
       component_id="verification-review", ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH",
       level="E1", functional_level="E2",
       description="Tanpa --functional: modul collector/acceptance ada dan runner isolasi dilaporkan (CONTAINER "
                   "bila boundary terbukti, selainnya WEAK + residual risk). Dengan --functional: uji mandiri "
                   "pemisahan zona kepercayaan (shadow module kandidat terdeteksi, kandidat di dalam controller "
                   "ditolak, suite tepercaya dibaca dari luar kandidat).")
def evidence_verifier(ctx: CheckContext) -> Outcome:
    present, placeholder = module_present("aeos.verification.collector")
    acc, acc_ph = module_present("aeos.verification.acceptance")
    evidence: dict[str, Any] = {"collector": present and not placeholder, "acceptance": acc and not acc_ph}
    if not evidence["collector"] or not evidence["acceptance"]:
        return Outcome("MISSING", "Verifier/collector independen belum diimplementasikan; hasil worker tetap "
                       "REVIEW/NOT-PROVEN.", evidence=evidence, error_code="VERIFIER_NOT_IMPLEMENTED")
    runner_view = _verifier_runner_view(ctx)
    evidence["verifier_runner"] = runner_view
    risks = list(runner_view.get("residual_risks") or [])
    if risks:
        evidence["residual_risks"] = risks
    iso = runner_view["isolation"]
    # Honest wording: the WEAK runner's residual risk is stated, not owner-accepted (no Decision records an
    # acceptance of it); OFFICE and runs produced in a container are BLOCKED instead of using it.
    iso_note = (f" Runner candidate code: isolasi {iso}"
                + ("" if iso == "CONTAINER" else " (WEAK: belum terisolasi, residual risk dinyatakan; OFFICE dan "
                   "run dari sandbox container diblokir; buktikan containment container + build image verifier "
                   "untuk CONTAINER)") + ".")
    if not ctx.functional:
        return Outcome("CONFIGURED", "Collector dan acceptance tersedia; independensi diuji dengan --functional "
                       "(uji mandiri pemisahan zona kepercayaan)." + iso_note, evidence=evidence)
    try:
        test = verifier_independence_self_test(ctx.repo)
    except Exception as exc:  # never green on an error
        return Outcome("UNKNOWN", f"Uji mandiri independensi verifier gagal dijalankan ({type(exc).__name__}).",
                       evidence=evidence, error_code="VERIFIER_SELF_TEST_ERROR")
    evidence["independence_self_test"] = test
    if not test["ok"]:
        return Outcome("FAILED", "Pemisahan kandidat/verifier tidak terbukti: " + ", ".join(
            k for k in ("shadow_detected", "overlap_refused", "sibling_allowed") if not test.get(k))
            + ("" if test.get("trusted_suite_hash") else " (suite tepercaya tidak terbaca)") + ".",
            evidence=evidence, error_code="VERIFIER_NOT_INDEPENDENT")
    return Outcome("VERIFIED", "Independensi verifier: shadow module kandidat terdeteksi, kandidat di dalam controller "
                   "ditolak, suite tepercaya di-hash dari checkout controller (skenario: uji mandiri, bukan run worker)."
                   + iso_note, evidence=evidence)


@check("evidence.trace_redaction", "EVIDENCE_REVIEW", "Redaksi trace/log berfungsi", ttl_seconds=86400,
       severity_fail="CRITICAL")
def evidence_trace_redaction(ctx: CheckContext) -> Outcome:
    # Synthetic credential shapes built at runtime (never literal tokens in source).
    samples = {
        "openrouter": "sk-or-v1-" + "a1b2c3d4" * 6,
        "github": "ghp_" + "Z9y8X7w6" * 5,
        "bearer": "Bearer " + "q" * 24,
        "assignment": "password=" + "hunter2hunter2",
    }
    leaks = [k for k, v in samples.items() if redact_text(f"trace {v} end") == f"trace {v} end"]
    nested = redact({"headers": {"authorization": "x" * 30}, "note": samples["github"]})
    nested_ok = nested["headers"]["authorization"] == REDACTED and REDACTED in nested["note"]
    evidence = {"patterns_tested": sorted(samples), "leaks": leaks, "nested_mapping_redacted": nested_ok}
    if leaks or not nested_ok:
        return Outcome("FAILED", f"Redaksi gagal untuk: {', '.join(leaks) or 'mapping bersarang'}.", evidence=evidence,
                       error_code="REDACTION_FAILED")
    return Outcome("VERIFIED", "Redaksi pola credential dan kunci sensitif berfungsi (uji sintetis).", evidence=evidence)


MAX_ACCEPTANCES_CHECKED = 200


@check("evidence.acceptance_invalidation", "EVIDENCE_REVIEW", "Acceptance usang tidak lagi berlaku (PERSONAL)",
       component_id="verification-review", needs_db=True, ttl_seconds=900, ok={"VERIFIED", "NOT_APPLICABLE"},
       invalidators=("repo:head", "manifest", "config:policy"), severity_fail="HIGH", scope={"domain": "PERSONAL"},
       description="Setiap AcceptanceRecord ACCEPTED (PERSONAL) dibandingkan dengan identitas saat ini (head run/PR, "
                   "hash PlanVersion, konfigurasi verifier/grader). Acceptance yang ikatannya berubah harus DITOLAK "
                   "oleh gate konsumen (verification.acceptance_is_valid, dipakai PR/merge gate): FAILED bila gate "
                   "masih menerimanya (§34). Acceptance usang yang ditolak saat dipakai bukan kegagalan; recheck "
                   "mencatat invalidasinya. OFFICE tidak diprobe (delivery OFFICE default deny).")
def evidence_acceptance_invalidation(ctx: CheckContext) -> Outcome:
    from sqlalchemy import select

    try:
        from aeos.models.evidence import AcceptanceRecord
        from aeos.verification.acceptance import acceptance_is_valid, latest_acceptance, staleness
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul verification belum tersedia.", error_code="VERIFIER_NOT_IMPLEMENTED")
    assert ctx.session is not None
    rows = list(ctx.session.scalars(select(AcceptanceRecord).where(
        AcceptanceRecord.acceptance_status == "ACCEPTED",
        (AcceptanceRecord.domain == Domain.PERSONAL.value) | AcceptanceRecord.domain.is_(None))
        .order_by(AcceptanceRecord.created_at.desc()).limit(MAX_ACCEPTANCES_CHECKED)))
    if not rows:
        return Outcome("NOT_APPLICABLE", "Belum ada acceptance ACCEPTED yang masih berlaku; tidak ada yang perlu "
                       "diinvalidasi.", evidence={"accepted": 0})
    refused: list[dict[str, Any]] = []
    still_accepted: list[dict[str, Any]] = []
    for rec in rows:
        try:
            reasons = staleness(ctx.session, rec)
        except Exception as exc:  # cannot compare: never claimed fine
            reasons = [f"tidak dapat dibandingkan ({type(exc).__name__})"]
        if not reasons:
            continue
        item = {"acceptance_id": rec.id, "task_id": rec.task_id,
                "reasons": [redact_text(r)[:160] for r in reasons][:4]}
        try:
            # the consumer gate (PR/merge) evaluates the task's latest acceptance; a stale one must be refused
            latest = latest_acceptance(ctx.session, rec.task_id)
            gate_accepts = latest is not None and latest.id == rec.id and acceptance_is_valid(ctx.session, rec.task_id)
        except Exception as exc:  # the gate cannot be evaluated: not proven, never green
            item["gate_error"] = type(exc).__name__
            gate_accepts = True
        (still_accepted if gate_accepts else refused).append(item)
    evidence = {"accepted_checked": len(rows), "stale_refused_by_gate": len(refused),
                "stale_still_accepted_by_gate": len(still_accepted), "items": (still_accepted + refused)[:20],
                "gate": "aeos.verification.acceptance.acceptance_is_valid"}
    if still_accepted:
        return Outcome("FAILED", f"{len(still_accepted)} acceptance usang (head/PlanVersion/konfigurasi verifier berubah) "
                       "MASIH diterima gate PR/merge; invalidasi tidak bekerja.", evidence=evidence,
                       error_code="ACCEPTANCE_NOT_INVALIDATED", severity="CRITICAL", remedy=Remedy(
                           operation="Hentikan PR/merge; recheck acceptance dan perbaiki gate verification",
                           command="POST /api/acceptance/<id>/recheck", privilege="SUPER_ADMIN",
                           evidence_to_collect="evidence.acceptance_invalidation VERIFIED"))
    if refused:
        return Outcome("VERIFIED", f"{len(rows)} acceptance ACCEPTED diperiksa; {len(refused)} sudah usang dan DITOLAK "
                       "gate saat dipakai (invalidasi saat baca). Jalankan recheck untuk mencatat invalidasinya.",
                       evidence=evidence, remedy=Remedy(
                           operation="Catat invalidasi acceptance usang (opsional; gate sudah menolaknya)",
                           command="POST /api/acceptance/<id>/recheck", privilege="SUPER_ADMIN",
                           evidence_to_collect="acceptance INVALIDATED"))
    return Outcome("VERIFIED", f"{len(rows)} acceptance ACCEPTED masih cocok dengan head/PlanVersion/konfigurasi "
                   "verifier saat ini.", evidence=evidence)


# Append-only tables whose sequence number is an INTEGER PRIMARY KEY (rowid): a row deleted in the middle
# leaves a gap that never closes, so a gap proves rows were removed below the application.
SEQ_TABLES = ("audit_events", "events")


def expected_guard_triggers(existing_tables: set[str]) -> list[str]:
    from aeos.core.db import APPEND_ONLY_TABLES, IMMUTABLE_COLUMNS

    names = [f"trg_{t}_{k}" for t in APPEND_ONLY_TABLES if t in existing_tables for k in ("no_update", "no_delete")]
    return names + [f"trg_{t}_immutable_cols" for t in IMMUTABLE_COLUMNS if t in existing_tables]


@check("evidence.append_only_guard", "EVIDENCE_REVIEW", "Guard append-only audit/evidence utuh",
       component_id="evidence-store", needs_db=True, ttl_seconds=900, severity_fail="CRITICAL",
       invalidators=("db:schema",))
def evidence_append_only_guard(ctx: CheckContext) -> Outcome:
    """§21/§34: detects what the triggers alone cannot - a guard trigger dropped at the database level, or audit/
    event rows deleted below the application. Detection only: the database stays writable by the controller OS
    user until verifier/worker code runs isolated (§20)."""
    from sqlalchemy import text

    assert ctx.session is not None
    rows = ctx.session.execute(text("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')")).all()
    tables = {n for t, n in rows if t == "table"}
    triggers = {n for t, n in rows if t == "trigger"}
    expected = expected_guard_triggers(tables)
    missing = sorted(set(expected) - triggers)
    gaps: dict[str, int] = {}
    for table in SEQ_TABLES:
        if table not in tables:
            continue
        # table names come from the SEQ_TABLES constant above, never from input
        count, low, high = ctx.session.execute(text(f"SELECT count(*), min(seq), max(seq) FROM {table}")).one()  # noqa: S608
        if count and (high - low + 1 != count or low != 1):
            gaps[table] = int(high - count)  # rows missing from 1..max(seq)
    evidence = {"triggers_expected": len(expected), "triggers_missing": missing, "seq_gaps": gaps,
                "limit": "deteksi saja: DB dapat ditulis oleh pengguna OS controller sampai runner terisolasi (§20)"}
    remedy = Remedy(operation="Hentikan run (emergency stop), bandingkan dengan backup terakhir, lalu pasang ulang guard",
                    command=aeos_cmd("db upgrade"), source="aeos.core.db.install_append_only_triggers",
                    target="<data>/aeos.sqlite3", privilege="pengguna lokal",
                    rollback="restore backup yang lolos restore test", evidence_to_collect="evidence.append_only_guard")
    if missing:
        return Outcome("FAILED", f"{len(missing)} trigger append-only/immutable hilang: {', '.join(missing[:6])}. "
                       "Perubahan di bawah aplikasi mungkin terjadi.", evidence=evidence,
                       error_code="APPEND_ONLY_GUARD_MISSING", remedy=remedy)
    if gaps:
        return Outcome("FAILED", "Baris audit/event hilang dari urutan seq ("
                       + ", ".join(f"{t}: {n}" for t, n in sorted(gaps.items())) + "); catatan dihapus di bawah aplikasi.",
                       evidence=evidence, error_code="AUDIT_SEQ_GAP", remedy=remedy)
    return Outcome("VERIFIED", f"{len(expected)} trigger guard ada; urutan audit/event tanpa celah.", evidence=evidence)
