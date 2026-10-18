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
* the broker has no capability to integrate a PR into any branch: human-only (U-09, §31).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.broker import grants
from aeos.core.errors import Blocked, NotFound, PolicyDenied, ValidationFailed
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


# ----------------------------------------------------------------------------- helpers
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


def _require_publishable(session: Session, run: WorkerRun, operation: str) -> None:
    """Only a runtime-collected candidate may leave the machine (§21, §50 "Worker mengubah
    grader/policy/pipeline di luar kontrak -> Ditolak sebelum promotion/remote publication").

    * the run finished its execution phase (REVIEW/DONE) — never a running, failed or cancelled one;
    * the runtime recorded the candidate head commit (what verification/review looked at);
    * a quarantined patch needs an ACTIVE human EXCEPTION decision bound to this run and that exact
      head commit (special review, ADR-0054)."""
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


def _side_effect(session: Session, run: WorkerRun, *, kind: str, key: str, target: dict[str, Any]) -> BrokerSideEffect:
    se = session.scalar(select(BrokerSideEffect).where(BrokerSideEffect.idempotency_key == key))
    if se is None:
        se = BrokerSideEffect(run_id=run.id, project_id=run.project_id, domain=run.domain, kind=kind, target=target,
                              idempotency_key=key, status="PENDING")
        session.add(se)
        session.flush()
    se.attempts = (se.attempts or 0) + 1
    return se


def _basic_header(token: str) -> str:
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    register_secret(raw)
    return f"Authorization: Basic {raw}"


def git_auth_env(remote_url: str, token: str | None, *, empty_global: Path) -> dict[str, str]:
    """Environment for ONE git invocation. The credential exists only in this child's env."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k.upper() not in {"GCM_INTERACTIVE"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty_global),
                "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    if token:
        u = urlparse(remote_url)
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{u.scheme}://{u.netloc}/.extraHeader",
                    "GIT_CONFIG_VALUE_0": _basic_header(token)})
    return env


def _git(args: list[str], *, cwd: Path, env: dict[str, str], hooks: Path) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", f"core.hooksPath={hooks.as_posix()}", "-c", "credential.helper=", "-c", "core.fsmonitor=false",
           "-c", "http.followRedirects=false", "-c", "protocol.ext.allow=never", *args]
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GIT_TIMEOUT, check=False)


def push_from_mirror(*, source_repo: Path, branch: str, remote_url: str, token: str | None, work_root: Path,
                     expected_head: str | None = None) -> dict[str, Any]:
    """Fetch the task branch from the untrusted candidate into a private bare mirror and push it.

    With ``expected_head`` the push is sha-pinned (``<sha>:refs/heads/<branch>``, never forced): exactly
    the verified commit is published even if the branch tip moved between fetch and push."""
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
        stage = "push"
        pinned_src, _ = validate_refspec(f"{head}:{dst}") if _SHA_RE.match(head or "") else (src, dst)
        env = git_auth_env(remote_url, token, empty_global=empty_global)
        push = _git(["push", "--porcelain", "--no-verify", remote_url, f"{pinned_src}:{dst}"], cwd=mirror, env=env,
                    hooks=hooks)
        out = redact_text((push.stdout or "") + (push.stderr or ""))[:800]
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
                           expected_head=expected_head)
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
    changed = sync.on_branch_updated(session, repo=repo, branch=branch, head_sha=head_sha, actor="broker")
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
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False) as client:
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
    head_obj = (pr or {}).get("head") if isinstance((pr or {}).get("head"), dict) else {}
    base_obj = (pr or {}).get("base") if isinstance((pr or {}).get("base"), dict) else {}
    return {"status": "CREATED" if outcome == "SUCCEEDED" else outcome, "ok": outcome == "SUCCEEDED",
            "number": se.external_id, "url": se.external_url, "side_effect_id": se.id, "error": detail[:300],
            "head_sha": head_obj.get("sha"), "base_sha": base_obj.get("sha"),
            "note": "draft PR; integration into the base branch is a human-only action"}


# ----------------------------------------------------------------------------- reconciliation (read-only)
def _ls_remote_head(url: str, branch: str, token: str | None) -> str | None:
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
                env=git_auth_env(url, token, empty_global=empty_global), hooks=hooks)
    if proc.returncode != 0:
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
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False) as client:
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
