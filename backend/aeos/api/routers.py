"""Router registry. Each control-plane module (§10) exposes ``router`` from ``<module>/router.py``.

Modules are code/contract boundaries, not microservices (§10). ``create_app(include=...)`` lets a
test mount only the modules it exercises.
"""

from __future__ import annotations

MODULE_ROUTERS: list[str] = [
    "aeos.identity.router",
    "aeos.events.router",
    "aeos.audit.router",
    "aeos.api.system",
    "aeos.doctor.router",
    "aeos.inventory.router",
    "aeos.portfolio.router",
    "aeos.intake.router",
    "aeos.knowledge.router",
    "aeos.planning.router",
    "aeos.policy.router",
    "aeos.claude_bridge.router",
    "aeos.routing.router",
    "aeos.secrets.router",
    "aeos.broker.router",
    "aeos.workforce.router",
    "aeos.worker_runtime.router",
    "aeos.verification.router",
    "aeos.publisher.router",
    "aeos.docs_engine.router",
    "aeos.delivery.router",
    "aeos.cost.router",
    "aeos.evaluation.router",
    "aeos.proposals.router",
    "aeos.research.router",
    "aeos.operations.router",
    "aeos.control_center.router",
]
