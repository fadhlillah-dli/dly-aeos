"""Trusted git broker: push ``aeos/*`` task branches and open PRs (§26, §31). See ADR-0053.

Boundary rules (each tested):
* only ``refs/heads/aeos/<...>:refs/heads/aeos/<...>`` (same name) is ever pushed — no default or
  protected branch, no force (``+``), no deletion (empty source), no tags, exactly one refspec;
* the refspec and the remote URL are built by the broker, never taken from the worktree config;
  credential-bearing or non-https remotes are rejected;
* the candidate repository is untrusted: the broker fetches the task branch into a fresh,
  broker-private bare mirror and pushes from there, with hooks disabled and the credential given
  to that single git invocation through ``GIT_CONFIG_COUNT`` (``http.<url>.extraHeader``) — it is
  never written to any config file and never passed on the command line;
* every operation needs an ACTIVE run-bound capability grant (GIT_BRANCH / PR_CREATE) and, for a
  remote write, the external-write interlock (§26);
* owner rule R44 (ADR-0132): every push and PR of a worker run needs the run's latest Claude checkpoint review to
  be a PASS bound to exactly the head commit being published - otherwise ``REVIEW_REQUIRED``, audited, before any
  grant is consumed or credential read; and the pushed history must be exactly the runtime's single candidate
  commit on the run's base (the net diff the reviewer saw) - worker-made commits are never published
  (``REVIEW_REQUIRED``, reason ``HISTORY``);
* the broker has no capability to integrate a PR into any branch: human-only (U-09, §31);
* owner rule (AGENTS.md invariant 11): before any push every commit of the pushed range (every commit of the
  head that no branch of the remote reaches yet) is authored AND committed by the configured identity of the
  provider (``commit_identities.github`` / ``.bitbucket``) and carries no AI attribution - otherwise
  ``COMMIT_IDENTITY_MISSING`` / ``COMMIT_IDENTITY_MISMATCH`` / ``AI_ATTRIBUTION_IN_COMMIT``, audited, nothing
  pushed; PR titles/bodies with AI attribution are refused (``AI_ATTRIBUTION_IN_TEXT``) before any credential is
  read.

OFFICE runs (and registered Bitbucket repositories) are handed to ``aeos.broker.bitbucket_broker``
before any GitHub validation or credential read (``is_bitbucket_run``, ADR-0111).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.broker import grants
from aeos.core import commit_identity as cid
from aeos.core.attribution import find_ai_attribution, refusal_message
from aeos.core.errors import AeosError, Blocked, NotFound, PolicyDenied, ValidationFailed
from aeos.core.ids import new_id
from aeos.core.redaction import redact_text, register_secret
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.portfolio import RepositoryReference
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun

GITHUB_API = "https://api.github.com"
GIT_TIMEOUT = 180
GITHUB_SECRET_REF = "personal/github"
# Secrets contract: purpose = "<namespace>:<use>" with the reference's namespace (aeos.secrets.facade).
GITHUB_SECRET_PURPOSE = "personal:broker.github"
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_BRANCH_RE = re.compile(r"^aeos/[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
PROTECTED_BRANCH_NAMES = frozenset({"main", "master", "trunk", "develop", "development", "release", "production",
                                    "prod", "staging", "head"})


# ----------------------------------------------------------------------------- validation
def validate_repo(repo_full_name: str) -> str:
    if not isinstance(repo_full_name, str) or not _REPO_RE.match(repo_full_name) or ".." in repo_full_name:
        raise ValidationFailed("repository must be owner/name")
    return repo_full_name


def validate_branch(branch: str, *, default_branch: str = "main") -> str:
    if not isinstance(branch, str) or not _BRANCH_RE.match(branch):
        raise PolicyDenied("only aeos/* task branches may be pushed", code="BRANCH_NOT_ALLOWED")
    bad = ("..", "//", "@{", "\\")
    if any(b in branch for b in bad) or branch.endswith(("/", ".", ".lock")):
        raise PolicyDenied("invalid branch name", code="BRANCH_NOT_ALLOWED")
    if any(seg.startswith(".") or seg.endswith(".lock") for seg in branch.split("/")):
        raise PolicyDenied("invalid branch name", code="BRANCH_NOT_ALLOWED")
    tail = branch.split("/", 1)[1].lower()
    if branch.lower() == default_branch.lower() or (tail in PROTECTED_BRANCH_NAMES and "/" not in tail):
        raise PolicyDenied("default/protected branch names are never pushed", code="BRANCH_NOT_ALLOWED")
    return branch


_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def validate_refspec(refspec: str, *, default_branch: str = "main") -> tuple[str, str]:
    """Accept exactly ``refs/heads/aeos/X:refs/heads/aeos/X`` or the sha-pinned form
    ``<full commit sha>:refs/heads/aeos/X``; reject everything else (force, deletion, tags, other
    destinations, several refspecs, abbreviated or symbolic sources)."""
    if not isinstance(refspec, str) or not refspec or any(c.isspace() for c in refspec) or "," in refspec or ";" in refspec:
        raise PolicyDenied("exactly one refspec is allowed", code="REFSPEC_REJECTED")
    if refspec.startswith("+"):
        raise PolicyDenied("force push is not allowed", code="REFSPEC_REJECTED")
    if refspec.count(":") != 1:
        raise PolicyDenied("refspec must be explicit src:dst", code="REFSPEC_REJECTED")
    src, dst = refspec.split(":", 1)
    if not src:
        raise PolicyDenied("branch deletion is not allowed", code="REFSPEC_REJECTED")
    if src.startswith("refs/tags/") or dst.startswith("refs/tags/"):
        raise PolicyDenied("tags are not pushed by the broker", code="REFSPEC_REJECTED")
    if not dst.startswith("refs/heads/aeos/") or (src != dst and not _SHA_RE.match(src)):
        raise PolicyDenied("only refs/heads/aeos/* may be pushed, to the same name or from an exact commit sha",
                           code="REFSPEC_REJECTED")
    validate_branch(dst[len("refs/heads/"):], default_branch=default_branch)
    return src, dst


def _is_local_remote(url: str) -> bool:
    if url.startswith("file://"):
        return True
    p = Path(url)
    return p.is_absolute() and p.exists()


def validate_remote_url(url: str) -> str:
    if not isinstance(url, str) or not url or url.startswith("-") or "::" in url:
        raise PolicyDenied("invalid remote", code="REMOTE_REJECTED")
    if _is_local_remote(url):
        return url
    u = urlparse(url)
    if u.scheme != "https" or u.username or u.password or "@" in (u.netloc or "") or u.query or u.fragment:
        raise PolicyDenied("remote must be an https URL without embedded credentials", code="REMOTE_REJECTED")
    return url


def github_remote(repo_full_name: str) -> str:
    return f"https://github.com/{validate_repo(repo_full_name)}.git"


# ----------------------------------------------------------------------------- §08 per-call observation
# Broker calls carry the same permission signal as the publisher's: a 401/403 on a PR call or on ``git push`` over
# HTTPS means the stored credential was rejected or its permissions changed. They are reported (method, URL, status,
# response headers - never a body or a credential) to the same observer hook, so the dependent readiness checks go
# STALE without waiting for the next ``aeos doctor --functional`` (aeos.doctor.provider_watch).

_GIT_HTTP_STATUS = re.compile(r"The requested URL returned error:\s*(\d{3})")
_GIT_AUTH_FAILED = re.compile(r"Authentication failed for|Invalid username or password|could not read Username",
                              re.IGNORECASE)
_GIT_DENIED = re.compile(r"Permission to \S+ denied|HTTP 403|403 Forbidden", re.IGNORECASE)


def observe_response(response: httpx.Response) -> None:
    """``httpx`` response event hook of the broker's API clients."""
    try:
        from aeos.publisher.facade import observe_httpx_response
    except ImportError:  # pragma: no cover - the publisher ships with the platform
        return
    observe_httpx_response(response)


def http_client() -> httpx.Client:
    """The broker's API client: no redirects, no proxy environment, every response observed (§08)."""
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False,
                        event_hooks={"response": [observe_response]})


def git_http_status(output: str) -> int | None:
    """The HTTP status a failed ``git`` call over HTTPS reports in its output (None when it names none)."""
    m = _GIT_HTTP_STATUS.search(output or "")
    if m:
        return int(m.group(1))
    if _GIT_AUTH_FAILED.search(output or ""):
        return 401
    if _GIT_DENIED.search(output or ""):
        return 403
    return None


def observe_git_failure(remote_url: str, output: str, *, operation: str) -> None:
    """Report the HTTP status of a failed ``git push`` / ``ls-remote`` to the observer hook (local remotes and
    outputs without a status are ignored). The URL never carries credentials (``validate_remote_url``)."""
    if not remote_url or _is_local_remote(remote_url):
        return
    status = git_http_status(output)
    if status is None:
        return
    u = urlparse(remote_url)
    try:
        from aeos.publisher.facade import observe_provider_response
    except ImportError:  # pragma: no cover
        return
    observe_provider_response(f"GIT {operation}", f"{u.scheme}://{u.hostname or ''}{u.path}", status, {})


# ----------------------------------------------------------------------------- helpers
def is_bitbucket_run(session: Session, run: WorkerRun, repo_full_name: str) -> bool:
    """OFFICE code lives in Bitbucket (U-02/U-03): every OFFICE run, and any target registered as a Bitbucket
    repository, goes through ``aeos.broker.bitbucket_broker`` - never the GitHub path or its credential."""
    if run.domain == "OFFICE":
        return True
    ref = _repo_ref(session, run.project_id, repo_full_name) if isinstance(repo_full_name, str) else None
    return ref is not None and (ref.provider or "").upper() == "BITBUCKET"


def _repo_ref(session: Session, project_id: str, repo_full_name: str) -> RepositoryReference | None:
    return session.scalar(select(RepositoryReference).where(RepositoryReference.project_id == project_id,
                                                            RepositoryReference.full_name == repo_full_name))


def _require_grant(session: Session, run: WorkerRun, kind: str, match: dict[str, str]) -> CapabilityGrant:
    rows = session.scalars(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id, CapabilityGrant.kind == kind,
                                                         CapabilityGrant.active.is_(True)))
    now = utcnow()
    for g in rows:
        from aeos.core.timeutil import ensure_aware

        if g.revoked_at is not None or ensure_aware(g.expires_at) <= now:
            continue
        scope = g.scope or {}
        if all(str(scope.get(k)) == v for k, v in match.items()):
            if not grants.consume_request(session, g.id):
                raise PolicyDenied(f"{kind} capability already used up", code="CAPABILITY_EXHAUSTED")
            return g
    raise PolicyDenied(f"no active {kind} capability for this run/target", code="CAPABILITY_MISSING")


def _interlock(session: Session, run: WorkerRun, *, provider: str, operation: str, target: dict[str, Any],
               grant: CapabilityGrant) -> None:
    from aeos.models.planning import TaskSpec
    from aeos.policy import facade as policy

    task = session.get(TaskSpec, run.task_id) if run.task_id else None
    try:
        # The write is bound to the ACTIVE human APPROVAL of the run's exact plan version + hash that allows
        # this operation; without it the interlock refuses (DECISION_MISSING) — integration amendment ADR-0053.
        approval = policy.find_active_decision(
            session, kind="APPROVAL", target_type="plan_version", target_id=task.plan_version_id,
            target_hash=task.plan_version_hash, operation=operation) if task is not None else None
        intent = policy.WriteIntent(
            actor_user_id=run.requested_by or "", project_id=run.project_id, domain=run.domain, provider=provider,
            operation=operation, target=target, approved_plan_version_id=task.plan_version_id if task else None,
            approved_hash=task.plan_version_hash if task else None, decision_id=approval.id if approval else None,
            capability_expires_at=grant.expires_at, extra={"run_id": run.id, "broker": True})
        res = policy.check_external_write(session, intent)
    except ImportError as exc:
        raise Blocked("policy interlock unavailable; external write refused (fail closed)",
                      code="INTERLOCK_UNAVAILABLE") from exc
    if not res.allowed:
        raise PolicyDenied("external-write interlock denied: " + "; ".join(res.reasons), code="INTERLOCK_DENIED",
                           details={"reasons": res.reasons})


PUBLISHABLE_RUN_STATES = frozenset({"REVIEW", "DONE"})
# Owner rule R44 (2026-10-28, ADR-0132): no worker candidate is published before a Claude review PASS of its exact head.
REVIEW_REQUIRED = "REVIEW_REQUIRED"


def _require_publishable(session: Session, run: WorkerRun, operation: str) -> None:
    """Only a runtime-collected candidate may leave the machine (§21, §50 "Worker mengubah
    grader/policy/pipeline di luar kontrak -> Ditolak sebelum promotion/remote publication").

    * the run finished its execution phase (REVIEW/DONE) — never a running, failed or cancelled one;
    * the runtime recorded the candidate head commit (what verification/review looked at);
    * a quarantined patch needs an ACTIVE human EXCEPTION decision bound to this run and that exact
      head commit (special review, ADR-0054);
    * owner rule R44 (ADR-0132): the run's latest Claude checkpoint review is a PASS bound to exactly that head
      commit (``_require_claude_review``).

    Every remote write of a run calls this first - GitHub and Bitbucket push and PR creation."""
    from aeos.workforce.integrations import quarantine_release

    if run.execution_status not in PUBLISHABLE_RUN_STATES:
        raise PolicyDenied(f"run is {run.execution_status}; only REVIEW/DONE candidates are published",
                           code="RUN_NOT_PUBLISHABLE")
    if not run.head_commit:
        raise PolicyDenied("no runtime-recorded head commit for this run", code="NO_RUNTIME_HEAD")
    if run.quarantined and quarantine_release(session, run) is None:
        audit.record(session, actor_type="service", actor_id="broker", action=f"broker.{operation}",
                     result="DENIED", target_type="run", target_id=run.id, project_id=run.project_id,
                     domain=run.domain, reason="quarantined patch without special-review release decision",
                     details={"head_commit": run.head_commit, "reasons": run.quarantine_reasons[:20]})
        raise PolicyDenied("patch is quarantined: special review + owner release decision bound to the head "
                           "commit is required before remote publication", code="QUARANTINED",
                           details={"reasons": [str(r) for r in (run.quarantine_reasons or [])[:20]]})
    _require_claude_review(session, run, operation)


def _require_claude_review(session: Session, run: WorkerRun, operation: str) -> None:
    """Owner rule R44 (2026-10-28, ADR-0132), paraphrased: all OpenCode and OpenRouter work passes a Claude review; a
    worker has no right to commit, push, let alone merge (the verbatim text is in the owner requests register).

    The run's runtime-recorded head - the only commit the broker pushes or opens a PR for - must carry a Claude
    checkpoint review PASS bound to exactly that commit (``aeos.verification.facade.publication_review``). No review,
    a review of another (older) head, a review of a truncated package, a non-PASS verdict, a PASS contradicted by its
    own findings, a MOCK review or a resolved reviewer model that is not Opus (owner rule 2026-10-28: never Fable):
    ``PolicyDenied(REVIEW_REQUIRED)``, audited, before any grant is consumed or credential read. The verification
    module missing is a refusal too (fail closed). The pushed history is checked in the mirror (``_history_problem``)."""
    head = run.head_commit or ""
    try:
        from aeos.verification import facade as verification

        gate = verification.publication_review(session, run, head)
    except ImportError:
        reason, message, details = ("REVIEW_GATE_UNAVAILABLE",
                                    f"no Claude review PASS for head {head[:12] or 'NONE'}: the review gate is "
                                    "unavailable (fail closed)", {})
    else:
        if gate.allowed:
            return
        reason, message, details = gate.reason, gate.message, gate.as_dict()
    details = {**details, "reason": reason, "head_commit": head, "operation": operation}
    audit.record(session, actor_type="service", actor_id="broker", action=f"broker.{operation}", result="DENIED",
                 target_type="run", target_id=run.id, project_id=run.project_id, domain=run.domain,
                 reason=f"{REVIEW_REQUIRED}: {message}"[:500], details={"code": REVIEW_REQUIRED, **details})
    raise PolicyDenied(message, code=REVIEW_REQUIRED, details=details)


def _side_effect(session: Session, run: WorkerRun, *, kind: str, key: str, target: dict[str, Any]) -> BrokerSideEffect:
    se = session.scalar(select(BrokerSideEffect).where(BrokerSideEffect.idempotency_key == key))
    if se is None:
        se = BrokerSideEffect(run_id=run.id, project_id=run.project_id, domain=run.domain, kind=kind, target=target,
                              idempotency_key=key, status="PENDING")
        session.add(se)
        session.flush()
    se.attempts = (se.attempts or 0) + 1
    return se


GITHUB_GIT_USER = "x-access-token"


def _basic_header(token: str, user: str = GITHUB_GIT_USER) -> str:
    raw = base64.b64encode(f"{user}:{token}".encode()).decode()
    register_secret(raw)
    return f"Authorization: Basic {raw}"


def git_auth_env(remote_url: str, token: str | None, *, empty_global: Path,
                 basic_user: str = GITHUB_GIT_USER) -> dict[str, str]:
    """Environment for ONE git invocation. The credential exists only in this child's env. ``basic_user`` is
    the provider's HTTP user for the token (GitHub ``x-access-token``; Bitbucket per credential kind)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k.upper() not in {"GCM_INTERACTIVE"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty_global),
                "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    if token:
        u = urlparse(remote_url)
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{u.scheme}://{u.netloc}/.extraHeader",
                    "GIT_CONFIG_VALUE_0": _basic_header(token, basic_user)})
    return env


def _git_cmd(args: list[str], hooks: Path) -> list[str]:
    return ["git", "-c", f"core.hooksPath={hooks.as_posix()}", "-c", "credential.helper=", "-c", "core.fsmonitor=false",
            "-c", "http.followRedirects=false", "-c", "protocol.ext.allow=never", *args]


def _git(args: list[str], *, cwd: Path, env: dict[str, str], hooks: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_git_cmd(args, hooks), cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT, check=False)


# ----------------------------------------------------------------------------- owner rule: commit identity gate
@dataclass(frozen=True)
class CommitPolicy:
    """What every pushed commit must satisfy (AGENTS.md invariant 11): authored and committed by ``email`` - the
    configured identity of ``provider`` - and free of AI attribution. The range is what the push would publish:
    every commit of the head that no branch of the remote reaches yet (read from the remote just before the push),
    up to ``MAX_CHECKED_COMMITS`` (more is refused, fail closed)."""

    provider: str
    email: str

    def __repr__(self) -> str:  # never print the address
        return f"CommitPolicy(provider={self.provider!r})"


def commit_policy(session: Session, run: WorkerRun, provider: str, *, action: str) -> CommitPolicy:
    """The push gate for *provider*, or ``PolicyDenied(COMMIT_IDENTITY_MISSING)`` (audited) when the owner has no
    valid identity configured for it - checked before any grant is consumed or credential read."""
    from aeos.core.config import get_settings

    ident = cid.resolve(get_settings(), provider)
    if not ident.configured:
        message = cid.missing_message(ident)
        audit.record(session, actor_type="service", actor_id="broker", action=action, result="DENIED",
                     target_type="run", target_id=run.id, project_id=run.project_id, domain=run.domain,
                     reason=message, details={"code": cid.CODE_MISSING, "provider": provider,
                                              "config_key": ident.key, "status": ident.status})
        raise PolicyDenied(message, code=cid.CODE_MISSING,
                           details={"provider": provider, "config_key": ident.key, "status": ident.status})
    return CommitPolicy(provider=provider, email=ident.email or "")


# The remote's branches as the broker read them just before the push; a private namespace of the throw-away mirror
# that is never pushed (the push refspec names the head sha and the task branch only).
REMOTE_REFS = "refs/aeos-remote/heads"


def _read_remote_branches(mirror: Path, remote_url: str, env: dict[str, str], hooks: Path) -> str | None:
    """Fetch every branch of the remote into ``REMOTE_REFS`` (an empty remote has none). What they reach is already
    on the code host; everything else of the head is what the push would publish. Returns why the remote could not
    be read (None = read)."""
    proc = _git(["fetch", "--no-tags", "--no-recurse-submodules", "--quiet", remote_url,
                 f"+refs/heads/*:{REMOTE_REFS}/*"], cwd=mirror, env=env, hooks=hooks)
    if proc.returncode == 0:
        return None
    out = redact_text((proc.stdout or "") + (proc.stderr or ""))[:400]
    observe_git_failure(remote_url, out, operation="FETCH")
    return out or f"git fetch exited {proc.returncode}"


def _pushed_commit_problems(mirror: Path, env: dict[str, str], hooks: Path, head: str,
                            policy: CommitPolicy) -> list[dict[str, str]]:
    """Check every commit the push would publish: the head's commits that no branch of the remote reaches
    (``_read_remote_branches`` ran first) - the run's own commits, and any commit below the run's base that is not on
    the remote yet. Raw objects are read with ``cat-file --batch`` (exact sizes), so a crafted message cannot forge
    another commit's identity."""
    listed = _git(["rev-list", f"--max-count={cid.MAX_CHECKED_COMMITS + 1}", head, "--not", f"--glob={REMOTE_REFS}"],
                  cwd=mirror, env=env, hooks=hooks)
    if listed.returncode != 0:
        return [{"sha": head, "code": cid.CODE_RANGE, "detail": "the commit range could not be read"}]
    shas = listed.stdout.split()
    if len(shas) > cid.MAX_CHECKED_COMMITS or not all(cid.valid_sha(s) for s in shas):
        return [{"sha": head, "code": cid.CODE_RANGE,
                 "detail": f"more than {cid.MAX_CHECKED_COMMITS} commits that are not on the remote yet (push the "
                           "existing history yourself first)" if len(shas) > cid.MAX_CHECKED_COMMITS
                           else "unexpected rev-list output"}]
    if not shas:
        return []
    proc = subprocess.run(_git_cmd(["cat-file", "--batch"], hooks), cwd=str(mirror), env=env,
                          input=("\n".join(shas) + "\n").encode("ascii"), capture_output=True, timeout=GIT_TIMEOUT,
                          check=False)
    try:
        if proc.returncode != 0:
            raise ValueError("cat-file failed")
        commits = cid.parse_cat_file_batch(proc.stdout)
    except ValueError:
        return [{"sha": head, "code": cid.CODE_RANGE, "detail": "the pushed commits could not be read"}]
    if [c.sha for c in commits] != shas:
        return [{"sha": head, "code": cid.CODE_RANGE, "detail": "the pushed commits could not be read"}]
    return cid.commit_problems(commits, provider=policy.provider, expected_email=policy.email)


def refuse_push(session: Session, run: WorkerRun, se: BrokerSideEffect, res: dict[str, Any], *, action: str,
                provider: str) -> None:
    """A push the commit gate refused: nothing reached the remote. The outbox row is FAILED, the refusal audited,
    and ``PolicyDenied`` raised with the gate's code (the caller commits before re-raising, like delivery does)."""
    problems = list(res.get("problems") or [])[:20]
    reason = {"reason": str(res["reason"])} if res.get("reason") else {}
    se.status = "FAILED"
    se.commit_sha = res.get("commit")
    se.result_summary = str(res.get("error") or "")[:500]
    audit.record(session, actor_type="service", actor_id="broker", action=action, result="DENIED", target_type="run",
                 target_id=run.id, project_id=run.project_id, domain=run.domain, reason=str(res.get("error", ""))[:500],
                 details={"code": res.get("code"), "provider": provider, "commit": res.get("commit"),
                          "problems": problems, **reason})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=run.id, run_id=run.id,
                project_id=run.project_id, domain=run.domain,
                payload={"kind": "PUSH_BRANCH", "status": "REFUSED", "code": res.get("code"), "provider": provider,
                         "commit": res.get("commit")})
    raise PolicyDenied(str(res.get("error") or "push refused"), code=str(res.get("code") or cid.CODE_IDENTITY_MISMATCH),
                       details={"side_effect_id": se.id, "provider": provider, "problems": problems,
                                "head_commit": res.get("commit"), **reason})


def _history_problem(mirror: Path, env: dict[str, str], hooks: Path, head: str, base: str) -> str | None:
    """Owner rule R44 (ADR-0132): what is pushed is what the reviewer saw. The checkpoint review looks at the net diff
    ``base..head``; a history with worker-made commits could carry content that a later commit removed (a secret
    added and deleted) and the review never saw. So the head must be one commit whose only parent is the run's
    runtime-recorded base. Returns why not (None = exactly one candidate commit on the base)."""
    if not _SHA_RE.match(base or ""):
        return "the run has no runtime-recorded base commit, so the reviewed history is unknown"
    line = _git(["rev-list", "--parents", "--max-count=1", head], cwd=mirror, env=env, hooks=hooks)
    shas = line.stdout.split() if line.returncode == 0 else []
    if not shas or shas[0] != head:
        return "the head's parents could not be read"
    parents = shas[1:]
    if parents == [base]:
        return None
    counted = _git(["rev-list", "--count", head, "--not", base], cwd=mirror, env=env, hooks=hooks)
    count = counted.stdout.strip() if counted.returncode == 0 else "an unknown number of"
    return (f"base {base[:12]}..head is {count} commit(s) and the head's parents are "
            f"{', '.join(p[:12] for p in parents) or 'none'}, not the single runtime candidate commit on the base")


def guard_outbound_text(session: Session, run: WorkerRun, *, action: str, where: str, texts: dict[str, str]) -> None:
    """Refuse (``AI_ATTRIBUTION_IN_TEXT``, audited) a PR title/body that carries AI attribution - never stripped,
    the owner removes it. Checked before any credential is read or request sent."""
    for field_name, text in texts.items():
        findings = find_ai_attribution(text)
        if not findings:
            continue
        message = refusal_message(f"{where} {field_name}", findings)
        details = {"where": where, "field": field_name, "findings": [f.as_dict() for f in findings[:10]]}
        audit.record(session, actor_type="service", actor_id="broker", action=action, result="DENIED",
                     target_type="run", target_id=run.id, project_id=run.project_id, domain=run.domain,
                     reason=message[:500], details={"code": "AI_ATTRIBUTION_IN_TEXT", **details})
        raise PolicyDenied(message, code="AI_ATTRIBUTION_IN_TEXT", details=details)


def push_from_mirror(*, source_repo: Path, branch: str, remote_url: str, token: str | None, work_root: Path,
                     commit_policy: CommitPolicy, expected_head: str | None = None,
                     basic_user: str = GITHUB_GIT_USER, reviewed_base: str | None = None) -> dict[str, Any]:
    """Fetch the task branch from the untrusted candidate into a private bare mirror and push it.

    With ``expected_head`` the push is sha-pinned (``<sha>:refs/heads/<branch>``, never forced): exactly
    the verified commit is published even if the branch tip moved between fetch and push. ``commit_policy`` is
    required: every commit of the pushed range is checked in the mirror before the push (owner rule); a violation
    returns ``refused`` with its ``code`` and nothing is pushed. With ``reviewed_base`` (a run's push always passes
    the run's base, an empty string when it has none) the head must be one commit whose only parent is that base -
    the diff the Claude review saw (owner rule R44): otherwise ``refused`` with code ``REVIEW_REQUIRED`` and reason
    ``HISTORY``, nothing pushed."""
    src, dst = validate_refspec(f"refs/heads/{branch}:refs/heads/{branch}")
    validate_remote_url(remote_url)
    if token:
        register_secret(token)
    mirror = work_root / f"push-{new_id()[:12]}.git"
    hooks = work_root / "nohooks"
    hooks.mkdir(parents=True, exist_ok=True)
    empty_global = work_root / "empty-gitconfig"
    empty_global.write_text("", encoding="utf-8")
    base_env = git_auth_env(remote_url, None, empty_global=empty_global)
    stage = "init"
    head: str | None = None
    try:
        init = _git(["init", "--bare", "--quiet", mirror.as_posix()], cwd=work_root, env=base_env, hooks=hooks)
        if init.returncode != 0:
            return {"ok": False, "stage": "init", "error": redact_text(init.stderr)[:400]}
        stage = "fetch"
        fetch = _git(["fetch", "--no-tags", "--no-recurse-submodules", "--quiet", source_repo.as_posix(),
                      f"{src}:{dst}"], cwd=mirror, env=base_env, hooks=hooks)
        if fetch.returncode != 0:
            return {"ok": False, "stage": "fetch", "error": redact_text(fetch.stderr)[:400]}
        head = _git(["rev-parse", dst], cwd=mirror, env=base_env, hooks=hooks).stdout.strip()
        if expected_head and head != expected_head:
            return {"ok": False, "stage": "verify", "commit": head,
                    "error": "task branch head differs from the runtime-recorded head"}
        # Owner rule: the gate checks exactly what the push would publish, so the remote's branches are read first
        # (same credential, same child-only environment as the push). An unreadable remote is a failed push -
        # nothing was sent - never a push of an unchecked range.
        stage = "remote_read"
        env = git_auth_env(remote_url, token, empty_global=empty_global, basic_user=basic_user)
        unread = _read_remote_branches(mirror, remote_url, env, hooks)
        if unread is not None:
            return {"ok": False, "stage": stage, "commit": head,
                    "error": f"the remote's branches could not be read, so the pushed range is unknown: {unread}"}
        stage = "commit_policy"
        problems = _pushed_commit_problems(mirror, base_env, hooks, head, commit_policy)
        if problems:
            code = cid.refusal_code(problems)
            detail = "; ".join(f"{p['sha'][:12]} {p['detail']}" for p in problems[:3])
            return {"ok": False, "stage": stage, "commit": head, "refused": True, "code": code, "problems": problems,
                    "error": f"{code}: push refused - {detail}"[:800]}
        if reviewed_base is not None:
            stage = "history"
            why = _history_problem(mirror, base_env, hooks, head, reviewed_base)
            if why is not None:
                return {"ok": False, "stage": stage, "commit": head, "refused": True, "code": REVIEW_REQUIRED,
                        "reason": "HISTORY", "problems": [{"sha": head, "code": "HISTORY", "detail": why}],
                        "error": (f"no Claude review PASS for head {head[:12]}: HISTORY - {why}; the reviewer saw only "
                                  "the net diff, so worker-made commits are never published: run the task again "
                                  "(the runtime commits the candidate once, on the base)")[:800]}
        stage = "push"
        pinned_src, _ = validate_refspec(f"{head}:{dst}") if _SHA_RE.match(head or "") else (src, dst)
        push = _git(["push", "--porcelain", "--no-verify", remote_url, f"{pinned_src}:{dst}"], cwd=mirror, env=env,
                    hooks=hooks)
        out = redact_text((push.stdout or "") + (push.stderr or ""))[:800]
        if push.returncode != 0:
            observe_git_failure(remote_url, out, operation="PUSH")
        return {"ok": push.returncode == 0, "stage": "push", "commit": head, "output": out,
                "error": "" if push.returncode == 0 else out}
    except subprocess.TimeoutExpired:
        # A push that timed out may still have landed on the remote: the outcome is UNKNOWN until
        # reconciled (§14 outbox/reconciliation, no exactly-once claim). Earlier stages never wrote.
        return {"ok": False, "stage": f"timeout:{stage}", "commit": head, "unknown": stage == "push",
                "error": f"git timed out during {stage}"}
    finally:
        _rmtree(mirror)


def _rmtree(path: Path) -> None:
    """Remove a git directory; Windows marks object files read-only, so clear the bit and retry."""
    def _retry(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:  # pragma: no cover
            shutil.rmtree(path, onerror=_retry)


# ----------------------------------------------------------------------------- operations
def push_task_branch(
    session: Session, *, run_id: str, worktree: str, branch: str, repo_full_name: str,
    remote_url: str | None = None, expected_head: str | None = None, expected_head_sha: str | None = None,
) -> dict[str, Any]:
    """Push the run's runtime-recorded head as ``<sha>:refs/heads/<branch>`` (never forced).

    Always returns ``head_sha`` (the commit that was — or, for FAILED/UNKNOWN, would have been —
    published) next to ``status`` PUSHED | FAILED | UNKNOWN; FAILED/UNKNOWN are returned, not raised,
    so the outbox row and audit record persist with the caller's commit. After a push the delivery
    hook ``aeos.delivery.sync.on_branch_updated`` re-observes open PRs of the branch (§14, §50)."""
    from aeos.core.config import get_settings

    expected_head = expected_head_sha or expected_head
    run = session.get(WorkerRun, run_id)
    if run is None:
        raise NotFound("run not found")
    if is_bitbucket_run(session, run, repo_full_name):
        from aeos.broker import bitbucket_broker

        return bitbucket_broker.push_task_branch(session, run=run, worktree=worktree, branch=branch,
                                                 repo_full_name=repo_full_name, remote_url=remote_url,
                                                 expected_head=expected_head)
    validate_repo(repo_full_name)
    ref = _repo_ref(session, run.project_id, repo_full_name)
    default_branch = ref.default_branch if ref else "main"
    validate_branch(branch, default_branch=default_branch)
    if run.branch and branch != run.branch:
        raise PolicyDenied("branch does not belong to this run", code="BRANCH_NOT_ALLOWED")
    if not run.workspace_ref or Path(worktree).resolve() != Path(run.workspace_ref).resolve():
        raise PolicyDenied("worktree does not belong to this run", code="WORKTREE_MISMATCH")
    _require_publishable(session, run, "git.push_branch")
    # Push exactly what the runtime committed (and verification/review saw), never a later change.
    if expected_head and expected_head != run.head_commit:
        raise PolicyDenied("expected head differs from the runtime-recorded head", code="HEAD_MISMATCH")
    expected_head = run.head_commit
    # Owner rule (AGENTS.md invariant 11): no push without the owner's GitHub identity configured.
    policy = commit_policy(session, run, cid.GITHUB, action="broker.git.push_branch")
    # Fetch from the trusted git dir recorded by the runtime; the candidate's own ``.git`` pointer
    # is worker-editable and never followed (ADR-0054).
    source = Path(run.git_dir_ref) if run.git_dir_ref and Path(run.git_dir_ref).is_dir() else Path(worktree)
    url = validate_remote_url(remote_url or github_remote(repo_full_name))
    local = _is_local_remote(url)
    # Credential-free remote + the exact commit are kept in the outbox for later reconciliation.
    target = {"repo": repo_full_name, "branch": branch, "remote": "local" if local else url, "remote_url": url,
              "expected_head": expected_head}
    grant = _require_grant(session, run, "GIT_BRANCH", {"repo": repo_full_name, "branch": branch})
    token: str | None = None
    if not local:
        _interlock(session, run, provider="github-repo", operation="push_branch", target=target, grant=grant)
        if run.domain != "PERSONAL":
            raise PolicyDenied("GitHub personal credentials are PERSONAL-only", code="DOMAIN_CREDENTIAL")
        from aeos.secrets import facade as secrets

        token = secrets.get_secret(GITHUB_SECRET_REF, purpose=GITHUB_SECRET_PURPOSE, actor="service:broker")
    se = _side_effect(session, run, kind="PUSH_BRANCH", key=f"push:{repo_full_name}:{branch}:{expected_head or run.head_commit or ''}",
                      target=target)
    work_root = get_settings().layout.root / "broker" / "git"
    work_root.mkdir(parents=True, exist_ok=True)
    res = push_from_mirror(source_repo=source, branch=branch, remote_url=url, token=token, work_root=work_root,
                           expected_head=expected_head, commit_policy=policy, reviewed_base=run.base_commit or "")
    if res.get("refused"):
        refuse_push(session, run, se, res, action="broker.git.push_branch", provider=cid.GITHUB)
    se.status = "SUCCEEDED" if res["ok"] else ("UNKNOWN" if res.get("unknown") else "FAILED")
    se.commit_sha = res.get("commit")
    se.result_summary = (res.get("output") or res.get("error") or "")[:500]
    audit.record(session, actor_type="service", actor_id="broker", action="broker.git.push_branch",
                 result="SUCCESS" if res["ok"] else "FAILED", target_type="run", target_id=run.id,
                 project_id=run.project_id, domain=run.domain, reason=res.get("error", ""),
                 details={"repo": repo_full_name, "branch": branch, "commit": res.get("commit"), "stage": res["stage"]})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=run.id, run_id=run.id,
                project_id=run.project_id, domain=run.domain,
                payload={"kind": "PUSH_BRANCH", "status": se.status, "branch": branch, "commit": res.get("commit")})
    head_sha = res.get("commit") or expected_head
    hook = _branch_updated(session, repo_full_name, branch, head_sha) if res["ok"] and head_sha else "NOT_CALLED"
    # Failures are returned (not raised) so the audit/outbox records persist with the caller's commit.
    return {"status": "PUSHED" if res["ok"] else se.status, "ok": bool(res["ok"]), "branch": branch,
            "commit": res.get("commit"), "head_sha": head_sha, "side_effect_id": se.id, "remote": target["remote"],
            "pr_observation": hook,
            "error": "" if res["ok"] else f"{res['stage']}: {res.get('error', '')}"[:300]}


def _branch_updated(session: Session, repo: str, branch: str, head_sha: str) -> str:
    """Delivery hook: open PR records of this branch re-observe the new head (acceptance STALE when it
    moved). Delivery missing -> reported, never assumed (fail closed: nothing is accepted by this)."""
    try:
        from aeos.delivery import sync
    except ImportError:
        return "DELIVERY_UNAVAILABLE"
    try:
        changed = sync.on_branch_updated(session, repo=repo, branch=branch, head_sha=head_sha, actor="broker")
    except AeosError as exc:
        # The push already landed: its outbox row (SUCCEEDED) must persist with the caller's commit, so a
        # refused observation is reported, never raised over the side effect. PR sync re-observes later.
        audit.record(session, actor_type="service", actor_id="broker", action="broker.git.branch_updated_hook",
                     result="FAILED", target_type="branch", target_id=branch[:200],
                     reason=f"{exc.code}: {exc.message}"[:500], details={"repo": repo, "head_sha": head_sha})
        return f"OBSERVATION_FAILED ({exc.code})"
    return f"OBSERVED ({changed} PR record(s) invalidated)"


def _find_open_pr(client: httpx.Client, api: str, repo: str, head: str, base: str, headers: dict[str, str],
                  *, state: str = "open") -> dict | None:
    owner = repo.split("/", 1)[0]
    r = client.get(f"{api}/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "base": base, "state": state},
                   headers=headers)
    if r.status_code == 200 and isinstance(r.json(), list) and r.json():
        return r.json()[0]
    return None


def create_pull_request(
    session: Session, *, run_id: str, repo_full_name: str, head: str, base: str, title: str, body: str,
    api_base: str = GITHUB_API,
) -> dict[str, Any]:
    run = session.get(WorkerRun, run_id)
    if run is None:
        raise NotFound("run not found")
    if is_bitbucket_run(session, run, repo_full_name):
        from aeos.broker import bitbucket_broker

        return bitbucket_broker.create_pull_request(session, run=run, repo_full_name=repo_full_name, head=head,
                                                    base=base, title=title, body=body)
    validate_repo(repo_full_name)
    ref = _repo_ref(session, run.project_id, repo_full_name)
    default_branch = ref.default_branch if ref else "main"
    validate_branch(head, default_branch=default_branch)
    if base.startswith("aeos/") or not re.match(r"^[A-Za-z0-9._/-]{1,200}$", base) or ".." in base:
        raise PolicyDenied("invalid PR base", code="PR_BASE_REJECTED")
    if run.domain != "PERSONAL":
        raise PolicyDenied("GitHub personal credentials are PERSONAL-only", code="DOMAIN_CREDENTIAL")
    if run.branch and head != run.branch:
        raise PolicyDenied("PR head does not belong to this run", code="BRANCH_NOT_ALLOWED")
    _require_publishable(session, run, "github.create_pr")
    # Owner rule (AGENTS.md invariant 11): no AI attribution in what lands on GitHub - refused, never stripped.
    guard_outbound_text(session, run, action="broker.github.create_pr", where="GitHub pull request",
                        texts={"title": title, "body": body})
    target = {"repo": repo_full_name, "head": head, "base": base}
    grant = _require_grant(session, run, "PR_CREATE", target)
    _interlock(session, run, provider="github-repo", operation="create_pr", target=target, grant=grant)
    key = f"pr:{repo_full_name}:{head}:{base}"
    existing = session.scalar(select(BrokerSideEffect).where(BrokerSideEffect.idempotency_key == key))
    if existing is not None and existing.status == "SUCCEEDED":
        audit.record(session, actor_type="service", actor_id="broker", action="broker.github.create_pr",
                     result="SUCCESS", target_type="run", target_id=run.id, project_id=run.project_id,
                     domain=run.domain, reason="already created (idempotent outbox); no API call",
                     details={"repo": repo_full_name, "head": head, "base": base, "number": existing.external_id})
        return {"status": "EXISTS", "number": existing.external_id, "url": existing.external_url,
                "side_effect_id": existing.id}
    from aeos.secrets import facade as secrets

    token = secrets.get_secret(GITHUB_SECRET_REF, purpose=GITHUB_SECRET_PURPOSE, actor="service:broker")
    se = _side_effect(session, run, kind="CREATE_PR", key=key, target=target)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dly-aeos-broker"}
    payload = {"title": redact_text(title)[:250], "head": head, "base": base, "body": redact_text(body)[:60000],
               "draft": True, "maintainer_can_modify": False}
    pr: dict | None = None
    outcome = "FAILED"
    detail = ""
    with http_client() as client:
        try:
            r = client.post(f"{api_base}/repos/{repo_full_name}/pulls", json=payload, headers=headers)
            if r.status_code == 201:
                pr, outcome = r.json(), "SUCCEEDED"
            elif r.status_code == 422 and "already exists" in r.text:
                pr = _find_open_pr(client, api_base, repo_full_name, head, base, headers)
                outcome = "SUCCEEDED" if pr else "FAILED"
                detail = "reconciled existing PR" if pr else redact_text(r.text)[:300]
            else:
                detail = f"GitHub {r.status_code}: {redact_text(r.text)[:300]}"
        except httpx.TimeoutException:
            # §50: the provider may have created the PR — reconcile before any retry.
            try:
                pr = _find_open_pr(client, api_base, repo_full_name, head, base, headers)
            except httpx.HTTPError:
                pr = None
            outcome = "SUCCEEDED" if pr else "UNKNOWN"
            detail = "timeout; reconciled" if pr else "timeout; outcome UNKNOWN until reconciled"
        except httpx.HTTPError as exc:
            detail = type(exc).__name__
    se.status = outcome
    if pr:
        se.external_id = str(pr.get("number"))
        se.external_url = str(pr.get("html_url") or "")
    se.result_summary = detail[:500]
    audit.record(session, actor_type="service", actor_id="broker", action="broker.github.create_pr",
                 result="SUCCESS" if outcome == "SUCCEEDED" else "FAILED", target_type="run", target_id=run.id,
                 project_id=run.project_id, domain=run.domain, reason=detail,
                 details={"repo": repo_full_name, "head": head, "base": base, "number": se.external_id})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=run.id, run_id=run.id,
                project_id=run.project_id, domain=run.domain,
                payload={"kind": "CREATE_PR", "status": outcome, "number": se.external_id, "url": se.external_url})
    raw_head, raw_base = (pr or {}).get("head"), (pr or {}).get("base")
    head_obj: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
    base_obj: dict[str, Any] = raw_base if isinstance(raw_base, dict) else {}
    return {"status": "CREATED" if outcome == "SUCCEEDED" else outcome, "ok": outcome == "SUCCEEDED",
            "number": se.external_id, "url": se.external_url, "side_effect_id": se.id, "error": detail[:300],
            "head_sha": head_obj.get("sha"), "base_sha": base_obj.get("sha"),
            "note": "draft PR; integration into the base branch is a human-only action"}


# ----------------------------------------------------------------------------- reconciliation (read-only)
def _ls_remote_head(url: str, branch: str, token: str | None, *, basic_user: str = GITHUB_GIT_USER) -> str | None:
    """Commit of ``refs/heads/<branch>`` on the remote (None = absent). Read-only; the credential
    exists only in this one child's environment."""
    from aeos.core.config import get_settings

    work = get_settings().layout.root / "broker" / "git"
    work.mkdir(parents=True, exist_ok=True)
    hooks = work / "nohooks"
    hooks.mkdir(parents=True, exist_ok=True)
    empty_global = work / "empty-gitconfig"
    empty_global.write_text("", encoding="utf-8")
    if token:
        register_secret(token)
    proc = _git(["ls-remote", "--heads", url, f"refs/heads/{branch}"], cwd=work,
                env=git_auth_env(url, token, empty_global=empty_global, basic_user=basic_user), hooks=hooks)
    if proc.returncode != 0:
        observe_git_failure(url, redact_text(proc.stderr or ""), operation="LS-REMOTE")
        raise Blocked("remote could not be read: " + redact_text(proc.stderr or "")[:300], code="REMOTE_UNREADABLE")
    for line in (proc.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}":
            return sha.strip()
    return None


def reconcile_side_effect(session: Session, side_effect_id: str, *, actor: str,
                          api_base: str = GITHUB_API) -> dict[str, Any]:
    """§14 "Crash recovery membaca ... publication records, dan status eksternal sebelum retry" /
    §50 "Provider timeout setelah create resource -> Rekonsiliasi ID/target sebelum retry".

    Reads the external state of a PENDING/UNKNOWN push or PR and records what is actually there.
    Strictly read-only towards the provider: it never pushes, creates, updates or merges anything.
    A read failure keeps the outcome UNKNOWN (fail closed: the scheduler keeps waiting)."""
    se = session.get(BrokerSideEffect, side_effect_id)
    if se is None:
        raise NotFound("side effect not found")
    if se.status not in ("PENDING", "UNKNOWN"):
        return {"id": se.id, "kind": se.kind, "status": se.status, "changed": False}
    if (se.target or {}).get("provider") == "bitbucket" or se.domain == "OFFICE":
        from aeos.broker import bitbucket_broker

        return bitbucket_broker.reconcile_side_effect(session, se, actor=actor)
    target = dict(se.target or {})
    before = se.status
    detail = ""
    if se.kind == "PUSH_BRANCH":
        branch = validate_branch(str(target.get("branch", "")))
        url = target.get("remote_url") or (github_remote(str(target.get("repo"))) if target.get("remote") != "local"
                                           else None)
        if not url:
            raise Blocked("remote of this push was not recorded; reconcile manually", code="REMOTE_UNKNOWN")
        url = validate_remote_url(str(url))
        token = None
        if not _is_local_remote(url):
            if se.domain != "PERSONAL":
                raise PolicyDenied("GitHub personal credentials are PERSONAL-only", code="DOMAIN_CREDENTIAL")
            from aeos.secrets import facade as secrets

            token = secrets.get_secret(GITHUB_SECRET_REF, purpose=GITHUB_SECRET_PURPOSE, actor="service:broker")
        try:
            sha = _ls_remote_head(url, branch, token)
        except subprocess.TimeoutExpired:
            sha, detail = None, "ls-remote timed out; outcome stays UNKNOWN"
        else:
            expected = target.get("expected_head") or se.commit_sha
            if sha and (not expected or sha == expected):
                se.status, se.commit_sha, detail = "SUCCEEDED", sha, f"remote branch at {sha[:12]} (reconciled)"
            elif sha:
                se.status, detail = "FAILED", f"remote branch at {sha[:12]}, not the expected {str(expected)[:12]}"
            else:
                se.status, detail = "FAILED", "branch not present on the remote (push did not land)"
    elif se.kind == "CREATE_PR":
        if se.domain != "PERSONAL":
            raise PolicyDenied("GitHub personal credentials are PERSONAL-only", code="DOMAIN_CREDENTIAL")
        repo = validate_repo(str(target.get("repo", "")))
        head = validate_branch(str(target.get("head", "")))
        base = str(target.get("base", ""))
        from aeos.secrets import facade as secrets

        token = secrets.get_secret(GITHUB_SECRET_REF, purpose=GITHUB_SECRET_PURPOSE, actor="service:broker")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dly-aeos-broker"}
        with http_client() as client:
            try:
                pr = _find_open_pr(client, api_base, repo, head, base, headers, state="all")
            except (httpx.HTTPError, ValueError):
                pr, detail = None, "GitHub could not be read; outcome stays UNKNOWN"
            else:
                if pr:
                    se.status = "SUCCEEDED"
                    se.external_id = str(pr.get("number"))
                    se.external_url = str(pr.get("html_url") or "")
                    detail = f"PR #{se.external_id} exists (reconciled)"
                else:
                    se.status, detail = "FAILED", "no PR exists for this head/base (creation did not happen)"
    else:
        raise ValidationFailed(f"unknown side effect kind {se.kind!r}")
    se.result_summary = redact_text(detail)[:500]
    audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                 action="broker.side_effect.reconcile", result="SUCCESS" if se.status != before else "FAILED",
                 target_type="broker_side_effect", target_id=se.id, project_id=se.project_id, domain=se.domain,
                 reason=detail, details={"kind": se.kind, "from": before, "to": se.status})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=se.run_id,
                run_id=se.run_id, project_id=se.project_id, domain=se.domain,
                payload={"kind": se.kind, "status": se.status, "reconciled": True, "detail": se.result_summary})
    return {"id": se.id, "kind": se.kind, "status": se.status, "changed": se.status != before, "detail": detail}
