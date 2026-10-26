"""Cross-module contract for the trusted capability broker (§25, §26, §31). Owned by ``aeos.broker``.

Workers never receive upstream credentials. They receive a short-lived capability token bound to
one run, scope and caps. Only the broker may push ``aeos/*`` task branches and open PRs; there is
no merge operation anywhere in the broker (human-only merge, U-09).

Provider follows the domain (U-02/U-03): PERSONAL runs push/open PRs on GitHub, OFFICE runs (and any
registered Bitbucket repository) on Bitbucket Cloud through ``aeos.broker.bitbucket_broker`` - same
signatures, same sha-pinned push / grant / interlock / no-integration guarantees (additive, ADR-0111).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.routing import CapabilityGrant, ModelRoute

# Route pools the model proxy can serve for worker tasks (additive contract, ADR-0106). ARENA/COMPARATOR/LOCAL/
# WATCHLIST never serve tasks: routing refuses such tasks at admission and the proxy refuses their requests.
SERVED_POOLS: tuple[str, ...] = ("WORKER",)
# Pools whose feasibility the owner can test through the controlled probe (additive, ADR-0111): one synthetic
# PUBLIC request on a free, TRIAL/APPROVED (hash-bound human decision) PERSONAL route; evidence only.
PROBE_POOLS: tuple[str, ...] = ("ARENA", "COMPARATOR", "LOCAL", "WORKER")
# Pools served ONLY to a TaskSpec bound to a RUNNING EvalRun of exactly that route (additive, wave 7, §49 M6 "model
# arena"): the MODEL_PROXY grant carries the evaluation scope (``scope["eval"]``) and the proxy re-checks it on every
# request through ``evaluation.facade.eval_serving`` (PERSONAL, TRIAL/APPROVED hash-bound, free, terms-eligible -
# the probe's checks), so an ARENA route never serves an ordinary task and never a paid request.
EVAL_POOLS: tuple[str, ...] = ("ARENA",)

# Upstream request shape (additive, ADR-0041 amendment 2026-10-25). The broker speaks the OpenAI chat-completions
# API only (``<endpoint>/chat/completions``); a route whose provider documents another API for the model
# (``mode_params.upstream_api``) is refused at the boundary instead of being sent a request it cannot answer.
UPSTREAM_API_CHAT = "openai.chat_completions"
UPSTREAM_APIS: tuple[str, ...] = (UPSTREAM_API_CHAT, "openai.responses", "anthropic.messages")
# Provider -> the only host its credential is ever sent to (a route edited to another host fails closed).
# ``opencode-go`` (additive, ADR-0041 amendment 2026-10-26): the Go subscription key goes to opencode.ai only.
PROVIDER_ENDPOINT_HOSTS: dict[str, tuple[str, ...]] = {
    "opencode-zen": ("opencode.ai",),
    "opencode-go": ("opencode.ai",),
    "gemini": ("generativelanguage.googleapis.com",),
}
# Endpoints an earlier seed wrote that contradict the provider documentation (E1, read 2026-10-25): S07 documents
# ``https://opencode.ai/inference/openai/v1/chat/completions`` (bare model ids; ``opencode/<id>`` is the OpenCode
# config format) and the Gemini API its OpenAI-compatible base ``.../v1beta/openai``. Old -> documented base.
SUPERSEDED_ENDPOINTS: dict[str, str] = {
    "https://opencode.ai/zen": "https://opencode.ai/inference/openai/v1",
    "https://generativelanguage.googleapis.com": "https://generativelanguage.googleapis.com/v1beta/openai",
}
# ``mode_params.reasoning`` of a route: a level the broker sets as ``reasoning_effort`` upstream, or a default
# marker (nothing is sent; the provider's default applies). A worker-sent ``reasoning_effort`` is never forwarded.
REASONING_LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")
REASONING_DEFAULT_MARKERS: tuple[str, ...] = ("HARNESS_DEFAULT", "PROVIDER_DEFAULT")


def issue_grant(
    session: Session, *, run_id: str, project_id: str, domain: str, kind: str, scope: dict[str, Any],
    ttl_seconds: int, max_requests: int | None = None, max_tokens: int | None = None,
    max_cost_usd: float | None = None,
) -> tuple[str, CapabilityGrant]:
    """Return (raw_token, grant). The raw token is handed to the sandbox only; DB stores its hash."""
    from aeos.broker.grants import issue_grant as impl

    return impl(session, run_id=run_id, project_id=project_id, domain=domain, kind=kind, scope=scope,
                ttl_seconds=ttl_seconds, max_requests=max_requests, max_tokens=max_tokens, max_cost_usd=max_cost_usd)


def revoke_grants(session: Session, *, run_id: str | None = None, all_active: bool = False, reason: str) -> int:
    from aeos.broker.grants import revoke_grants as impl

    return impl(session, run_id=run_id, all_active=all_active, reason=reason)


def push_task_branch(
    session: Session, *, run_id: str, worktree: str, branch: str, repo_full_name: str,
    expected_head_sha: str | None = None,
) -> dict[str, Any]:
    """Push only ``refs/heads/aeos/*`` without force/delete; never the default branch.

    Sha-pinned: the broker pushes ``<runtime head sha>:refs/heads/<branch>``. ``expected_head_sha``
    (additive, integration 2026-10-18) must equal that head or the push is refused (HEAD_MISMATCH).
    The result always carries ``head_sha`` and ``status`` PUSHED | FAILED | UNKNOWN (FAILED/UNKNOWN are
    returned, not raised: reconcile an UNKNOWN with ``reconcile_side_effect`` before any retry).

    Owner rule (additive, 2026-10-26, AGENTS.md invariant 11): every commit the push would publish (every commit of
    the head that no branch of the remote reaches yet, read just before the push) must be authored and committed by
    the configured identity of the provider (``commit_identities.github`` / ``.bitbucket``) and carry no AI
    attribution. A remote whose branches cannot be read is a FAILED push (nothing sent). Otherwise ``PolicyDenied`` with ``COMMIT_IDENTITY_MISSING`` (before any grant or
    credential), ``COMMIT_IDENTITY_MISMATCH``, ``AI_ATTRIBUTION_IN_COMMIT`` or ``COMMIT_RANGE_UNBOUNDED``; the
    refusal is audited and the outbox row FAILED (nothing reached the remote)."""
    from aeos.broker.git_broker import push_task_branch as impl

    return impl(session, run_id=run_id, worktree=worktree, branch=branch, repo_full_name=repo_full_name,
                expected_head_sha=expected_head_sha)


def create_pull_request(
    session: Session, *, run_id: str, repo_full_name: str, head: str, base: str, title: str, body: str
) -> dict[str, Any]:
    """Open a draft PR (GitHub, or Bitbucket for OFFICE). A title/body with AI attribution is refused with
    ``PolicyDenied(AI_ATTRIBUTION_IN_TEXT)`` before any credential is read (additive, 2026-10-26)."""
    from aeos.broker.git_broker import create_pull_request as impl

    return impl(session, run_id=run_id, repo_full_name=repo_full_name, head=head, base=base, title=title, body=body)


def credential_ref_problem(domain: str, ref: str, *, project_id: str | None = None) -> str | None:
    """Why a route's ``mode_params.credential_ref`` may not serve a worker model call of *domain*
    (None = allowed): OFFICE -> ``office/`` only; PERSONAL -> ``personal/``, ``workers/`` or the run's own
    ``projects/<id>/``; ``platform/`` never. Routing uses the same rule for eligibility (additive,
    integration 2026-10-18)."""
    from aeos.broker.model_proxy import credential_namespace_problem as impl

    return impl(domain, ref, project_id=project_id)


def upstream_request_shape(route: ModelRoute) -> dict[str, Any]:
    """The upstream request the broker would build for *route* - ``url`` (``<endpoint>/chat/completions``),
    ``model`` (the bare upstream model id), ``catalog_model_id``, ``api``, ``reasoning_effort`` (or ``NOT_SENT``) -
    and ``code``/``problem`` when the broker refuses to build it (no secret, no DB; additive, ADR-0041 amendment
    2026-10-25). Routing uses it so a route the broker refuses is never selected or activated."""
    from aeos.broker.model_proxy import request_shape as impl

    return impl(route)


def probe_route(session: Session, *, route_key: str, actor: str) -> dict[str, Any]:
    """§23 route feasibility: one synthetic PUBLIC request to a free, human-activated (TRIAL/APPROVED, hash-bound)
    PERSONAL route of ``PROBE_POOLS``; records PROBE evidence (E3) + ledger + audit and changes nothing about the
    route. Raises PolicyDenied when the route may not be probed (additive, ADR-0111; caller commits)."""
    from aeos.broker.feasibility import probe_route as impl

    return impl(session, route_key, actor=actor)


def reconcile_side_effect(session: Session, *, side_effect_id: str, actor: str) -> dict[str, Any]:
    """Read the external state of a PENDING/UNKNOWN push/PR and record it before any retry (§14).
    Read-only towards the provider. Added by the completeness review; existing signatures unchanged."""
    from aeos.broker.git_broker import reconcile_side_effect as impl

    return impl(session, side_effect_id, actor=actor)
