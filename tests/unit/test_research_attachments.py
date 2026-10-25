"""§01 U-11 "every intake surface supports file upload, including .md" (W6-004): research registration keeps the
intake attachments submitted with a manual entry. Each attachment must be in the entry's own domain and not REJECTED
by intake scanning (the change-proposal rule); an executable is refused by intake itself; another domain's attachment
is answered like a missing one; a shared platform entry never gets a domain's attachment pinned to it."""

from __future__ import annotations

from aeos.models.audit import AuditEvent
from aeos.models.portfolio import Attachment, Project
from aeos.models.research import ResearchEntry
from aeos.research import service as research
from tests.conftest import AuthedClient


def _project(db, domain: str = "PERSONAL", key: str = "riset") -> Project:
    p = Project(project_key=f"{key}-{domain.lower()}", name=f"Riset {domain}", domain=domain)
    db.add(p)
    db.commit()
    return p


def _upload(client: AuthedClient, project: Project, name: str, data: bytes, mime: str):
    return client.post(f"/api/projects/{project.id}/attachments", files={"file": (name, data, mime)},
                       data={"source": "research"})


def _row(db, project: Project, *, scan_status: str = "CLEAN") -> Attachment:
    att = Attachment(project_id=project.id, domain=project.domain, filename="brief.md", stored_name="x.md",
                     size_bytes=10, sha256="sha256:" + "a" * 64, scan_status=scan_status)
    db.add(att)
    db.commit()
    return att


def test_a_md_brief_is_kept_on_the_manual_entry(personal_client: AuthedClient, db):
    proj = _project(db)
    up = _upload(personal_client, proj, "brief.md", b"# Catatan riset\n\nKenapa alat ini relevan.\n", "text/markdown")
    assert up.status_code == 200, up.text
    att_id = up.json()["attachment"]["id"]
    r = personal_client.post("/api/research", json={"url": "https://github.com/acme/widget", "attachment_ids": [att_id]})
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["attachment_ids"] == [att_id] and entry["attachments"][0]["note"].startswith("lampiran adalah DATA")
    assert personal_client.get(f"/api/research/{entry['id']}").json()["attachment_ids"] == [att_id]
    assert db.get(ResearchEntry, entry["id"]).submitted_domain == "PERSONAL"
    # a duplicate submission of the domain's own manual entry adds the new file (never dropped silently)
    second = _row(db, proj)
    dup = personal_client.post("/api/research", json={"url": "github.com/acme/widget", "attachment_ids": [second.id]})
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert dup.json()["entry"]["attachment_ids"] == [att_id, second.id]
    assert db.query(AuditEvent).filter(AuditEvent.action == "research.entry.attach").count() == 1


def test_an_executable_is_refused_by_intake(personal_client: AuthedClient, db):
    proj = _project(db)
    r = _upload(personal_client, proj, "tool.exe", b"MZ\x90\x00" + b"\x00" * 60, "application/octet-stream")
    assert r.status_code in (400, 415, 422), r.text  # intake allow-list: nothing to attach


def test_rejected_and_other_domain_attachments_are_refused(personal_client: AuthedClient, db):
    rejected = _row(db, _project(db), scan_status="REJECTED")
    office = _row(db, _project(db, domain="OFFICE"))
    r = personal_client.post("/api/research", json={"url": "https://github.com/acme/one", "attachment_ids": [rejected.id]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ATTACHMENT_REJECTED"
    r = personal_client.post("/api/research", json={"url": "https://github.com/acme/two", "attachment_ids": [office.id]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ATTACHMENT_NOT_FOUND"
    assert "OFFICE" not in r.text  # answered like a missing id: no metadata of the other domain
    r = personal_client.post("/api/research", json={"url": "https://github.com/acme/three", "attachment_ids": ["nope"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ATTACHMENT_NOT_FOUND"
    assert db.query(ResearchEntry).filter(ResearchEntry.identity.like("github:acme/%")).count() == 0  # fail closed


def test_a_shared_platform_entry_never_gets_a_domain_attachment(personal_client: AuthedClient, db):
    research.seed_registry(db)
    db.commit()
    att = _row(db, _project(db))
    r = personal_client.post("/api/research", json={"url": "https://github.com/anomalyco/opencode",
                                                    "attachment_ids": [att.id]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESEARCH_ENTRY_SHARED"
    seeded = db.query(ResearchEntry).filter(ResearchEntry.entry_key == "tool:opencode").one()
    assert seeded.attachment_ids == []
    # without attachments the duplicate answer is unchanged
    plain = personal_client.post("/api/research", json={"url": "https://github.com/anomalyco/opencode"})
    assert plain.status_code == 200 and plain.json()["duplicate"] is True
