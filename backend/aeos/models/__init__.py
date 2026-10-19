"""Canonical control-plane records (master §13). Importing this package registers all mappers.

Module ownership: each file is owned by the module named in its docstring. Other modules read
these records through services; they do not create parallel tables for the same concept (§13:
"Nama objek baru ... bukan mandat membuat subsistem atau tabel duplikat").
"""

from aeos.models.audit import AuditEvent, Event
from aeos.models.cost import Budget, BudgetReservation, QuotaState, UsageLedgerEntry
from aeos.models.decisions import ArchitectureDecisionRecord, ArchitectureSnapshot, Decision, PolicySnapshot
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact, ReviewRecord
from aeos.models.identity import AuthSession, LoginAttempt, SystemSetting, User
from aeos.models.inventory import (
    CheckResult,
    ComponentObservation,
    ComponentReference,
    ComponentRelation,
    InstallManifestRecord,
)
from aeos.models.planning import (
    Feature,
    Milestone,
    Plan,
    PlanningArtifact,
    PlanningContext,
    PlanningRun,
    PlanVersion,
    TaskDependency,
    TaskSpec,
)
from aeos.models.portfolio import Attachment, Project, ProjectIntegration, RepositoryReference
from aeos.models.publishing import (
    DocumentRecord,
    DocumentRevision,
    ExternalReference,
    OperationalWorkProjection,
    Publication,
    PublicationOperation,
)
from aeos.models.research import (
    ChangeProposal,
    ContextPack,
    EvalCaseCandidate,
    EvalRun,
    EvalSuite,
    MemoryRecord,
    ResearchEntry,
)
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import AgentDefinition, Lease, RoleSkillPack, TaskExecution, WorkerRun

__all__ = [
    "AcceptanceRecord",
    "AgentDefinition",
    "ArchitectureDecisionRecord",
    "ArchitectureSnapshot",
    "Attachment",
    "AuditEvent",
    "AuthSession",
    "Budget",
    "BudgetReservation",
    "CapabilityGrant",
    "ChangeProposal",
    "CheckResult",
    "ComponentObservation",
    "ComponentReference",
    "ComponentRelation",
    "ContextPack",
    "Decision",
    "DocumentRecord",
    "DocumentRevision",
    "EvalCaseCandidate",
    "EvalRun",
    "EvalSuite",
    "Event",
    "EvidenceArtifact",
    "ExternalReference",
    "Feature",
    "InstallManifestRecord",
    "Lease",
    "LoginAttempt",
    "MemoryRecord",
    "Milestone",
    "ModelRoute",
    "OperationalWorkProjection",
    "Plan",
    "PlanVersion",
    "PlanningArtifact",
    "PlanningContext",
    "PlanningRun",
    "PolicySnapshot",
    "Project",
    "ProjectIntegration",
    "Publication",
    "PublicationOperation",
    "QuotaState",
    "RepositoryReference",
    "ResearchEntry",
    "ReviewRecord",
    "RoleSkillPack",
    "SystemSetting",
    "TaskDependency",
    "TaskExecution",
    "TaskSpec",
    "UsageLedgerEntry",
    "User",
    "WorkerRun",
]
