"""ContextPack builder (§24 Konteks, retrieval dan memori; §13 ContextPack).

A pack is the minimum, provenance-carrying context for one task/role:

* repository context = file/symbol subset of an EXACT commit (``git ls-tree`` / ``git grep -n`` /
  ``git cat-file blob <sha>:<path>``), never a dump of the repo and never the working tree (symlinks and
  submodules are skipped, so nothing outside the repository can be read);
* project context = metadata search over this project's plans/tasks/decisions/docs (+ global ADRs);
* policy context = the trusted controller's ``policy/`` files, read-only with hashes; missing policy
  is reported, never reconstructed;
* runtime context = metadata + redacted inline summaries of EvidenceArtifacts written by trusted
  collectors (verifier/doctor/collector, ``trusted=True``) for this project; worker-reported evidence
  is never runtime context, and MOCK/DEMO evidence keeps its label;
* research and attachments are labelled DATA / UNTRUSTED_DATA; memory is MEMORY_DATA (never policy);
* every item has kind/ref/provenance/classification/hash and a token estimate (chars/4) checked
  against the budget; forbidden scope (defaults + request + TaskSpec) is always respected;
* classification (ADR-0133): an item keeps the class it had when it was created - its own label (memory, attachment,
  evidence, policy, research, the TaskSpec), else the project's class at the record's creation time
  (``portfolio.facade.classification_at``; the highest class the project ever had when the time is unknown), never
  below the project's current class; repository content at the pack's commit takes the project's current class. A
  task-bound pack has a ceiling, the task's effective class (``routing.facade.effective_task_class``): an item above it
  is left out and listed in ``omitted`` with a classification reason, and the local MCI source never enters a pack for
  a PUBLIC task;
* the pack is hashed; it becomes STALE when the repository head moves off its base commit, the
  policy file set changes, its TTL expires, or a newer pack for the same scope supersedes it.
"""

from __future__ import annotations

import fnmatch
import functools
import math
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import DataClassification, Domain
from aeos.core.errors import AeosError, NotFound, ValidationFailed
from aeos.core.hashing import content_hash, file_sha256, sha256_bytes, sha256_text
from aeos.core.paths import UnsafePathError, ensure_within, repo_root
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.identity.facade import Principal
from aeos.knowledge import memory as memory_mod
from aeos.models.evidence import EvidenceArtifact
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
REPO_KINDS = frozenset({"symbol", "repo_file", "code_match"})
INCLUDE_KINDS = ("policy", "task", "symbol", "repo_file", "code_match", "project_doc", "metadata", "runtime", "memory",
                 "attachment", "research")
MANDATORY = frozenset({"policy", "task"})
ROLE_PROFILES: dict[str, tuple[str, ...]] = {
    "default": ("policy", "task", "symbol", "repo_file", "code_match", "project_doc", "metadata", "memory"),
    "planner": ("policy", "task", "project_doc", "metadata", "memory", "attachment", "research", "repo_file", "symbol"),
    "feature-implementer": ("policy", "task", "symbol", "repo_file", "code_match", "project_doc", "runtime", "memory"),
    "reviewer": ("policy", "task", "symbol", "repo_file", "code_match", "project_doc", "metadata", "runtime", "memory"),
    "security-reviewer": ("policy", "task", "symbol", "repo_file", "code_match", "project_doc", "metadata", "runtime"),
    "docs-writer": ("policy", "task", "project_doc", "metadata", "memory", "repo_file", "attachment"),
    "researcher": ("policy", "task", "research", "metadata", "memory"),
}
# §24 "Summary tidak mengalahkan source terbaru": sources are collected (and get the budget) before
# summaries; memory and metadata search results come last and are marked subordinate to the source.
SOURCE_ORDER = ("policy", "task", "symbol", "repo_file", "code_match", "runtime", "project_doc", "metadata", "attachment",
                "research", "memory")
SUMMARY_KINDS = frozenset({"memory", "metadata"})
ACCEPTED_DOC_STATUSES = ("APPROVED", "PUBLISHED")
PACK_STATUSES = ("ACTIVE", "STALE", "EXPIRED", "SUPERSEDED")
DEFAULT_FORBIDDEN = (".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*id_rsa*",
                     "*id_dsa*", "*id_ecdsa*", "*id_ed25519*", "*secrets.*", "secrets/*", "*/secrets/*", "*credentials*",
                     ".git/*", "*.sqlite3", "*.sqlite", "*.db", "*.kdbx", "*.keystore", "*.jks", "*.tfstate",
                     "*.tfstate.*", ".npmrc", "*/.npmrc", ".pypirc", "*/.pypirc", ".netrc", "*/.netrc", ".ssh/*",
                     "*/.ssh/*", ".aws/*", "*/.aws/*")
CAPS = {"symbol": 20, "repo_file": 20, "code_match": 30, "metadata": 20, "runtime": 10, "memory": 20, "attachment": 10,
        "research": 10, "project_doc": 12}
TRUSTED_EVIDENCE_ORIGINS = frozenset({"verifier", "doctor", "collector"})
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
    def __init__(self, project: Project, budget: int, *, ceiling: str | None = None,
                 class_at: Callable[[Any], str] | None = None):
        self.project = project
        self.budget = budget
        self.used = 0
        self.items: list[dict[str, Any]] = []
        self.omitted: list[dict[str, Any]] = []
        self.refs: set[str] = set()
        self.ceiling = ceiling  # ADR-0133: a task-bound pack never carries an item above the task's effective class
        self.class_at = class_at
        self.withheld = 0

    def item_class(self, kind: str, classification: str | None, created_at: Any) -> str:
        """The item's own class (ADR-0133): its label, else the project's class when the record was created (the
        highest it ever had when that time is unknown); repository content takes the current project class. Never
        below the project's current class."""
        if classification is not None:
            base = classification
        elif kind in REPO_KINDS or self.class_at is None:
            base = self.project.data_classification
        else:
            base = self.class_at(created_at)
        return cls.highest(base, self.project.data_classification)

    def add(self, *, kind: str, ref: str, content: str, provenance: dict[str, Any], trust: str,
            classification: str | None = None, title: str = "", truncatable: bool = False,
            mandatory: bool = True, created_at: Any = None) -> bool:
        if ref in self.refs:
            return False
        item_cls = self.item_class(kind, classification, created_at)
        if self.ceiling is not None and cls.rank(item_cls) > cls.rank(self.ceiling):
            self.withheld += 1
            self.omitted.append({"kind": kind, "ref": ref, "classification": item_cls, "mandatory": False,
                                 "reason": f"classification {item_cls} above the task's class {self.ceiling}"})
            return False
        redacted = redact_text(content)
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
                self.omitted.append({"kind": kind, "ref": ref, "reason": "token budget", "tokens": tokens,
                                     **({} if mandatory else {"mandatory": False})})
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


def _local_only_mci(rel: str) -> bool:
    """Owner-local canonical MCI material (the source, its pack mapping) that a copy in the checkout must never turn
    into policy context: the repository is public and the source is read from this machine only (.gitignore guards
    the same names)."""
    name = rel.rsplit("/", 1)[-1]
    return rel.split("/", 1)[0].lower() == "canonical" or name.upper().startswith("MCI_V2_SOURCE_OF_TRUTH") \
        or name.lower() == "mci-pack-mapping.yaml"


def misplaced_mci_files() -> list[str]:
    """``policy/`` paths of canonical MCI material placed in the checkout by mistake (never read; see policy_files)."""
    root = repo_root() / "policy"
    if not root.is_dir():
        return []
    return [f"policy/{rel}" for rel in (p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file())
            if _local_only_mci(rel)]


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
        if _local_only_mci(p.relative_to(root).as_posix()):
            continue  # an MCI copy in the checkout is never policy context (misplaced_mci_files names it)
        try:
            files.append(ensure_within(root, p))  # a symlink out of policy/ is ignored
        except UnsafePathError:
            continue
    return root, files


def policy_hashes() -> dict[str, str]:
    root, files = policy_files()
    return {p.relative_to(root.resolve()).as_posix(): file_sha256(p) for p in files}


# §24 "Policy context: MCI ... read-only, versi/hash jelas". The canonical MCI source may be larger than the per-file
# cap: it is then split at line boundaries into labelled parts of at most MAX_POLICY_BYTES (each item names its part,
# line range, part hash and the whole document's hash); parts beyond MAX_MCI_PARTS or the token budget are listed as
# omitted, and the pack warns that gates relying on them stay NOT-PROVEN. Other policy files over the cap are skipped
# with a warning (never silently truncated).
MAX_MCI_PARTS = 16
MCI_CHUNKING_RULE = (f"canonical MCI over {MAX_POLICY_BYTES} bytes: split at line boundaries into parts of at most "
                     f"{MAX_POLICY_BYTES} bytes, at most {MAX_MCI_PARTS} parts per pack")


def _mci_parts(data: bytes) -> list[tuple[int, int, bytes]]:
    """(first line, last line, bytes) parts of at most MAX_POLICY_BYTES; an over-long line is split hard."""
    parts: list[tuple[int, int, bytes]] = []
    buf, first, line_no = b"", 1, 0
    for line in data.splitlines(keepends=True):
        line_no += 1
        while len(line) > MAX_POLICY_BYTES:  # a single line longer than a part
            if buf:
                parts.append((first, line_no - 1, buf))
                buf = b""
            parts.append((line_no, line_no, line[:MAX_POLICY_BYTES]))
            line = line[MAX_POLICY_BYTES:]
            first = line_no
        if buf and len(buf) + len(line) > MAX_POLICY_BYTES:
            parts.append((first, line_no - 1, buf))
            buf, first = b"", line_no
        if not buf:
            first = line_no
        buf += line
    if buf:
        parts.append((first, line_no, buf))
    return parts


def _mci_provenance(mci: dict[str, Any]) -> dict[str, Any]:
    return {"mci_status": mci["status"], "mci_declared_status": mci["declared_status"],
            "mci_verification": mci["verification_code"], "mci_version": mci["version"],
            "mci_content_hash": mci["content_hash"], "mci_compliance": mci["compliance"]}


def _same_digest(a: Any, b: Any) -> bool:
    def norm(v: Any) -> str:
        t = str(v or "").strip().lower()
        return t[7:] if t.startswith("sha256:") else t

    return bool(norm(a)) and norm(a) == norm(b)


def _add_mci_source(col: _Collector, path: Path, ref: str, mci: dict[str, Any], warnings: list[str]) -> None:
    """Add the VERIFIED local MCI source (whole, or in labelled parts). The bytes are re-hashed as read: a file that
    changed after verification is not used (the pack says so). Its parts are not mandatory for the budget - the
    reference in policy/mci-reference.yaml is - and every omitted part is named in the warnings."""
    data = path.read_bytes()
    digest = "sha256:" + sha256_bytes(data)
    if not _same_digest(digest, mci["content_hash"]):
        warnings.append(f"canonical MCI {ref} changed after verification (hash mismatch): not included; gates relying "
                        "on it stay NOT-PROVEN")
        return
    base = {"source": "owner-local canonical MCI source (read-only, never in the repository)",
            "path": mci.get("source_location") or ref, "sha256": digest, "read_only": True, "mci_source": True,
            **_mci_provenance(mci)}
    if len(data) <= MAX_POLICY_BYTES:
        if not col.add(kind="policy", ref=ref, content=data.decode("utf-8", errors="replace"), provenance=base,
                       trust="POLICY_READ_ONLY", classification="INTERNAL", title=f"{ref} (MCI)", mandatory=False):
            warnings.append(f"canonical MCI {ref} not in this pack (token budget); gates relying on it stay NOT-PROVEN")
        return
    parts = _mci_parts(data)
    n = len(parts)
    omitted: list[int] = []
    for i, (a, b, chunk) in enumerate(parts, 1):
        if i > MAX_MCI_PARTS:
            omitted.append(i)
            continue
        prov = {**base, "part": {"index": i, "of": n, "lines": [a, b], "bytes": len(chunk),
                                 "sha256": "sha256:" + sha256_bytes(chunk)}, "chunking": MCI_CHUNKING_RULE}
        if not col.add(kind="policy", ref=f"{ref}#part-{i}-of-{n}", content=chunk.decode("utf-8", errors="replace"),
                       provenance=prov, trust="POLICY_READ_ONLY", classification="INTERNAL",
                       title=f"{ref} (MCI bagian {i}/{n}, baris {a}-{b})", mandatory=False):
            omitted.append(i)
    warnings.append(f"canonical MCI {ref} is {len(data)} bytes: split into {n} labelled parts ({MCI_CHUNKING_RULE})")
    if omitted:
        warnings.append(f"canonical MCI {ref} only partly in this pack: parts {omitted} of {n} omitted (part cap or "
                        "token budget); gates relying on the omitted parts stay NOT-PROVEN")


def mci_state() -> dict[str, Any]:
    """The verified MCI reference a pack was built against (a change makes the pack STALE)."""
    from aeos.policy import facade as policy

    m = policy.mci_reference(repo=repo_root())
    return {k: m[k] for k in ("status", "verification_code", "version", "content_hash")}


def _collect_policy(col: _Collector, warnings: list[str], deferred: list[Callable[[], None]] | None = None) -> None:
    """Policy files (mandatory) and the VERIFIED local MCI source. With *deferred*, the MCI source is queued instead of
    added: build_pack adds it once the mandatory task context is in, so a large MCI never pushes the task out of the
    budget (BUDGET_TOO_SMALL); its parts take what is left and every omitted part is named."""
    from aeos.policy import facade as policy

    root, files = policy_files()
    misplaced = misplaced_mci_files()
    if misplaced:
        warnings.append(f"canonical MCI material inside the repository checkout is never read (public repository; "
                        f"keep it in <data dir>/policy/canonical): {', '.join(misplaced[:5])}")
    if not files:
        warnings.append("policy context MISSING: no policy/ files in the trusted controller; nothing reconstructed")
        col.add(kind="policy", ref="policy:missing", content="POLICY CONTEXT MISSING — gates that need it stay BLOCKED.",
                provenance={"source": "trusted-controller policy/", "status": "MISSING"}, trust="POLICY_READ_ONLY",
                classification="INTERNAL", title="policy/ (missing)")
        return
    # the status the policy module itself verifies (declared VERIFIED with a missing/mismatched file is UNVERIFIED)
    repo = repo_root()
    mci = policy.mci_reference(repo=repo)
    if mci["status"] != "VERIFIED":
        warnings.append(f"MCI reference status is {mci['status']} (declared {mci['declared_status']}, "
                        f"{mci['verification_code']}): canonical policy alignment UNVERIFIED")
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
            prov.update(_mci_provenance(mci))
        col.add(kind="policy", ref=f"policy/{rel}", content=text, provenance=prov, trust="POLICY_READ_ONLY",
                classification="INTERNAL", title=f"policy/{rel}")
    # the canonical source itself lives on this machine only (the repository is public): only a VERIFIED source is
    # canonical policy context; the item names its logical location, never a host path
    source = policy.mci_source_path(repo=repo)
    if source is None or not source.is_file():
        return
    if col.ceiling == DataClassification.PUBLIC.value:
        # ADR-0133: the owner-local MCI source never enters a pack for a PUBLIC task, whatever its status (not read)
        warnings.append("local MCI source not included: it never goes into a pack for a PUBLIC task")
        return
    if mci["status"] == "VERIFIED":
        add = functools.partial(_add_mci_source, col, source, f"mci:{source.name}", mci, warnings)
        if deferred is None:
            add()
        else:
            deferred.append(add)
    else:
        warnings.append(f"local MCI source not included: it is not VERIFIED ({mci['verification_code']})")


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
            classification=task.data_classification, title=f"TaskSpec {task.task_key}",  # own label (ADR-0133)
            provenance={"source": "aeos-db task_specs (immutable)", "task_spec_id": task.id,
                        "plan_version_id": task.plan_version_id, "spec_hash": task.spec_hash})


# §24 "Project context: PRD/RFC/TDD, keputusan, kontrak, accepted artifacts": only accepted records become
# metadata context - an approved/published plan, an ACTIVE decision, an ACCEPTED ADR, an APPROVED/PUBLISHED document
# (TaskSpecs exist only for compiled, approved plan versions). Drafts, superseded, rejected or revoked records are
# never project context, not even as a SUBORDINATE summary.
ACCEPTED_METADATA_STATUSES: dict[str, frozenset[str] | None] = {
    "plan": frozenset({"APPROVED", "PUBLISHING", "PUBLISHED", "PUBLICATION_PARTIAL", "PUBLICATION_FAILED"}),
    "task": None,  # immutable TaskSpec of an approved PlanVersion
    "decision": frozenset({"ACTIVE"}),
    "adr": frozenset({"ACCEPTED"}),
    "document": frozenset(ACCEPTED_DOC_STATUSES),
}


def metadata_accepted(item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or "")
    if kind not in ACCEPTED_METADATA_STATUSES:
        return False
    allowed = ACCEPTED_METADATA_STATUSES[kind]
    return allowed is None or str(item.get("status") or "").upper() in allowed


def _effective_item(session: Session, it: dict[str, Any]) -> dict[str, Any]:
    """The search item with the status consumers must judge by: a decision past ``expires_at`` is EXPIRED even before
    the expiry is written back; an ADR's status is the owner's recorded decision while it binds the current content,
    else the (builder-written) file status."""
    from aeos.policy import facade as policy

    kind = it.get("kind")
    if kind == "decision":
        return {**it, "status": policy.decision_effective_status(session, str(it["id"])) or "UNKNOWN",
                "stored_status": it.get("status")}
    if kind == "adr":
        reg = policy.adr_register_status(session, str(it["id"]))
        if reg is None:
            return {**it, "status": "UNKNOWN"}
        return {**it, "status": reg["effective_status"], "file_status": reg["file_status"],
                "subtitle": f"{reg['effective_status']} ({reg['status_source']})",
                "status_source": reg["status_source"], "owner_status_state": reg["owner_status_state"]}
    return it


def _collect_metadata(col: _Collector, session: Session, principal: Principal, project: Project, terms: list[str]) -> None:
    from aeos.portfolio.search import search

    n = 0
    for term in terms:
        try:
            res = search(session, principal, Domain(project.domain), q=term, kind="plan,task,decision,adr,document",
                         limit=20, project_id=project.id)
        except ValidationFailed:
            continue
        for raw in res["items"]:
            # only this project's records and platform-global ones (no cross-project leakage)
            if raw.get("project_id") not in (None, project.id) or n >= CAPS["metadata"]:
                continue
            it = _effective_item(session, raw)
            if not metadata_accepted(it):
                continue  # a draft/superseded/revoked record is not project context (not a budget omission)
            body = f"{it['kind']}: {it['title']}" + (f"\n{it['subtitle']}" if it.get("subtitle") else "") + \
                (f"\n{it['snippet']}" if it.get("snippet") else "")
            if col.add(kind="metadata", ref=f"{it['kind']}:{it['id']}", content=body, trust="AEOS_RECORD",
                       provenance={"source": "aeos-db metadata search", "record_kind": it["kind"], "record_id": it["id"],
                                   "status": it.get("status"), "url": it.get("url"), "term": term,
                                   **({"status_source": it["status_source"]} if it.get("status_source") else {}),
                                   "precedence": "SUBORDINATE",
                                   "subordinate_to": "project_doc (approved) and repository source items"},
                       title=it["title"]):
                n += 1


def _collect_project_docs(col: _Collector, session: Session, project: Project) -> None:
    """§24 project context: the APPROVED PlanVersion (PRD/RFC/TDD sections, decisions, contracts) and the
    ACCEPTED Product 1 documents rendered from it. Drafts, superseded versions and unapproved plans are
    never project context."""
    import json

    from aeos.models.planning import Plan, PlanVersion
    from aeos.models.publishing import DocumentRecord, DocumentRevision

    n = 0
    plans = list(session.scalars(select(Plan).where(Plan.project_id == project.id, Plan.domain == project.domain,
                                                    Plan.approved_version_id.is_not(None))
                                 .order_by(Plan.created_at.desc())))
    approved_ids: set[str] = set()
    for plan in plans:
        pv = session.get(PlanVersion, plan.approved_version_id)
        if pv is None or pv.project_id != project.id:
            continue
        approved_ids.add(pv.id)
        content = pv.content if isinstance(pv.content, dict) else {}
        for section, value in content.items():
            if n >= CAPS["project_doc"]:
                return
            body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if col.add(kind="project_doc", ref=f"plan_version:{pv.id}#{section}", content=f"{section}:\n{body}",
                       trust="AEOS_RECORD", truncatable=True, title=f"{plan.title} v{pv.version} · {section}",
                       created_at=pv.created_at,
                       provenance={"source": "aeos-db plan_versions (APPROVED, immutable)", "plan_id": plan.id,
                                   "plan_version_id": pv.id, "version": pv.version, "manifest_hash": pv.manifest_hash,
                                   "status": "APPROVED", "section": section}):
                n += 1
    if not approved_ids:
        return
    docs = list(session.scalars(select(DocumentRecord).where(
        DocumentRecord.project_id == project.id, DocumentRecord.domain == project.domain,
        DocumentRecord.status.in_(ACCEPTED_DOC_STATUSES), DocumentRecord.source_plan_version_id.in_(approved_ids))))
    for doc in docs:
        if n >= CAPS["project_doc"]:
            return
        rev = session.scalar(select(DocumentRevision).where(
            DocumentRevision.document_id == doc.id, DocumentRevision.kind == "RENDERED",
            DocumentRevision.status == "CURRENT").order_by(DocumentRevision.created_at.desc()).limit(1))
        if rev is None or not rev.body_markdown:
            continue
        if col.add(kind="project_doc", ref=f"document:{doc.logical_doc_id}", content=rev.body_markdown, trust="AEOS_RECORD",
                   truncatable=True, title=doc.title, created_at=rev.created_at,
                   provenance={"source": "aeos-db document_revisions (RENDERED, CURRENT)", "document_id": doc.id,
                               "status": doc.status, "content_hash": rev.content_hash,
                               "plan_version_id": doc.source_plan_version_id}):
            n += 1


def _collect_memory(col: _Collector, session: Session, principal: Principal, project: Project,
                    terms: list[str], base_commit: str | None) -> None:
    rows = memory_mod.usable_for_context(session, project, allowed_project_ids=principal.allowed_project_ids)
    if terms:
        low = [t.lower() for t in terms]
        rows = [r for r in rows if any(t in r.content.lower() for t in low)]
    subordinate = f"source at {base_commit[:12]}" if base_commit else "the pack's source items"
    for r in rows[: CAPS["memory"]]:
        prov = r.provenance or {}
        col.add(kind="memory", ref=f"memory:{r.id}", content=r.content, trust="MEMORY_DATA", classification=r.classification,
                title=f"Memori ({r.scope})",
                provenance={"source": "aeos-db memory_records (ACCEPTED)", "memory_id": r.id, "scope": r.scope,
                            "source_type": prov.get("source_type"), "source_id": prov.get("source_id"),
                            "auth_decision_id": r.auth_decision_id, "owner_project_id": r.project_id,
                            # §41/§43: whether this memory's acceptance was evaluated as a system change
                            "system_change_evaluation": (prov.get("system_change") or {}).get("evaluation",
                                                                                               "NOT_EVALUATED"),
                            "precedence": "SUBORDINATE", "subordinate_to": subordinate})


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


def _collect_runtime(col: _Collector, session: Session, project: Project, task: TaskSpec | None) -> None:
    """§24 runtime context: evidence collected by trusted tools, already reduced/redacted (inline summary)."""
    import json

    q = select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.domain == project.domain,
        EvidenceArtifact.trusted.is_(True), EvidenceArtifact.origin.in_(sorted(TRUSTED_EVIDENCE_ORIGINS)))
    if task is not None:
        q = q.where(EvidenceArtifact.task_id == task.id)
    for ev in session.scalars(q.order_by(EvidenceArtifact.created_at.desc()).limit(CAPS["runtime"])):
        mode = (ev.mode or "LIVE").upper()
        label = "" if mode == "LIVE" else f"[{mode}] "
        lines = [f"{label}{ev.kind} ({ev.origin}, {ev.collector_identity}) evidence {ev.evidence_level}",
                 f"Claim: {ev.claim}" if ev.claim else "",
                 f"Environment: {json.dumps(ev.environment, sort_keys=True, default=str)}" if ev.environment else "",
                 f"Summary: {json.dumps(ev.inline, sort_keys=True, default=str)}" if ev.inline else ""]
        body = "\n".join(x for x in lines if x)
        col.add(kind="runtime", ref=f"evidence:{ev.id}", content=body, trust="RUNTIME_EVIDENCE",
                classification=ev.classification, title=f"{label}{ev.kind} · {ev.evidence_level}", truncatable=True,
                provenance={"source": "aeos-db evidence_artifacts (trusted collector)", "evidence_id": ev.id,
                            "origin": ev.origin, "collector_identity": ev.collector_identity,
                            "content_hash": ev.content_hash, "evidence_level": ev.evidence_level, "mode": mode,
                            "run_id": ev.run_id, "task_id": ev.task_id, "collected_at": ev.created_at.isoformat()})


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


def _collect_task_artifacts(col: _Collector, task: TaskSpec, artifacts: list[dict[str, Any]]) -> None:
    """§21 TaskSpec input artifacts (hash-bound attachments delivered by the planning context) as UNTRUSTED DATA
    items under the pack's token budget (wave 7, W6-071): right after the TaskSpec, before any summary."""
    for i, art in enumerate(artifacts[:CAPS["attachment"]]):
        name = str(art.get("name") or art.get("ref") or f"artifact-{i}")[:200]
        content = art.get("content")
        if content is None:
            content = {k: v for k, v in art.items() if k != "content"}
        if not isinstance(content, str):
            import json

            content = json.dumps(content, ensure_ascii=False, default=str)
        # ADR-0133: a delivered attachment keeps its own class (the executor states it; unknown -> the project's
        # highest class); other artifacts are part of the immutable TaskSpec and carry its class
        own = art.get("classification") if art.get("kind") == "attachment" else task.data_classification  # own label (ADR-0133)
        col.add(kind="attachment", ref=f"task-artifact:{art.get('ref') or name}", content=content,
                trust="UNTRUSTED_DATA", classification=own, title=name, truncatable=True,
                provenance={"source": TASK_ARTIFACT_SOURCE, "task_spec_id": task.id, "ref": art.get("ref"),
                            "sha256": art.get("sha256"), "kind": art.get("kind")})


TASK_ARTIFACT_SOURCE = "TaskSpec input_artifacts (hash-bound; untrusted DATA)"


def build_pack(
    session: Session, principal: Principal, project: Project, *, task_id: str | None = None,
    role_key: str | None = None, repository_id: str | None = None, base_commit: str | None = None,
    query: Any = None, paths: Any = None, forbidden_paths: Any = None, token_budget: int | None = None,
    include: Any = None, ttl_hours: int | None = None, task_artifacts: list[dict[str, Any]] | None = None,
    system_actor: str | None = None,
) -> tuple[ContextPack, ContextPackMeta]:
    """``task_artifacts`` / ``system_actor`` (additive, wave 7): the TaskSpec's delivered input artifacts as DATA
    items, and a system builder (the worker runtime at run start) recorded as actor_type ``system``."""
    from aeos.portfolio.service import require_writable

    require_writable(project)
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
            # Office source never enters a Personal pack (and vice versa): checked before any content read.
            repos.require_checkout_in_domain(session, principal, project, repo, path,
                                             action="knowledge.context_pack.build")
            resolved_commit = gitread.resolve_commit(path, base_ref)
            reader = _RepoReader(path, resolved_commit, forbidden, allowed)
            if reader.truncated:
                warnings.append("repository tree listing truncated; context may be incomplete")
            if repo.verification_status != "VERIFIED":
                warnings.append(f"repository {repo.full_name} identity is {repo.verification_status}")
            if repo.local_status != "OK":
                warnings.append(f"local checkout status is {repo.local_status}: it is not confirmed to be "
                                f"{repo.full_name} (run Verifikasi identitas to re-check the origin remote)")

    policy_snapshot = policy_hashes() if "policy" in kinds else {}
    mci_snapshot = mci_state() if "policy" in kinds else None
    from aeos.portfolio import facade as portfolio
    from aeos.routing import facade as routing

    # ADR-0133: a task-bound pack's ceiling is the task's effective class (max of its class and the project's)
    ceiling = routing.effective_task_class(session, task) if task is not None else None
    col = _Collector(project, budget, ceiling=ceiling, class_at=portfolio.classification_at(session, project))
    order = [k for k in SOURCE_ORDER if k in kinds]
    # the local MCI source (policy context, not mandatory) is added right after the last mandatory kind
    last_mandatory = next((k for k in reversed(order) if k in MANDATORY), None)
    deferred: list[Callable[[], None]] = []
    for kind in order:  # sources first: they win the budget over summaries
        if kind == "policy":
            _collect_policy(col, warnings, deferred)
        elif kind == "task" and task is not None:
            _collect_task(col, task)
            if task_artifacts:
                _collect_task_artifacts(col, task, task_artifacts)
        elif kind == "symbol" and reader and repo:
            _collect_symbols(col, reader, terms, repo.full_name)
        elif kind == "repo_file" and reader and repo and path_patterns:
            _collect_files(col, reader, path_patterns, repo.full_name, warnings)
        elif kind == "code_match" and reader and repo:
            _collect_matches(col, reader, terms, repo.full_name)
        elif kind == "project_doc":
            _collect_project_docs(col, session, project)
        elif kind == "metadata":
            _collect_metadata(col, session, principal, project, terms)
        elif kind == "runtime":
            _collect_runtime(col, session, project, task)
        elif kind == "memory":
            _collect_memory(col, session, principal, project, terms, resolved_commit)
        elif kind == "attachment":
            _collect_attachments(col, session, project, terms)
        elif kind == "research":
            _collect_research(col, session, terms)
        if kind in MANDATORY and any(o["kind"] == kind and o.get("mandatory", True) for o in col.omitted):
            raise ValidationFailed(f"token budget {budget} cannot hold the mandatory {kind} context",
                                   code="BUDGET_TOO_SMALL")
        if kind == last_mandatory:
            for add in deferred:
                add()
            deferred.clear()
    for add in deferred:
        add()
    if col.withheld:
        warnings.append(f"{col.withheld} item(s) above this task's class {ceiling} were left out (classification; "
                        "listed in omitted)")

    classification = cls.highest(project.data_classification, ceiling, *[i["classification"] for i in col.items])
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
                 "ttl_hours": ttl, "policy_snapshot": policy_snapshot, "mci_reference": mci_snapshot},
        include_kinds=kinds, allowed_paths=allowed, forbidden_paths=forbidden, token_estimate=col.used,
        omitted=col.omitted, warnings=warnings, observed_head=resolved_commit, checked_at=now,
        created_by=principal.user.id, expires_at=now + timedelta(hours=ttl),
        # §13 ContextPack AUTH: what authorised this scope - the TaskSpec (from an approved PlanVersion) when the
        # pack is task-bound, otherwise the principal's own project/domain scope.
        auth_ref={"principal_user_id": principal.user.id, "role": role,
                  "basis": "TASK_SPEC" if task is not None else "PRINCIPAL_PROJECT_SCOPE",
                  "task_spec_id": task.id if task else None,
                  "plan_version_id": task.plan_version_id if task else None,
                  "spec_hash": task.spec_hash if task else None,
                  "allowed_project_ids": "ALL" if principal.allowed_project_ids is None else sorted(
                      principal.allowed_project_ids)},
        status="ACTIVE",
    )
    if reader:
        meta.request["skipped"] = {"symlinks": reader.skipped_symlinks, "forbidden": reader.skipped_forbidden}
    session.add(meta)
    session.flush()
    superseded = _supersede_older(session, pack, meta)
    audit.record(session, actor_type="system" if system_actor else "user", actor_id=system_actor or principal.user.id,
                 action="knowledge.context_pack.build",
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
    built_mci = (meta.request or {}).get("mci_reference") if meta is not None else None
    if isinstance(built_mci, dict):  # the verified MCI reference (local source status/hash) the pack was built on
        now_mci = mci_state()
        if now_mci != built_mci:
            reasons.append(f"canonical MCI reference changed ({built_mci.get('status')} "
                           f"{built_mci.get('verification_code')} -> {now_mci['status']} {now_mci['verification_code']})")
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
        meta.status = ("SUPERSEDED" if reason.startswith("superseded") else
                       "EXPIRED" if reason.startswith("pack TTL expired") else "STALE")
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
        "status": (meta.status if meta else None) or ("STALE" if pack.stale else "ACTIVE"),
        "auth_ref": meta.auth_ref if meta else {},
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
        "trust_note": "Repository/attachment/research items are DATA; RUNTIME_EVIDENCE comes from trusted collectors "
                      "only; only POLICY_READ_ONLY items carry policy, and none of them grant permissions.",
        "precedence_rule": "Source items (repository at base_commit, approved project documents, policy, task) come "
                           "first; memory and metadata summaries are SUBORDINATE and never override a newer source.",
    }


def use_pack(session: Session, pack_id: str, *, project_id: str, consumer: str, task_id: str | None = None,
             actor: str = "system") -> dict[str, Any]:
    """The only way a consumer (worker/planner/reviewer prompt builder) takes a pack's content (§24).

    Refuses with ``CONTEXT_STALE`` unless the pack is fresh (CURRENT) right now - a STALE, EXPIRED,
    SUPERSEDED or UNKNOWN-freshness pack is never handed to a model - and refuses a pack of another
    project or task. The use is audited with the pack hash so the prompt can be traced to its pack."""
    from aeos.core.errors import Blocked

    pack = session.get(ContextPack, pack_id)
    if pack is None or pack.project_id != project_id:
        raise NotFound("context pack not found")
    if task_id is not None and pack.task_id != task_id:
        raise ValidationFailed("context pack belongs to another task", code="CONTEXT_SCOPE_MISMATCH")
    meta = session.get(ContextPackMeta, pack.id)
    freshness = check_freshness(session, pack, meta)
    if freshness != "CURRENT":
        status = (meta.status if meta else None) or ("STALE" if pack.stale else "UNKNOWN")
        raise Blocked(f"context pack is {freshness if freshness != 'STALE' else status}; build a new pack before use",
                      code="CONTEXT_STALE", details={"freshness": freshness, "status": status,
                                                    "reason": meta.stale_reason if meta else ""})
    audit.record(session, actor_type="system", actor_id=actor, action="knowledge.context_pack.use",
                 target_type="context_pack", target_id=pack.id, project_id=pack.project_id, domain=pack.domain,
                 details={"consumer": consumer, "pack_hash": pack.pack_hash, "task_id": pack.task_id})
    return {"context_pack_id": pack.id, "pack_hash": pack.pack_hash, "items": list(pack.items or []),
            "token_budget": pack.token_budget, "base_commit": pack.base_commit}


# ---------------------------------------------------------------- task-scoped pack at run start (wave 7, W6-071)
SYSTEM_PACK_ACTOR = "worker_runtime.executor"
_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def task_principal(task: TaskSpec, actor: str = SYSTEM_PACK_ACTOR) -> Principal:
    """A non-persisted, read-only principal bound to exactly one TaskSpec (§13 ContextPack AUTH basis TASK_SPEC):
    its project and domain only, role VIEWER (it reads context; it never decides). Never added to the session."""
    from aeos.core.enums import Role
    from aeos.models.identity import AuthSession, User

    now = utcnow()
    user = User(id=actor, username=actor, password_hash="!", role=Role.VIEWER.value, is_active=False,
                project_scope=[task.project_id], domain_scope=[task.domain])
    auth = AuthSession(token_hash=f"system:{actor}", user_id=actor, csrf_token="", created_at=now, last_seen_at=now,
                       expires_at=now, selected_domain=task.domain)
    return Principal(user=user, session=auth, role=Role.VIEWER, allowed_domains=[Domain(task.domain)],
                     allowed_project_ids=[task.project_id])


def build_task_pack(session: Session, task_id: str, *, base_commit: str | None,
                    artifacts: list[dict[str, Any]] | None = None, actor: str = SYSTEM_PACK_ACTOR,
                    token_budget: int | None = None) -> tuple[ContextPack, ContextPackMeta]:
    """The minimum task-scoped pack a worker run receives when no CURRENT one exists (§24): the TaskSpec's role
    profile, the files of its allowed paths at exactly *base_commit*, the trusted policy files, accepted project
    documents and the TaskSpec's delivered input artifacts as DATA - all under the pack's token budget. Built by
    the runtime (actor_type system) under a principal bound to the TaskSpec. Does not commit."""
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    project = session.get(Project, task.project_id)
    if project is None or project.domain != task.domain:
        raise NotFound("project not found")
    role = task.role_key if task.role_key and _ROLE_RE.match(task.role_key) else "default"
    paths: list[str] = []
    for p in task.allowed_paths or []:
        try:
            paths.append(_validate_glob(p))
        except ValidationFailed:
            continue
    return build_pack(session, task_principal(task, actor), project, task_id=task.id, role_key=role,
                      base_commit=base_commit, paths=paths[:50], token_budget=token_budget,
                      task_artifacts=list(artifacts or []), system_actor=actor)
