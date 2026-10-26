"""Contract references for the §29 diagram checks and the §27 08. runbooks, read from the project's own records and
its repository at a recorded commit (master §16 Diagram/Visual Author "diagrams checked against contracts", §29
"Sumber / verifikasi", §27 08. Runbooks).

* Platform project (DLY AEOS documents itself): the SQLAlchemy metadata (tables, columns, foreign keys), the
  InstallManifest inventory (components and their DESIGN relations), the route contract
  ``contracts/api/routes.json`` and, from the platform repository at its HEAD commit (git object database, commit
  recorded): CI workflow jobs, deploy files and the reviewed ``docs/runbooks/*.md``.
* Product project: its registered local checkout, after the same domain checks every content read uses
  (``portfolio.facade.project_checkout``), at a KNOWN commit: the merge commit of the newest merged
  pull request of that repository, else the commit of its last recorded read-only inventory. From that commit only:
  OpenAPI files (routes), ``schema.prisma`` / SQL / Alembic migrations (tables, columns, foreign keys), compose /
  package / chart / Kubernetes names (components, deploy references), CI files (jobs, stages, triggers) and deploy
  files (IaC kinds), the plan's ``interface_contracts[].contract_paths`` and ``docs/runbooks/*.md``.
* No checkout, no known commit or a refused domain check: nothing is read and every check stays UNVERIFIED with the
  reason (fail closed, never a guessed path or the working tree of a product).

Read-only by construction: ``git ls-tree`` and ``git cat-file blob`` at the commit (``aeos.portfolio.facade``),
bounded file counts and sizes, sensitive file names (``.env``, keys, ``secrets.yaml`` ...) never read, nothing
executed, no network. Parsed facts per (checkout, commit, contract paths) are cached in process: a commit's content
is immutable.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import AeosError
from aeos.core.redaction import redact_text
from aeos.docs_engine import renderer as r
from aeos.docs_engine.visuals import VisualReferences
from aeos.models.portfolio import Project

log = logging.getLogger("aeos.docs_engine.references")

REFERENCE_KEYS = ("components", "relations", "routes", "tables", "ci_jobs", "deploy_refs")
MAX_FILES_PER_KIND = 40
MAX_BLOB_BYTES = 256 * 1024
MAX_TREE_ENTRIES = 100_000
_CACHE_MAX = 32
_FACTS_CACHE: dict[tuple[str, str, tuple[str, ...]], CheckoutFacts] = {}


@dataclass
class ProjectReferences:
    visual: VisualReferences
    runbooks: list[r.RunbookSource] = field(default_factory=list)
    commit: str | None = None
    source: str = ""


@dataclass
class CheckoutFacts:
    """What one commit of a repository states (pure data; parsed once per commit)."""

    commit: str
    routes: set[str] = field(default_factory=set)
    route_files: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    columns: dict[str, set[str]] = field(default_factory=dict)
    foreign_keys: set[tuple[str, str]] = field(default_factory=set)
    table_aliases: dict[str, str] = field(default_factory=dict)
    schema_files: list[str] = field(default_factory=list)
    components: set[str] = field(default_factory=set)
    component_files: list[str] = field(default_factory=list)
    ci_jobs: set[str] = field(default_factory=set)
    ci_files: list[str] = field(default_factory=list)
    deploy_refs: set[str] = field(default_factory=set)
    deploy_files: list[str] = field(default_factory=list)
    runbooks: list[r.RunbookSource] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)  # reference -> why it could not be derived
    truncated: bool = False


# ------------------------------------------------------------------------------------------------ parsers (pure)

def _unquote(name: str) -> str:
    n = name.strip().strip('"`[]').strip()
    return n.rsplit(".", 1)[-1].strip('"`[]') if "." in n else n


def openapi_paths(text: str, filename: str = "") -> set[str]:
    """Paths of an OpenAPI 3 / Swagger 2 document (JSON or YAML), also under the base path its servers name."""
    try:
        doc = json.loads(text) if filename.lower().endswith(".json") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return set()
    if not isinstance(doc, dict) or not ({"openapi", "swagger"} & set(doc)) or not isinstance(doc.get("paths"), dict):
        return set()
    paths = {str(p) for p in doc["paths"] if str(p).startswith("/")}
    bases: set[str] = set()
    if isinstance(doc.get("basePath"), str):
        bases.add(doc["basePath"])
    for srv in doc.get("servers") or []:
        url = str(srv.get("url") or "") if isinstance(srv, dict) else ""
        m = re.match(r"^(?:[a-z][a-z0-9+.-]*://[^/]+)?(/[^?#]*)?", url, re.I)
        if m and m.group(1):
            bases.add(m.group(1))
    out = set(paths)
    for base in bases:
        b = "/" + base.strip("/")
        if b != "/":
            out |= {b + p for p in paths}
    return out


_PRISMA_BLOCK = re.compile(r"^[ \t]*(model|view|enum|type)\s+(\w+)\s*\{(.*?)^[ \t]*\}", re.S | re.M)


def parse_prisma(text: str) -> tuple[set[str], dict[str, set[str]], set[tuple[str, str]], dict[str, str]]:
    """(tables, columns, foreign keys, aliases) of a ``schema.prisma``: models (and their ``@@map`` table names),
    scalar fields (and ``@map`` column names), ``@relation(fields: ...)`` links and implicit many-to-many tables."""
    text = re.sub(r"//[^\n]*", "", text)
    blocks = [(m.group(1), m.group(2), m.group(3)) for m in _PRISMA_BLOCK.finditer(text)]
    models = {name: body for kind, name, body in blocks if kind in ("model", "view")}
    table_of = {}
    for name, body in models.items():
        mapped = re.search(r'@@map\(\s*(?:name:\s*)?"([^"]+)"', body)
        table_of[name] = mapped.group(1) if mapped else name
    tables = set(table_of.values())
    aliases = {name: t for name, t in table_of.items() if name != t}
    columns: dict[str, set[str]] = {}
    fks: set[tuple[str, str]] = set()
    lists: dict[str, set[str]] = {}
    for name, body in models.items():
        cols: set[str] = set()
        for raw in body.splitlines():
            line = raw.strip()
            m = re.match(r"^(\w+)\s+(\w+)(\[\])?(\?)?(.*)$", line)
            if not m or line.startswith("@@"):
                continue
            field_name, typ, is_list, _opt, rest = m.groups()
            if typ in models:
                if "@relation" in rest and "fields" in rest:
                    fks.add((table_of[name], table_of[typ]))
                elif is_list:
                    lists.setdefault(name, set()).add(typ)
                continue
            cols.add(field_name)
            mapped = re.search(r'@map\(\s*(?:name:\s*)?"([^"]+)"', rest)
            if mapped:
                cols.add(mapped.group(1))
        columns[table_of[name]] = cols
    for a, targets in lists.items():
        for b in targets:
            if a in lists.get(b, set()):  # implicit m-n: Prisma keeps a junction table "_AToB"
                junction = "_" + "To".join(sorted((a, b)))
                fks |= {(junction, table_of[a]), (junction, table_of[b])}
    return tables, columns, fks, aliases


def _split_top(body: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


_SQL_NAME = r"((?:[\"`\[]?[\w$]+[\"`\]]?\.)?[\"`\[]?[\w$]+[\"`\]]?)"
_SQL_CREATE = re.compile(rf"^create\s+(?:(?:global\s+|local\s+)?temp(?:orary)?\s+|unlogged\s+)?table\s+"
                         rf"(?:if\s+not\s+exists\s+)?{_SQL_NAME}\s*\((.*)\)[^)]*$", re.I | re.S)
_SQL_ALTER = re.compile(rf"^alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?{_SQL_NAME}\s+(.*)$", re.I | re.S)
_SQL_DROP = re.compile(rf"^drop\s+table\s+(?:if\s+exists\s+)?{_SQL_NAME}", re.I)
_SQL_REF = re.compile(rf"\breferences\s+{_SQL_NAME}", re.I)
_SQL_CONSTRAINT = ("constraint", "primary", "unique", "check", "key", "index", "foreign", "exclude", "fulltext")


def parse_sql(texts: Iterable[str]) -> tuple[set[str], dict[str, set[str]], set[tuple[str, str]]]:
    """Tables, columns and foreign keys after applying SQL migrations in order (``CREATE TABLE``, ``ALTER TABLE ...
    ADD/DROP/RENAME``, ``DROP TABLE``). Other statements are ignored."""
    cols: dict[str, set[str]] = {}
    fks: set[tuple[str, str]] = set()
    for text in texts:
        clean = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        clean = re.sub(r"--[^\n]*", " ", clean)
        for stmt in (s.strip() for s in clean.split(";")):
            if not stmt:
                continue
            m = _SQL_CREATE.match(stmt)
            if m:
                table = _unquote(m.group(1))
                cols[table] = set()
                for item in _split_top(m.group(2)):
                    low = item.lower()
                    ref = _SQL_REF.search(item)
                    if ref:
                        fks.add((table, _unquote(ref.group(1))))
                    if not low.startswith(_SQL_CONSTRAINT):
                        cols[table].add(_unquote(item.split()[0]))
                continue
            m = _SQL_ALTER.match(stmt)
            if m:
                table = _unquote(m.group(1))
                for action in _split_top(m.group(2)):
                    low = action.lower()
                    ren = re.match(r"rename\s+to\s+(\S+)", action, re.I)
                    if ren:
                        new = _unquote(ren.group(1))
                        cols[new] = cols.pop(table, set())
                        fks = {(new if a == table else a, new if b == table else b) for a, b in fks}
                        table = new
                        continue
                    ren = re.match(r"rename\s+(?:column\s+)?(\S+)\s+to\s+(\S+)", action, re.I)
                    if ren:
                        cols.setdefault(table, set()).discard(_unquote(ren.group(1)))
                        cols[table].add(_unquote(ren.group(2)))
                        continue
                    ref = _SQL_REF.search(action)
                    if ref and low.startswith("add"):
                        fks.add((table, _unquote(ref.group(1))))
                    add = re.match(r"add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?(\S+)", action, re.I)
                    if add and add.group(1).lower() not in _SQL_CONSTRAINT:
                        cols.setdefault(table, set()).add(_unquote(add.group(1)))
                    drop = re.match(r"drop\s+(?:column\s+)?(?:if\s+exists\s+)?(\S+)", action, re.I)
                    if drop and drop.group(1).lower() not in ("constraint", "index", "primary", "foreign"):
                        cols.setdefault(table, set()).discard(_unquote(drop.group(1)))
                continue
            m = _SQL_DROP.match(stmt)
            if m:
                table = _unquote(m.group(1))
                cols.pop(table, None)
                fks = {(a, b) for a, b in fks if table not in (a, b)}
    return set(cols), cols, fks


def _call_args(text: str, start: int) -> str:
    """The argument text of the call whose ``(`` is at *start* (balanced parentheses, strings skipped)."""
    depth, i, quote = 0, start, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return text[start + 1:]


_ALEMBIC_REV = re.compile(r"^revision\s*(?::[^=]+)?=\s*['\"]([^'\"]+)['\"]", re.M)
_ALEMBIC_DOWN = re.compile(r"^down_revision\s*(?::[^=]+)?=\s*(.+)$", re.M)
_PY_COLUMN = re.compile(r"\bColumn\(\s*['\"](\w+)['\"]")
_PY_FK = re.compile(r"\bForeignKey\(\s*['\"](\w+)\.\w+['\"]")
_PY_FKC = re.compile(r"\bForeignKeyConstraint\(\s*\[[^\]]*\]\s*,\s*\[\s*['\"](\w+)\.")
_ALEMBIC_OPS = re.compile(r"\b(op|batch_op)\.(create_table|add_column|drop_table|drop_column|rename_table|"
                          r"create_foreign_key|alter_column)\s*\(|\bop\.batch_alter_table\(\s*['\"](\w+)['\"]")


def _alembic_order(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    revs: dict[str, tuple[str, str]] = {}
    down: dict[str, set[str]] = {}
    for path, text in files:
        m = _ALEMBIC_REV.search(text)
        if not m:
            return sorted(files)
        revs[m.group(1)] = (path, text)
        d = _ALEMBIC_DOWN.search(text)
        down[m.group(1)] = set(re.findall(r"['\"]([^'\"]+)['\"]", d.group(1))) if d else set()
    out: list[tuple[str, str]] = []
    done: set[str] = set()
    pending = sorted(revs)
    while pending:
        ready = [x for x in pending if not (down[x] & set(revs)) - done]
        if not ready:  # cycle / missing parent: keep path order for the rest
            out += sorted(revs[x] for x in pending)
            break
        for x in ready:
            out.append(revs[x])
            done.add(x)
        pending = [x for x in pending if x not in done]
    return out


def parse_alembic(files: list[tuple[str, str]]) -> tuple[set[str], dict[str, set[str]], set[tuple[str, str]]]:
    """Tables/columns/foreign keys after the ``upgrade()`` steps of Alembic migrations in revision order
    (``op.create_table``/``add_column``/``drop_table``/``drop_column``/``rename_table``/``create_foreign_key`` and the
    ``batch_alter_table`` forms)."""
    cols: dict[str, set[str]] = {}
    fks: set[tuple[str, str]] = set()
    for _path, text in _alembic_order(files):
        up = re.search(r"^def upgrade\(.*?(?=^def downgrade\(|\Z)", text, re.S | re.M)
        body = up.group(0) if up else ""
        batch: str | None = None
        for m in _ALEMBIC_OPS.finditer(body):
            if m.group(3):
                batch = m.group(3)
                continue
            who, op = m.group(1), m.group(2)
            args = _call_args(body, m.end() - 1)
            names = re.findall(r"^\s*['\"](\w+)['\"]", args)
            strs = re.findall(r"['\"](\w+)['\"]", args.split("sa.", 1)[0] if op == "add_column" else args)
            table = batch if who == "batch_op" else (names[0] if names else None)
            if op == "create_table" and table:
                cols[table] = set(_PY_COLUMN.findall(args))
                fks |= {(table, t) for t in _PY_FK.findall(args) + _PY_FKC.findall(args)}
            elif op == "add_column" and table:
                cols.setdefault(table, set()).update(_PY_COLUMN.findall(args))
                fks |= {(table, t) for t in _PY_FK.findall(args)}
            elif op == "drop_table" and table:
                cols.pop(table, None)
                fks = {(a, b) for a, b in fks if table not in (a, b)}
            elif op == "drop_column" and table:
                gone = strs[0] if who == "batch_op" else (strs[1] if len(strs) > 1 else None)
                if gone:
                    cols.setdefault(table, set()).discard(gone)
            elif op == "rename_table" and len(strs) >= 2:
                cols[strs[1]] = cols.pop(strs[0], set())
                fks = {(strs[1] if a == strs[0] else a, strs[1] if b == strs[0] else b) for a, b in fks}
            elif op == "create_foreign_key":
                # op.create_foreign_key(name, source, referent, ...) / batch_op.create_foreign_key(name, referent, ...)
                if re.match(r"\s*None\b", args):  # an unnamed constraint: the name slot holds no string
                    strs = ["", *strs]
                if who == "batch_op" and batch and len(strs) >= 2:
                    fks.add((batch, strs[1]))
                elif who == "op" and len(strs) >= 3:
                    fks.add((strs[1], strs[2]))
    return set(cols), cols, fks


_GH_RESERVED = {"stages", "variables", "include", "default", "workflow", "image", "services", "before_script",
                "after_script", "cache", "pages"}


def _clean_job_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\$\{\{.*?\}\}|\[[^\]]*\]|\([^)]*\)", " ", name)).strip()


def _env_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return {value["name"]}
    return set()


def ci_names(text: str, path: str) -> set[str]:
    """Job ids/names, stages, triggers and job environments of one CI file (GitHub Actions, GitLab CI, CircleCI,
    Azure Pipelines, Bitbucket Pipelines, Jenkinsfile)."""
    out: set[str] = set()
    if PurePosixPath(path).name == "Jenkinsfile":
        return {m for m in re.findall(r"\bstage\s*\(\s*['\"]([^'\"]+)['\"]", text)}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return out
    if not isinstance(doc, dict):
        return out
    if path.startswith(".github/workflows/"):
        if isinstance(doc.get("name"), str):
            out.add(doc["name"])
        on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on:` key as True
        out |= {str(k) for k in (on if isinstance(on, dict) else on if isinstance(on, list) else [on]) if k}
        for jid, job in (doc.get("jobs") or {}).items():
            out.add(str(jid))
            if isinstance(job, dict):
                if isinstance(job.get("name"), str) and _clean_job_name(job["name"]):
                    out.add(_clean_job_name(job["name"]))
                out |= _env_names(job.get("environment"))
        return out
    if PurePosixPath(path).name.startswith(".gitlab-ci"):
        out |= {str(s) for s in doc.get("stages") or []}
        for key, job in doc.items():
            if isinstance(key, str) and key not in _GH_RESERVED and not key.startswith(".") and isinstance(job, dict):
                out.add(key)
                if isinstance(job.get("stage"), str):
                    out.add(job["stage"])
                out |= _env_names(job.get("environment"))
        return out
    if isinstance(doc.get("jobs"), dict):  # CircleCI
        out |= {str(k) for k in doc["jobs"]}
        out |= {str(k) for k in (doc.get("workflows") or {}) if k != "version"}
    for stage in doc.get("stages") or []:  # Azure Pipelines
        if isinstance(stage, dict):
            out |= {str(stage[k]) for k in ("stage", "displayName") if isinstance(stage.get(k), str)}
            for job in stage.get("jobs") or []:
                if isinstance(job, dict):
                    out |= {str(job[k]) for k in ("job", "deployment", "displayName") if isinstance(job.get(k), str)}
                    out |= _env_names(job.get("environment"))
    pipelines = doc.get("pipelines")
    if isinstance(pipelines, dict):  # Bitbucket Pipelines: step names
        out |= {m for m in re.findall(r"^\s*name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.M)}
        out |= {m for m in re.findall(r"^\s*deployment:\s*(\S+)\s*$", text, re.M)}
    return out


def _image_name(image: str) -> str:
    base = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return base.split(":", 1)[0]


def compose_names(text: str) -> set[str]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return set()
    services = doc.get("services") if isinstance(doc, dict) else None
    out: set[str] = set()
    for name, svc in (services or {}).items() if isinstance(services, dict) else []:
        out.add(str(name))
        if isinstance(svc, dict):
            if isinstance(svc.get("container_name"), str):
                out.add(svc["container_name"])
            if isinstance(svc.get("image"), str):
                out.add(_image_name(svc["image"]))
    return out


_K8S_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Service", "CronJob", "Job", "Ingress", "Pod"}


def manifest_names(path: str, text: str) -> set[str]:
    """Component names stated by a package/chart/Kubernetes manifest at its path."""
    name = PurePosixPath(path).name
    out: set[str] = set()
    try:
        if name == "package.json":
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                out |= {data["name"], data["name"].rsplit("/", 1)[-1]}
        elif name in ("pyproject.toml", "Cargo.toml"):
            data = tomllib.loads(text)
            for section in (data.get("project"), (data.get("tool") or {}).get("poetry"), data.get("package")):
                if isinstance(section, dict) and isinstance(section.get("name"), str):
                    out.add(section["name"])
        elif name == "go.mod":
            m = re.search(r"^module\s+(\S+)", text, re.M)
            if m:
                out |= {m.group(1), m.group(1).rstrip("/").rsplit("/", 1)[-1]}
        elif name == "Chart.yaml":
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                out.add(data["name"])
        elif name.endswith((".yaml", ".yml")):
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict) and doc.get("kind") in _K8S_KINDS:
                    meta = doc.get("metadata")
                    if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                        out.add(meta["name"])
        elif name.endswith(".tf"):
            out |= {f"{t}.{n}" for t, n in re.findall(r'^\s*resource\s+"(\w+)"\s+"(\w+)"', text, re.M)}
            out |= {n for _t, n in re.findall(r'^\s*resource\s+"(\w+)"\s+"(\w+)"', text, re.M)}
    except (ValueError, yaml.YAMLError, tomllib.TOMLDecodeError):
        return set()
    return {x for x in out if x.strip()}


# deploy kind (portfolio inventory label) -> words a deployment diagram uses for it
DEPLOY_KIND_WORDS = {"container": ("container", "docker", "Dockerfile"), "compose": ("compose", "docker compose"),
                     "kubernetes": ("kubernetes", "k8s"), "helm": ("helm",), "terraform": ("terraform",),
                     "kustomize": ("kustomize",), "serverless": ("serverless",), "paas": ("paas",),
                     "cloud-iac": ("cloud-iac",), "ansible": ("ansible",), "skaffold": ("skaffold",)}


def parse_runbook(stem: str, path: str, text: str, commit: str | None) -> r.RunbookSource:
    """A reviewed runbook (``# Title`` + ``## Section`` headings: Trigger / Authority / Steps / Verification /
    rollback ...) as a RunbookSource; section bodies are redacted."""
    title = stem
    sections: list[tuple[str, str]] = []
    head: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("# ") and title == stem:
            title = line[2:].strip().removeprefix("Runbook:").strip()
        elif line.startswith("## "):
            if head is not None:
                sections.append((head, "\n".join(buf).strip()))
            head, buf = line[3:].strip().lower(), []
        elif head is not None:
            buf.append(line)
    if head is not None:
        sections.append((head, "\n".join(buf).strip()))
    return r.RunbookSource(key=stem, title=title, path=path, commit=commit,
                           sections=tuple((h, redact_text(b)) for h, b in sections if b))


def _is_runbook(path: str) -> bool:
    p = PurePosixPath(path)
    return (p.parent.as_posix().lower() == "docs/runbooks" and p.suffix.lower() == ".md"
            and p.stem.lower() != "readme")


# ------------------------------------------------------------------------------------------------ checkout reading

def read_checkout(top: Path, commit: str, *, extra_paths: tuple[str, ...] = (), runbooks: bool = True
                  ) -> CheckoutFacts:
    """Parse the contract files of *top* at *commit* (cached per checkout + commit + extra paths)."""
    key = (str(top), commit, extra_paths)
    hit = _FACTS_CACHE.get(key)
    if hit is not None:
        return hit
    from aeos.portfolio import facade as portfolio

    files, truncated = portfolio.git_list_files(top, commit, max_entries=MAX_TREE_ENTRIES)  # sensitive names excluded
    present = set(files)
    facts = CheckoutFacts(commit=commit, truncated=truncated)

    def read(path: str) -> str | None:
        raw = portfolio.git_read_file(top, commit, path, max_bytes=MAX_BLOB_BYTES)
        return raw.decode("utf-8", errors="replace") if raw is not None else None

    labels = {p: portfolio.classify_repo_path(p) for p in files}
    contract = {p: labels[p]["contract"] for p in files}
    extra = [p for p in dict.fromkeys(extra_paths) if p in present]
    # routes: detected OpenAPI files + the plan's contract paths that ARE OpenAPI documents
    for path in [p for p in files if contract[p] == "openapi"][:MAX_FILES_PER_KIND] + extra:
        if not path.lower().endswith((".json", ".yaml", ".yml")):
            continue
        text = read(path)
        found = openapi_paths(text or "", path)
        if found:
            facts.routes |= found
            facts.route_files.append(path)
    # schema: schema.prisma first (the canonical schema), else SQL + Alembic migrations in order
    prisma = [p for p in files if contract[p] == "prisma"] + [p for p in extra if p.endswith(".prisma")]
    migrations = [p for p in files if contract[p] == "db-migrations"] + [p for p in extra if p.endswith(".sql")]
    for path in prisma[:MAX_FILES_PER_KIND]:
        tables, cols, fks, aliases = parse_prisma(read(path) or "")
        if tables:
            facts.tables |= tables
            facts.columns.update(cols)
            facts.foreign_keys |= fks
            facts.table_aliases.update(aliases)
            facts.schema_files.append(path)
    if not facts.tables and migrations:
        sql = [(p, read(p) or "") for p in sorted(x for x in migrations if x.endswith(".sql"))[:200]]
        py = [(p, read(p) or "") for p in sorted(x for x in migrations if x.endswith(".py"))[:400]]
        py = [(p, t) for p, t in py if "op." in t]
        for parsed, used in ((parse_sql([t for _p, t in sql]), sql), (parse_alembic(py), py)):
            tables, cols, fks = parsed
            if tables:
                facts.tables |= tables
                facts.columns.update(cols)
                facts.foreign_keys |= fks
                facts.schema_files += [p for p, _t in used]
        if not facts.tables:
            facts.notes["tables"] = ("Migration ditemukan, tetapi tidak ada yang dapat dibaca (didukung: schema.prisma, "
                                     "SQL CREATE/ALTER TABLE, Alembic op.create_table/add_column); ERD tetap "
                                     "UNVERIFIED.")
    # components: compose services, package / chart / Kubernetes / Terraform names
    deploy = [(p, labels[p]["deploy"]) for p in files if labels[p]["deploy"]]
    manifests = [p for p in files if PurePosixPath(p).name in ("package.json", "pyproject.toml", "Cargo.toml",
                                                                "go.mod", "Chart.yaml")]
    for path, kind in deploy[:MAX_FILES_PER_KIND * 2]:
        facts.deploy_refs |= set(DEPLOY_KIND_WORDS.get(kind or "", (kind or "",)))
        facts.deploy_files.append(path)
        if kind == "compose" or kind in ("kubernetes", "terraform"):
            text = read(path) or ""
            names = compose_names(text) if kind == "compose" else manifest_names(path, text)
            facts.components |= names
            facts.deploy_refs |= names
            if names:
                facts.component_files.append(path)
    for path in manifests[:MAX_FILES_PER_KIND]:
        names = manifest_names(path, read(path) or "")
        if names:
            facts.components |= names
            facts.component_files.append(path)
            if PurePosixPath(path).name == "Chart.yaml":
                facts.deploy_refs |= names
    # pipeline: CI job ids/names, stages, triggers and job environments
    for path in [p for p in files if labels[p]["ci"]][:MAX_FILES_PER_KIND]:
        label = labels[path]["ci"] or ""
        names = ci_names(read(path) or "", path)
        facts.ci_files.append(path)
        facts.ci_jobs |= names | {label}
    if runbooks:
        for path in [p for p in files if _is_runbook(p)][:MAX_FILES_PER_KIND]:
            text = read(path)
            if text is not None:
                facts.runbooks.append(parse_runbook(PurePosixPath(path).stem, path, text, commit))
    if len(_FACTS_CACHE) >= _CACHE_MAX:
        _FACTS_CACHE.clear()
    _FACTS_CACHE[key] = facts
    return facts


def _contract_paths(content: Any) -> tuple[str, ...]:
    out: list[str] = []
    for c in (content or {}).get("interface_contracts") or [] if isinstance(content, dict) else []:
        if isinstance(c, dict):
            for p in c.get("contract_paths") or []:
                s = str(p).strip().replace("\\", "/")
                while s.startswith("./"):  # only a leading "./" (".github/...", ".api/..." keep their dot)
                    s = s[2:]
                # repository-relative only: never "..", an absolute path or a drive ("C:/...")
                if s and ".." not in PurePosixPath(s).parts and not s.startswith("/") and ":" not in s.split("/")[0]:
                    out.append(s)
    return tuple(sorted(set(out))[:50])


def _short(sha: str | None) -> str:
    return (sha or "")[:12]


def _apply_facts(vr: VisualReferences, facts: CheckoutFacts, where: str, *, product: bool) -> None:
    """Checkout-derived references into *vr* (an empty set = nothing found = the check stays UNVERIFIED)."""
    if facts.ci_files and facts.ci_jobs:
        vr.ci_jobs = set(facts.ci_jobs)
        vr.ci_source = f"pipeline CI {where} ({', '.join(facts.ci_files[:4])})"
    else:
        vr.missing["ci_jobs"] = f"Tidak ada file CI yang terbaca di {where}; pipeline belum dibandingkan."
    if facts.deploy_refs:
        vr.deploy_refs = set(facts.deploy_refs)
        vr.deploy_source = f"IaC/deployment {where} ({', '.join(facts.deploy_files[:4])})"
    else:
        vr.missing["deploy_refs"] = f"Tidak ada file IaC/deployment di {where}; topologi belum dibandingkan."
    if not product:
        return
    if facts.routes:
        vr.routes = set(facts.routes)
        vr.route_source = f"OpenAPI {where}: {', '.join(facts.route_files[:4])}"
    else:
        vr.missing["routes"] = f"Tidak ada dokumen OpenAPI di {where}; path API belum dibandingkan."
    if facts.tables:
        vr.tables = set(facts.tables)
        vr.columns = {t: set(c) for t, c in facts.columns.items()}
        vr.foreign_keys = set(facts.foreign_keys)
        vr.table_aliases = dict(facts.table_aliases)
        vr.schema_source = f"schema {where} ({', '.join(facts.schema_files[:3])})"
    else:
        vr.missing["tables"] = facts.notes.get("tables") or (
            f"Tidak ada schema.prisma / migration di {where}; ERD tetap PROPOSAL (UNVERIFIED).")
    if facts.components:
        vr.components = set(facts.components)
        vr.component_source = f"manifest/compose {where} ({', '.join(facts.component_files[:4])})"
        vr.missing["relations"] = "checkout tidak memuat manifest relasi komponen"
    else:
        vr.missing["components"] = (f"Tidak ada compose/package/chart manifest yang menamai komponen di {where}; "
                                    "node belum dibandingkan.")


# ------------------------------------------------------------------------------------------------ platform / product

def platform_runbooks() -> list[r.RunbookSource]:
    """The reviewed ``docs/runbooks/*.md`` of the platform repository at HEAD (commit recorded); without git, the
    working tree, labelled as not recorded."""
    from aeos.core.paths import repo_root

    try:
        from aeos.portfolio import facade as portfolio

        top = portfolio.git_toplevel(repo_root())
        sha = portfolio.git_resolve_commit(top, "HEAD")
        return list(read_checkout(top, sha).runbooks)
    except (AeosError, OSError, ValueError) as exc:
        log.info("platform runbooks read from the working tree: %s", type(exc).__name__)
    out = []
    for path in sorted((repo_root() / "docs" / "runbooks").glob("*.md")):
        if path.stem.lower() == "readme":
            continue
        try:
            out.append(parse_runbook(path.stem, f"docs/runbooks/{path.name}", path.read_text(encoding="utf-8"), None))
        except OSError:
            continue
    return out


def _platform(session: Session, project: Project) -> ProjectReferences:
    from aeos.core.db import Base
    from aeos.core.paths import repo_root
    from aeos.docs_engine import service
    from aeos.models.inventory import ComponentReference, ComponentRelation

    vr = VisualReferences(component_source="InstallManifest/inventory",
                          schema_source="schema platform (SQLAlchemy metadata)",
                          route_source="contracts/api/routes.json", route_prefix="/api/")
    vr.tables = service.known_schema_names(project)
    vr.columns = service.known_schema_columns(project)
    vr.foreign_keys = {(t.name, fk.column.table.name) for t in Base.metadata.tables.values() for fk in t.foreign_keys}
    vr.table_aliases = {m.class_.__name__: str(getattr(m.local_table, "name", "")) for m in Base.registry.mappers
                        if getattr(m.local_table, "name", None)}
    rows = list(session.execute(select(ComponentReference.component_id, ComponentReference.name,
                                       ComponentReference.friendly_name)
                                .where(ComponentReference.tombstoned_at.is_(None))))
    vr.components = {str(x) for row in rows for x in row if x} or None
    vr.component_ids = {str(x): str(row[0]) for row in rows for x in row if x}
    if vr.components is None:
        vr.missing["components"] = "InstallManifest belum dimuat (aeos seed); node belum dibandingkan."
    rels = session.execute(select(ComponentRelation.source_id, ComponentRelation.target_id).where(
        ComponentRelation.lens == "DESIGN", ComponentRelation.tombstoned_at.is_(None)))
    vr.relations = {(str(a), str(b)) for a, b in rels} or None
    if vr.relations is None:
        vr.missing["relations"] = "relasi DESIGN InstallManifest belum dimuat"
    vr.routes = service.known_route_paths(project)
    if vr.routes is None:
        vr.missing["routes"] = ("Route contract platform (contracts/api/routes.json) tidak terbaca; path belum "
                                "dibandingkan.")
    out = ProjectReferences(visual=vr)
    try:
        from aeos.portfolio import facade as portfolio

        top = portfolio.git_toplevel(repo_root())
        sha = portfolio.git_resolve_commit(top, "HEAD")
        facts = read_checkout(top, sha)
        where = f"repository platform @ {_short(sha)}"
        _apply_facts(vr, facts, where, product=False)
        out.runbooks, out.commit, vr.source, vr.commit = list(facts.runbooks), sha, where, sha
    except (AeosError, OSError, ValueError) as exc:
        reason = f"Repository platform tidak dapat dibaca pada commit tercatat ({type(exc).__name__})."
        vr.missing.setdefault("ci_jobs", reason)
        vr.missing.setdefault("deploy_refs", reason)
        out.runbooks = platform_runbooks()
    return out


def _known_commit(session: Session, project: Project, repo: Any, top: Path) -> tuple[str | None, str]:
    """The commit AEOS recorded for this checkout: the merge commit of the newest merged pull request of the
    repository, else the commit of its last read-only inventory (never the working tree)."""
    from aeos.models.delivery_ext import PullRequestRecord
    from aeos.portfolio import facade as portfolio

    candidates: list[tuple[str, str]] = []
    merged = session.scalars(select(PullRequestRecord).where(
        PullRequestRecord.project_id == project.id, PullRequestRecord.domain == project.domain,
        PullRequestRecord.repo_full_name == repo.full_name, PullRequestRecord.merged.is_(True),
        PullRequestRecord.merge_commit_sha.is_not(None))
        .order_by(PullRequestRecord.merged_at.desc(), PullRequestRecord.created_at.desc()).limit(5))
    for pr in merged:
        candidates.append((pr.merge_commit_sha or "", f"merge commit PR #{pr.number}" if pr.number else "merge commit"))
    if repo.inventory_commit:
        candidates.append((repo.inventory_commit, "commit inventaris repository"))
    for sha, how in candidates:
        try:
            return portfolio.git_resolve_commit(top, sha), how
        except AeosError:
            continue  # not present in this checkout (not fetched): try the next recorded commit
    return None, ""


def _product(session: Session, project: Project, content: Any) -> ProjectReferences:
    from aeos.portfolio import facade as portfolio

    def none(reason: str) -> ProjectReferences:
        return ProjectReferences(visual=VisualReferences(missing={k: reason for k in REFERENCE_KEYS}))

    try:
        # domain checks before any content read (fail closed: a refused checkout is never read)
        repo, path = portfolio.project_checkout(session, project, action="docs.references.read")
    except AeosError as exc:
        # the code only: a refusal names the other domain's provider host, a git error can name a local path; neither
        # belongs in this domain's documents (§37, §50)
        log.info("checkout of project %s not read for diagram references: %s", project.id, exc.code)
        return none(f"Checkout tidak dibaca ({exc.code}): pemeriksaan domain/akses checkout menolak pembacaan; "
                    "diagram tidak dibandingkan dengan kontraknya.")
    if repo is None:
        return none("Proyek ini belum punya repository terdaftar; diagram tidak dapat dibandingkan dengan kontraknya.")
    if path is None:
        return none(f"Repository {repo.full_name} belum punya checkout lokal terdaftar (Proyek > Repository); "
                    "kontraknya tidak dibaca.")
    try:
        top = portfolio.git_toplevel(path)
        sha, how = _known_commit(session, project, repo, top)
        if sha is None:
            return none(f"Belum ada commit tercatat untuk {repo.full_name} (jalankan inventaris repository atau merge "
                        "perubahan yang diterima); kontraknya tidak dibaca dari working tree.")
        facts = read_checkout(top, sha, extra_paths=_contract_paths(content))
    except AeosError as exc:
        log.info("checkout of project %s not readable at its known commit: %s", project.id, exc.code)
        return none(redact_text(f"Checkout {repo.full_name} tidak dapat dibaca pada commit tercatat ({exc.code}); "
                                "diagram tidak dibandingkan dengan kontraknya.")[:300])
    except (OSError, ValueError) as exc:
        return none(f"Checkout {repo.full_name} tidak dapat dibaca ({type(exc).__name__}).")
    identity = "" if repo.verification_status == "VERIFIED" else ", identitas repository belum VERIFIED"
    where = redact_text(f"checkout {repo.full_name} @ {_short(sha)} ({how}{identity})")
    vr = VisualReferences(source=where, commit=sha)
    _apply_facts(vr, facts, where, product=True)
    if facts.truncated:
        vr.source += "; daftar file terpotong"
    return ProjectReferences(visual=vr, runbooks=list(facts.runbooks), commit=sha, source=where)


def project_references(session: Session, project: Project, content: Any = None) -> ProjectReferences:
    """References for the diagram checks and runbooks of *project* (never raises: a failure is UNVERIFIED)."""
    try:
        return _platform(session, project) if project.is_platform else _product(session, project, content)
    except Exception as exc:  # a reference read must never break rendering; the checks stay UNVERIFIED
        log.warning("references for project %s unavailable: %s", project.id, type(exc).__name__)
        reason = f"Referensi kontrak tidak dapat dibaca ({type(exc).__name__}); diagram tetap UNVERIFIED."
        return ProjectReferences(visual=VisualReferences(missing={k: reason for k in REFERENCE_KEYS}))


def passed_integration_tests(session: Session, project: Project, keys: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Test key -> (evidence id, commit) for each wanted test that PASSED in a trusted verifier TEST_REPORT of the
    trusted suite at an ACCEPTED head of this project (content integrity-checked; worker evidence never counts)."""
    from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact
    from aeos.verification import facade as verification

    wanted = set(keys)
    if not wanted:
        return {}
    heads = {h for (h,) in session.execute(select(AcceptanceRecord.head_commit).where(
        AcceptanceRecord.project_id == project.id, AcceptanceRecord.domain == project.domain,
        AcceptanceRecord.acceptance_status == "ACCEPTED", AcceptanceRecord.head_commit.is_not(None))) if h}
    if not heads:
        return {}
    out: dict[str, tuple[str, str]] = {}
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.domain == project.domain,
        EvidenceArtifact.kind == "TEST_REPORT", EvidenceArtifact.origin == "verifier",
        EvidenceArtifact.trusted.is_(True)).order_by(EvidenceArtifact.created_at.desc()).limit(200))
    for ev in rows:
        commit = str((ev.environment or {}).get("commit") or "")
        if commit not in heads:
            continue
        data = verification.read_json(ev)  # integrity-checked against the content hash
        if not data or data.get("suite") != "trusted-acceptance":
            continue
        for case in ((data.get("report") or {}).get("cases") or []):
            key = str(case.get("key") or "") if isinstance(case, dict) else ""
            if key in wanted and case.get("outcome") == "passed":
                out.setdefault(key, (ev.id, commit))
        if wanted <= set(out):
            break
    return out
