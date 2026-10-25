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
The worker runtime's window (wave 7, opt-in ``CROSS_PROCESS_TOLERATED``) also accepts a grant's usage counters
growing, a grant or session being revoked and a session's ``last_seen_at`` moving forward, even from another
process: a writer making only those changes (inflating usage, revoking, keeping an owner session from idling out)
is not detected there. On ``capability_grants`` (``CROSS_PROCESS_STRICT``) the controller's own writes mask nothing
(review wave 7: the in-process broker's usage ``UPDATE`` used to hide the whole table for the rest of a run). On the
other tables a foreign change to a row this controller process also wrote by ORM in the same window is masked (for
example the owner's own session row, touched while the owner uses the dashboard). The container runner never
mounts the data dir, which is the actual prevention.
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
                        "OR key LIKE 'flags.%' OR key LIKE 'bootstrap.%' "
                        # wave 7: the worker-admission integrity hold (clearing it would re-admit workers)
                        "OR key LIKE 'worker_runtime.integrity%'"),
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


# Opt-in (wave 7, worker runs): changes ANOTHER AEOS process makes legitimately while a worker runs, e.g. when the
# executor runs in the CLI (`aeos work run-once`) and the server process serves the run's model calls: the broker
# counts usage on the run's capability grant and may deactivate it, and the server touches an owner session's
# last_seen_at. A modified row whose ONLY changes are these columns, each in its allowed direction, is reported as
# ``tolerated``, never as foreign. Any other column, an added/removed row, or a change against the direction (a
# usage counter going down, a grant re-activated, a revocation cleared) stays foreign. The verifier never uses it.
CROSS_PROCESS_TOLERATED: dict[str, dict[str, str]] = {
    "capability_grants": {"used_requests": "grow", "used_tokens": "grow", "used_cost_usd": "grow",
                          "active": "deactivate", "revoked_at": "set_once", "revoked_reason": "set_once"},
    "auth_sessions": {"last_seen_at": "grow", "revoked_at": "set_once", "revoked_reason": "set_once"},
}
# Opt-in strict tables (review wave 7): every change THIS process legitimately makes to them during a run is itself
# one of the tolerated changes (the broker's usage counters are Core ``UPDATE`` statements, revocation/expiry are ORM
# updates of active/revoked_*) or an ORM insert of a new grant. So the controller's own writes do NOT mask them: a
# table-level journal entry (Core statement) never skips the table, and a modified row journaled as this process's
# own must still change only tolerated columns in their allowed direction. Without this, the in-server broker's
# first usage UPDATE hid every forged cap or forged grant row for the rest of the run.
CROSS_PROCESS_STRICT: frozenset[str] = frozenset({"capability_grants"})
# per tolerated table: row key -> (digest of the row WITHOUT the tolerated columns, {tolerated column: value})
Tolerated = dict[str, dict[str, tuple[str, dict[str, Any]]]]


def _ge(new: Any, old: Any) -> bool:
    try:
        return float(new) >= float(old)
    except (TypeError, ValueError):
        return str(new) >= str(old)  # ISO timestamps as SQLite stores them compare in time order


def _direction_ok(rule: str, old: Any, new: Any) -> bool:
    if old == new:
        return True
    if rule == "grow":
        return new is not None and (old is None or _ge(new, old))
    if rule == "deactivate":
        return bool(old) and not bool(new)
    if rule == "set_once":
        return old is None
    return False


def digest(engine: Engine, *, tolerate: dict[str, dict[str, str]] | None = None,
           tolerated: Tolerated | None = None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Row digests of every trust table that exists, read through a fresh connection (committed state), and
    the tables with more rows than ``MAX_ROWS_PER_TABLE`` (only partly compared: never a PASS, fail closed).
    With ``tolerate`` (see ``CROSS_PROCESS_TOLERATED``) the same row read also fills ``tolerated``."""
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
            soft = set((tolerate or {}).get(table) or ()) if tolerated is not None and key_idx is not None else set()
            soft_rows: dict[str, tuple[str, dict[str, Any]]] = {}
            rows: dict[str, str] = {}
            for i, row in enumerate(res):
                if i >= MAX_ROWS_PER_TABLE:
                    truncated.append(table)
                    break
                key = str(row[key_idx]) if key_idx is not None else f"#{i}"
                values = tuple(row) if not volatile else tuple(row[j] for j in keep)
                rows[key] = hashlib.sha256(repr(values).encode("utf-8", "replace")).hexdigest()
                if soft:
                    # the stable part leaves out the volatile columns too, as the row digest above does
                    stable = tuple(v for c, v in zip(cols, row, strict=True) if c not in soft and c not in volatile)
                    soft_rows[key] = (hashlib.sha256(repr(stable).encode("utf-8", "replace")).hexdigest(),
                                      {c: v for c, v in zip(cols, row, strict=True) if c in soft})
            out[table] = rows
            if soft and tolerated is not None:
                tolerated[table] = soft_rows
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

    def __init__(self, engine: Engine, *, tolerate: dict[str, dict[str, str]] | None = None,
                 strict: frozenset[str] | set[str] | None = None):
        """``tolerate`` (opt-in, worker runs only): ``CROSS_PROCESS_TOLERATED``-shaped column rules; ``strict``: the
        tolerated tables whose own writes never mask a change (``CROSS_PROCESS_STRICT``)."""
        install()
        self.engine = engine
        self.journal = Journal()
        self.tolerate = {t: dict(c) for t, c in (tolerate or {}).items() if t in _TRUST}
        self.strict = frozenset(t for t in (strict or ()) if t in self.tolerate)
        self.tolerated_before: Tolerated = {}
        with _lock:
            _windows[id(self)] = self.journal
        self.truncated: set[str] = set()
        self.foreign_all: list[dict[str, str]] = []
        try:
            self.before: dict[str, dict[str, str]] | None
            self.before, cut = digest(engine, tolerate=self.tolerate, tolerated=self.tolerated_before)
            self.truncated.update(cut)
            self.error: str | None = None
        except Exception as exc:  # unreadable: the comparison is UNKNOWN (reported, never PASS)
            self.before, self.error = None, type(exc).__name__

    def _tolerated_change(self, table: str, key: str, after: Tolerated) -> list[str] | None:
        """The tolerated columns that changed, when they are the ONLY change and each moved in its allowed
        direction; else None (foreign)."""
        rules = self.tolerate.get(table)
        old, new = self.tolerated_before.get(table, {}).get(key), after.get(table, {}).get(key)
        if not rules or old is None or new is None or old[0] != new[0]:
            return None
        changed = sorted(c for c in rules if old[1].get(c) != new[1].get(c))
        if not changed or not all(_direction_ok(rules[c], old[1].get(c), new[1].get(c)) for c in changed):
            return None
        return changed

    def close(self) -> dict[str, Any]:
        with _lock:
            _windows.pop(id(self), None)
        if self.before is None:
            return {"status": "UNKNOWN", "error": self.error, "foreign": [], "own_writes": 0}
        after_tolerated: Tolerated = {}
        try:
            after, cut = digest(self.engine, tolerate=self.tolerate, tolerated=after_tolerated)
        except Exception as exc:
            return {"status": "UNKNOWN", "error": type(exc).__name__, "foreign": [], "own_writes": 0}
        self.truncated.update(cut)
        foreign: list[dict[str, str]] = []
        tolerated: list[dict[str, Any]] = []
        for table in sorted(set(self.before) | set(after)):
            strict = table in self.strict
            if table in self.journal.tables and not strict:
                continue
            old, new = self.before.get(table, {}), after.get(table, {})
            for key in sorted(set(old) | set(new)):
                if old.get(key) == new.get(key):
                    continue
                change = "added" if key not in old else "removed" if key not in new else "modified"
                own = (table, key) in self.journal.rows
                if own and not (strict and change == "modified"):
                    continue  # this process's own ORM write (strict tables: its own insert/delete only)
                columns = self._tolerated_change(table, key, after_tolerated) if change == "modified" else None
                if columns is not None:
                    if not own:
                        tolerated.append({"table": table, "id": key[:64], "columns": columns})
                    continue
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
        if self.tolerate:
            result["tolerated"] = tolerated[:100]
            result["tolerated_count"] = len(tolerated)
        if self.truncated:
            result["truncated_tables"] = sorted(self.truncated)
            result["error"] = (f"more than {MAX_ROWS_PER_TABLE} rows in {', '.join(sorted(self.truncated))}: "
                               "only partly compared")
        return result
