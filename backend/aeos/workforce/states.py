"""§14 runtime state machine for TaskExecution (see ADR-0050).

Legal edges are exactly the §14 diagram plus the two cases the master requires the
implementation to handle explicitly:

* cancellation from every non-terminal state (QUEUED/BLOCKED/LEASED/REVIEW -> CANCELLED);
* lease expiry while RUNNING. The diagram only names ``LEASED -> BLOCKED: lease hilang``; a lost
  lease (or another §22 stop condition the controller enforces: quota exhausted, operator
  reassign) while RUNNING maps to ``RUNNING -> BLOCKED`` and is allowed ONLY with one of
  ``RUNNING_STOP_CODES``. Nothing else may leave RUNNING except REVIEW/FAILED/CANCELLED.

DONE is reachable only from REVIEW and only through ``scheduler.mark_accepted`` with a valid
AcceptanceRecord; ``transition_task`` refuses DONE (§14 "REVIEW --> DONE: penerimaan valid").

DONE is terminal with exactly one exception (ADR-0050 amendment 2026-10-18): §14 "Perubahan
head/base yang relevan membatalkan acceptance/approval lama dan memicu verifikasi ulang" and §50
"PR baru mengubah head atau base terkait -> Acceptance stale; retest/review". When the acceptance a
DONE task rests on becomes STALE, ``scheduler.reopen_for_reverification`` moves it ``DONE -> READY``
(a new bounded attempt) or ``DONE -> BLOCKED`` (no attempt left / plan no longer approved), and only
with a ``REOPEN_CODES`` reason code. No other caller can leave DONE.
"""

from __future__ import annotations

from aeos.core.enums import TERMINAL_EXECUTION_STATES, ExecutionState

S = ExecutionState

LEGAL_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    S.QUEUED: frozenset({S.READY, S.BLOCKED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.READY, S.CANCELLED}),
    S.READY: frozenset({S.LEASED, S.CANCELLED}),
    S.LEASED: frozenset({S.RUNNING, S.BLOCKED, S.CANCELLED}),
    S.RUNNING: frozenset({S.REVIEW, S.FAILED, S.CANCELLED, S.BLOCKED}),
    S.REVIEW: frozenset({S.READY, S.DONE, S.BLOCKED, S.CANCELLED}),
    # Re-verification only (acceptance STALE), never a free exit: see REOPEN_CODES / is_legal.
    S.DONE: frozenset({S.READY, S.BLOCKED}),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

# Reason codes that justify RUNNING -> BLOCKED (controller-enforced stop conditions, §22).
RUNNING_STOP_CODES = frozenset({"LEASE_LOST", "LEASE_LOST_PROCESS_ALIVE", "QUOTA_EXHAUSTED", "REASSIGNED",
                                "CONTROLLER_SHUTDOWN", "POLICY_CHANGED"})
# Reason codes that justify leaving DONE (acceptance invalidated by a head/base/config/plan change).
REOPEN_CODES = frozenset({"ACCEPTANCE_STALE"})

# Execution states in which a worker may hold resources (used for caps / write-set checks).
ACTIVE_STATES = frozenset({S.LEASED, S.RUNNING})
NON_TERMINAL_STATES = frozenset(s for s in S if s not in TERMINAL_EXECUTION_STATES)

# WorkerRun.execution_status for one attempt (subset of the same vocabulary).
RUN_TERMINAL = frozenset({S.DONE.value, S.FAILED.value, S.CANCELLED.value})


def is_legal(from_state: str, to_state: str, *, reason_code: str = "") -> bool:
    try:
        src, dst = S(from_state), S(to_state)
    except ValueError:
        return False
    if dst not in LEGAL_TRANSITIONS[src]:
        return False
    if src is S.DONE:
        return reason_code in REOPEN_CODES
    return not (src is S.RUNNING and dst is S.BLOCKED and reason_code not in RUNNING_STOP_CODES)


def all_pairs() -> list[tuple[ExecutionState, ExecutionState]]:
    return [(a, b) for a in S for b in S]
