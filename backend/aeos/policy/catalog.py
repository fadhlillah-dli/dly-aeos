"""Static policy vocabularies used by decisions, the external-write interlock and approvals.

These are control-plane contracts (§04, §14, §20, §26), not tunable thresholds. No MCI threshold
or exception is invented here (§02, §20).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aeos.core.enums import Domain
from aeos.portfolio import providers as pv

# §04 domain profiles: which external providers a domain may write to. One vocabulary for the
# Integration Registry, publisher, interlock, doctor and UI lives in ``aeos.portfolio.providers``.
PROVIDERS_BY_DOMAIN: dict[str, tuple[str, ...]] = dict(pv.WRITE_PROVIDERS)
ALL_PROVIDERS: tuple[str, ...] = tuple(p for ps in PROVIDERS_BY_DOMAIN.values() for p in ps)
ATLASSIAN_PROVIDERS = frozenset({pv.CONFLUENCE, pv.JIRA, pv.BITBUCKET})

# Publication operations (U-10: one approval covers the explicit operations of one publication run).
# These are exactly the operation kinds the publisher's outbox rows carry (aeos.publisher.jobs).
PUBLISH_OPERATIONS: dict[str, tuple[str, ...]] = {
    pv.GOOGLE_DRIVE: ("create_folder", "create_doc", "update_doc"),
    pv.GITHUB_ISSUES: ("ensure_label", "create_issue", "update_issue"),
    pv.CONFLUENCE: ("create_folder", "create_doc", "update_doc", "upload_attachment"),
    pv.JIRA: ("create_issue", "update_issue"),
}
# Publication writes need the run's PUBLICATION decision (ADR-0074) under an APPROVE_AND_PUBLISH approval.
TARGET_PUBLICATION = "publication"
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
    # §10 "Policy & Decision Registry: rollback decision": a human rollback of a change proposal, bound to the
    # proposal + accepted candidate hash and the rollback/restore operation that actually ran (ADR-0082).
    "ROLLBACK",
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


def normalize_provider(provider: str | None) -> str:
    """Canonical provider id for human input (legacy spellings such as ``google-docs`` are accepted on
    input and stored canonical). The interlock itself compares exact ids (fail closed)."""
    return pv.normalize(provider)


def operations_for_provider(provider: str) -> tuple[str, ...]:
    return PUBLISH_OPERATIONS.get(provider, ()) + DELIVERY_OPERATIONS.get(provider, ())


def known_operation(provider: str, operation: str) -> bool:
    return operation in operations_for_provider(provider)


def is_publication_write(provider: str, operation: str) -> bool:
    return operation in PUBLISH_OPERATIONS.get(provider, ())


TARGET_META_KEYS = frozenset({"provider", "label", "title", "kind", "display_name", "name"})


def target_in_scope(target: dict[str, Any], approved_targets: list[Any], provider: str) -> bool:
    """True when *target* lies within one approved target scope of the same provider.

    An approved target is a scope (e.g. ``{"provider": "google-drive", "root_id": "F1"}``); every
    identifying key it names must be present in the intent target with an equal value."""
    if not isinstance(target, dict) or not target:
        return False
    for scope in approved_targets or []:
        if not isinstance(scope, dict) or scope.get("provider") != provider:
            continue
        keys = [k for k in scope if k not in TARGET_META_KEYS]
        if not keys:
            continue  # a provider-only scope is too broad to bind a write
        if all(k in target and str(target[k]) == str(scope[k]) for k in keys):
            return True
    return False


# ---- exception decisions read by other modules (exact target shapes, ADR-0042 / ADR-0051) -------------

@dataclass(frozen=True)
class ExceptionPreset:
    target_type: str
    target_id: str
    domains: tuple[str, ...]
    details: Mapping[str, bool]  # fixed detail values the consumer requires
    flags: tuple[str, ...] = ()  # optional boolean details the owner chooses
    allowed_operations: tuple[str, ...] = ()
    consumer: str = ""


EXCEPTION_PRESETS: dict[str, ExceptionPreset] = {
    "secret_store_interim_keyring": ExceptionPreset(
        "secret_store", "interim-keyring", (Domain.PERSONAL.value,), {"secret_store_interim_keyring": True},
        allowed_operations=("store_personal_secrets",), consumer="aeos.secrets.provider.interim_exception"),
    "worker_sandbox_process": ExceptionPreset(
        "worker_sandbox", "process", (Domain.PERSONAL.value,), {"allow_process_sandbox": True}, ("allow_bash",),
        allowed_operations=("run_worker_process_sandbox",),
        consumer="aeos.worker_runtime.sandbox.process_sandbox_exception"),
}
# Detail keys whose meaning a consumer reads; they are accepted only on their preset target.
EXCEPTION_DETAIL_KEYS = frozenset({"secret_store_interim_keyring", "allow_process_sandbox", "allow_bash"})


def exception_preset_for_target(target_type: str | None, target_id: str | None) -> str | None:
    for name, spec in EXCEPTION_PRESETS.items():
        if spec.target_type == target_type and spec.target_id == target_id:
            return name
    return None
