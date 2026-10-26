"""Static policy vocabularies used by decisions, the external-write interlock and approvals.

These are control-plane contracts (§04, §14, §20, §26), not tunable thresholds. No MCI threshold
or exception is invented here (§02, §20).
"""

from __future__ import annotations

import re
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
    "RETIREMENT",
})
# §33 "verifikasi final oleh pemilik yang sah": a RETIREMENT decision is recorded only through the
# retirement endpoint (``aeos.policy.retirement``), bound to the project and the hash of the checklist the
# owner verified. ``RETIRED`` is reachable only while that binding still matches the live checklist.
TARGET_PROJECT_RETIREMENT = "project"
RETIREMENT_OPERATIONS: tuple[str, ...] = ("retire_project",)
# §19/§21 "dua worker aktif": the concurrency mode is a POLICY decision on exactly this target, carrying
# the evaluation evidence and the rollback (previous mode) in its details (ADR-0050 amendment).
TARGET_WORKFORCE = "workforce"
TARGET_ID_CONCURRENCY = "concurrency"
# §44 DecisionRecord fields for adopting a routing/role/mode result. A mode/route adoption decision that
# lacks one of them is refused (the owner sees options, goal, evidence, trade-off and when it stops applying).
DECISION_RECORD_FIELDS: tuple[str, ...] = ("options", "goal", "evidence_ids", "cost_quality_tradeoff", "invalidate_if")
# Targets whose decision is written only by a dedicated endpoint that checks its own evidence first (company
# authorisation + company-approved route; passing evaluation run + DecisionRecord). The generic decision endpoint
# refuses them, so a decision on these targets can never skip those checks. (RETIREMENT is not a generic kind.)
TARGET_OFFICE_ROUTE = "office_route"
# §44 "Hasil routing/role/mode menjadi DecisionRecord": a PASS role evaluation becomes the basis of admission
# eligibility only through the owner's adoption decision on role_adoption/<role_key>, bound to the role revision
# and the exact EvalRun, carrying the DecisionRecord (workforce checks the evaluation before recording it).
TARGET_ROLE_ADOPTION = "role_adoption"
# §02 change control: the owner accepts/rejects/supersedes an ADR as a Decision of kind ADR on adr/<ADR-NNNN>,
# bound to the content hash of the loaded ADR file, recorded only from a live SUPER_ADMIN session (decisions page
# or ``aeos policy adr-decide`` with the password). A later edit of the file makes that owner status STALE.
TARGET_ADR = "adr"
ADR_OUTCOMES: tuple[str, ...] = ("ACCEPTED", "REJECTED", "SUPERSEDED")
DEDICATED_TARGETS: dict[str, str] = {
    TARGET_OFFICE_ROUTE: "POST /api/policy/office/routes",
    TARGET_WORKFORCE: "POST /api/workforce/concurrency",
    TARGET_ROLE_ADOPTION: "POST /api/workforce/roles/{role_key}/adopt",
    TARGET_ADR: "POST /api/adrs/{adr_key}/decision",
}
# §41/§43 "Perubahan policy, model, tool schema, hooks, prompt/memory, fallback, context strategy tetap system
# change dengan eval/decision/rollback": a generic POLICY decision on one of these targets changes how agents/models
# behave, so it is recorded only with completed, all-gates-PASS evaluation runs (evidence of exactly this change)
# and the rollback target it replaces. Model routes have their own activation path (config-hash-bound evidence).
SYSTEM_CHANGE_TARGET_TYPES = frozenset({
    "policy", "policy_rule", "agent_policy", "model", "model_id", "model_route", "route", "tool_schema", "tool_set",
    "tools", "hook", "hooks", "prompt", "prompt_revision", "memory", "memory_policy", "fallback", "fallback_chain",
    "context_strategy", "retrieval", "harness",
    # §15 supervisor stage-model pin (planning_baseline/stage_models, read by planning.configured_stage_models) is a
    # model change; §11 "Perubahan schema/tool/prompt/instruction/memory diperlakukan sebagai system change"
    "planning_baseline", "stage_models", "supervisor", "instruction", "instructions", "schema", "skill", "skills",
    "role_skill_pack", "agent_definition",
})


def normalize_target_type(target_type: str | None) -> str:
    """Case, ``-``, spaces, dots and slashes -> ``_`` (``Tool-Schema`` == ``tool_schema``)."""
    return re.sub(r"[^a-z0-9]+", "_", str(target_type or "").strip().lower()).strip("_")


def system_change_target(target_type: str | None) -> bool:
    """Whether a decision target is a §41/§43 system change. The spelling is normalised (case, ``-``, spaces, dots,
    slashes -> ``_``) so a variant such as ``Tool-Schema`` never records an unevaluated system change."""
    return normalize_target_type(target_type) in SYSTEM_CHANGE_TARGET_TYPES


# §41/§43 "evidence of exactly this change": how a cited EvalRun is bound to the change it is evidence of
# (``recording.system_change_evidence``). Any system change binds by its exact evaluated configuration:
# ``target_hash`` equal to the run's ``configuration_hash`` (or its ``route_config_hash``). Where the §43
# AIConfiguration has a field for the changed value, ``target_version`` naming the new value binds a run whose
# configuration carries exactly that value. A target type without such a field (hooks, tool schemas, skills, agent
# definitions, schemas) binds by the configuration hash only. A run that is not bound to the change is refused
# (SYSTEM_CHANGE_EVIDENCE_UNRELATED): an unrelated suite or configuration never counts as evaluation.
SYSTEM_CHANGE_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "policy": ("policy_version",), "policy_rule": ("policy_version",), "agent_policy": ("policy_version",),
    "model": ("model_id", "model_version"), "model_id": ("model_id", "model_version"),
    "supervisor": ("model_id", "model_version"),
    # route-like changes: target_version = the route key the change adopts (the run evaluated exactly that route)
    "model_route": ("provider_route",), "route": ("provider_route",), "fallback": ("provider_route",),
    "fallback_chain": ("provider_route",),
    "prompt": ("prompt_revision",), "prompt_revision": ("prompt_revision",), "instruction": ("prompt_revision",),
    "instructions": ("prompt_revision",),
    # target_version = the full tool set (comma separated), compared as a set
    "tool_set": ("tools",), "tools": ("tools",),
    "harness": ("harness_version",),
    # target_version = a value the evaluated context parameters carry (e.g. {"strategy": "rag-v2"})
    "context_strategy": ("context_params",), "retrieval": ("context_params",), "memory": ("context_params",),
    "memory_policy": ("context_params",),
}
# Targets bound by their own identity rather than a new value in target_version. Memory acceptance
# (aeos.knowledge.memory): every collected sample of the run had exactly this record (target_id) in its executed
# worker context (collector ``context_memory_ids``); stated context parameters alone never bind (review, wave 7).
IDENTITY_BOUND_TARGETS = frozenset({"memory_record"})
# §15 supervisor stage-model pin (read by planning.configured_stage_models): every pinned model id
# (details.models) needs a cited run of exactly that model (configuration model_id/model_version) - never a hash.
STAGE_MODEL_TARGET: tuple[str, str] = ("planning_baseline", "stage_models")


def is_stage_model_pin(target_type: str | None, target_id: str | None) -> bool:
    norm = normalize_target_type(target_type)
    return norm == "stage_models" or (norm == STAGE_MODEL_TARGET[0] and str(target_id or "").strip()
                                      == STAGE_MODEL_TARGET[1])
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


# ---- owner attestations and owner policy choices read by other modules (POLICY decisions, exact target shape) --
#
# §12 "Menonaktifkan penggunaan tambahan berbayar sesuai pengaturan resmi": AEOS cannot read the claude.ai account
# setting (no official API). The owner turns paid extra usage off in claude.ai and records it here; the Doctor
# check claude.extra_billing and the ClaudeNativeBridge view then show it as an owner attestation (evidence E0,
# CONFIGURED - never VERIFIED). The attestation needs an explicit expiry chosen by the owner (no default).
#
# §29 screenshot collector: whether it may make an authenticated, read-only capture of pages behind a login (e.g.
# the Control Center's own routes) is an owner POLICY choice (ALLOWED | DENIED, with the scope it covers), read by
# ``aeos.docs_engine.screenshots.authenticated_capture_status`` through the policy facade. No decision = UNDECIDED;
# UNDECIDED and DENIED keep every capture unauthenticated (fail closed), and so does ALLOWED while this build has
# no authenticated capture path.

TARGET_CLAUDE_EXTRA_USAGE = "claude_extra_usage"
TARGET_SCREENSHOT_CAPTURE = "screenshot_capture"
# §34 pack -> MCI control mapping approval (aeos.policy.snapshot.mci_reference, aeos.policy.recording)
TARGET_MCI_PACK_MAPPING = "mci_pack_mapping"


@dataclass(frozen=True)
class PolicyPreset:
    target_type: str
    target_id: str
    domains: tuple[str, ...]
    detail_key: str  # the detail the consumer reads
    value: bool | None = None  # attestation: the boolean this preset records
    consumer: str = ""
    label: str = ""
    # an owner attestation (E0, expiry required) states a setting AEOS cannot read; a choice preset records the
    # owner's policy choice among ``choices`` (expiry optional), with a free-text scope detail
    attestation: bool = True
    choices: tuple[str, ...] = ()
    scope_key: str | None = None
    scope_required_for: tuple[str, ...] = ()  # the choices that are recorded only with a stated scope


SCREENSHOT_CAPTURE_PRESET = "screenshot_authenticated_capture"
POLICY_PRESETS: dict[str, PolicyPreset] = {
    "claude_extra_usage_off": PolicyPreset(
        TARGET_CLAUDE_EXTRA_USAGE, Domain.PERSONAL.value, (Domain.PERSONAL.value,), "extra_usage_disabled", True,
        consumer="aeos.doctor.checks.claude.claude_extra_billing, aeos.claude_bridge.bridge.status",
        label="Atestasi pemilik: extra usage berbayar Claude NONAKTIF di pengaturan akun claude.ai"),
    SCREENSHOT_CAPTURE_PRESET: PolicyPreset(
        TARGET_SCREENSHOT_CAPTURE, Domain.PERSONAL.value, (Domain.PERSONAL.value,), "authenticated_capture",
        consumer="aeos.docs_engine.screenshots.authenticated_capture_status",
        label="Capture screenshot terautentikasi read-only (halaman di balik login, mis. rute Control Center)",
        attestation=False, choices=("ALLOWED", "DENIED"), scope_key="scope", scope_required_for=("ALLOWED",)),
}
# Detail keys whose meaning a consumer reads; they are accepted only on their preset target.
POLICY_PRESET_DETAIL_KEYS = frozenset(p.detail_key for p in POLICY_PRESETS.values())
# Length bounds of the free-text scope a choice preset records
POLICY_PRESET_SCOPE_MIN, POLICY_PRESET_SCOPE_MAX = 5, 500


def policy_preset_for_target(target_type: str | None, target_id: str | None) -> str | None:
    for name, spec in POLICY_PRESETS.items():
        if spec.target_type == target_type and spec.target_id == target_id:
            return name
    return None


def policy_preset_for_target_type(target_type: str | None) -> str | None:
    """The preset that owns *target_type*, whatever the target id: a decision on another id of a preset's target type
    (e.g. ``screenshot_capture/OFFICE``) is refused as a mismatch instead of being recorded where no consumer reads
    it."""
    return next((name for name, spec in POLICY_PRESETS.items() if spec.target_type == target_type), None)


# §08 Doctor ``worker.route_behaviour``: the owner's tool/schema failure-rate threshold, a POLICY decision on exactly
# this target with ``details.max_failure_rate`` in 0..1 (read by aeos.doctor.checks.worker._threshold; no default)
TARGET_ROUTE_BEHAVIOUR: tuple[str, str] = ("worker_route_behaviour", "tool_schema_failures")
ROUTE_BEHAVIOUR_DETAIL_KEY = "max_failure_rate"
