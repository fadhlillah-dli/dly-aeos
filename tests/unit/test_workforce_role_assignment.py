"""Organization view assignment (master §17 "OrganizationView projects catalog, assignment and ownership")."""

from __future__ import annotations

from aeos.core.enums import Domain
from aeos.workforce import views
from tests.unit.test_operations_sli import make_task


def test_roles_view_counts_active_tasks_per_role_and_domain(db, settings):
    from aeos.workforce import seed

    seed.run(db)
    db.commit()
    make_task(db, Domain.PERSONAL, "asg-p", "QUEUED")  # TaskSpec.role_key defaults to feature-implementer
    make_task(db, Domain.PERSONAL, "asg-p2", "DONE")  # terminal: not an assignment
    make_task(db, Domain.OFFICE, "asg-o", "QUEUED")
    roles = {r["role_key"]: r for g in views.roles_view(db, domain="PERSONAL")["groups"] for r in g["roles"]}
    assert roles["feature-implementer"]["assigned_tasks"] == 1
    office = {r["role_key"]: r for g in views.roles_view(db, domain="OFFICE")["groups"] for r in g["roles"]}
    assert office["feature-implementer"]["assigned_tasks"] == 1
    agents = views.agents_view(db, domain="PERSONAL")
    assert all("owner" in a for a in agents)
