"""Existing-project inventory (§05 step 6, §37): read-only facts about a repository at a known commit.

``inventory_repository(path)`` is the contract planning calls to fill ``PlanningContext.repo_inventory``.
It never scaffolds, installs, builds or runs tests: it reads the git object database at an exact
commit (``git ls-tree`` + small ``git cat-file blob`` reads) and reports coverage gaps explicitly instead of
claiming the whole machine or dependency graph was mapped.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from aeos.core.errors import AeosError
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.portfolio import gitread

INVENTORY_SCHEMA = "1"

LANGUAGES: dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic",
    ".c": "C", ".h": "C/C++ header", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".m": "Objective-C",
    ".mm": "Objective-C++", ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".pl": "Perl", ".lua": "Lua",
    ".r": "R", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell",
    ".clj": "Clojure", ".sql": "SQL", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".psm1": "PowerShell", ".bat": "Batch", ".cmd": "Batch", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sass": "Sass", ".less": "Less", ".vue": "Vue", ".svelte": "Svelte", ".md": "Markdown",
    ".mdx": "MDX", ".rst": "reStructuredText", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".json": "JSON",
    ".xml": "XML", ".tf": "HCL (Terraform)", ".hcl": "HCL", ".proto": "Protocol Buffers", ".graphql": "GraphQL",
    ".gql": "GraphQL", ".ipynb": "Jupyter Notebook", ".mako": "Mako", ".j2": "Jinja",
}

# manifest file name -> (ecosystem, candidate lockfile names)
MANIFESTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "package.json": ("npm", ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
                             "npm-shrinkwrap.json")),
    "pyproject.toml": ("python", ("poetry.lock", "uv.lock", "pdm.lock", "requirements.lock", "Pipfile.lock")),
    "setup.py": ("python", ()),
    "setup.cfg": ("python", ()),
    "Pipfile": ("python-pipenv", ("Pipfile.lock",)),
    "environment.yml": ("conda", ("conda-lock.yml",)),
    "go.mod": ("go", ("go.sum",)),
    "Cargo.toml": ("cargo", ("Cargo.lock",)),
    "pom.xml": ("maven", ()),
    "build.gradle": ("gradle", ("gradle.lockfile",)),
    "build.gradle.kts": ("gradle", ("gradle.lockfile",)),
    "Gemfile": ("bundler", ("Gemfile.lock",)),
    "composer.json": ("composer", ("composer.lock",)),
    "mix.exs": ("mix", ("mix.lock",)),
    "pubspec.yaml": ("pub", ("pubspec.lock",)),
    "Package.swift": ("swiftpm", ("Package.resolved",)),
    "deno.json": ("deno", ("deno.lock",)),
}
_REQUIREMENTS = re.compile(r"^requirements([-_.][\w.-]+)?\.(txt|in)$", re.IGNORECASE)
LOCKFILES = frozenset(lf for _, lfs in MANIFESTS.values() for lf in lfs) | {"packages.lock.json"}

TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "e2e", "integration_tests", "testing"})
_TEST_FILE = re.compile(
    r"(^test_.+\.py$|.+_test\.py$|.+\.(test|spec)\.(t|j)sx?$|.+_test\.go$|.+Test\.java$|.+Tests?\.cs$|.+_spec\.rb$)"
)
TEST_FRAMEWORK_FILES = {
    "pytest.ini": "pytest", "conftest.py": "pytest", "tox.ini": "tox", "noxfile.py": "nox",
    "jest.config.js": "jest", "jest.config.ts": "jest", "jest.config.mjs": "jest", "jest.config.cjs": "jest",
    "vitest.config.ts": "vitest", "vitest.config.js": "vitest", "vitest.config.mts": "vitest",
    "playwright.config.ts": "playwright", "playwright.config.js": "playwright",
    "cypress.config.ts": "cypress", "cypress.config.js": "cypress", "cypress.json": "cypress",
    "karma.conf.js": "karma", ".mocharc.json": "mocha", ".mocharc.yml": "mocha", "phpunit.xml": "phpunit",
    "phpunit.xml.dist": "phpunit", ".rspec": "rspec",
}
JS_TEST_DEPS = ("vitest", "jest", "mocha", "@playwright/test", "cypress", "ava", "jasmine", "karma",
                "@testing-library/react")

CI_PATTERNS = [
    (re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"), "GitHub Actions"),
    (re.compile(r"^\.gitlab-ci\.ya?ml$"), "GitLab CI"),
    (re.compile(r"^bitbucket-pipelines\.ya?ml$"), "Bitbucket Pipelines"),
    (re.compile(r"(^|/)Jenkinsfile$"), "Jenkins"),
    (re.compile(r"^\.circleci/config\.ya?ml$"), "CircleCI"),
    (re.compile(r"^azure-pipelines\.ya?ml$"), "Azure Pipelines"),
    (re.compile(r"^\.travis\.ya?ml$"), "Travis CI"),
    (re.compile(r"^\.buildkite/"), "Buildkite"),
    (re.compile(r"^\.drone\.ya?ml$"), "Drone"),
]
DEPLOY_PATTERNS = [
    (re.compile(r"(^|/)Dockerfile([.-][\w.-]+)?$|\.dockerfile$", re.IGNORECASE), "container"),
    (re.compile(r"(^|/)(docker-)?compose(\.[\w-]+)?\.ya?ml$"), "compose"),
    (re.compile(r"\.tf$|\.tfvars$|(^|/)terragrunt\.hcl$"), "terraform"),
    (re.compile(r"(^|/)Chart\.yaml$"), "helm"),
    (re.compile(r"(^|/)(k8s|kubernetes|manifests|deploy)/.+\.ya?ml$"), "kubernetes"),
    (re.compile(r"(^|/)kustomization\.ya?ml$"), "kustomize"),
    (re.compile(r"(^|/)serverless\.ya?ml$"), "serverless"),
    (re.compile(r"(^|/)(Procfile|app\.yaml|fly\.toml|vercel\.json|netlify\.toml|render\.yaml|railway\.json)$"), "paas"),
    (re.compile(r"(^|/)(cdk\.json|Pulumi\.ya?ml|template\.ya?ml|samconfig\.toml)$"), "cloud-iac"),
    (re.compile(r"(^|/)(playbook[\w-]*|site)\.ya?ml$|(^|/)ansible\.cfg$"), "ansible"),
    (re.compile(r"(^|/)skaffold\.ya?ml$"), "skaffold"),
]
CONTRACT_PATTERNS = [
    (re.compile(r"\.proto$"), "protobuf"),
    (re.compile(r"(^|/)(openapi|swagger)[\w.-]*\.(ya?ml|json)$", re.IGNORECASE), "openapi"),
    (re.compile(r"\.(graphql|gql)$"), "graphql"),
    (re.compile(r"\.schema\.json$|(^|/)(schemas?|contracts?)/.+\.(json|ya?ml)$"), "json-schema"),
    (re.compile(r"\.avsc$"), "avro"),
    (re.compile(r"(^|/)schema\.prisma$"), "prisma"),
    (re.compile(r"(^|/)(migrations?|alembic)/.+\.(py|sql)$"), "db-migrations"),
    (re.compile(r"(^|/)asyncapi[\w.-]*\.(ya?ml|json)$"), "asyncapi"),
]
DOC_PATTERNS = [
    (re.compile(r"^README(\.[\w]+)?$", re.IGNORECASE), "readme"),
    (re.compile(r"^(CHANGELOG|CHANGES|HISTORY)(\.[\w]+)?$", re.IGNORECASE), "changelog"),
    (re.compile(r"^(CONTRIBUTING|CODE_OF_CONDUCT|SECURITY)(\.[\w]+)?$", re.IGNORECASE), "governance"),
    (re.compile(r"^(LICENSE|LICENCE|COPYING)(\.[\w]+)?$", re.IGNORECASE), "license"),
    (re.compile(r"(^|/)(adr|adrs|decisions)/.+\.md$", re.IGNORECASE), "adr"),
    (re.compile(r"^docs?/.+\.(md|mdx|rst|txt|adoc)$", re.IGNORECASE), "docs"),
    (re.compile(r"^(AGENTS|CLAUDE)\.md$", re.IGNORECASE), "agent-instructions"),
]
SENSITIVE_NAMES = re.compile(r"(^|/)(\.env(\.[\w-]+)?|id_rsa|id_ed25519|.+\.pem|.+\.key|.+\.p12|.+\.pfx|"
                             r"credentials(\.json)?|secrets?\.(ya?ml|json))$", re.IGNORECASE)
SAMPLE_LIMIT = 50
MAX_MANIFESTS = 500
_SMALL_READ = 256 * 1024


def _match(patterns: list[tuple[re.Pattern[str], str]], path: str) -> str | None:
    for pat, label in patterns:
        if pat.search(path):
            return label
    return None


def _push(bucket: dict[str, list[str]], key: str, path: str) -> None:
    if len(bucket[key]) < SAMPLE_LIMIT:
        bucket[key].append(path)


def _js_test_frameworks(repo: Path, commit: str, package_jsons: list[str]) -> set[str]:
    found: set[str] = set()
    for pj in package_jsons[:10]:
        raw = gitread.show_blob(repo, commit, pj, max_bytes=_SMALL_READ, complete_only=True)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        deps: dict[str, Any] = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(key), dict):
                deps.update(data[key])
        found.update(d for d in JS_TEST_DEPS if d in deps)
    return found


def _pyproject_pytest(repo: Path, commit: str, paths: list[str]) -> bool:
    for p in paths[:5]:
        raw = gitread.show_blob(repo, commit, p, max_bytes=_SMALL_READ, complete_only=True)
        if raw and b"[tool.pytest" in raw:
            return True
    return False


def inventory_repository(path: Path | str, *, commit: str | None = None, max_files: int = 100_000) -> dict[str, Any]:
    """Read-only inventory of the repository at *path* at an exact commit (default: HEAD)."""
    top = gitread.toplevel(Path(path).expanduser().resolve())
    sha = gitread.resolve_commit(top, commit or "HEAD")
    try:
        head: str | None = sha if not commit else gitread.resolve_commit(top, "HEAD")
    except AeosError:  # empty repository has no HEAD commit
        head = None
    entries, truncated = gitread.ls_tree(top, sha, max_entries=max_files)
    branch = gitread.current_branch(top)
    try:
        dirty, dirty_entries = gitread.is_dirty(top)
        dirty_state: dict[str, Any] = {"dirty": dirty, "entries": dirty_entries}
    except Exception as exc:  # status can fail on exotic repos; report instead of guessing
        dirty_state = {"dirty": None, "error": exc.__class__.__name__}

    lang_files: Counter[str] = Counter()
    lang_bytes: Counter[str] = Counter()
    manifests: list[dict[str, Any]] = []
    lockfiles: list[str] = []
    test_dirs: set[str] = set()
    test_files: list[str] = []
    frameworks: set[str] = set()
    ci: dict[str, list[str]] = defaultdict(list)
    deploy: dict[str, list[str]] = defaultdict(list)
    contracts: dict[str, list[str]] = defaultdict(list)
    docs: dict[str, list[str]] = defaultdict(list)
    sensitive: list[str] = []
    symlinks: list[str] = []
    submodules: list[str] = []
    package_jsons: list[str] = []
    pyprojects: list[str] = []
    file_names_by_dir: dict[str, set[str]] = defaultdict(set)
    total_bytes = 0

    for e in entries:
        if e.is_submodule:
            submodules.append(e.path)
            continue
        if e.is_symlink:
            if len(symlinks) < SAMPLE_LIMIT:
                symlinks.append(e.path)
            continue
        if e.kind != "blob":
            continue
        p = PurePosixPath(e.path)
        name = p.name
        parent = str(p.parent) if str(p.parent) != "." else ""
        file_names_by_dir[parent].add(name)
        total_bytes += e.size or 0
        lang = LANGUAGES.get(p.suffix.lower())
        if lang:
            lang_files[lang] += 1
            lang_bytes[lang] += e.size or 0
        if name in MANIFESTS or _REQUIREMENTS.match(name) or name.endswith(".csproj"):
            eco = MANIFESTS[name][0] if name in MANIFESTS else ("python" if _REQUIREMENTS.match(name) else "nuget")
            manifests.append({"path": e.path, "ecosystem": eco})
            if name == "package.json":
                package_jsons.append(e.path)
            if name == "pyproject.toml":
                pyprojects.append(e.path)
        if name in LOCKFILES:
            lockfiles.append(e.path)
        dir_parts = p.parts[:-1]
        for idx, part in enumerate(dir_parts):
            if part in TEST_DIRS:
                test_dirs.add("/".join(dir_parts[: idx + 1]))
                break
        if _TEST_FILE.match(name):
            if len(test_files) < SAMPLE_LIMIT:
                test_files.append(e.path)
            if name.endswith(".py"):
                frameworks.add("pytest-style")
            elif name.endswith("_test.go"):
                frameworks.add("go test")
        if name in TEST_FRAMEWORK_FILES:
            frameworks.add(TEST_FRAMEWORK_FILES[name])
        label = _match(CI_PATTERNS, e.path)
        if label:
            _push(ci, label, e.path)
        label = _match(DEPLOY_PATTERNS, e.path)
        if label:
            _push(deploy, label, e.path)
        label = _match(CONTRACT_PATTERNS, e.path)
        if label:
            _push(contracts, label, e.path)
        label = _match(DOC_PATTERNS, e.path)
        if label:
            _push(docs, label, e.path)
        if SENSITIVE_NAMES.search(e.path) and len(sensitive) < SAMPLE_LIMIT:
            sensitive.append(e.path)

    frameworks |= _js_test_frameworks(top, sha, package_jsons)
    if _pyproject_pytest(top, sha, pyprojects):
        frameworks.add("pytest")
    if "pytest" in frameworks:
        frameworks.discard("pytest-style")

    # Pair each manifest with a lockfile in the same directory (missing lock = reproducibility gap).
    for m in manifests:
        mp = PurePosixPath(m["path"])
        d = str(mp.parent) if str(mp.parent) != "." else ""
        candidates = MANIFESTS.get(mp.name, ("", ()))[1]
        locks = sorted(n for n in file_names_by_dir.get(d, set()) if n in candidates)
        m["lockfiles"] = [f"{d}/{n}" if d else n for n in locks]
        m["lockfile_status"] = "PRESENT" if locks else ("NOT_APPLICABLE" if not candidates else "MISSING")

    languages = [
        {"language": lang, "files": n, "bytes": lang_bytes[lang]} for lang, n in lang_files.most_common()
    ]
    gaps = [
        "Dependency contents/SBOM and transitive packages not resolved (manifest/lockfile presence only).",
        "Tests were not executed; baseline results come only from the trusted verifier.",
        "Untracked or uncommitted working-tree files are not part of this commit inventory.",
    ]
    if truncated:
        gaps.insert(0, f"Tree listing truncated at {max_files} entries.")
    if submodules:
        gaps.append("Submodule contents are not inventoried.")
    if len(manifests) > MAX_MANIFESTS:
        gaps.append(f"{len(manifests)} dependency manifests found; only the first {MAX_MANIFESTS} are listed.")
        manifests = manifests[:MAX_MANIFESTS]
    result: dict[str, Any] = {
        "schema": INVENTORY_SCHEMA,
        "source": f"git ls-tree {sha}",
        "commit": sha,
        # The checkout's HEAD when the inventory ran; differs from "commit" for a pinned older commit.
        "head_commit": head,
        "commit_is_head": head == sha,
        "branch": branch,
        "working_tree": dirty_state,
        "checked_at": utcnow().isoformat(),
        "files": sum(1 for e in entries if e.kind == "blob" and not e.is_symlink),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "languages": languages,
        "dependency_manifests": manifests,
        "lockfiles": sorted(lockfiles)[:SAMPLE_LIMIT],
        "tests": {
            "directories": sorted(test_dirs)[:SAMPLE_LIMIT],
            "files_sample": test_files,
            "frameworks": sorted(frameworks),
            "baseline": {"status": "NOT_RUN", "reason": "inventory is read-only; tests run only in the trusted verifier"},
        },
        "ci": {k: v for k, v in sorted(ci.items())},
        "deployment": {k: v for k, v in sorted(deploy.items())},
        "contracts": {k: v for k, v in sorted(contracts.items())},
        "docs": {k: v for k, v in sorted(docs.items())},
        "symlinks": symlinks,
        "submodules": submodules[:SAMPLE_LIMIT],
        "sensitive_paths": sensitive,
        "coverage_gaps": gaps,
        "scaffolded": False,
        "evidence_level": "E1",
    }
    volatile = {"checked_at", "working_tree", "head_commit", "commit_is_head", "branch"}
    result["inventory_hash"] = content_hash({k: v for k, v in result.items() if k not in volatile})
    return result
