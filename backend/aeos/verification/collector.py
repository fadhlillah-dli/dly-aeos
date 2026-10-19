"""Evidence collector (§34) — the only writer of EvidenceArtifact rows.

Rules enforced here (callers cannot override them):

* ``trusted`` is derived from ``origin`` ∈ ``TRUSTED_COLLECTORS``; there is no parameter to force it.
* Evidence level is assigned **per claim** and capped by origin and kind:
  untrusted origins (worker, human attachment, unknown) → at most **E0** (a claim);
  static artifacts (diff, lint, config, provider documentation/settings) → at most **E1**;
  controlled tests run by a trusted runner → at most **E2**;
  runtime observations (provider CI, probes, restore tests, screenshots of the real app) → **E3**;
  **E4** (regression-protected) only for verifier evidence that references the current trusted
  protected suite by hash. The requested level is a ceiling request, never an upgrade.
* Content is redacted *before* it is hashed and written; the stored bytes are what the hash covers.
  Binary content cannot be redacted, so binary content that matches a credential pattern is refused.
* Files live under the managed data dir (``DataLayout.evidence(domain, project_key)``, the domain
  evidence dir, or ``<data>/platform/evidence``); ``location`` is stored relative to the data dir.
* EvidenceArtifact is append-only (DB trigger). This local table is not a WORM / tamper-proof
  store against a host administrator (§34).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import get_settings
from aeos.core.enums import DataClassification, DataMode, Domain, EvidenceLevel
from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash, sha256_bytes
from aeos.core.ids import new_id
from aeos.core.paths import ensure_within
from aeos.core.redaction import contains_secret, redact, redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import Project
from aeos.verification.facade import TRUSTED_COLLECTORS

log = logging.getLogger("aeos.verification.collector")

LEVELS: tuple[str, ...] = tuple(level.value for level in EvidenceLevel)
INLINE_MAX_CHARS = 4000
MAX_EVIDENCE_BYTES = 50 * 1024 * 1024
_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,47}$")

LEVEL_EXPLANATIONS: dict[str, str] = {
    "E0": "Klaim — belum ada bukti independen (mis. laporan atau lampiran worker).",
    "E1": "Bukti statis — diff, lint, konfigurasi atau dokumentasi provider; bukan bukti perilaku.",
    "E2": "Uji terkendali — suite terpercaya dijalankan verifier independen pada commit tertentu.",
    "E3": "Observasi runtime — CI provider, probe atau observasi target nyata pada waktu tertentu.",
    "E4": "Terlindungi regresi — klaim dicakup suite terlindungi yang dirujuk verifier dengan hash.",
}

# Kind families decide the highest level a trusted origin can assign.
STATIC_KINDS = frozenset({
    "DIFF", "CHANGED_FILES", "SCOPE_CHECK", "LINT_REPORT", "STATIC_ANALYSIS", "SECRET_SCAN",
    "CONFIG_SNAPSHOT", "PROVIDER_CAPABILITY", "DOC_REFERENCE", "MANIFEST", "PR_BUNDLE",
    "GOVERNANCE_HANDOFF", "REVIEW_PACKAGE", "ATTACHMENT", "NOTE",
})
CONTROLLED_TEST_KINDS = frozenset({"TEST_REPORT", "VERIFICATION_REPORT", "CONTAINMENT_TEST", "JUNIT_XML",
                                   "WORKER_CONTRACT_TEST"})
RUNTIME_KINDS = frozenset({
    "PROBE", "RUNTIME_OBSERVATION", "CI_STATUS", "RESTORE_TEST", "SMOKE_TEST", "SCREENSHOT", "LOG", "HEALTH",
})


def level_index(level: str | None) -> int:
    try:
        return LEVELS.index(str(level))
    except ValueError:
        return 0


def _min_level(a: str, b: str) -> str:
    return a if level_index(a) <= level_index(b) else b


def _protected_suite_reference_valid(environment: dict[str, Any]) -> bool:
    """E4 requires a reference to the *current* trusted protected suite (hash + test ids)."""
    ref = environment.get("protected_suite")
    if not isinstance(ref, dict):
        return False
    suite_hash = ref.get("suite_hash")
    test_ids = ref.get("test_ids")
    if not suite_hash or not isinstance(test_ids, list) or not test_ids:
        return False
    try:
        from aeos.verification.suite import trusted_suite_hash

        return suite_hash == trusted_suite_hash()
    except Exception:  # missing/unreadable trusted suite -> no E4 (fail closed)
        log.warning("could not compute trusted suite hash for E4 check", exc_info=True)
        return False


def level_ceiling(*, trusted: bool, origin: str, kind: str, environment: dict[str, Any]) -> tuple[str, str]:
    """Return (highest assignable level, reason) for this origin/kind/environment."""
    if not trusted:
        return "E0", f"origin '{origin}' is not an independent collector; recorded as a claim (E0)"
    if origin == "verifier" and _protected_suite_reference_valid(environment):
        return "E4", "verifier evidence referencing the current protected regression suite"
    if kind in RUNTIME_KINDS:
        return "E3", "runtime observation by a trusted collector"
    if kind in CONTROLLED_TEST_KINDS:
        return "E2", "controlled test executed by a trusted runner"
    return "E1", "static artifact; does not prove runtime behaviour"


@dataclass(frozen=True)
class _Serialized:
    data: bytes | None
    content_type: str
    ext: str
    inline: dict[str, Any]


def _serialize(content: bytes | str | dict[str, Any] | None) -> _Serialized:
    if content is None:
        return _Serialized(None, "", "", {"no_content": True})
    if isinstance(content, dict):
        red = redact(content, pii=True)
        text = json.dumps(red, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        compact = json.dumps(red, sort_keys=True, ensure_ascii=False, default=str)
        if len(compact) <= INLINE_MAX_CHARS:
            inline: dict[str, Any] = {"content": json.loads(compact)}
        else:
            summary = {
                k: (v[:300] if isinstance(v, str) else v)
                for k, v in red.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
            inline = {"summary": summary, "keys": sorted(str(k) for k in red)[:50], "truncated": True}
        return _Serialized(text.encode("utf-8"), "application/json", ".json", inline)
    if isinstance(content, bytes):
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            if contains_secret(content.decode("latin-1")):
                raise PolicyDenied(
                    "binary evidence matches a credential pattern and cannot be redacted; not stored",
                    code="EVIDENCE_SECRET_DETECTED",
                ) from None
            return _Serialized(content, "application/octet-stream", ".bin",
                               {"binary": True, "redaction": "NOT_APPLICABLE_BINARY"})
        content = decoded
    if isinstance(content, str):
        red_text = redact_text(content, pii=True)
        stripped = red_text.lstrip()
        if stripped.startswith("<?xml") or stripped.startswith("<testsuite"):
            ctype, ext = "application/xml", ".xml"
        else:
            ctype, ext = "text/plain; charset=utf-8", ".txt"
        inline = {"text": red_text[:INLINE_MAX_CHARS], "truncated": len(red_text) > INLINE_MAX_CHARS}
        return _Serialized(red_text.encode("utf-8"), ctype, ext, inline)
    raise ValidationFailed("evidence content must be bytes, str, dict or None", code="EVIDENCE_CONTENT_TYPE")


def _evidence_dir(session: Session, project_id: str | None, domain: str | None) -> tuple[Path, str | None, str | None]:
    layout = get_settings().layout
    if project_id:
        project = session.get(Project, project_id)
        if project is None:
            raise ValidationFailed("evidence refers to an unknown project", code="EVIDENCE_UNKNOWN_PROJECT")
        if domain is not None and domain != project.domain:
            # U-05: the domain of a record is the project's human-chosen domain, never re-labelled.
            raise ValidationFailed("evidence domain does not match the project's domain",
                                   code="EVIDENCE_DOMAIN_MISMATCH")
        return layout.evidence(Domain(project.domain), project.project_key), project.id, project.domain
    if domain is not None:
        return layout.domain_root(Domain(domain)) / "evidence", None, domain
    return layout.root / "platform" / "evidence", None, None


def collect_evidence(
    session: Session, *, kind: str, origin: str, collector_identity: str, claim: str,
    content: bytes | str | dict[str, Any] | None = None, evidence_level: str = "E0",
    subject_type: str = "", subject_id: str = "", project_id: str | None = None, domain: str | None = None,
    run_id: str | None = None, task_id: str | None = None, environment: dict[str, Any] | None = None,
    classification: str = "INTERNAL", mode: str = "LIVE", impl: Any = None,
) -> EvidenceArtifact:
    # ``impl`` is ignored: the frozen facade forwards ``**locals()``, which includes its own ``impl`` name.
    del impl
    kind_n = (kind or "").strip().upper()
    if not _KIND_RE.match(kind_n):
        raise ValidationFailed(f"invalid evidence kind: {kind!r}", code="EVIDENCE_KIND_INVALID")
    origin_n = (origin or "").strip().lower()
    if not origin_n or len(origin_n) > 64:
        raise ValidationFailed("evidence origin is required", code="EVIDENCE_ORIGIN_REQUIRED")
    identity = (collector_identity or "").strip()
    if not identity or len(identity) > 128:
        raise ValidationFailed("collector identity is required (max 128 chars)", code="EVIDENCE_IDENTITY_REQUIRED")
    if evidence_level not in LEVELS:
        raise ValidationFailed(f"invalid evidence level: {evidence_level!r}", code="EVIDENCE_LEVEL_INVALID")
    if classification not in {c.value for c in DataClassification}:
        raise ValidationFailed(f"invalid classification: {classification!r}")
    if mode not in {m.value for m in DataMode}:
        raise ValidationFailed(f"invalid data mode: {mode!r}")
    if domain is not None and domain not in {d.value for d in Domain}:
        raise ValidationFailed(f"invalid domain: {domain!r}")
    if environment is not None and not isinstance(environment, dict):
        raise ValidationFailed("environment must be a mapping")

    trusted = origin_n in TRUSTED_COLLECTORS
    env = redact(dict(environment or {}), pii=True)
    ceiling, ceiling_reason = level_ceiling(trusted=trusted, origin=origin_n, kind=kind_n, environment=env)
    assigned = _min_level(evidence_level, ceiling)
    if not trusted and evidence_level != "E0":
        log.info("downgraded untrusted evidence from %s to E0 (origin=%s kind=%s)", evidence_level, origin_n, kind_n)

    target_dir, project_id, domain = _evidence_dir(session, project_id, domain)
    ser = _serialize(content)
    if ser.data is not None and len(ser.data) > MAX_EVIDENCE_BYTES:
        raise ValidationFailed("evidence content exceeds the 50 MB limit", code="EVIDENCE_TOO_LARGE")

    ev_id = new_id()
    now = utcnow()
    location: str | None = None
    written: Path | None = None
    if ser.data is not None:
        layout_root = get_settings().layout.root
        month_dir = target_dir / now.strftime("%Y%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        path = ensure_within(layout_root, month_dir / f"{ev_id}{ser.ext}")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(ser.data)
        os.replace(tmp, path)
        written = path
        location = path.relative_to(layout_root.resolve()).as_posix()
        digest = "sha256:" + sha256_bytes(ser.data)
    else:
        digest = content_hash({"kind": kind_n, "claim": redact_text(claim or "", pii=True), "environment": env,
                               "subject": [subject_type, subject_id], "at": now.isoformat()})

    inline = dict(ser.inline)
    inline["level"] = {"requested": evidence_level, "assigned": assigned, "ceiling": ceiling, "reason": ceiling_reason}
    ev = EvidenceArtifact(
        id=ev_id,
        project_id=project_id,
        domain=domain,
        subject_type=subject_type or "",
        subject_id=str(subject_id or ""),
        run_id=run_id,
        task_id=task_id,
        claim=redact_text(claim or "", pii=True)[:4000],
        kind=kind_n,
        origin=origin_n,
        collector_identity=redact_text(identity),
        trusted=trusted,
        content_hash=digest,
        content_type=ser.content_type,
        classification=classification,
        location=location,
        size_bytes=len(ser.data) if ser.data is not None else None,
        inline=inline,
        evidence_level=assigned,
        environment=env,
        created_at=now,
        mode=mode,
    )
    session.add(ev)
    try:
        session.flush()
    except Exception:
        if written is not None:
            written.unlink(missing_ok=True)
        raise
    events.emit(
        session, source=origin_n if trusted else "untrusted-input", kind="evidence.collected",
        entity_type="evidence", entity_id=ev.id, project_id=project_id, domain=domain, run_id=run_id, mode=mode,
        payload={"kind": kind_n, "level": assigned, "trusted": trusted, "task_id": task_id},
    )
    return ev


def evidence_summary(ev: EvidenceArtifact) -> dict[str, Any]:
    """The small structured summary of an artifact (full dict when it fit inline)."""
    inline = ev.inline or {}
    if isinstance(inline.get("content"), dict):
        return inline["content"]
    if isinstance(inline.get("summary"), dict):
        return inline["summary"]
    return {}


def read_json(ev: EvidenceArtifact | None) -> dict[str, Any] | None:
    """Full JSON content of an artifact, only when its stored bytes still match ``content_hash``."""
    if ev is None:
        return None
    data, integrity = read_content(ev)
    if data is None or integrity != "VERIFIED":
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_content(ev: EvidenceArtifact) -> tuple[bytes | None, str]:
    """Read stored content and check its integrity against ``content_hash``.

    Returns (bytes or None, integrity) where integrity is VERIFIED | MISMATCH | MISSING | NO_CONTENT.
    """
    if not ev.location:
        return None, "NO_CONTENT"
    root = get_settings().layout.root
    try:
        path = ensure_within(root, root / ev.location)
    except ValueError:
        return None, "MISSING"
    if not path.is_file():
        return None, "MISSING"
    data = path.read_bytes()
    ok = ("sha256:" + sha256_bytes(data)) == ev.content_hash
    return data, "VERIFIED" if ok else "MISMATCH"
