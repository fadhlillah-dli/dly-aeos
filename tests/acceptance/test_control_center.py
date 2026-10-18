"""Control Center honesty (§34, §35, §41; §02 MCI). §50 rows:

* 1.40 "Klaim compliance dari template/scanner hijau" -> NOT-PROVEN; MCI UNVERIFIED;
* 2.8 "SSE/stream putus" -> resume from the last cursor; last observed + STALE/UNKNOWN, never "live";
* 3.26 "Nama master tetap Final setelah perubahan sah" -> the same file is updated; history kept;
* 3.27 "Mock diagram, roster, costs atau tests ditampilkan" -> labelled MOCK; never counted.

The event stream is driven over raw ASGI (the transport detail a browser EventSource provides): the
client reads until the server's ``ready`` frame and then disconnects.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from aeos.core.enums import Domain
from aeos.core.hashing import file_sha256
from aeos.core.paths import repo_root
from tests.acceptance.world import GIT_TIMEOUT, create_project, seed_catalog
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

MASTER = "docs/architecture/DLY_AEOS_Master_Architecture_Final.md"


def _ok(r: Any, code: int = 200) -> dict[str, Any]:
    assert r.status_code == code, r.text
    return r.json()


async def _sse_until_ready(app: Any, cookie: str, last_event_id: int | None) -> tuple[int, list[dict[str, Any]]]:
    ready = anyio.Event()
    frames: list[dict[str, Any]] = []
    status: list[int] = []
    buf = [""]
    sent = [False]

    async def receive() -> dict[str, Any]:
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await ready.wait()  # the connection drops right after the server says it is live
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message["type"] == "http.response.body":
            buf[0] += message.get("body", b"").decode()
            while "\n\n" in buf[0]:
                frame, buf[0] = buf[0].split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data: "):
                        d = json.loads(line[6:])
                        frames.append(d)
                        if d.get("type") == "ready":
                            ready.set()
            if not message.get("more_body", False):
                ready.set()

    headers = [(b"host", b"testserver"), (b"cookie", cookie.encode())]
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/api/events/stream", "raw_path": b"/api/events/stream", "query_string": b"",
             "root_path": "", "headers": headers, "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    with anyio.fail_after(30):
        await app(scope, receive, send)
    return status[0], frames


def _state_counts(db: Any) -> tuple[int, ...]:
    from sqlalchemy import func, select

    from aeos.models.audit import Event
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.inventory import ComponentObservation
    from aeos.models.workforce import WorkerRun

    db.expire_all()
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (Event, UsageLedgerEntry,
                                                                          ComponentObservation, WorkerRun))


def _stream(client: AuthedClient, app: Any, last_event_id: int | None) -> list[dict[str, Any]]:
    cookie = "; ".join(f"{k}={v}" for k, v in client.client.cookies.items())
    status, frames = anyio.run(_sse_until_ready, app, cookie, last_event_id)
    assert status == 200
    return frames


# ---------------------------------------------------------------------------------------------- 1.40
def test_compliance_is_never_claimed_from_templates_or_green_scanners(personal_client: AuthedClient, db, host,
                                                                      monkeypatch, tmp_path):
    from aeos.policy import snapshot

    seed_catalog(db)
    mci = _ok(personal_client.get("/api/policy/mci"))
    assert (mci["status"], mci["compliance"], mci["packs_mapping_status"]) == ("UNVERIFIED", "NOT-PROVEN", "UNVERIFIED")
    assert all(r["threshold_status"] == "UNVERIFIED" for r in mci["risk_classes"])
    policy = _ok(personal_client.get("/api/control-center/overview"))["sections"]["policy"]
    assert "MCI: UNVERIFIED" in policy["reason"] and "UNVERIFIED" in policy["evidence_limit"]
    _ok(personal_client.post("/api/doctor/run", json={"group": "CANONICAL_POLICY"}))
    checks = {c["check_id"]: c for c in _ok(personal_client.get("/api/doctor/checks"))["items"]}
    assert checks["policy.mci"]["effective_status"] not in ("VERIFIED", "READY", "PASS")
    # a template that *claims* VERIFIED compliance: a wrong hash stays UNVERIFIED ...
    template = tmp_path / "policy"
    template.mkdir()

    def claim(content_hash: str) -> None:
        (template / "mci-reference.yaml").write_text(
            "mci:\n  name: MCI_V2_SOURCE_OF_TRUTH\n  status: VERIFIED\n  version: '2'\n"
            f"  content_hash: '{content_hash}'\n  location: policy/mci-reference.yaml\n"
            "  note: 'template says: fully compliant, all scanners green'\n"
            "packs: [SEC/GRC, AI-GOV]\n", encoding="utf-8")

    monkeypatch.setattr(snapshot, "policy_dir", lambda: template)
    claim("sha256:" + "0" * 64)
    mci = _ok(personal_client.get("/api/policy/mci"))
    assert mci["declared_status"] == "VERIFIED" and mci["status"] == "UNVERIFIED"
    assert "hash mismatch" in mci["verification_note"] and mci["compliance"] == "NOT-PROVEN"
    # ... and even a hash-matching reference proves the source, never compliance or the pack mapping
    claim(file_sha256(Path(repo_root()) / "policy" / "mci-reference.yaml"))
    mci = _ok(personal_client.get("/api/policy/mci"))
    assert mci["status"] == "VERIFIED"
    assert mci["compliance"] == "NOT-PROVEN" and mci["packs_mapping_status"] == "UNVERIFIED"


# ---------------------------------------------------------------------------------------------- 2.8
def test_broken_stream_resumes_from_the_cursor_and_is_never_shown_live(personal_client: AuthedClient, db, app, host):
    from aeos.events import service as events

    stream = _ok(personal_client.get("/api/control-center/overview"))["sections"]["stream"]
    assert stream["status"] == "UNKNOWN" and stream["checked_at"] is None, "no events: UNKNOWN, never live"
    for i in range(3):
        events.emit(db, source="acc", kind=f"acc.step{i}", domain="PERSONAL")
    db.commit()
    seqs = [e["seq"] for e in _ok(personal_client.get("/api/events"))["events"]]
    assert len(seqs) == 3
    first = _stream(personal_client, app, None)
    assert [f.get("seq") for f in first if f["type"] == "event"] == seqs and first[-1]["type"] == "ready"
    # the connection broke after the first event was seen; meanwhile work continued (also in OFFICE)
    personal_client.select_domain(Domain.OFFICE)
    office = create_project(personal_client, domain="OFFICE", name="Kantor Stream")
    personal_client.select_domain(Domain.PERSONAL)
    events.emit(db, source="acc", kind="acc.office", domain="OFFICE", project_id=office["id"])
    events.emit(db, source="acc", kind="acc.step3", domain="PERSONAL")
    db.commit()
    before = _state_counts(db)
    resumed = _stream(personal_client, app, seqs[0])
    again = _stream(personal_client, app, seqs[0])  # a second reconnect from the same cursor
    assert _state_counts(db) == before, "reconnecting writes no usage, events or state"
    assert [f.get("seq") for f in again] == [f.get("seq") for f in resumed], "replay is deterministic"
    kinds = [f["kind"] for f in resumed if f["type"] == "event"]
    assert [k for k in kinds if k.startswith("acc.")] == ["acc.step1", "acc.step2", "acc.step3"]
    assert "acc.office" not in kinds and "acc.step0" not in kinds, "no duplicates, no other-domain events"
    assert all(f.get("domain") in (None, "PERSONAL") for f in resumed if f["type"] == "event")
    got = [f["seq"] for f in resumed if f["type"] == "event"]
    assert got == sorted(set(got)), "ordered and de-duplicated by seq"
    assert resumed[-1] == {"type": "ready", "cursor": got[-1]}
    stream = _ok(personal_client.get("/api/control-center/overview"))["sections"]["stream"]
    assert stream["status"] == "RECORDED" and stream["data"]["last_kind"] == "acc.step3"
    assert "bukan bukti proses sedang berjalan" in stream["evidence_limit"], "last observed, not 'live'"
    assert stream["checked_at"], "the last observed time is shown"


# ---------------------------------------------------------------------------------------------- 3.26
def test_master_keeps_its_final_name_and_history_after_lawful_changes(personal_client: AuthedClient, db, host):
    seed_catalog(db)
    root = Path(repo_root())
    masters = sorted(p.relative_to(root).as_posix() for p in (root / "docs").rglob("*Master_Architecture*"))
    assert masters == [MASTER], "one master file; no revision copies"
    text = (root / MASTER).read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# DLY_AEOS_Master_Architecture_Final"
    assert "Nama dokumen tetap, isi dapat diperbarui secara terkendali" in text
    assert "Tidak ada nomor revisi pada nama file atau judul" in text
    # every consumer reads the same path (inventory manifest validation, workforce role sources)
    from aeos.inventory.manifest import MASTER_REL
    from aeos.workforce.catalog import ROLE_PRACTICE_SOURCES

    assert MASTER_REL == MASTER and [Path(MASTER).name] == ROLE_PRACTICE_SOURCES
    # history is kept by git and by ADR amendments, never by renamed copies. The independent verifier runs
    # this suite against an exported candidate snapshot (a plain tree without .git, see
    # aeos.verification.verifier): there the controller's history is not readable and a candidate cannot
    # rewrite it, so only the file-level checks apply. In a checkout (developer run, CI) git is mandatory.
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
                              timeout=GIT_TIMEOUT)

    top = git("rev-parse", "--show-toplevel")
    exported_snapshot = not (root / ".git").exists() and (
        top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve())
    if not exported_snapshot:
        assert top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve(), top.stderr
        log = git("log", "--format=%H", "--follow", "--", MASTER)
        assert log.returncode == 0 and log.stdout.split(), "the master is versioned in git history"
        names = git("log", "--all", "--format=", "--name-only", "--", "docs").stdout.split()
        masters_ever = {n for n in names if "master_architecture" in Path(n).name.lower()}
        assert masters_ever == {MASTER}, f"no renamed/revision master ever committed: {sorted(masters_ever)}"
    adrs = list((root / "docs" / "decisions").glob("ADR-*.md"))
    assert adrs and all("_v" not in a.stem.lower() and "final" not in a.stem.lower() for a in adrs)
    # ADRs are listed through the API from those same files (history as decisions, not copies)
    listed = _ok(personal_client.get("/api/adrs"))
    assert listed["items"], "ADR history is served"


# ---------------------------------------------------------------------------------------------- 3.27
def test_mock_costs_runs_diagrams_and_tests_are_labelled_and_never_counted(personal_client: AuthedClient, db, host):
    from typer.testing import CliRunner

    from aeos.cli import app as cli
    from aeos.cost import facade as cost
    from aeos.verification import facade as verification

    seed_catalog(db)
    project = create_project(personal_client, name="Mock Labels")
    cost.record_usage(db, provider="openrouter", category="WORKER_API", source="fixture", project_id=project["id"],
                      domain="PERSONAL", cost_usd=12.5, mode="MOCK")
    db.commit()
    res = CliRunner().invoke(cli, ["work", "demo-fixtures"])
    assert res.exit_code == 0, res.output
    overview = _ok(personal_client.get("/api/control-center/overview"))
    assert overview["contains_mock"] is True and overview["mode"] in ("MIXED", "MOCK", "DEMO")
    costs = overview["sections"]["cost"]["data"]
    assert costs["actual_usd"] == 0 and costs["mock_entries_excluded"] >= 1, "MOCK cost is never actual cost"
    runs = _ok(personal_client.get("/api/runs"))
    assert runs["items"] and {r["mode"] for r in runs["items"]} == {"DEMO"} and runs["mode"] == "DEMO"
    # a MOCK test report from a trusted collector still cannot support any gate
    ev = verification.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="fixture",
                                       claim="mock: 120 tests passed", content={"passed": 120}, evidence_level="E3",
                                       domain="PERSONAL", mode="MOCK")
    db.commit()
    assert ev.evidence_level not in ("E3", "E4"), "MOCK is never E3"
    p = _ok(personal_client.post("/api/proposals", json={"kind": "ADD", "sources": ["https://github.com/oraios/serena"],
                                                         "problem": "uji", "expected_benefit": "uji"}))
    r = personal_client.post(f"/api/proposals/{p['id']}/evidence", json={"evidence_ids": [ev.id]})
    assert r.status_code == 422 and "not trusted LIVE evidence" in r.text
    # diagrams: the DESIGN lens is labelled DESIGN/manifest, and nothing is drawn as observed
    design = _ok(personal_client.get("/api/inventory/map", params={"lens": "DESIGN"}))
    assert design["mode"] == "DESIGN" and design["source"] == design["lens_source"]
    assert design["nodes"] and all(n["basis"] == ["manifest"] for n in design["nodes"])
    assert {e["status"] for e in design["edges"]} <= {"DESIGN"}, "a design edge is never drawn as observed traffic"
    observed = _ok(personal_client.get("/api/inventory/map", params={"lens": "OBSERVED"}))
    assert observed["nodes"] and not [n for n in observed["nodes"] if n["present"]]
