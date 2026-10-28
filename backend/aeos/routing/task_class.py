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
