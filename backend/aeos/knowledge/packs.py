"""ContextPack builder (§24 Konteks, retrieval dan memori; §13 ContextPack).

A pack is the minimum, provenance-carrying context for one task/role:

* repository context = file/symbol subset of an EXACT commit (``git ls-tree`` / ``git grep -n`` /
  ``git cat-file blob <sha>:<path>``), never a dump of the repo and never the working tree (symlinks and
  submodules are skipped, so nothing outside the repository can be read);
* project context = metadata search over this project's plans/tasks/decisions/docs (+ global ADRs);
* policy context = the trusted controller's ``policy/`` files, read-only with hashes; missing policy
  is reported, never reconstructed;
* research and attachments are labelled DATA / UNTRUSTED_DATA; memory is MEMORY_DATA (never policy);
* every item has kind/ref/provenance/classification/hash and a token estimate (chars/4) checked
  against the budget; forbidden scope (defaults + request + TaskSpec) is always respected;
* the pack is hashed; it becomes STALE when the repository head moves off its base commit, the
  policy file set changes, its TTL expires, or a newer pack for the same scope supersedes it.
"""

from __future__ import annotations

import fnmatch
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, NotFound, ValidationFailed
from aeos.core.hashing import content_hash, file_sha256, sha256_text
from aeos.core.paths import UnsafePathError, ensure_within, repo_root
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.identity.service import Principal
from aeos.knowledge import memory as memory_mod
from aeos.models.knowledge_ext import ContextPackMeta
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Attachment, Project, RepositoryReference
from aeos.models.research import ContextPack, ResearchEntry
from aeos.portfolio import classification as cls
from aeos.portfolio import gitread
from aeos.portfolio import repositories as repos

DEFAULT_BUDGET = 8000
DEFAULT_TTL_HOURS, MAX_TTL_HOURS = 168, 720
MAX_REPO_READ_BYTES = 512 * 1024
MIN_BUDGET, MAX_BUDGET = 1024, 200_000
ITEM_OVERHEAD_TOKENS = 16
INCLUDE_KINDS = ("policy", "task", "symbol", "repo_file", "code_match", "metadata", "memory", "attachment", "research")
MANDATORY = frozenset({"policy", "task"})
ROLE_PROFILES: dict[str, tuple[str, ...]] = {
    "default": ("policy", "task", "symbol", "repo_file", "code_match", "metadata", "memory"),
    "planner": ("policy", "task", "metadata", "memory", "attachment", "research", "repo_file", "symbol"),
    "feature-implementer": ("policy", "task", "symbol", "repo_file", "code_match", "memory"),
    "reviewer": ("policy", "task", "symbol", "repo_file", "code_match", "metadata", "memory"),
    "security-reviewer": ("policy", "task", "symbol", "repo_file", "code_match", "metadata"),
    "docs-writer": ("policy", "task", "metadata", "memory", "repo_file", "attachment"),
    "researcher": ("policy", "task", "research", "metadata", "memory"),
}
DEFAULT_FORBIDDEN = (".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*id_rsa*",
                     "*id_ed25519*", "*secrets.*", "*credentials*", ".git/*", "*.sqlite3", "*.sqlite", "*.db",
                     "*.kdbx", "*.keystore", "*.jks")
CAPS = {"symbol": 20, "repo_file": 20, "code_match": 30, "metadata": 20, "memory": 20, "attachment": 10, "research": 10}
_TERM_RE = re.compile(r"^[^\x00-\x1f\x7f]{2,100}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,63}$")
_SYMBOL_KEYWORDS = "def|class|function|interface|type|enum|struct|trait|fn|func|const|let|var|module"
POLICY_SUFFIXES = (".yaml", ".yml", ".json", ".md", ".toml")
MAX_POLICY_FILES, MAX_POLICY_BYTES = 20, 64 * 1024


def _tokens(text: str) -> int:
    return math.ceil(len(text) / 4) + ITEM_OVERHEAD_TOKENS


def _validate_glob(p: Any) -> str:
    if not isinstance(p, str):
        raise ValidationFailed("path patterns must be strings", code="INVALID_PATH_PATTERN")
    v = p.strip().replace("\\", "/")
    if (not v or len(v) > 200 or v.startswith("/") or ".." in v.split("/") or re.match(r"^[A-Za-z]:", v)
            or "\x00" in v):
        raise ValidationFailed(f"invalid repository path pattern {p!r} (relative patterns only, no '..')",
                               code="INVALID_PATH_PATTERN")
    return v


def _validate_terms(terms: Any) -> list[str]:
    if terms in (None, ""):
        return []
    if isinstance(terms, str):
        terms = [t for t in re.split(r"[,\n]", terms) if t.strip()]
    if not isinstance(terms, list) or len(terms) > 10:
        raise ValidationFailed("query must be a list of at most 10 terms", code="INVALID_QUERY")
    out = []
    for t in terms:
        if not isinstance(t, str) or not _TERM_RE.match(t.strip()):
            raise ValidationFailed("each query term must be 2..100 printable characters", code="INVALID_QUERY")
        out.append(t.strip())
    return out


def _forbidden(path: str, patterns: list[str]) -> bool:
    low = path.lower()
    return any(fnmatch.fnmatchcase(low, pat.lower()) for pat in patterns)


def _allowed(path: str, allowed: list[str]) -> bool:
    return not allowed or any(fnmatch.fnmatchcase(path, pat) or path.startswith(pat.rstrip("*").rstrip("/") + "/")
                              for pat in allowed)


class _Collector:
    def __init__(self, project: Project, budget: int):
        self.project = project
        self.budget = budget
        self.used = 0
        self.items: list[dict[str, Any]] = []
        self.omitted: list[dict[str, Any]] = []
        self.refs: set[str] = set()

    def add(self, *, kind: str, ref: str, content: str, provenance: dict[str, Any], trust: str,
            classification: str | None = None, title: str = "", truncatable: bool = False) -> bool:
        if ref in self.refs:
            return False
        redacted = redact_text(content)
        item_cls = cls.highest(classification or self.project.data_classification, self.project.data_classification)
        tokens = _tokens(redacted)
        truncated = False
        if self.used + tokens > self.budget:
            room = self.budget - self.used - ITEM_OVERHEAD_TOKENS
            if truncatable and room >= 64:
                suffix = "\n[... truncated to fit the token budget]"
                redacted = redacted[: room * 4 - len(suffix)] + suffix
                tokens = _tokens(redacted)
                truncated = True
            else:
                self.omitted.append({"kind": kind, "ref": ref, "reason": "token budget", "tokens": tokens})
                return False
        self.used += tokens
        self.refs.add(ref)
        self.items.append({
            "id": f"i{len(self.items) + 1}", "kind": kind, "ref": ref, "title": title or ref,
            "content": redacted, "provenance": provenance, "classification": item_cls,
            "hash": "sha256:" + sha256_text(redacted), "tokens": tokens, "trust": trust,
            "truncated": truncated, "redacted": redacted != content,
        })
        return True


# ---------------------------------------------------------------- policy context


def policy_files() -> tuple[Path, list[Path]]:
    root = repo_root() / "policy"
    if not root.is_dir():
        return root, []
    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        if len(files) >= MAX_POLICY_FILES:
            break
        if not p.is_file() or p.suffix.lower() not in POLICY_SUFFIXES:
            continue
        try:
            files.append(ensure_within(root, p))  # a symlink out of policy/ is ignored
        except UnsafePathError:
            continue
    return root, files


def policy_hashes() -> dict[str, str]:
    root, files = policy_files()
    return {p.relative_to(root.resolve()).as_posix(): file_sha256(p) for p in files}


def _collect_policy(col: _Collector, warnings: list[str]) -> None:
    root, files = policy_files()
    if not files:
        warnings.append("policy context MISSING: no policy/ files in the trusted controller; nothing reconstructed")
        col.add(kind="policy", ref="policy:missing", content="POLICY CONTEXT MISSING — gates that need it stay BLOCKED.",
                provenance={"source": "trusted-controller policy/", "status": "MISSING"}, trust="POLICY_READ_ONLY",
                classification="INTERNAL", title="policy/ (missing)")
        return
    for p in files:
        rel = p.relative_to(root.resolve()).as_posix()
        size = p.stat().st_size
        if size > MAX_POLICY_BYTES:
            warnings.append(f"policy/{rel} skipped: larger than {MAX_POLICY_BYTES} bytes")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        prov: dict[str, Any] = {"source": "trusted-controller policy/ (read-only)", "path": f"policy/{rel}",
                                "sha256": file_sha256(p), "read_only": True}
        if rel == "mci-reference.yaml":
            m = re.search(r"^\s*status:\s*(\S+)", text, re.MULTILINE)
            prov["mci_status"] = m.group(1) if m else "UNKNOWN"
            if prov["mci_status"] != "VERIFIED":
                warnings.append(f"MCI reference status is {prov['mci_status']}: canonical policy alignment UNVERIFIED")
        col.add(kind="policy", ref=f"policy/{rel}", content=text, provenance=prov, trust="POLICY_READ_ONLY",
                classification="INTERNAL", title=f"policy/{rel}")


# ---------------------------------------------------------------- repository context


class _RepoReader:
    def __init__(self, path: Path, commit: str, forbidden: list[str], allowed: list[str]):
        self.path = path
        self.commit = commit
        self.forbidden = forbidden
        self.allowed = allowed
        self._blobs: dict[str, list[str] | None] = {}
        entries, self.truncated = gitread.ls_tree(path, commit)
        self.files: dict[str, gitread.TreeEntry] = {}
        self.skipped_symlinks = 0
        self.skipped_forbidden = 0
        for e in entries:
            if e.is_symlink:
                self.skipped_symlinks += 1
                continue
            if e.kind != "blob":
                continue
            if _forbidden(e.path, forbidden):
                self.skipped_forbidden += 1
                continue
            if not _allowed(e.path, allowed):
                continue
            self.files[e.path] = e

    def ok(self, path: str) -> bool:
        return path in self.files

    def too_large(self, path: str) -> bool:
        e = self.files.get(path)
        return bool(e and e.size is not None and e.size > MAX_REPO_READ_BYTES)

    def lines(self, path: str) -> list[str] | None:
        if path not in self._blobs:
            raw = None
            if self.ok(path) and not self.too_large(path):  # never a truncated prefix presented as the file
                raw = gitread.show_blob(self.path, self.commit, path, max_bytes=MAX_REPO_READ_BYTES,
                                        complete_only=True)
            if raw is None or b"\x00" in raw[:8192]:
                self._blobs[path] = None
            else:
                self._blobs[path] = raw.decode("utf-8", errors="replace").splitlines()
        return self._blobs[path]

    def snippet(self, path: str, line: int, ctx: int) -> tuple[str, int, int] | None:
        lines = self.lines(path)
        if lines is None:
            return None
        a = max(1, line - ctx)
        b = min(len(lines), line + ctx)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(a, b + 1))
        return body, a, b

    def prov(self, path: str, **extra: Any) -> dict[str, Any]:
        e = self.files.get(path)
        return {"source": "git", "commit": self.commit, "path": path, "blob": e.sha if e else None, **extra}


def _collect_symbols(col: _Collector, reader: _RepoReader, terms: list[str], full_name: str) -> None:
    n = 0
    for term in terms:
        if not _IDENT_RE.match(term):
            continue
        pattern = (rf"^[[:space:]]*(export[[:space:]]+)?(default[[:space:]]+)?(pub[[:space:]]+)?(async[[:space:]]+)?"
                   rf"({_SYMBOL_KEYWORDS})[[:space:]]+[A-Za-z0-9_]*{term}[A-Za-z0-9_]*")
        for path, line, _text in gitread.grep(reader.path, reader.commit, pattern, extended=True, max_count_per_file=3):
            if n >= CAPS["symbol"] or not reader.ok(path):
                continue
            snip = reader.snippet(path, line, 12)
            if snip is None:
                continue
            body, a, b = snip
            if col.add(kind="symbol", ref=f"{path}#L{a}-L{b}", content=body, trust="REPO_DATA",
                       provenance=reader.prov(path, line=line, lines=[a, b], match="symbol", term=term, repo=full_name),
                       title=f"{path}:{line} (simbol '{term}')", truncatable=True):
                n += 1


def _collect_files(col: _Collector, reader: _RepoReader, patterns: list[str], full_name: str,
                   warnings: list[str]) -> None:
    n = 0
    for pat in patterns:
        matched = [p for p in sorted(reader.files) if fnmatch.fnmatchcase(p, pat)]
        if not matched:
            warnings.append(f"path pattern {pat!r} matched no allowed file at {reader.commit[:12]}")
        for path in matched:
            if n >= CAPS["repo_file"]:
                warnings.append("repo_file cap reached; narrow the path patterns")
                return
            lines = reader.lines(path)
            if lines is None:
                why = f"larger than {MAX_REPO_READ_BYTES} bytes" if reader.too_large(path) else "binary or unreadable"
                warnings.append(f"{path} skipped ({why})")
                continue
            body = "\n".join(lines[:400]) + ("\n[... more lines omitted]" if len(lines) > 400 else "")
            if col.add(kind="repo_file", ref=f"{path}@{reader.commit[:12]}", content=body, trust="REPO_DATA",
                       provenance=reader.prov(path, lines=[1, min(len(lines), 400)], repo=full_name), title=path,
                       truncatable=True):
                n += 1


def _collect_matches(col: _Collector, reader: _RepoReader, terms: list[str], full_name: str) -> None:
    n = 0
    for term in terms:
        for path, line, _text in gitread.grep(reader.path, reader.commit, term, extended=False, max_count_per_file=2):
            if n >= CAPS["code_match"]:
                return
            if not reader.ok(path):
                continue
            snip = reader.snippet(path, line, 3)
            if snip is None:
                continue
            body, a, b = snip
            if col.add(kind="code_match", ref=f"{path}#L{a}-L{b}", content=body, trust="REPO_DATA",
                       provenance=reader.prov(path, line=line, lines=[a, b], match="git grep -n", term=term,
                                              repo=full_name), title=f"{path}:{line}"):
                n += 1


# ---------------------------------------------------------------- records


def _collect_task(col: _Collector, task: TaskSpec) -> None:
    lines = [f"Task {task.task_key}: {task.title}", f"Goal: {task.goal}"]
    for label, values in (("Acceptance", task.acceptance_criteria), ("Expected outputs", task.expected_outputs),
                          ("Allowed paths", task.allowed_paths), ("Forbidden paths", task.forbidden_paths),
                          ("Stop conditions", task.stop_conditions)):
        if values:
            lines.append(f"{label}:")
            lines.extend(f"- {v}" for v in values)
    lines.append(f"Risk class: {task.risk_class}; base commit: {task.base_commit or 'UNKNOWN'}")
    col.add(kind="task", ref=f"task:{task.task_key}", content="\n".join(lines), trust="AEOS_RECORD",
            classification=task.data_classification, title=f"TaskSpec {task.task_key}",
            provenance={"source": "aeos-db task_specs (immutable)", "task_spec_id": task.id,
                        "plan_version_id": task.plan_version_id, "spec_hash": task.spec_hash})


def _collect_metadata(col: _Collector, session: Session, principal: Principal, project: Project, terms: list[str]) -> None:
    from aeos.portfolio.search import search

    n = 0
    for term in terms:
        try:
            res = search(session, principal, Domain(project.domain), q=term, kind="plan,task,decision,adr,document",
                         limit=20, project_id=project.id)
        except ValidationFailed:
            continue
        for it in res["items"]:
            # only this project's records and platform-global ones (no cross-project leakage)
            if it.get("project_id") not in (None, project.id) or n >= CAPS["metadata"]:
                continue
            body = f"{it['kind']}: {it['title']}" + (f"\n{it['subtitle']}" if it.get("subtitle") else "") + \
                (f"\n{it['snippet']}" if it.get("snippet") else "")
            if col.add(kind="metadata", ref=f"{it['kind']}:{it['id']}", content=body, trust="AEOS_RECORD",
                       provenance={"source": "aeos-db metadata search", "record_kind": it["kind"], "record_id": it["id"],
                                   "status": it.get("status"), "url": it.get("url"), "term": term},
                       title=it["title"]):
                n += 1


def _collect_memory(col: _Collector, session: Session, principal: Principal, project: Project,
                    terms: list[str]) -> None:
    rows = memory_mod.usable_for_context(session, project, allowed_project_ids=principal.allowed_project_ids)
    if terms:
        low = [t.lower() for t in terms]
        rows = [r for r in rows if any(t in r.content.lower() for t in low)]
    for r in rows[: CAPS["memory"]]:
        prov = r.provenance or {}
        col.add(kind="memory", ref=f"memory:{r.id}", content=r.content, trust="MEMORY_DATA", classification=r.classification,
                title=f"Memori ({r.scope})",
                provenance={"source": "aeos-db memory_records (ACCEPTED)", "memory_id": r.id, "scope": r.scope,
                            "source_type": prov.get("source_type"), "source_id": prov.get("source_id"),
                            "auth_decision_id": r.auth_decision_id, "owner_project_id": r.project_id})


def _collect_attachments(col: _Collector, session: Session, project: Project, terms: list[str]) -> None:
    from aeos.intake.service import read_extracted_text

    rows = list(session.scalars(select(Attachment).where(
        Attachment.project_id == project.id, Attachment.parse_status == "PARSED").order_by(Attachment.created_at.desc())))
    n = 0
    low = [t.lower() for t in terms]
    for att in rows:
        if n >= CAPS["attachment"]:
            return
        try:
            text, _ = read_extracted_text(session, att, max_chars=200_000)
        except AeosError:
            continue
        if low:
            lines = text.splitlines()
            hits = [i for i, ln in enumerate(lines) if any(t in ln.lower() for t in low)][:5]
            if not hits:
                continue
            body = "\n...\n".join("\n".join(lines[max(0, i - 2): i + 3]) for i in hits)
        else:
            body = text[:6000]
        if col.add(kind="attachment", ref=f"attachment:{att.id}", content=body, trust="UNTRUSTED_DATA",
                   classification=att.classification, title=att.filename, truncatable=True,
                   provenance={"source": "intake (untrusted)", "attachment_id": att.id, "sha256": att.sha256,
                               "filename": att.filename, "scan_status": att.scan_status,
                               "untrusted_instruction": att.untrusted_instruction}):
            n += 1


def _collect_research(col: _Collector, session: Session, terms: list[str]) -> None:
    if not terms:
        return
    conds = []
    for t in terms:
        pat = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds += [ResearchEntry.name.ilike(pat, escape="\\"), ResearchEntry.relevance.ilike(pat, escape="\\"),
                  ResearchEntry.category.ilike(pat, escape="\\")]
    rows = list(session.scalars(select(ResearchEntry).where(or_(*conds)).limit(CAPS["research"])))
    for r in rows:
        body = "\n".join(x for x in [
            f"{r.name} ({r.category or 'uncategorised'}) — lifecycle {r.lifecycle}, evidence {r.evidence_level}",
            f"Relevance: {r.relevance}" if r.relevance else "",
            f"Limitations: {r.limitations}" if r.limitations else "",
            f"Sources: {', '.join(r.urls[:5])}" if r.urls else "",
            f"Researched: {r.research_date or 'UNKNOWN'}; checked: {r.checked_at.isoformat() if r.checked_at else 'UNKNOWN'}",
        ] if x)
        col.add(kind="research", ref=f"research:{r.id}", content=body, trust="DATA", classification="INTERNAL",
                title=r.name, provenance={"source": "research registry (external, DATA only)", "research_id": r.id,
                                          "urls": r.urls[:5], "research_date": r.research_date,
                                          "evidence_level": r.evidence_level, "uncertainty": r.limitations or "UNKNOWN"})


# ---------------------------------------------------------------- build


def build_pack(
    session: Session, principal: Principal, project: Project, *, task_id: str | None = None,
    role_key: str | None = None, repository_id: str | None = None, base_commit: str | None = None,
    query: Any = None, paths: Any = None, forbidden_paths: Any = None, token_budget: int | None = None,
    include: Any = None, ttl_hours: int | None = None,
) -> tuple[ContextPack, ContextPackMeta]:
    budget = int(token_budget or DEFAULT_BUDGET)
    if not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise ValidationFailed(f"token_budget must be {MIN_BUDGET}..{MAX_BUDGET}", code="INVALID_BUDGET")
    ttl = DEFAULT_TTL_HOURS if ttl_hours is None else int(ttl_hours)
    if not 1 <= ttl <= MAX_TTL_HOURS:
        raise ValidationFailed(f"ttl_hours must be 1..{MAX_TTL_HOURS}", code="INVALID_TTL")
    role = (role_key or "default").strip()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", role):
        raise ValidationFailed("invalid role_key", code="INVALID_ROLE")
    kinds = list(ROLE_PROFILES.get(role, ROLE_PROFILES["default"]))
    if include:
        if not isinstance(include, list) or any(k not in INCLUDE_KINDS for k in include):
            raise ValidationFailed(f"include must be a subset of {INCLUDE_KINDS}", code="INVALID_INCLUDE")
        kinds = [k for k in INCLUDE_KINDS if k in include or k in MANDATORY]
    terms = _validate_terms(query)
    path_patterns = [_validate_glob(p) for p in (paths or [])][:50]
    forbidden = list(DEFAULT_FORBIDDEN) + [_validate_glob(p) for p in (forbidden_paths or [])][:100]
    allowed: list[str] = []
    warnings: list[str] = []

    task: TaskSpec | None = None
    if task_id:
        task = session.get(TaskSpec, task_id)
        if task is None or task.project_id != project.id or task.domain != project.domain:
            raise NotFound("task not found")
        allowed = [_validate_glob(p) for p in (task.allowed_paths or [])]
        forbidden += [_validate_glob(p) for p in (task.forbidden_paths or [])]
    elif "task" in kinds:
        kinds.remove("task")

    repo: RepositoryReference | None = None
    if repository_id:
        repo = session.get(RepositoryReference, repository_id)
        if repo is None or repo.project_id != project.id:
            raise NotFound("repository not found")
    else:
        repo = repos.primary_repository(session, project.id)
    repo_kinds = {"symbol", "repo_file", "code_match"} & set(kinds)
    reader: _RepoReader | None = None
    resolved_commit: str | None = None
    base_ref = base_commit or (task.base_commit if task and task.base_commit else None) or "HEAD"
    if repo_kinds:
        path = repos.resolve_local_path(repo) if repo else None
        if repo is None:
            warnings.append("repository context unavailable: no repository registered for this project")
        elif path is None:
            warnings.append(f"repository context unavailable: no local checkout for {repo.full_name}")
        else:
            resolved_commit = gitread.resolve_commit(path, base_ref)
            reader = _RepoReader(path, resolved_commit, forbidden, allowed)
            if reader.truncated:
                warnings.append("repository tree listing truncated; context may be incomplete")
            if repo.verification_status != "VERIFIED":
                warnings.append(f"repository {repo.full_name} identity is {repo.verification_status}")

    policy_snapshot = policy_hashes() if "policy" in kinds else {}
    col = _Collector(project, budget)
    for kind in kinds:
        if kind == "policy":
            _collect_policy(col, warnings)
        elif kind == "task" and task is not None:
            _collect_task(col, task)
        elif kind == "symbol" and reader and repo:
            _collect_symbols(col, reader, terms, repo.full_name)
        elif kind == "repo_file" and reader and repo and path_patterns:
            _collect_files(col, reader, path_patterns, repo.full_name, warnings)
        elif kind == "code_match" and reader and repo:
            _collect_matches(col, reader, terms, repo.full_name)
        elif kind == "metadata":
            _collect_metadata(col, session, principal, project, terms)
        elif kind == "memory":
            _collect_memory(col, session, principal, project, terms)
        elif kind == "attachment":
            _collect_attachments(col, session, project, terms)
        elif kind == "research":
            _collect_research(col, session, terms)
        if kind in MANDATORY and any(o["kind"] == kind for o in col.omitted):
            raise ValidationFailed(f"token budget {budget} cannot hold the mandatory {kind} context",
                                   code="BUDGET_TOO_SMALL")

    classification = cls.highest(project.data_classification, task.data_classification if task else None,
                                 *[i["classification"] for i in col.items])
    pack = ContextPack(
        project_id=project.id, domain=project.domain, task_id=task.id if task else None, role_key=role,
        base_commit=resolved_commit, items=col.items, token_budget=budget, classification=classification, stale=False,
    )
    pack.pack_hash = content_hash({
        "project_id": project.id, "domain": project.domain, "task_id": pack.task_id, "role_key": role,
        "base_commit": resolved_commit, "token_budget": budget,
        "items": [{"kind": i["kind"], "ref": i["ref"], "hash": i["hash"]} for i in col.items],
    })
    session.add(pack)
    session.flush()
    now = utcnow()
    meta = ContextPackMeta(
        context_pack_id=pack.id, repository_id=repo.id if repo and reader else None,
        repository_full_name=repo.full_name if repo else None,
        request={"query": terms, "paths": path_patterns, "base_ref": base_ref, "include": include or None,
                 "ttl_hours": ttl, "policy_snapshot": policy_snapshot},
        include_kinds=kinds, allowed_paths=allowed, forbidden_paths=forbidden, token_estimate=col.used,
        omitted=col.omitted, warnings=warnings, observed_head=resolved_commit, checked_at=now,
        created_by=principal.user.id, expires_at=now + timedelta(hours=ttl),
    )
    if reader:
        meta.request["skipped"] = {"symlinks": reader.skipped_symlinks, "forbidden": reader.skipped_forbidden}
    session.add(meta)
    session.flush()
    superseded = _supersede_older(session, pack, meta)
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="knowledge.context_pack.build",
                 target_type="context_pack", target_id=pack.id, project_id=project.id, domain=project.domain,
                 details={"role": role, "task_id": pack.task_id, "base_commit": resolved_commit, "items": len(col.items),
                          "tokens": col.used, "budget": budget, "omitted": len(col.omitted), "hash": pack.pack_hash,
                          "ttl_hours": ttl, "superseded": superseded})
    events.emit(session, source="knowledge", kind="knowledge.context_pack.created", entity_type="context_pack",
                entity_id=pack.id, entity_version=pack.pack_hash, project_id=project.id, domain=project.domain,
                payload={"items": len(col.items), "tokens": col.used, "budget": budget})
    return pack, meta


# ---------------------------------------------------------------- staleness


def _policy_changes(pack: ContextPack, meta: ContextPackMeta | None) -> list[str]:
    if not any(i.get("kind") == "policy" for i in pack.items or []):
        return []
    current = policy_hashes()
    snapshot = (meta.request or {}).get("policy_snapshot") if meta is not None else None
    reasons: list[str] = []
    if isinstance(snapshot, dict):  # the whole policy file set as seen at build time
        for rel in sorted(set(snapshot) | set(current)):
            if rel not in current:
                reasons.append(f"policy file policy/{rel} was removed")
            elif rel not in snapshot:
                reasons.append(f"policy file policy/{rel} was added")
            elif current[rel] != snapshot[rel]:
                reasons.append(f"policy file policy/{rel} changed")
        return reasons
    for item in pack.items or []:  # packs built before the snapshot existed: compare included items
        if item.get("kind") == "policy" and item.get("ref", "").startswith("policy/"):
            rel = item["ref"][len("policy/"):]
            if current.get(rel) != (item.get("provenance") or {}).get("sha256"):
                reasons.append(f"policy file {item['ref']} changed or was removed")
    return reasons


def check_freshness(session: Session, pack: ContextPack, meta: ContextPackMeta | None) -> str:
    """CURRENT | STALE | UNKNOWN. Marks the pack STALE (sticky) when its base or policy moved, its
    TTL expired or it was superseded."""
    if pack.stale:
        return "STALE"
    reasons: list[str] = []
    unknown = False
    if meta is not None and meta.expires_at is not None and ensure_aware(meta.expires_at) <= utcnow():  # type: ignore[operator]
        reasons.append("pack TTL expired; build a new pack")
    reasons.extend(_policy_changes(pack, meta))
    head: str | None = None
    if meta is not None and meta.repository_id and pack.base_commit:
        repo = session.get(RepositoryReference, meta.repository_id)
        path = repos.resolve_local_path(repo) if repo else None
        if path is None:
            unknown = True
        else:
            try:
                head = gitread.resolve_commit(path, "HEAD")
            except AeosError:
                unknown = True
            if head and head != pack.base_commit:
                reasons.append(f"repository head moved {pack.base_commit[:12]} -> {head[:12]} (pack built for an older base)")
    if meta is not None:
        meta.checked_at = utcnow()
        if head:
            meta.observed_head = head
    if reasons:
        _mark_stale(session, pack, meta, "; ".join(reasons))
        return "STALE"
    return "UNKNOWN" if unknown else "CURRENT"


def _supersede_older(session: Session, pack: ContextPack, meta: ContextPackMeta) -> list[str]:
    """A new pack for the same project/task/role/repository supersedes older non-stale ones.

    Only task-scoped packs supersede: ad-hoc packs (no TaskSpec) with different queries are separate
    explorations and simply expire through their TTL."""
    if pack.task_id is None:
        return []
    task_cond = ContextPack.task_id == pack.task_id
    repo_cond = (ContextPackMeta.repository_id.is_(None) if meta.repository_id is None
                 else ContextPackMeta.repository_id == meta.repository_id)
    rows = session.execute(
        select(ContextPack, ContextPackMeta).join(ContextPackMeta, ContextPackMeta.context_pack_id == ContextPack.id)
        .where(ContextPack.project_id == pack.project_id, ContextPack.domain == pack.domain,
               ContextPack.id != pack.id, ContextPack.stale.is_(False), ContextPack.role_key == pack.role_key,
               task_cond, repo_cond)
    ).all()
    out: list[str] = []
    for old, old_meta in rows:
        old_meta.superseded_by = pack.id
        _mark_stale(session, old, old_meta, f"superseded by context pack {pack.id}")
        out.append(old.id)
    return out


def _mark_stale(session: Session, pack: ContextPack, meta: ContextPackMeta | None, reason: str) -> None:
    pack.stale = True
    if meta is not None:
        meta.stale_reason = reason[:2000]
    session.flush()
    events.emit(session, source="knowledge", kind="knowledge.context_pack.stale", entity_type="context_pack",
                entity_id=pack.id, entity_version=pack.pack_hash, project_id=pack.project_id, domain=pack.domain,
                payload={"reason": reason[:300]})


def mark_stale_for_repository(session: Session, repository_id: str, new_head: str, *, reason: str) -> int:
    rows = session.execute(
        select(ContextPack, ContextPackMeta).join(ContextPackMeta, ContextPackMeta.context_pack_id == ContextPack.id)
        .where(ContextPackMeta.repository_id == repository_id, ContextPack.stale.is_(False))
    ).all()
    n = 0
    for pack, meta in rows:
        if pack.base_commit and pack.base_commit != new_head:
            _mark_stale(session, pack, meta, reason)
            n += 1
    return n


def get_scoped(session: Session, project_domain: str, pack_id: str) -> tuple[ContextPack, ContextPackMeta | None]:
    pack = session.get(ContextPack, pack_id)
    if pack is None or pack.domain != project_domain:
        raise NotFound("context pack not found")
    return pack, session.get(ContextPackMeta, pack.id)


def list_packs(session: Session, project: Project) -> list[tuple[ContextPack, ContextPackMeta | None]]:
    rows = session.execute(
        select(ContextPack, ContextPackMeta).outerjoin(ContextPackMeta, ContextPackMeta.context_pack_id == ContextPack.id)
        .where(ContextPack.project_id == project.id, ContextPack.domain == project.domain)
        .order_by(ContextPack.created_at.desc()).limit(100)
    ).all()
    return [(p, m) for p, m in rows]


def pack_view(pack: ContextPack, meta: ContextPackMeta | None, *, freshness: str | None = None,
              include_content: bool = True) -> dict[str, Any]:
    items = pack.items or []
    if not include_content:
        items = [{k: v for k, v in i.items() if k != "content"} for i in items]
    return {
        "id": pack.id,
        "project_id": pack.project_id,
        "domain": pack.domain,
        "task_id": pack.task_id,
        "role_key": pack.role_key,
        "base_commit": pack.base_commit,
        "classification": pack.classification,
        "token_budget": pack.token_budget,
        "token_estimate": meta.token_estimate if meta else sum(int(i.get("tokens", 0)) for i in pack.items or []),
        "pack_hash": pack.pack_hash,
        "stale": pack.stale,
        "freshness": freshness or ("STALE" if pack.stale else "UNKNOWN"),
        "stale_reason": meta.stale_reason if meta else "",
        "repository": meta.repository_full_name if meta else None,
        "request": meta.request if meta else {},
        "include_kinds": meta.include_kinds if meta else [],
        "forbidden_paths": meta.forbidden_paths if meta else [],
        "allowed_paths": meta.allowed_paths if meta else [],
        "omitted": meta.omitted if meta else [],
        "warnings": meta.warnings if meta else [],
        "checked_at": meta.checked_at.isoformat() if meta and meta.checked_at else None,
        "expires_at": meta.expires_at.isoformat() if meta and meta.expires_at else None,
        "superseded_by": meta.superseded_by if meta else None,
        "item_count": len(pack.items or []),
        "kinds": sorted({i.get("kind") for i in pack.items or []}),
        "items": items,
        "created_at": pack.created_at.isoformat(),
        "trust_note": "Repository/attachment/research items are DATA; only POLICY_READ_ONLY items carry policy, "
                      "and none of them grant permissions.",
    }
