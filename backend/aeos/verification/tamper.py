"""Foreign writes to the control-plane trust tables while the verifier runs candidate code (§21 "workers may
not change approval records / policy / routing policy", §20 Verifier ENF; defence in depth for the WEAK
process runner, where candidate code runs as the controller's OS user and could open the SQLite DB).

A ``Window`` digests every row of the trust tables (approval decisions and requests, policy snapshots,
model routes, agent definitions/skill packs, budgets, users, install manifests, TaskSpecs/PlanVersions,
acceptance/review records and evidence) through a fresh connection before the first candidate step and again
after the last one. Writes made *by this controller process* in that window are journaled by SQLAlchemy
event hooks (ORM flushes per row id; any other write statement per table), so the controller's own legitimate
work is not mistaken for tampering. Any other changed row - added, modified or removed - is a foreign write:
the verification is QUARANTINED and a HIGH alert raised.

Residual risks (stated, not hidden): reads of the DB are not detected; a foreign write that is undone
before the window closes is not detected; a foreign write to a table this controller process also wrote
through a non-ORM statement in the same window is masked; a write by *another* AEOS process (a CLI command
recording a decision during a verification) is reported as foreign (fail closed - re-run the verification).
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
)
_TRUST = frozenset(TRUST_TABLES)
_WRITE_RE = re.compile(r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+'
                       r'["`\[]?(\w+)', re.IGNORECASE)
MAX_ROWS_PER_TABLE = 200_000

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


def digest(engine: Engine) -> dict[str, dict[str, str]]:
    """Row digests of every trust table that exists, read through a fresh connection (committed state)."""
    out: dict[str, dict[str, str]] = {}
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))} \
            if engine.dialect.name == "sqlite" else set(TRUST_TABLES)
        for table in TRUST_TABLES:
            if table not in names:
                continue
            res = conn.execute(text(f'SELECT * FROM "{table}" LIMIT {MAX_ROWS_PER_TABLE}'))  # noqa: S608 - fixed names
            cols = list(res.keys())
            key_idx = cols.index("id") if "id" in cols else None
            rows: dict[str, str] = {}
            for i, row in enumerate(res):
                key = str(row[key_idx]) if key_idx is not None else f"#{i}"
                rows[key] = hashlib.sha256(repr(tuple(row)).encode("utf-8", "replace")).hexdigest()
            out[table] = rows
    return out


class Window:
    """Open before the first candidate step, ``close()`` after the last one."""

    def __init__(self, engine: Engine):
        install()
        self.engine = engine
        self.journal = Journal()
        with _lock:
            _windows[id(self)] = self.journal
        try:
            self.before: dict[str, dict[str, str]] | None = digest(engine)
            self.error: str | None = None
        except Exception as exc:  # unreadable: the comparison is UNKNOWN (reported, never PASS)
            self.before, self.error = None, type(exc).__name__

    def close(self) -> dict[str, Any]:
        with _lock:
            _windows.pop(id(self), None)
        if self.before is None:
            return {"status": "UNKNOWN", "error": self.error, "foreign": [], "own_writes": 0}
        try:
            after = digest(self.engine)
        except Exception as exc:
            return {"status": "UNKNOWN", "error": type(exc).__name__, "foreign": [], "own_writes": 0}
        foreign: list[dict[str, str]] = []
        for table in sorted(set(self.before) | set(after)):
            if table in self.journal.tables:
                continue
            old, new = self.before.get(table, {}), after.get(table, {})
            for key in sorted(set(old) | set(new)):
                if old.get(key) == new.get(key) or (table, key) in self.journal.rows:
                    continue
                change = "added" if key not in old else "removed" if key not in new else "modified"
                foreign.append({"table": table, "id": key[:64], "change": change})
        return {"status": "FAIL" if foreign else "PASS", "foreign": foreign[:100], "foreign_count": len(foreign),
                "tables": len(after), "rows": sum(len(v) for v in after.values()),
                "own_writes": len(self.journal.rows), "own_tables": sorted(self.journal.tables)}
