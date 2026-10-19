"""RoleSkillPack contract and evaluation-derived eligibility (§17, §19, §43, §46).

* ``role_contract`` is what a run receives: competence, inputs, outputs, boundaries, forbidden actions,
  verification and failure modes of the exact pack revision (the worker prompt and the checkpoint package
  carry it; a title in a prompt grants nothing).
* ``role_evaluation`` derives a role's evaluation status from protected-suite EvalRuns that are bound to the
  pack's *current* revision (``configuration.role_revision_hash``). Anything else is NOT_EVALUATED or STALE —
  never PASS by default.
* ``refresh_role_eval_status`` writes the derived status to ``RoleSkillPack.eval_status``; the evaluation module
  calls it after a result is recorded.

No LLM is involved; everything is computed from canonical records.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.models.workforce import RoleSkillPack
from aeos.workforce import catalog

NOT_EVALUATED = "NOT_EVALUATED"
CONTRACT_FIELDS = ("inputs", "outputs", "example_outputs", "boundaries", "forbidden_actions", "verification",
                   "failure_modes", "allowed_scope")


def executor_role_keys() -> frozenset[str]:
    return catalog.EXECUTOR_ROLE_KEYS


def _catalog_spec(role_key: str) -> dict[str, Any] | None:
    return next((r for r in catalog.ALL_ROLES if r["role_key"] == role_key), None)


def _row(session: Session | None, role_key: str) -> RoleSkillPack | None:
    if session is None:
        return None
    return session.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == role_key))


def role_revision(session: Session | None, role_key: str) -> str | None:
    """Revision of the executor pack (seeded row first, else the code catalog). None = not an executor role."""
    if role_key not in catalog.EXECUTOR_ROLE_KEYS:
        return None
    row = _row(session, role_key)
    if row is not None and row.revision_hash:
        return row.revision_hash
    from aeos.workforce.seed import role_revision_hash

    spec = _catalog_spec(role_key)
    return role_revision_hash(spec) if spec else None


def role_contract(session: Session | None, role_key: str | None) -> dict[str, Any] | None:
    """The competence contract of an executor role (None when the role is not a catalog executor)."""
    if not role_key or role_key not in catalog.EXECUTOR_ROLE_KEYS:
        return None
    row = _row(session, role_key)
    if row is not None:
        data = {f: getattr(row, f) for f in CONTRACT_FIELDS}
        return {"role_key": role_key, "name": row.name, "version": row.version, "revision_hash": row.revision_hash,
                "eval_status": row.eval_status, **data}
    spec = _catalog_spec(role_key) or {}
    return {"role_key": role_key, "name": spec.get("name", role_key), "version": "1",
            "revision_hash": role_revision(None, role_key), "eval_status": NOT_EVALUATED,
            **{f: spec.get(f, [] if f != "allowed_scope" else {}) for f in CONTRACT_FIELDS}}


def role_evaluation(session: Session, role_key: str, revision_hash: str | None) -> dict[str, Any]:
    """PASS | FAIL | STALE | NOT_EVALUATED for *role_key* at *revision_hash*, with the basis."""
    from aeos.workforce.seed import evaluation_cases_for

    suites = [c["suite_key"] for c in evaluation_cases_for(role_key)]
    if not suites:
        return {"status": NOT_EVALUATED, "run_id": None, "suites": [],
                "basis": "belum ada suite evaluasi terproteksi untuk role ini"}
    try:
        from aeos.evaluation.service import eligibility
        from aeos.models.research import EvalRun
    except ImportError:
        return {"status": NOT_EVALUATED, "run_id": None, "suites": suites,
                "basis": "modul evaluasi tidak tersedia (fail closed)"}
    runs = list(session.scalars(select(EvalRun).where(
        EvalRun.role_key == role_key, EvalRun.suite_key.in_(suites), EvalRun.mode == "LIVE",
        EvalRun.status.in_(["COMPLETED", "FAILED"])).order_by(EvalRun.updated_at.desc())))
    other_revision = None
    for run in runs:
        bound = (run.configuration or {}).get("role_revision_hash")
        if not revision_hash or bound != revision_hash:
            other_revision = other_revision or run
            continue
        gates = eligibility(run.gate_results or {})
        accepted = int(((run.metrics or {}).get("outcome") or {}).get("accepted", 0) or 0)
        ok = run.status == "COMPLETED" and gates["eligible"] and accepted > 0
        return {"status": "PASS" if ok else "FAIL", "run_id": run.id, "suites": suites, "suite_key": run.suite_key,
                "basis": f"EvalRun {run.id} ({run.status}, gates {gates['status']}) untuk revisi role ini"}
    if other_revision is not None:
        return {"status": "STALE", "run_id": other_revision.id, "suites": suites,
                "basis": "hasil evaluasi ada untuk revisi role lain; revisi saat ini belum dievaluasi"}
    return {"status": NOT_EVALUATED, "run_id": None, "suites": suites,
            "basis": "belum ada EvalRun selesai yang terikat ke revisi role ini"}


def refresh_role_eval_status(session: Session, role_key: str | None = None) -> int:
    """Write the derived evaluation status into RoleSkillPack rows (all, or one role). Returns rows changed."""
    q = select(RoleSkillPack)
    if role_key:
        q = q.where(RoleSkillPack.role_key == role_key)
    changed = 0
    for row in session.scalars(q):
        status = role_evaluation(session, row.role_key, row.revision_hash)["status"]
        if row.eval_status != status:
            row.eval_status = status
            changed += 1
    session.flush()
    return changed
