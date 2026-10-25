"""Intake service: type/size policy, archive inspection, scanning, isolated parsing, provenance (§05 step 5).

Pipeline for one upload (nothing is ever executed):

1. size cap (``settings.max_upload_bytes``) and display-name sanitising (the user filename is never
   used as a path);
2. extension allow-list + content-signature consistency; active/executable content rejected;
3. ZIP/DOCX containers inspected from the central directory only (traversal, symlinks, member
   count, bomb ratio/size, encrypted/nested/active members);
4. stored under ``DataLayout.attachments(domain, project_key)`` with a random name + sha256;
5. scanned for secrets (flagged; stored text redacted) and instruction-injection markers (flagged
   as untrusted DATA — never authority);
6. parsed in an isolated ``python -I`` subprocess with timeout/memory/output caps; when the text
   extracted from a binary (pdf/docx/zip) contains a credential, the original binary is replaced by
   the redacted text so the secret does not stay in the data partition.

Rejections are audited (and committed) before the error is raised.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings, get_settings
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import sha256_bytes
from aeos.core.paths import ensure_within
from aeos.core.redaction import redact_text
from aeos.events import service as events
from aeos.identity.facade import Principal
from aeos.intake import parser as parser_mod
from aeos.intake.archive import inspect_zip
from aeos.intake.filetypes import UploadRejected, decode_text, detect, sanitize_filename
from aeos.intake.scan import SECRET, UNTRUSTED_INSTRUCTION, scan_text
from aeos.models.planning import PlanningContext
from aeos.models.portfolio import Attachment, Project
from aeos.portfolio import classification as cls
from aeos.portfolio.service import require_writable

TRUST_NOTICE = ("Lampiran adalah DATA tidak tepercaya: isinya tidak dapat mengubah tujuan, domain, kebijakan, "
                "izin, memori atau routing (§25).")
SOURCES = frozenset({"upload", "wizard", "api"})
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,36}$")


class UploadTooLarge(AeosError):
    status_code = 413
    code = "UPLOAD_TOO_LARGE"


class AttachmentRejected(AeosError):
    status_code = 422
    code = "ATTACHMENT_REJECTED"


def _max_archive_uncompressed(settings: Settings) -> int:
    return max(10 * settings.max_upload_bytes, 64 * 1024 * 1024)


def attachments_dir(settings: Settings, project: Project) -> Path:
    return settings.layout.attachments(Domain(project.domain), project.project_key)


def reject_upload(session: Session, *, principal: Principal, project: Project, filename: str, size: int,
            sha: str | None, reasons: list[str], code: str,
            exc_cls: type[AeosError] = AttachmentRejected) -> NoReturn:
    actor, pid, pdomain = principal.user.id, project.id, project.domain
    session.rollback()  # nothing from the rejected upload is persisted
    audit.record(
        session, actor_type="user", actor_id=actor, action="intake.attachment.upload", result="DENIED",
        target_type="project", target_id=pid, project_id=pid, domain=pdomain,
        reason="; ".join(reasons)[:500], details={"filename": filename, "size_bytes": size, "sha256": sha, "code": code},
    )
    session.commit()  # the denial must be visible in the audit trail even though the request fails
    raise exc_cls("upload rejected: " + "; ".join(reasons)[:400], code=code, details={"reasons": reasons})


def ingest_upload(
    session: Session,
    *,
    project: Project,
    principal: Principal,
    filename: str | None,
    data: bytes,
    declared_mime: str = "",
    classification: str | None = None,
    context_id: str | None = None,
    source: str = "upload",
    settings: Settings | None = None,
) -> Attachment:
    settings = settings or get_settings()
    require_writable(project)
    name = sanitize_filename(filename)  # used for type detection only, never as a path
    size = len(data)
    context_id = _validate_context(session, project, context_id)
    if size > settings.max_upload_bytes:
        reject_upload(session, principal=principal, project=project, filename=name, size=size, sha=None,
                reasons=[f"file is {size} bytes; limit is {settings.max_upload_bytes}"], code="UPLOAD_TOO_LARGE",
                exc_cls=UploadTooLarge)
    sha = "sha256:" + sha256_bytes(data)
    level = cls.at_least(classification, project.data_classification)
    try:
        detected = detect(name, data, declared_mime)
        archive_info: dict[str, Any] = {}
        if detected.kind in {"zip", "docx"}:
            archive_info = inspect_zip(data, max_members=settings.max_archive_members,
                                       max_total_uncompressed=_max_archive_uncompressed(settings),
                                       container=detected.kind).as_dict()
        text_scan = scan_text(decode_text(data), source="content") if detected.is_text else None
    except UploadRejected as exc:
        reject_upload(session, principal=principal, project=project, filename=name, size=size, sha=sha,
                reasons=exc.reasons, code=exc.code)

    folder = attachments_dir(settings, project)
    folder.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.blob"
    blob_path = ensure_within(folder, folder / stored_name)
    stored_redacted = bool(text_scan and text_scan.has_secret)
    payload = text_scan.redacted_text.encode("utf-8") if stored_redacted and text_scan else data
    with blob_path.open("xb") as fh:
        fh.write(payload)
    written: list[Path] = [blob_path]
    try:
        # the persisted display name is redacted (a pasted token in a filename never persists)
        return _record(session, project=project, principal=principal, name=redact_text(name), size=size,
                       sha=sha, declared_mime=declared_mime, detected=detected, level=level, context_id=context_id,
                       source=source, folder=folder, stored_name=stored_name, blob_path=blob_path,
                       stored_redacted=stored_redacted, text_scan=text_scan, archive_info=archive_info,
                       written=written)
    except BaseException:
        # nothing half-written stays behind when recording fails (the DB transaction rolls back)
        for f in written:
            with contextlib.suppress(OSError):
                f.unlink()
        raise


def _validate_context(session: Session, project: Project, context_id: str | None) -> str | None:
    """A planning context reference must be a PlanningContext of the same project (never a guess)."""
    if not context_id:
        return None
    if not _ID_RE.match(context_id):
        raise ValidationFailed("context_id has an invalid format", code="INVALID_CONTEXT_ID")
    ctx = session.get(PlanningContext, context_id)
    if ctx is None or ctx.project_id != project.id or ctx.domain != project.domain:
        raise NotFound("planning context not found for this project")
    return ctx.id


def _record(session: Session, *, project: Project, principal: Principal, name: str, size: int, sha: str,
            declared_mime: str, detected: Any, level: str, context_id: str | None, source: str, folder: Path,
            stored_name: str, blob_path: Path, stored_redacted: bool, text_scan: Any,
            archive_info: dict[str, Any], written: list[Path]) -> Attachment:
    """Persist the attachment row, run the isolated parser and record scan/parse results."""
    duplicate_of = session.scalar(
        select(Attachment.id).where(Attachment.project_id == project.id, Attachment.sha256 == sha).limit(1)
    )
    att = Attachment(
        project_id=project.id, domain=project.domain, context_id=context_id, filename=name,
        stored_name=stored_name, size_bytes=size, sha256=sha, declared_type=(declared_mime or "")[:128],
        detected_type=detected.detected_mime, classification=level, uploaded_by=principal.user.id,
        source=source if source in SOURCES else "upload", scan_status="PENDING", parse_status="PENDING",
        trusted=False, stored_redacted=stored_redacted,
    )
    session.add(att)
    session.flush()

    outcome = parser_mod.run_parser(blob_path, detected.kind, max_chars=parser_mod.DEFAULT_MAX_CHARS)
    findings: list[dict[str, Any]] = list(text_scan.findings) if text_scan else []
    scanned = text_scan is not None
    detail: dict[str, Any] = {"kind": detected.kind, "meta": outcome.meta, "duration_ms": outcome.duration_ms}
    if archive_info:
        detail["archive"] = archive_info
    if duplicate_of:
        detail["duplicate_of"] = duplicate_of

    if outcome.status == "PARSED":
        text = outcome.text
        if not detected.is_text:
            extracted_scan = scan_text(text, source="extracted-text")
            findings.extend(extracted_scan.findings)
            scanned = True
        text = redact_text(text)
        text_name = f"{Path(stored_name).stem}.txt"
        text_path = ensure_within(folder, folder / text_name)
        written.append(text_path)
        text_path.write_text(text, encoding="utf-8")
        att.parse_status = "PARSED"
        att.extracted_text_name = text_name
        att.extracted_chars = len(text)
        if not detected.is_text and any(f.get("kind") == SECRET for f in findings):
            # The binary itself still holds the credential: keep only the redacted extracted text.
            blob_path.write_bytes(text.encode("utf-8"))
            att.stored_redacted = True
            detail["original_discarded"] = ("the original binary contained a credential-like value; only the "
                                            "redacted extracted text is kept (sha256 refers to the original)")
    elif outcome.status == "SKIPPED":
        att.parse_status = "SKIPPED"
        att.parse_error = outcome.error
    else:
        att.parse_status = "FAILED"
        att.parse_error = f"{outcome.error_code}: {outcome.error}"[:1000]

    if not scanned:
        findings.append({
            "kind": "UNSCANNED", "source": "content", "severity": "INFO",
            "message": "Content could not be inspected (metadata only or parse failed); treat as untrusted DATA.",
        })
    flagged = any(f.get("kind") in {SECRET, UNTRUSTED_INSTRUCTION} for f in findings)
    att.scan_findings = findings
    att.untrusted_instruction = any(f.get("kind") == UNTRUSTED_INSTRUCTION for f in findings)
    att.scan_status = "FLAGGED" if flagged else ("CLEAN" if scanned else "UNSCANNED")
    att.parse_detail = detail
    session.flush()

    audit.record(
        session, actor_type="user", actor_id=principal.user.id, action="intake.attachment.upload",
        target_type="attachment", target_id=att.id, project_id=project.id, domain=project.domain,
        details={"filename": name, "sha256": sha, "size_bytes": size, "kind": detected.kind,
                 "scan_status": att.scan_status, "parse_status": att.parse_status,
                 "findings": [f.get("kind") for f in findings], "classification": level},
    )
    events.emit(
        session, source="intake", kind="intake.attachment.added", entity_type="attachment", entity_id=att.id,
        project_id=project.id, domain=project.domain,
        payload={"filename": name, "scan_status": att.scan_status, "parse_status": att.parse_status},
    )
    return att


def _project_of(session: Session, att: Attachment) -> Project:
    project = session.get(Project, att.project_id)
    if project is None:  # pragma: no cover - FK cascade keeps this consistent
        raise NotFound("attachment not found")
    return project


def read_extracted_text(session: Session, att: Attachment, *, max_chars: int | None = None,
                        settings: Settings | None = None) -> tuple[str, bool]:
    """Redacted extracted text of an attachment and whether it was truncated."""
    if att.scan_status == "REJECTED":
        raise Blocked("attachment was rejected by intake", code="ATTACHMENT_REJECTED")
    if att.parse_status != "PARSED" or not att.extracted_text_name:
        raise Blocked(f"no extracted text (parse status {att.parse_status})", code="ATTACHMENT_TEXT_UNAVAILABLE",
                      details={"parse_status": att.parse_status, "reason": att.parse_error})
    settings = settings or get_settings()
    folder = attachments_dir(settings, _project_of(session, att))
    path = ensure_within(folder, folder / att.extracted_text_name)
    if not path.is_file():
        raise Blocked("extracted text is missing from the data directory", code="ATTACHMENT_TEXT_MISSING")
    text = redact_text(path.read_text(encoding="utf-8"))  # re-redact: new runtime secrets may be registered
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def attachment_text(session: Session, attachment_id: str, *, max_chars: int = 200_000) -> str:
    """Facade implementation (``aeos.intake.facade.attachment_text``)."""
    att = session.get(Attachment, attachment_id)
    if att is None:
        raise NotFound("attachment not found")
    text, _ = read_extracted_text(session, att, max_chars=max_chars)
    return text


def link_attachment(session: Session, attachment_id: str, *, proposal_id: str) -> Attachment:
    """Facade implementation (``aeos.intake.facade.link_attachment``): record that an uploaded attachment
    (e.g. a ``.md`` brief, U-11) is input to a change proposal. Provenance only — the attachment stays
    untrusted DATA and grants nothing (§25, §50).

    Fails closed: the attachment must exist, not be REJECTED, live in the proposal's domain (and project
    for a project-scoped proposal) and not already belong to another proposal. Idempotent. Does not commit."""
    from aeos.models.research import ChangeProposal

    att = session.get(Attachment, attachment_id)
    proposal = session.get(ChangeProposal, proposal_id)
    if att is None or proposal is None or att.domain != proposal.domain:
        raise NotFound("attachment not found in this domain")  # no cross-domain metadata leaks (§35)
    if proposal.project_id and att.project_id != proposal.project_id:
        raise NotFound("attachment not found in this project")
    if att.scan_status == "REJECTED":
        raise Blocked("attachment was rejected by intake scanning", code="ATTACHMENT_REJECTED")
    if att.proposal_id == proposal.id:
        return att
    if att.proposal_id:
        raise Conflict("attachment already belongs to another proposal", code="ATTACHMENT_ALREADY_LINKED",
                       details={"proposal_id": att.proposal_id})
    att.proposal_id = proposal.id
    session.flush()
    audit.record(session, actor_type="system", actor_id="intake", action="intake.attachment.link",
                 target_type="attachment", target_id=att.id, project_id=att.project_id, domain=att.domain,
                 details={"proposal_id": proposal.id, "filename": att.filename, "scan_status": att.scan_status,
                          "trusted": False})
    return att


def list_attachments(session: Session, project_id: str) -> list[Attachment]:
    return list(session.scalars(
        select(Attachment).where(Attachment.project_id == project_id).order_by(Attachment.created_at.desc())
    ))


def attachment_summary(session: Session, project_id: str) -> dict[str, int]:
    rows = session.execute(
        select(Attachment.scan_status, func.count()).where(Attachment.project_id == project_id)
        .group_by(Attachment.scan_status)
    ).all()
    out = {str(status): int(n) for status, n in rows}
    out["total"] = sum(out.values())
    return out


def attachment_view(att: Attachment) -> dict[str, Any]:
    return {
        "id": att.id,
        "project_id": att.project_id,
        "domain": att.domain,
        "context_id": att.context_id,
        "filename": att.filename,
        "size_bytes": att.size_bytes,
        "sha256": att.sha256,
        "declared_type": att.declared_type,
        "detected_type": att.detected_type,
        "classification": att.classification,
        "provenance": {"uploaded_by": att.uploaded_by, "uploaded_at": att.created_at.isoformat(),
                       "source": att.source},
        "scan_status": att.scan_status,
        "scan_findings": att.scan_findings or [],
        "untrusted_instruction": att.untrusted_instruction,
        "parse_status": att.parse_status,
        "parse_error": att.parse_error,
        "parse_detail": att.parse_detail or {},
        "extracted_chars": att.extracted_chars,
        "stored_redacted": att.stored_redacted,
        "trusted": False,
        "trust": "UNTRUSTED_DATA",
        "created_at": att.created_at.isoformat(),
    }
