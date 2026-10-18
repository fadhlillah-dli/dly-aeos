"""Intake API (U-11, §05 step 5). Attachments are DATA; deleting them is not exposed (retention policy)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep, load_project
from aeos.core.errors import AeosError, NotFound, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.intake import service
from aeos.intake.filetypes import ALLOWED_EXTENSIONS
from aeos.models.portfolio import Attachment

router = APIRouter(tags=["intake"])
MULTIPART_OVERHEAD = 1024 * 1024


class LengthRequired(AeosError):
    status_code = 411
    code = "LENGTH_REQUIRED"


@router.get("/api/intake/policy")
def intake_policy(principal: CurrentPrincipal, settings: SettingsDep) -> dict[str, Any]:
    """What the upload surface accepts (shown in the wizard before any file is sent)."""
    return {
        "allowed_extensions": list(ALLOWED_EXTENSIONS),
        "max_upload_bytes": settings.max_upload_bytes,
        "max_archive_members": settings.max_archive_members,
        "executed": False,
        "trust": "UNTRUSTED_DATA",
        "notice": service.TRUST_NOTICE,
    }


@router.post("/api/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: str, request: Request, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
    settings: SettingsDep,
) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    # Bound the request before the multipart body is spooled (§05: size is checked first).
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise LengthRequired("Content-Length is required for uploads")
    if int(length) > settings.max_upload_bytes + MULTIPART_OVERHEAD:
        await run_in_threadpool(
            _reject_oversize, db, principal, project, int(length), settings.max_upload_bytes,
        )
    form = await request.form(max_files=1, max_fields=8)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValidationFailed("multipart field 'file' is required", code="FILE_REQUIRED")
        data = await upload.read(settings.max_upload_bytes + 1)
        filename = upload.filename
        declared = upload.content_type or ""
        classification = form.get("classification")
        context_id = form.get("context_id")
        source = form.get("source")
    finally:
        await form.close()
    att = await run_in_threadpool(
        _ingest, db, project, principal, filename, data, declared,
        classification if isinstance(classification, str) and classification else None,
        context_id if isinstance(context_id, str) and context_id else None,
        source if isinstance(source, str) and source else "upload", settings,
    )
    return {"attachment": service.attachment_view(att), "notice": service.TRUST_NOTICE}


def _reject_oversize(db: Any, principal: Any, project: Any, length: int, limit: int) -> None:
    service.reject_upload(db, principal=principal, project=project, filename="(not read)", size=length, sha=None,
                    reasons=[f"request is {length} bytes; upload limit is {limit}"], code="UPLOAD_TOO_LARGE",
                    exc_cls=service.UploadTooLarge)


def _ingest(db: Any, project: Any, principal: Any, filename: str | None, data: bytes, declared: str,
            classification: str | None, context_id: str | None, source: str, settings: Any) -> Attachment:
    att = service.ingest_upload(
        db, project=project, principal=principal, filename=filename, data=data, declared_mime=declared,
        classification=classification, context_id=context_id, source=source, settings=settings,
    )
    db.commit()
    return att


@router.get("/api/projects/{project_id}/attachments")
def list_project_attachments(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    items = [service.attachment_view(a) for a in service.list_attachments(db, project.id)]
    return {"items": items, "summary": service.attachment_summary(db, project.id), "source": "aeos-intake",
            "checked_at": utcnow().isoformat(), "mode": "LIVE", "notice": service.TRUST_NOTICE}


def _scoped_attachment(db: Any, principal: Any, domain: Any, attachment_id: str) -> Attachment:
    att = db.get(Attachment, attachment_id)
    if att is None:
        raise NotFound("attachment not found")
    load_project(db, principal, att.project_id, domain=domain)  # cross-domain / out-of-scope -> 404
    return att


@router.get("/api/attachments/{attachment_id}")
def get_attachment(attachment_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    att = _scoped_attachment(db, principal, domain, attachment_id)
    return {"attachment": service.attachment_view(att), "notice": service.TRUST_NOTICE}


@router.get("/api/attachments/{attachment_id}/text")
def get_attachment_text(
    attachment_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
    max_chars: int = Query(200_000, ge=1, le=2_000_000),
) -> dict[str, Any]:
    att = _scoped_attachment(db, principal, domain, attachment_id)
    text, truncated = service.read_extracted_text(db, att, max_chars=max_chars)
    return {
        "attachment_id": att.id,
        "text": text,
        "truncated": truncated,
        "redacted": True,
        "trust": "UNTRUSTED_DATA",
        "findings": att.scan_findings or [],
        "notice": service.TRUST_NOTICE,
    }
