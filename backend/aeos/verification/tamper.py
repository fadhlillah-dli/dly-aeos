"""Foreign writes to the control-plane trust tables while the verifier runs candidate code (§21 "workers may
not change approval records / policy / routing policy", §20 Verifier ENF; defence in depth for the WEAK
process runner, where candidate code runs as the controller's OS user and could open the SQLite DB).

A ``Window`` digests every row of the trust tables (approval decisions and requests, policy snapshots,
model routes, agent definitions/skill packs, budgets, users and their server-side sessions, capability
grants, projects and their integrations, held-out eval suites, install manifests, TaskSpecs/PlanVersions,
acceptance/review records, evidence, and the trust-relevant system settings: containment proofs, write
enablement, flags, bootstrap) - and the SQLite schema with its append-only/immutability triggers - through a
fresh connection before the first candidate step and again after the last one. Writes made *by this
controller process* in that window are journaled by SQLAlchemy
event hooks (ORM flushes per row id; any other write statement per table), so the controller's own legitimate
work is not mistaken for tampering. Any other changed row - added, modified or removed - is a foreign write:
the verification is QUARANTINED, a HIGH alert raised, and the foreign row neutralised where a safe inverse exists
(``aeos.verification.neutralize``: approvals INVALIDATED, sessions/grants revoked, routes DISABLED, policy snapshots
deactivated, ...; the rest is listed for the owner's special review). Every process-runner step runs in its own
kill group (``aeos.verification.procgroup``), so nothing the candidate started survives to write after the window.

Residual risks (stated, not hidden): reads of the DB are not detected; a foreign write that is undone
before the window closes is not detected; a foreign write to a table this controller process also wrote
through a non-ORM statement in the same window is masked; a write by *another* AEOS process (a CLI command
recording a decision during a verification) is reported as foreign (fail closed - re-run the verification);
the rows of the high-volume append-only logs (``audit_events``, ``events``) are not digested - only their
guarding triggers are (a drop-modify-recreate inside the window is not detected for those two tables); the
activity timestamps ``auth_sessions.last_seen_at`` and ``users.last_login_at`` are not compared (``_VOLATILE_COLUMNS``:
no trust attribute, rewritten by the server on ordinary use).
The container runner never mounts the data dir, which is the actual prevention.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

TRUST_TABLES: tuple[str, ...] = (
    "decisions", "decision_requests", "policy_snapshots", "model_routes", "agent_definitions", "role_skill_packs",
    "budgets", "users", "install_manifests", "task_specs", "plan_versions", "acceptance_records", "review_records",
    "evidence_artifacts",
    # review 2026-10-19 (K2): a forged server-side session impersonates a human (AUTH), a forged capability grant
    # widens a run's broker scope, an integration row redirects an external write target, an eval suite is a
    # held-out grader (§21), the project row carries domain/classification policy, and system_settings holds
    # the isolation proofs (container containment reports), external-write enablement and feature flags.
    "auth_sessions", "capability_grants", "project_integrations", "eval_suites", "projects", "system_settings",
)
_TRUST = frozenset(TRUST_TABLES)
# Only the trust-relevant rows of a general key/value table are compared (its other rows are caches and
# counters other AEOS processes - e.g. a Doctor CLI run - update legitimately). Fixed SQL, no user input.
_ROW_FILTERS: dict[str, str] = {
    "system_settings": ("key LIKE 'worker_runtime.containment.%' OR key LIKE 'write_enabled.%' "
                        "OR key LIKE 'flags.%' OR key LIKE 'bootstrap.%'"),
}
# Activity timestamps the API server rewrites on ordinary use (a session's last request, at most every 30 s; an
# account's last sign-in). They are no trust attribute - a changed value grants nothing without the session token or
# the password - so they are left out of the row digest: an open dashboard during a CLI verification (another AEOS
# process) is not taken for tampering, and neutralisation never revokes the owner's live session for it. Every other
# column of these rows (token hash, user, expiry, revocation, password hash, role, scopes, ...) is compared.
_VOLATILE_COLUMNS: dict[str, frozenset[str]] = {
    "auth_sessions": frozenset({"last_seen_at"}),
    "users": frozenset({"last_login_at"}),
}
_WRITE_RE = re.compile(r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+'
                       r'["`\[]?(\w+)', re.IGNORECASE)
MAX_ROWS_PER_TABLE = 200_000
# pseudo-table of the window: the SQLite schema (sqlite_master), compared like the trust tables
SCHEMA_KEY = "sqlite_schema"

_lock = threading.Lock()
_windows: dict[int, Journal] = {}
_flushing = threading.local()


@dataclass
class Journal:
    rows: set[tuple[str, str]] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)


def _record(rows: set[tuple[str, str]] | None = None, tables: set[str] | None = None) -> None:
    with _lock:
        for j in _windows.values():
            j.rows |= rows or set()
            j.tables |= tables or set()


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    _flushing.depth = getattr(_flushing, "depth", 0) + 1


def _after_flush(session: Session, flush_context: Any) -> None:
    if not _windows:  # no verification window open: zero overhead for every other flush in the process
        return
    rows: set[tuple[str, str]] = set()
    for obj in (*session.new, *session.dirty, *session.deleted):
        table = getattr(getattr(obj, "__table__", None), "name", None)
        if table not in _TRUST:
            continue
        try:
            pk = inspect(obj).mapper.primary_key_from_instance(obj)
        except Exception:  # unknown identity: journal the whole table (conservative towards the controller)
            _record(tables={table})
            continue
        rows.add((table, str(pk[0]) if len(pk) == 1 else repr(tuple(pk))))
    if rows:
        _record(rows=rows)


def _flush_done(session: Session, *args: Any) -> None:
    _flushing.depth = max(0, getattr(_flushing, "depth", 0) - 1)


def _reset_flush(session: Session, *args: Any) -> None:
    _flushing.depth = 0


def _before_cursor_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any,
                           executemany: bool) -> None:
    if not _windows or getattr(_flushing, "depth", 0) > 0:
        return  # ORM flush statements are journaled per row by _after_flush
    m = _WRITE_RE.match(statement or "")
    if m and m.group(1).lower() in _TRUST:
        _record(tables={m.group(1).lower()})


_installed = False


def install() -> None:
    """Register the journal hooks once per process (class-level: every Session / Engine)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_flush_postexec", _flush_done)
    event.listen(Session, "after_rollback", _reset_flush)
    event.listen(Session, "after_soft_rollback", _reset_flush)
    # a flush with nothing to do after before_flush never reaches after_flush_postexec
    event.listen(Session, "after_transaction_end", _reset_flush)
    event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
    _installed = True


def digest(engine: Engine) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Row digests of every trust table that exists, read through a fresh connection (committed state), and
    the tables with more rows than ``MAX_ROWS_PER_TABLE`` (only partly compared: never a PASS, fail closed)."""
    out: dict[str, dict[str, str]] = {}
    truncated: list[str] = []
    sqlite = engine.dialect.name == "sqlite"
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))} \
            if sqlite else set(TRUST_TABLES)
        for table in TRUST_TABLES:
            if table not in names:
                continue
            order = " ORDER BY rowid" if sqlite else ""  # deterministic position keys for tables without an id
            where = f" WHERE {_ROW_FILTERS[table]}" if table in _ROW_FILTERS else ""
            res = conn.execute(text(f'SELECT * FROM "{table}"{where}{order} '  # noqa: S608 - fixed names
                                    f"LIMIT {MAX_ROWS_PER_TABLE + 1}"))
            cols = list(res.keys())
            # the row key is the primary key, as the controller's own writes are journaled (_after_flush)
            key_col = "id" if "id" in cols else "key" if "key" in cols else None
            key_idx = cols.index(key_col) if key_col is not None else None
            volatile = _VOLATILE_COLUMNS.get(table, frozenset())
            keep = [i for i, c in enumerate(cols) if c not in volatile]
            rows: dict[str, str] = {}
            for i, row in enumerate(res):
                if i >= MAX_ROWS_PER_TABLE:
                    truncated.append(table)
                    break
                key = str(row[key_idx]) if key_idx is not None else f"#{i}"
                values = tuple(row) if not volatile else tuple(row[j] for j in keep)
                rows[key] = hashlib.sha256(repr(values).encode("utf-8", "replace")).hexdigest()
            out[table] = rows
        if sqlite:
            # The schema itself: tables, indexes and the append-only / immutable-binding triggers that guard
            # Decision, AuditEvent, EvidenceArtifact, PlanVersion and TaskSpec rows (AGENTS.md invariant 4).
            # The controller never changes its schema while it runs (migrations run at startup only), so any
            # change here - e.g. candidate code dropping a trigger to rewrite "append-only" history - is
            # foreign (review 2026-10-19, K2).
            schema: dict[str, str] = {}
            for typ, name, tbl, sql in conn.execute(text("SELECT type, name, tbl_name, sql FROM sqlite_master")):
                schema[f"{typ}:{name}"] = hashlib.sha256(
                    repr((typ, name, tbl, sql)).encode("utf-8", "replace")).hexdigest()
            out[SCHEMA_KEY] = schema
    return out, truncated


class Window:
    """Open before the first candidate step, ``close()`` after the last one."""

    def __init__(self, engine: Engine):
        install()
        self.engine = engine
        self.journal = Journal()
        with _lock:
            _windows[id(self)] = self.journal
        self.truncated: set[str] = set()
        self.foreign_all: list[dict[str, str]] = []
        try:
            self.before: dict[str, dict[str, str]] | None
            self.before, cut = digest(engine)
            self.truncated.update(cut)
            self.error: str | None = None
        except Exception as exc:  # unreadable: the comparison is UNKNOWN (reported, never PASS)
            self.before, self.error = None, type(exc).__name__

    def close(self) -> dict[str, Any]:
        with _lock:
            _windows.pop(id(self), None)
        if self.before is None:
            return {"status": "UNKNOWN", "error": self.error, "foreign": [], "own_writes": 0}
        try:
            after, cut = digest(self.engine)
        except Exception as exc:
            return {"status": "UNKNOWN", "error": type(exc).__name__, "foreign": [], "own_writes": 0}
        self.truncated.update(cut)
        foreign: list[dict[str, str]] = []
        for table in sorted(set(self.before) | set(after)):
            if table in self.journal.tables:
                continue
            old, new = self.before.get(table, {}), after.get(table, {})
            for key in sorted(set(old) | set(new)):
                if old.get(key) == new.get(key) or (table, key) in self.journal.rows:
                    continue
                change = "added" if key not in old else "removed" if key not in new else "modified"
                foreign.append({"table": table, "id": key, "change": change})
        # every foreign row with its full key, for neutralisation (aeos.verification.neutralize): the report below
        # lists only the first rows with shortened keys - a row beyond them must not stay in effect
        self.foreign_all = [dict(f) for f in foreign]
        foreign = [{**f, "id": f["id"][:64]} for f in foreign]
        status = "FAIL" if foreign else ("UNKNOWN" if self.truncated else "PASS")
        result: dict[str, Any] = {
            "status": status, "foreign": foreign[:100], "foreign_count": len(foreign), "tables": len(after),
            "rows": sum(len(v) for v in after.values()), "own_writes": len(self.journal.rows),
            "own_tables": sorted(self.journal.tables)}
        if self.truncated:
            result["truncated_tables"] = sorted(self.truncated)
            result["error"] = (f"more than {MAX_ROWS_PER_TABLE} rows in {', '.join(sorted(self.truncated))}: "
                               "only partly compared")
        return result
