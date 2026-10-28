"""Canonical vocabularies from the master architecture.

Every enum here is referenced by section number so that changes stay traceable to the
design. Enums are str-based so they serialise naturally into JSON and SQLite.
"""

from __future__ import annotations

from enum import StrEnum


class Domain(StrEnum):
    """§04/U-05 — chosen by a human before any prompt; never inferred by a model."""

    PERSONAL = "PERSONAL"
    OFFICE = "OFFICE"


class Role(StrEnum):
    """§02 auth baseline — VIEWER is read-only and enforced by the backend."""

    SUPER_ADMIN = "SUPER_ADMIN"
    VIEWER = "VIEWER"


class EvidenceLevel(StrEnum):
    """§34 evidence ladder, always assigned per claim."""

    E0 = "E0"  # claim
    E1 = "E1"  # static
    E2 = "E2"  # controlled test
    E3 = "E3"  # runtime observation
    E4 = "E4"  # regression-protected


class CheckStatus(StrEnum):
    """§08 Readiness Doctor summary statuses (+ STALE from §08 invalidation)."""

    MISSING = "MISSING"
    CONFIGURED = "CONFIGURED"
    CONNECTED = "CONNECTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    STALE = "STALE"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Capability(StrEnum):
    """§08 minimal capability gates. Human-only merge/production is never a worker capability."""

    CAN_OPEN_DASHBOARD = "CAN_OPEN_DASHBOARD"
    CAN_PLAN_PERSONAL = "CAN_PLAN_PERSONAL"
    CAN_PLAN_OFFICE = "CAN_PLAN_OFFICE"
    CAN_PUBLISH_PERSONAL = "CAN_PUBLISH_PERSONAL"
    CAN_RUN_WORKER = "CAN_RUN_WORKER"
    CAN_REVIEW_WITH_MAX = "CAN_REVIEW_WITH_MAX"
    CAN_PREPARE_PR = "CAN_PREPARE_PR"
    CAN_USE_OFFICE_INTEGRATIONS = "CAN_USE_OFFICE_INTEGRATIONS"
    CAN_VALIDATE_STAGING = "CAN_VALIDATE_STAGING"


class PlanningState(StrEnum):
    """§14 planning stages plus revision/failure/publication branches."""

    DRAFT = "DRAFT"
    CONTEXT_READY = "CONTEXT_READY"
    PRIMARY_PLANNING = "PRIMARY_PLANNING"
    CHALLENGE_REVIEW = "CHALLENGE_REVIEW"
    SYNTHESIS = "SYNTHESIS"
    FINAL_REVIEW = "FINAL_REVIEW"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    REJECTED = "REJECTED"
    PLANNING_FAILED = "PLANNING_FAILED"
    REVIEW_FAILED = "REVIEW_FAILED"
    NEEDS_DECISION = "NEEDS_DECISION"
    PUBLICATION_PARTIAL = "PUBLICATION_PARTIAL"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    AWAITING_MANUAL_CLAUDE = "AWAITING_MANUAL_CLAUDE"  # §12 fallback: package handed to the user


class ExecutionState(StrEnum):
    """§14 canonical runtime states — never merged with planning/publication/acceptance state."""

    QUEUED = "QUEUED"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_EXECUTION_STATES = frozenset({ExecutionState.DONE, ExecutionState.FAILED, ExecutionState.CANCELLED})


class Verdict(StrEnum):
    """§34 ReviewRecord verdicts."""

    PASS_STATIC = "PASS-STATIC"
    PASS_CONTROLLED = "PASS-CONTROLLED"
    PASS_STAGING = "PASS-STAGING"
    PASS_PRODUCTION = "PASS-PRODUCTION"
    NOT_PROVEN = "NOT-PROVEN"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"


class RiskClass(StrEnum):
    """§20 risk classes; thresholds come from canonical policy (MCI) when available."""

    R0 = "R0"  # read
    R1 = "R1"  # local reversible change
    R2 = "R2"  # external/shared change
    R3 = "R3"  # production/destructive/privileged
    R4 = "R4"  # catastrophic


class ContentType(StrEnum):
    """§28 document content kinds."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    AI_MAINTAINED = "AI_MAINTAINED"
    HUMAN_GOVERNED = "HUMAN_GOVERNED"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


# §38 inventory dimensions — stored separately, never collapsed into one "active" badge.
class AdmissionStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    UNDER_REVIEW = "UNDER_REVIEW"
    TRIAL_APPROVED = "TRIAL_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"


class InstallationStatus(StrEnum):
    NOT_INSTALLED = "NOT_INSTALLED"
    INSTALLED = "INSTALLED"
    INSTALL_FAILED = "INSTALL_FAILED"
    REMOVED = "REMOVED"
    UNKNOWN = "UNKNOWN"


class ConfigurationStatus(StrEnum):
    MISSING = "MISSING"
    CONFIGURED = "CONFIGURED"
    INVALID = "INVALID"
    DRIFTED = "DRIFTED"
    UNKNOWN = "UNKNOWN"


class ConnectivityStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    CONNECTED = "CONNECTED"
    UNREACHABLE = "UNREACHABLE"
    EXPIRED_AUTH = "EXPIRED_AUTH"
    UNKNOWN = "UNKNOWN"


class FunctionalStatus(StrEnum):
    NOT_TESTED = "NOT_TESTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    STALE = "STALE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Enablement(StrEnum):
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class UsageStatus(StrEnum):
    NEVER_OBSERVED = "NEVER_OBSERVED"
    IDLE = "IDLE"
    IN_USE = "IN_USE"
    LAST_USED = "LAST_USED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class LifecycleFinal(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    QUARANTINED = "QUARANTINED"
    RETIRING = "RETIRING"
    RETIRED = "RETIRED"


class DesignLabel(StrEnum):
    """§37 labels for blueprint-seeded catalog entries (never installation evidence)."""

    PLANNED = "PLANNED"
    SELECTED_DESIGN = "SELECTED_DESIGN"
    PROFILE_DESIGN = "PROFILE_DESIGN"
    OPTIONAL_CANDIDATE = "OPTIONAL_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    UNVERIFIED = "UNVERIFIED"
    WATCHLIST = "WATCHLIST"


class DataMode(StrEnum):
    """§35/§49 — fixtures are always labelled; never shown as live workforce."""

    LIVE = "LIVE"
    MOCK = "MOCK"
    DEMO = "DEMO"
    DESIGN = "DESIGN"
    ESTIMATE = "ESTIMATE"


class RouteStatus(StrEnum):
    """ModelRoute.status (§23). TRIAL and APPROVED are the two ACTIVE states (a human decision is bound to the
    config hash); CANDIDATE/UNTESTED are catalogue rows, DISABLED is an owner switch-off."""

    CANDIDATE = "CANDIDATE"
    UNTESTED = "UNTESTED"
    TRIAL = "TRIAL"
    APPROVED = "APPROVED"
    DISABLED = "DISABLED"


class RouteAvailability(StrEnum):
    """ModelRoute.availability: whether the provider catalogue listed the model at the last refresh."""

    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
