"""Static policy vocabularies used by decisions, the external-write interlock and approvals.

These are control-plane contracts (§04, §14, §20, §26), not tunable thresholds. No MCI threshold
or exception is invented here (§02, §20).
"""

from __future__ import annotations

from aeos.core.enums import Domain

# §04 domain profiles: which external providers a domain may write to.
PROVIDERS_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    Domain.PERSONAL.value: ("google-drive", "google-docs", "github-issues", "github-repo"),
    Domain.OFFICE.value: ("confluence", "jira", "bitbucket"),
}
ALL_PROVIDERS: tuple[str, ...] = tuple(p for ps in PROVIDERS_BY_DOMAIN.values() for p in ps)
ATLASSIAN_PROVIDERS = frozenset({"confluence", "jira", "bitbucket"})

# Publication operations (U-10: one approval covers the explicit operations of one publication).
PUBLISH_OPERATIONS: dict[str, tuple[str, ...]] = {
    "google-drive": ("create_folder", "update_folder"),
    "google-docs": ("create_doc", "update_doc"),
    "github-issues": ("create_issue", "update_issue"),
    "confluence": ("create_folder", "create_page", "update_page"),
    "jira": ("create_issue", "update_issue"),
}
# Delivery operations performed only by the trusted broker on task branches (§31). No merge.
DELIVERY_OPERATIONS: dict[str, tuple[str, ...]] = {
    "github-repo": ("push_branch", "create_pr"),
    "bitbucket": ("push_branch", "create_pr"),
}
# Internal (non-external) operations an approval may bind.
PLAN_OPERATIONS: tuple[str, ...] = ("compile_dag", "execute_tasks")

# U-09: these are human-only and never authorised for AEOS components by any approval.
HUMAN_ONLY_OPERATIONS = frozenset({
    "merge", "merge_pr", "approve_pr", "push_default_branch", "force_push", "delete_branch",
    "deploy_production", "promote_production",
})

# ADRs that record an explicit owner instruction and may therefore back an ``owner:ADR-NNNN``
# (HUMAN_RECORDED) decision. Design ADRs written while building the seed are ACCEPTED documentation,
# not owner AUTH; an ADR file edit alone can never mint an owner actor. Extending this set is a code
# change under the protected path ``backend/aeos/policy/**`` (quarantined review, §09).
OWNER_RECORDED_ADRS = frozenset({"ADR-0003"})
# The only decision kinds an owner-recorded actor may hold. ADR-0003 §3 explicitly does NOT delegate
# plan approval, publication approval, risk acceptance, budgets, policy or OFFICE authorisation, so an
# ``owner:ADR-NNNN`` actor can never record an APPROVAL/POLICY/RISK_ACCEPTANCE/... decision, nor revoke one.
OWNER_RECORDED_KINDS = frozenset({"EXCEPTION"})

DECISION_KINDS = frozenset({
    "APPROVAL", "REJECTION", "REVISION_REQUEST", "EXCEPTION", "ADR", "POLICY", "TRIAL", "PROMOTION",
    "RISK_ACCEPTANCE", "PUBLICATION", "DOMAIN_SELECTION", "ROUTE_ACTIVATION", "BUDGET", "WRITE_ENABLEMENT",
})
# Kinds a human may record through the generic decision endpoint. APPROVAL/REJECTION/REVISION are
# recorded only through the plan decision endpoint, bound to an exact PlanVersion + hash.
GENERIC_DECISION_KINDS: tuple[str, ...] = ("EXCEPTION", "RISK_ACCEPTANCE", "WRITE_ENABLEMENT", "POLICY", "BUDGET")
# Kinds whose content is part of the active PolicySnapshot.
SNAPSHOT_DECISION_KINDS: tuple[str, ...] = ("POLICY", "EXCEPTION")

DECISION_STATUSES: tuple[str, ...] = ("ACTIVE", "EXPIRED", "INVALIDATED", "SUPERSEDED")

# Keys a POLICY decision may override in the OFFICE policy (§23). Anything else is ignored.
OFFICE_POLICY_KEYS: tuple[str, ...] = (
    "external_worker_allowed", "approved_worker_routes", "claude_max_allowed_for_office_data",
    "trace_destinations_allowed", "requires_isolated_runtime", "atlassian_deployment",
)

TARGET_PROVIDER_WRITE = "provider_write"
TARGET_OFFICE_POLICY = "office_policy"
TARGET_PLAN_VERSION = "plan_version"


def providers_for(domain: str | Domain) -> tuple[str, ...]:
    return PROVIDERS_BY_DOMAIN.get(Domain(domain).value, ())


def operations_for_provider(provider: str) -> tuple[str, ...]:
    return PUBLISH_OPERATIONS.get(provider, ()) + DELIVERY_OPERATIONS.get(provider, ())


def known_operation(provider: str, operation: str) -> bool:
    return operation in operations_for_provider(provider)
