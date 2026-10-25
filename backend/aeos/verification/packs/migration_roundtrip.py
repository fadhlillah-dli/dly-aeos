"""Migration upgrade/downgrade/restore driver (§16 DB/Data Engineer "Uji migration/rollback/restore", §31 G2; W6-046).

Run by the verifier as candidate code (it imports the candidate's Alembic env and revisions) through the verifier
runner and inside the tamper window: ``python -c <this file> <migrations dir> <throwaway sqlite path>``. Prints
one JSON line. Legs:

1. ``upgrade head`` -> ``downgrade base`` -> ``upgrade head`` on an empty throwaway SQLite database;
2. seed: one generated row per table that accepts it (typed placeholder values; tables refusing a generated
   row - constraints, triggers - are listed, never forced);
3. backup with the SQLite online-backup API, delete the database, restore it from the backup and compare a
   per-table content digest of the seeded rows (restore of populated data, not just a reopen);
4. with the seeded data present: downgrade one revision and upgrade to head again (rollback with data).

Standard library + alembic only; never touches anything but the two paths it is given.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from typing import Any

_PLACEHOLDERS = (("INT", 1), ("BOOL", 0), ("REAL", 0.0), ("FLOA", 0.0), ("DOUB", 0.0), ("NUM", 0),
                 ("DEC", 0), ("JSON", "{}"), ("DATE", "2026-01-01 00:00:00"), ("TIME", "2026-01-01 00:00:00"),
                 ("BLOB", b"seed"))


def _value(table: str, column: str, decl: str) -> Any:
    up = (decl or "").upper()
    for marker, value in _PLACEHOLDERS:
        if marker in up:
            return value
    return f"seed-{table}-{column}"[:32]


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("select name from sqlite_master where type='table' order by name").fetchall()
    return [r[0] for r in rows if r[0] != "alembic_version" and not r[0].startswith("sqlite_")]


def _digest(conn: sqlite3.Connection, tables: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tables:
        h = hashlib.sha256()
        # table names come from sqlite_master of the throwaway DB, quoted as identifiers
        q = 'select * from "' + t.replace('"', '""') + '" order by 1'  # noqa: S608
        for row in conn.execute(q):
            h.update(repr(row).encode("utf-8"))
        out[t] = h.hexdigest()
    return out


def _seed(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    seeded: list[str] = []
    refused: list[str] = []
    for t in _tables(conn):
        cols = conn.execute('pragma table_info("' + t.replace('"', '""') + '")').fetchall()
        names = [c[1] for c in cols]
        values = [_value(t, c[1], c[2]) for c in cols]
        columns = ", ".join('"' + n.replace('"', '""') + '"' for n in names)
        marks = ", ".join("?" for _ in names)
        # identifiers from the throwaway DB's own schema, quoted; values are bound parameters
        sql = 'insert into "' + t.replace('"', '""') + '" (' + columns + ") values (" + marks + ")"  # noqa: S608
        try:
            conn.execute("savepoint s")
            conn.execute(sql, values)
            conn.execute("release s")
            seeded.append(t)
        except sqlite3.Error:
            conn.execute("rollback to s")
            conn.execute("release s")
            refused.append(t)
    conn.commit()
    return seeded, refused


def main(argv: list[str], out: dict[str, Any]) -> dict[str, Any]:
    mig_dir, db_path = argv[1], argv[2]
    os.environ["AEOS_CONFIG"] = db_path + ".noconfig"
    out["leg"] = "load"
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", mig_dir)
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + db_path.replace(os.sep, "/"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    out["heads"] = list(heads)
    if len(heads) != 1:
        out["error"] = "history does not have exactly one head: " + repr(heads)
        return out
    head = heads[0]
    out["head"] = head
    out["leg"] = "roundtrip"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        rev = conn.execute("select version_num from alembic_version").fetchone()
        tables = _tables(conn)
    finally:
        conn.close()
    out["restored_revision"] = rev[0] if rev else None
    out["tables"] = len(tables)
    out["roundtrip_ok"] = out["restored_revision"] == head and out["tables"] > 0
    if not out["roundtrip_ok"]:
        out["error"] = "restored revision " + repr(out["restored_revision"]) + " != head"
        return out
    # seed -> backup -> delete -> restore -> compare (restore of populated data)
    out["leg"] = "restore"
    conn = sqlite3.connect(db_path)
    try:
        seeded, refused = _seed(conn)
        before = _digest(conn, seeded)
        backup_path = db_path + ".bak"
        dest = sqlite3.connect(backup_path)
        try:
            conn.backup(dest)
        finally:
            dest.close()
    finally:
        conn.close()
    out["seeded_tables"] = len(seeded)
    out["seed_refused_tables"] = refused[:50]
    os.remove(db_path)
    src = sqlite3.connect(backup_path)
    try:
        restored = sqlite3.connect(db_path)
        try:
            src.backup(restored)
            after = _digest(restored, seeded)
            rev2 = restored.execute("select version_num from alembic_version").fetchone()
        finally:
            restored.close()
    finally:
        src.close()
    out["restore_ok"] = before == after and bool(rev2) and rev2[0] == head
    out["restore_compared_tables"] = len(seeded)
    if not out["restore_ok"]:
        out["error"] = "restored database differs from the seeded backup"
        return out
    # rollback with data present: one revision down, then forward again
    current = script.get_revision(head)
    down = current.down_revision if current is not None else None
    if isinstance(down, (tuple, list)):
        down = down[0] if down else None
    out["data_rollback_target"] = down or "base"
    out["leg"] = "data_rollback"
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        rev3 = conn.execute("select version_num from alembic_version").fetchone()
    finally:
        conn.close()
    out["data_rollback_ok"] = bool(rev3) and rev3[0] == head
    if not out["data_rollback_ok"]:
        out["error"] = "downgrade/upgrade with seeded data did not return to head"
        return out
    out["ok"] = True
    out["leg"] = "done"
    return out


if __name__ == "__main__":
    result: dict[str, Any] = {"ok": False}
    try:
        main(sys.argv, result)
    except Exception as exc:  # the candidate's migration code failed: reported with its leg, never raised
        result["ok"] = False
        result["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    print(json.dumps(result, default=str))
