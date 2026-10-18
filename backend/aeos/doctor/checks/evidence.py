"""§08 Evidence/review: collector/verifier independence, artifact permissions, trace redaction, acceptance suite."""

from __future__ import annotations

import sys
from typing import Any

from aeos.core.enums import Domain
from aeos.core.redaction import REDACTED, redact, redact_text
from aeos.doctor.checks import check
from aeos.doctor.checks._common import module_present
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd


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
def _windows() -> bool:
    # One platform source for every check (sys.platform), so remedies match the OS they are shown on.
    return sys.platform.startswith("win")


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
        evidence["acl_checked"] = False
        return Outcome("CONFIGURED", "Partisi PERSONAL/OFFICE terpisah dan dapat ditulis; ACL Windows tidak diperiksa.",
                       evidence=evidence)
    return Outcome("VERIFIED", "Partisi PERSONAL/OFFICE terpisah, dapat ditulis, tanpa akses pengguna lain.",
                   evidence=evidence)


@check("evidence.verifier", "EVIDENCE_REVIEW", "Verifier/collector independen tersedia",
       component_id="verification-review", ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH")
def evidence_verifier(ctx: CheckContext) -> Outcome:
    present, placeholder = module_present("aeos.verification.collector")
    acc, acc_ph = module_present("aeos.verification.acceptance")
    evidence = {"collector": present and not placeholder, "acceptance": acc and not acc_ph}
    if not evidence["collector"] or not evidence["acceptance"]:
        return Outcome("MISSING", "Verifier/collector independen belum diimplementasikan; hasil worker tetap "
                       "REVIEW/NOT-PROVEN.", evidence=evidence, error_code="VERIFIER_NOT_IMPLEMENTED")
    return Outcome("CONFIGURED", "Collector dan acceptance tersedia; independensi dibuktikan oleh uji modul verification.",
                   evidence=evidence)


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
