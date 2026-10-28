"""The data class routing applies to a task (ADR-0133, §23, §24).

A TaskSpec is immutable and keeps the class it was compiled with. Routing applies the higher of that class and the
project's current class, so raising a project (``aeos project reclassify``) takes effect for its open work at once:
a less restrictive route stops taking those tasks at the next selection, fallback, capacity check and per-request
check of a decision C route. Lowering a project never relaxes a compiled task: the task's own class stays the floor.

Every routing reader of a task's class goes through ``effective_task_class``; it is the only place that combines them.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from aeos.core.enums import DataClassification
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project

ORDER: tuple[str, ...] = tuple(c.value for c in DataClassification)  # PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED


def effective_task_class(session: Session, task: TaskSpec) -> str:
    """max(task class, current project class).

    An unknown task label is returned unchanged so the caller's own validation refuses it (fail closed); an unknown
    project label counts as the strictest class. A task whose project row cannot be read keeps its own class (a
    project is never deleted, §33)."""
    own = str(task.data_classification or "").strip().upper()
    if own not in ORDER:
        return str(task.data_classification or "")
    project = session.get(Project, task.project_id) if task.project_id else None
    if project is None:
        return own
    floor = str(project.data_classification or "").strip().upper()
    if floor not in ORDER:
        return ORDER[-1]
    return ORDER[max(ORDER.index(own), ORDER.index(floor))]


def rank(label: str | None) -> int:
    """Position in ORDER; an unknown label ranks as the strictest class."""
    text = str(label or "").strip().upper()
    return ORDER.index(text) if text in ORDER else len(ORDER) - 1


def route_class_problem(route: object, klass: str) -> str | None:
    """Why *route* may not carry data of *klass* (None = it may): the class is not in the route's
    ``allowed_data_classes``, or the route's data terms restrict it to PUBLIC (training/trial/UNKNOWN terms or an
    UNVERIFIED model identity, ``routing.service.terms_restriction``)."""
    allowed = list(getattr(route, "allowed_data_classes", None) or [])
    if klass not in allowed:
        return f"data classification {klass} is not allowed by route {getattr(route, 'route_key', '?')} " \
               f"(allowed: {allowed})"
    if klass == ORDER[0]:
        return None
    try:
        from aeos.routing.service import terms_restriction
    except ImportError:  # only when a test substitutes the routing service: the class check above still holds
        return None
    restriction = terms_restriction(getattr(route, "data_terms", None) or {}, getattr(route, "identity_status", None))
    return f"{klass} data on route {getattr(route, 'route_key', '?')}: {restriction}" if restriction else None


def run_class_problem(session: Session, route: object, *, run_id: str | None) -> str | None:
    """Per-request check of the broker for ANY route (ADR-0133): the run's task, at its effective class (so a raise of
    its project applies at the next model call), must be data the route may carry. A grant without a run or a run
    without a task (a route probe) carries no task data and is not judged here; a run whose TaskSpec cannot be read
    is refused (fail closed)."""
    if not run_id:
        return None
    from aeos.models.workforce import WorkerRun

    run = session.get(WorkerRun, run_id)
    if run is None or not run.task_id:
        return None
    task = session.get(TaskSpec, run.task_id)
    if task is None:
        return "the run's TaskSpec cannot be read, so its data classification is unknown (fail closed)"
    klass = effective_task_class(session, task)
    problem = route_class_problem(route, klass)
    return f"the run's task is {klass}: {problem}" if problem else None
