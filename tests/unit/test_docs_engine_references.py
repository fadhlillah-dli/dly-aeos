"""§16 / §29 diagrams checked against the product's OWN contracts, read from its registered checkout at a known
commit (git object database, never the working tree), and the platform's checks extended to relations, foreign
keys, CI jobs and integration-test evidence. Everything goes through ``service.render_version`` here, so the
wiring itself is protected (W6-047, W6-094..W6-098)."""

from __future__ import annotations

import copy
import json
import re

import pytest

from aeos.docs_engine import references as refs_mod
from aeos.docs_engine import service
from aeos.docs_engine import visuals as vis
from aeos.docs_engine.plan_content import DiagramView
from aeos.models.portfolio import RepositoryReference
from tests.unit.test_portfolio_support import commit_file, git, make_repo, needs_git
from tests.unit.test_publisher_support import install_fakes, make_plan, make_project
from tests.unit.test_publisher_support import schema_sample_content as sample

TASKS = [{"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]}]

OPENAPI = """openapi: 3.0.3
info: {title: Contoh, version: "1"}
servers:
  - url: https://api.contoh.example/v1
paths:
  /tasks:
    get: {responses: {"200": {description: ok}}}
  /tasks/{taskId}:
    patch: {responses: {"200": {description: ok}}}
"""
PRISMA = """// schema
model User {
  id    Int    @id @default(autoincrement())
  email String @unique
  tasks Task[]
  @@map("users")
}

model Task {
  id      Int    @id
  title   String
  ownerId Int    @map("owner_id")
  owner   User   @relation(fields: [ownerId], references: [id])
  tags    Tag[]
}

model Tag {
  id    Int    @id
  label String
  tasks Task[]
}
"""
COMPOSE = """services:
  web:
    image: ghcr.io/contoh/web:1.2
  api:
    build: ./api
    depends_on: [db]
  db:
    image: postgres:16
"""
WORKFLOW = """name: CI
on:
  push:
  pull_request:
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: ruff check .}]
  test:
    name: test [G1] (${{ matrix.os }})
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  deploy-staging:
    environment: staging
    runs-on: ubuntu-latest
    steps: [{run: ./deploy.sh}]
"""
RUNBOOK = """# Runbook: Restart contoh-api

## Trigger
API tidak menjawab /health.

## Prerequisites
Akses SSH ke host.

## Steps
1. `systemctl restart contoh-api`

## Verification
`curl /health` mengembalikan 200.

## Rollback
Kembalikan rilis sebelumnya.
"""


def _diagram(key: str, kind: str, source: str, verification_source: str = "") -> dict:
    return {"key": key, "title": key, "kind": kind, "mermaid": source, "verification_source": verification_source}


PRODUCT_DIAGRAMS = [
    _diagram("comp-ok", "component", "flowchart LR\n  w[web] --> a[api] --> d[(db)]"),
    _diagram("comp-bad", "component", "flowchart LR\n  w[web] --> k[Kafka cluster]"),
    _diagram("seq-ok", "sequence", "sequenceDiagram\n  web->>api: GET /v1/tasks\n  web->>api: PATCH /tasks/7"),
    _diagram("seq-bad", "sequence", "sequenceDiagram\n  web->>api: DELETE /v1/projects/1"),
    _diagram("erd-ok", "erd", "erDiagram\n  users ||--o{ Task : owns\n  Task }o--o{ Tag : tagged\n"
                              "  Task {\n    int id\n    string title\n  }"),
    _diagram("erd-bad", "erd", "erDiagram\n  users ||--o{ Tag : owns\n  users {\n    string budget\n  }"),
    _diagram("cicd-ok", "cicd", "flowchart LR\n  push --> lint --> t[test] --> deploy-staging --> staging"),
    _diagram("cicd-bad", "cicd", "flowchart LR\n  lint --> s[sonarqube scan]"),
    _diagram("deploy-ok", "deployment", "flowchart LR\n  c[docker compose] --> api --> db"),
    _diagram("deploy-bad", "deployment", "flowchart LR\n  api --> l[Lambda]"),
]


@pytest.fixture
def product(settings, db, monkeypatch, tmp_path):
    install_fakes(monkeypatch)
    repo = make_repo(tmp_path / "contoh", {
        "api/openapi.yaml": OPENAPI, "prisma/schema.prisma": PRISMA, "docker-compose.yml": COMPOSE,
        ".github/workflows/ci.yml": WORKFLOW, "docs/runbooks/restart.md": RUNBOOK, ".env": "SECRET=x",
        "package.json": '{"name": "@contoh/web"}'})
    sha = git(repo, "rev-parse", "HEAD")
    project = make_project(db)
    db.add(RepositoryReference(project_id=project.id, provider="GITHUB", full_name="owner/contoh-app",
                               url="https://github.com/owner/contoh-app", local_path_ref=str(repo),
                               inventory_commit=sha))
    db.commit()
    content = sample()
    content["architecture"]["diagrams"] = copy.deepcopy(PRODUCT_DIAGRAMS)
    _plan, version = make_plan(db, project, content, tasks=TASKS)
    return {"settings": settings, "db": db, "project": project, "version": version, "repo": repo, "sha": sha}


def _visuals(world) -> dict[str, vis.VisualArtifact]:
    result, _d, _s = service.render_version(world["db"], world["settings"], world["version"], persist=False,
                                            render_visuals=False)
    return {v.key: v for v in result.visuals}


@needs_git
def test_product_diagrams_are_checked_against_the_checkout_at_the_known_commit(product):
    v = _visuals(product)
    short = product["sha"][:12]
    assert v["comp-ok"].verification_status == "CONSISTENT", v["comp-ok"].verification_notes
    assert short in v["comp-ok"].verification_notes[0] and "docker-compose.yml" in v["comp-ok"].verification_notes[0]
    assert "relasi belum dibandingkan" in v["comp-ok"].verification_notes[0]
    assert v["comp-bad"].verification_status == "MISMATCH" and "Kafka cluster" in v["comp-bad"].verification_notes[0]
    assert v["seq-ok"].verification_status == "CONSISTENT", v["seq-ok"].verification_notes
    assert "api/openapi.yaml" in v["seq-ok"].verification_notes[0]
    assert v["seq-bad"].verification_status == "MISMATCH" and "/v1/projects/1" in v["seq-bad"].verification_notes[0]
    assert v["erd-ok"].verification_status == "CONSISTENT", v["erd-ok"].verification_notes
    assert "FK" in v["erd-ok"].verification_notes[0] and "JUNCTION" in v["erd-ok"].verification_notes[0]
    assert v["erd-bad"].verification_status == "MISMATCH" and "users.budget" in v["erd-bad"].verification_notes[0]
    assert v["cicd-ok"].verification_status == "CONSISTENT", v["cicd-ok"].verification_notes
    assert v["cicd-bad"].verification_status == "MISMATCH" and "sonarqube scan" in v["cicd-bad"].verification_notes[0]
    assert v["deploy-ok"].verification_status == "CONSISTENT", v["deploy-ok"].verification_notes
    assert v["deploy-bad"].verification_status == "MISMATCH" and "Lambda" in v["deploy-bad"].verification_notes[-1]


@needs_git
def test_product_references_never_come_from_the_working_tree_or_an_unknown_commit(product):
    db, repo = product["db"], product["repo"]
    # an uncommitted edit is not part of the known commit: the route it adds stays unknown
    (repo / "api" / "openapi.yaml").write_text(OPENAPI.replace("/tasks:", "/projects/{id}:\n    get: {}\n  /tasks:"),
                                               encoding="utf-8")
    assert _visuals(product)["seq-bad"].verification_status == "MISMATCH"
    # a newer commit is read only once AEOS recorded it (merged PR or inventory)
    newer = commit_file(repo, "api/openapi.yaml", OPENAPI + "  /v1/projects/{id}:\n    delete: {}\n", "routes")
    assert _visuals(product)["seq-bad"].verification_status == "MISMATCH"
    ref = db.query(RepositoryReference).filter_by(full_name="owner/contoh-app").one()
    ref.inventory_commit = newer
    db.commit()
    assert _visuals(product)["seq-bad"].verification_status == "CONSISTENT"
    # no recorded commit at all -> nothing is read, every check UNVERIFIED with the reason
    ref.inventory_commit = None
    db.commit()
    v = _visuals(product)
    for key in ("comp-bad", "seq-bad", "erd-bad", "cicd-bad", "deploy-bad"):
        assert v[key].verification_status == "UNVERIFIED", key
        assert any("Belum ada commit tercatat" in n for n in v[key].verification_notes), key


@needs_git
def test_the_plans_contract_paths_name_openapi_files_the_inventory_does_not_classify(settings, db, monkeypatch,
                                                                                     tmp_path):
    """W6-047: ``interface_contracts[].contract_paths`` of the approved plan are read at the known commit too (here
    OpenAPI documents the inventory does not label openapi, one under a dot directory); a path that leaves the
    repository root (even when its tail exists in the repository) or a sensitive file name is never read."""
    install_fakes(monkeypatch)

    def spec(path: str) -> str:
        return json.dumps({"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": {path: {}}})

    repo = make_repo(tmp_path / "kontrak", {"contracts/api.json": spec("/sync/{id}"), ".api/extra.json": spec("/extra"),
                                            "x/outside.json": spec("/outside"), ".env": spec("/leak")})
    project = make_project(db, key="kontrak")
    db.add(RepositoryReference(project_id=project.id, provider="GITHUB", full_name="owner/kontrak",
                               url="https://github.com/owner/kontrak", local_path_ref=str(repo),
                               inventory_commit=git(repo, "rev-parse", "HEAD")))
    db.commit()
    content = sample()
    content["architecture"]["diagrams"] = [
        _diagram("ok", "sequence", "sequenceDiagram\n  a->>b: PUT /sync/9\n  b->>a: GET /extra"),
        _diagram("outside", "sequence", "sequenceDiagram\n  a->>b: GET /outside"),
        _diagram("leak", "sequence", "sequenceDiagram\n  a->>b: GET /leak")]
    world = {"settings": settings, "db": db}
    _plan, world["version"] = make_plan(db, project, copy.deepcopy(content), tasks=TASKS)
    assert _visuals(world)["ok"].verification_status == "UNVERIFIED", "not an OpenAPI file by its name alone"
    content["interface_contracts"] = [{
        "key": "C1", "title": "Sync API", "kind": "api", "feature_keys": [], "provider_task_key": "T1",
        "consumer_task_keys": [], "definition": "PUT /sync/{id}", "compatibility": "",
        "contract_paths": ["contracts/api.json", "./.api/extra.json", "../x/outside.json", "C:/x/outside.json", ".env"]}]
    _plan, world["version"] = make_plan(db, project, content, tasks=TASKS)
    v = _visuals(world)
    assert v["ok"].verification_status == "CONSISTENT", v["ok"].verification_notes
    assert "contracts/api.json" in v["ok"].verification_notes[0] and ".api/extra.json" in v["ok"].verification_notes[0]
    assert v["outside"].verification_status == "MISMATCH", "a path that leaves the root is refused, not normalised"
    assert v["leak"].verification_status == "MISMATCH", "a sensitive file is never a contract source"


@needs_git
def test_an_unrelated_commit_does_not_re_render_the_documents_that_cite_it(product):
    """§41 "hash/diff menentukan bagian dokumentasi yang berubah": the commit the references were read at is named
    in the body, but the same facts at a newer commit are the same input, so the narrative and the runbooks are not
    re-rendered (nor a material checkpoint delta / new revision to publish) for every commit of the repository."""
    db, repo = product["db"], product["repo"]

    def docs():
        result, _d, _s = service.render_version(db, product["settings"], product["version"], persist=False,
                                                render_visuals=False)
        ids = result.by_id()
        return ids["03-architecture-overview"], ids["08-runbooks/runbook-restart"]

    overview, runbook = docs()
    assert product["sha"][:12] in (overview.body_markdown or "") and product["sha"][:12] in (runbook.body_markdown or "")
    unrelated = commit_file(repo, "README.md", "# Contoh\n", "readme")
    ref = db.query(RepositoryReference).filter_by(full_name="owner/contoh-app").one()
    ref.inventory_commit = unrelated
    db.commit()
    overview2, runbook2 = docs()
    assert unrelated[:12] in (overview2.body_markdown or ""), "the body names the commit it was built from"
    assert overview2.input_hash == overview.input_hash and runbook2.input_hash == runbook.input_hash
    changed = commit_file(repo, "docs/runbooks/restart.md", RUNBOOK.replace("systemctl", "service"), "runbook")
    ref.inventory_commit = changed
    db.commit()
    overview3, runbook3 = docs()
    assert runbook3.input_hash != runbook.input_hash and "service restart contoh-api" in (runbook3.body_markdown or "")
    assert overview3.input_hash == overview.input_hash, "the diagram facts did not change"


@needs_git
def test_a_checkout_that_crosses_the_domain_boundary_is_not_read(product, monkeypatch):
    from aeos.core.errors import PolicyDenied
    from aeos.portfolio import repositories

    def refuse(*_a, **_k):  # the real refusal names the other domain's provider host (origin_domain_conflict)
        raise PolicyDenied("the checkout's origin is hosted on bitbucket.zz-tenant-host.example (OFFICE code provider)",
                           code="CHECKOUT_OTHER_DOMAIN_ORIGIN")

    monkeypatch.setattr(repositories, "require_checkout_in_domain", refuse)
    result, _d, _s = service.render_version(product["db"], product["settings"], product["version"], persist=False,
                                            render_visuals=False)
    v = {x.key: x for x in result.visuals}
    assert v["comp-bad"].verification_status == "UNVERIFIED"
    assert any("CHECKOUT_OTHER_DOMAIN_ORIGIN" in n for n in v["comp-bad"].verification_notes)
    assert all("zz-tenant-host" not in (d.body_markdown or "") for d in result.documents), "no other-domain host"


@needs_git
def test_product_runbooks_come_from_the_checkout_at_the_known_commit(product):
    result, _d, _s = service.render_version(product["db"], product["settings"], product["version"], persist=False,
                                            render_visuals=False)
    doc = result.by_id()["08-runbooks/runbook-restart"]
    body = doc.body_markdown or ""
    assert "systemctl restart contoh-api" in body and product["sha"][:12] in body
    assert "Procedure" not in doc.pending_sections and "Verification" not in doc.pending_sections
    assert "Escalation" in doc.pending_sections, "a section the runbook does not state stays pending"


def test_contract_parsers_read_openapi_prisma_sql_alembic_ci_and_compose():
    assert {"/tasks", "/tasks/{taskId}", "/v1/tasks"} <= refs_mod.openapi_paths(OPENAPI, "openapi.yaml")
    assert refs_mod.openapi_paths('{"paths": {"/x": {}}}', "a.json") == set(), "not an OpenAPI document"
    tables, cols, fks, aliases = refs_mod.parse_prisma(PRISMA)
    assert tables == {"users", "Task", "Tag"} and aliases == {"User": "users"}
    assert {"ownerId", "owner_id", "title"} <= cols["Task"] and "owner" not in cols["Task"]
    assert ("Task", "users") in fks and {("_TagToTask", "Task"), ("_TagToTask", "Tag")} <= fks
    t, c, f = refs_mod.parse_sql([
        'CREATE TABLE "projects" (id uuid PRIMARY KEY, name text NOT NULL, price numeric(10,2));',
        "create table if not exists tasks (id int, project_id uuid REFERENCES projects(id), "
        "CONSTRAINT ck CHECK (id > 0));\nALTER TABLE tasks ADD COLUMN due date;\n-- comment;\n"
        "ALTER TABLE tasks ADD CONSTRAINT fk FOREIGN KEY (owner) REFERENCES users (id);\n"
        "CREATE TABLE tmp (x int); DROP TABLE tmp;"])
    assert t == {"projects", "tasks"} and c["tasks"] == {"id", "project_id", "due"} and "price" in c["projects"]
    assert f == {("tasks", "projects"), ("tasks", "users")}
    first = ("m/1.py", "revision = 'a1'\ndown_revision = None\ndef upgrade():\n"
                       "    op.create_table('users', sa.Column('id', sa.Integer()), sa.Column('email', sa.String()))\n"
                       "def downgrade():\n    op.drop_table('users')\n")
    second = ("m/0.py", "revision = 'b2'\ndown_revision = 'a1'\ndef upgrade():\n"
                        "    op.create_table('tasks', sa.Column('id', sa.Integer()),\n"
                        "        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id')))\n"
                        "    with op.batch_alter_table('users') as batch_op:\n"
                        "        batch_op.add_column(sa.Column('name', sa.String()))\n"
                        "    op.create_foreign_key(None, 'tasks', 'teams', ['team_id'], ['id'])\n")
    t, c, f = refs_mod.parse_alembic([second, first])  # revision order, not file order; downgrade ignored
    assert t == {"users", "tasks"} and c["users"] == {"id", "email", "name"}
    assert f == {("tasks", "users"), ("tasks", "teams")}
    names = refs_mod.ci_names(WORKFLOW, ".github/workflows/ci.yml")
    assert {"CI", "push", "pull_request", "lint", "test", "deploy-staging", "staging"} <= names
    gitlab = refs_mod.ci_names("stages: [build, deploy]\n.tpl: {}\nbuild-job:\n  stage: build\n  script: [make]\n"
                               "deploy-prod:\n  stage: deploy\n  environment: {name: production}\n", ".gitlab-ci.yml")
    assert {"build", "deploy", "build-job", "deploy-prod", "production"} <= gitlab and ".tpl" not in gitlab
    assert refs_mod.compose_names(COMPOSE) == {"web", "api", "db", "postgres"}
    assert refs_mod.manifest_names("web/package.json", '{"name": "@contoh/web"}') == {"@contoh/web", "web"}
    assert refs_mod.manifest_names("k8s/app.yaml", "kind: Deployment\nmetadata: {name: contoh-api}\n---\n"
                                                   "kind: ConfigMap\nmetadata: {name: cfg}\n") == {"contoh-api"}


def test_flowchart_edges_are_parsed_with_link_texts_chains_and_groups():
    src = ("flowchart LR\n  a[Control Center] -->|REST| b((API))\n  b -- uses --> c[(DB)]\n"
           "  b-->d & e\n  e -.-> f\n  end-user --> a\n  subgraph X [Host]\n  end\n")
    assert vis.flow_edges(src) == [("a", "b"), ("b", "c"), ("b", "d"), ("b", "e"), ("e", "f"), ("end-user", "a")]
    nodes = vis.flow_nodes(src)
    assert "uses" not in nodes and nodes["end-user"] == "end-user" and nodes["c"] == "DB"


def _visual(kind: str, source: str, verification_source: str = "") -> vis.VisualArtifact:
    return vis.source_only([DiagramView(key=kind, title=kind, kind=kind, source=source,
                                        verification_source=verification_source)])[0]


def test_an_edge_needs_a_registered_relation_and_an_erd_line_a_foreign_key():
    refs = vis.VisualReferences(components={"a", "b", "c"}, component_ids={"a": "a", "b": "b", "c": "c"},
                                relations={("a", "b")})
    ok = _visual("component", "flowchart LR\n  b --> a")
    bad = _visual("component", "flowchart LR\n  a --> b --> c")
    vis.verify_visuals([ok, bad], refs=refs)
    assert ok.verification_status == "CONSISTENT" and "relasi" in ok.verification_notes[0]
    assert bad.verification_status == "MISMATCH" and "b -> c" in bad.verification_notes[0]
    schema = vis.VisualReferences(tables={"projects", "plans", "notes"},
                                  columns={"projects": {"id"}, "plans": {"id"}, "notes": {"id", "plan_id"}},
                                  foreign_keys={("plans", "projects")})
    erd = [_visual("erd", "erDiagram\n  projects ||--o{ plans : has\n  plans ||--o{ notes : has"),
           _visual("erd", "erDiagram\n  projects ||--o{ notes : has")]
    vis.verify_visuals(erd, refs=schema)
    assert erd[0].verification_status == "CONSISTENT" and "COLUMN" in erd[0].verification_notes[0]
    assert erd[1].verification_status == "MISMATCH" and "projects - notes" in erd[1].verification_notes[0]


def test_a_sequence_is_verified_only_by_tests_that_passed_at_an_accepted_head():
    src = ("sequenceDiagram\n  %% test: tests/integration/test_flow.py::test_create\n"
           "  UI->>API: POST /api/plans")
    routes = vis.VisualReferences(routes={"/api/plans"}, route_prefix="/api/")
    pending = _visual("sequence", src)
    vis.verify_visuals([pending], refs=routes)
    assert pending.verification_status == "CONSISTENT"
    assert "tests.integration.test_flow::test_create" in pending.verification_notes[-1]
    routes.passed_tests = {"tests.integration.test_flow::test_create": ("ev-1", "c" * 40)}
    done = _visual("sequence", src)
    vis.verify_visuals([done], refs=routes)
    assert done.verification_status == "VERIFIED" and "ev-1" in done.verification_notes[-1]
    assert vis.named_tests("x", "tests/integration/test_a.py::TestX::test_y") == ["tests.integration.test_a.TestX::test_y"]
    from aeos.verification.junit import nodeid_to_key

    for node in ("tests/a/test_x.py::test_y", "tests/a/test_x.py::TestC::test_d[p::q/r]", "tests\\w\\test_z.py"):
        assert vis.junit_key(node) == nodeid_to_key(node), "the verifier's own key form"


# ---------------------------------------------------------------------------------------------- platform wiring


@pytest.fixture
def platform(settings, db, monkeypatch):
    from aeos.inventory import seed as inv_seed

    install_fakes(monkeypatch)
    inv_seed.run(db)
    project = make_project(db, key="platform-refs")
    project.is_platform = True
    db.commit()
    return {"settings": settings, "db": db, "project": project}


def _platform_visuals(world, diagrams, content=None):
    content = content or sample()
    content["architecture"]["diagrams"] = diagrams
    _plan, version = make_plan(world["db"], world["project"], content, tasks=TASKS)
    result, _d, _s = service.render_version(world["db"], world["settings"], version, persist=False,
                                            render_visuals=False)
    return {v.key: v for v in result.visuals}, result


def test_platform_component_diagram_uses_the_real_inventory_and_its_relations(platform):
    """W6-094: the seeded InstallManifest decides — a registered component pair with a DESIGN relation is
    CONSISTENT, an invented link between two real components or an unregistered node is MISMATCH."""
    v, _r = _platform_visuals(platform, [
        _diagram("ok", "component", "flowchart LR\n  ui[Control Center frontend] --> cp[DLY AEOS control plane]\n"
                                    "  cp --> db[SQLite metadata store]"),
        _diagram("edge", "component", "flowchart LR\n  ui[Control Center frontend] --> db[SQLite metadata store]"),
        _diagram("node", "context", "flowchart LR\n  ui[Control Center] --> k[Kafka cluster]")])
    assert v["ok"].verification_status == "CONSISTENT", v["ok"].verification_notes
    assert v["edge"].verification_status == "MISMATCH"
    assert "Control Center frontend -> SQLite metadata store" in v["edge"].verification_notes[0]
    assert v["node"].verification_status == "MISMATCH" and "Kafka cluster" in v["node"].verification_notes[0]


def test_platform_sequence_routes_and_integration_test_evidence_through_render(platform):
    """W6-095: platform paths vs contracts/api/routes.json; VERIFIED once the named test PASSED in a trusted
    verifier TEST_REPORT at an accepted head of the project."""
    from aeos.models.evidence import AcceptanceRecord
    from aeos.verification import facade as verification

    db, project = platform["db"], platform["project"]
    test_id = "tests/acceptance/test_self_build.py::test_flow"
    diagrams = [_diagram("ok", "sequence", "sequenceDiagram\n  UI->>API: GET /api/projects/{project_id}",
                         verification_source=f"route contract; {test_id}"),
                _diagram("bad", "sequence", "sequenceDiagram\n  UI->>API: POST /api/nope/zzz")]
    v, _r = _platform_visuals(platform, copy.deepcopy(diagrams))
    assert v["ok"].verification_status == "CONSISTENT" and v["bad"].verification_status == "MISMATCH"
    assert "/api/nope/zzz" in v["bad"].verification_notes[0]
    head = "d" * 40
    db.add(AcceptanceRecord(task_id="t", run_id="r", project_id=project.id, domain=project.domain,
                            artifact_hash="sha256:a", head_commit=head, acceptance_status="ACCEPTED"))
    ev = verification.collect_evidence(
        db, kind="TEST_REPORT", origin="verifier", collector_identity="aeos-verifier", claim="trusted suite PASS",
        content={"suite": "trusted-acceptance", "status": "PASS",
                 "report": {"cases": [{"key": "tests.acceptance.test_self_build::test_flow", "outcome": "passed"}]}},
        evidence_level="E2", project_id=project.id, domain=project.domain, environment={"commit": head})
    db.commit()
    v, _r = _platform_visuals(platform, copy.deepcopy(diagrams))
    assert v["ok"].verification_status == "VERIFIED", v["ok"].verification_notes
    assert ev.id in v["ok"].verification_notes[-1] and head[:12] in v["ok"].verification_notes[-1]


def test_platform_erd_relations_are_compared_with_the_foreign_keys(platform):
    """W6-096: plans -> projects is a declared foreign key; users -> plan_versions is not declared anywhere."""
    v, _r = _platform_visuals(platform, [
        _diagram("ok", "erd", "erDiagram\n  projects ||--o{ plans : has\n  PlanVersion }o--|| plans : of"),
        _diagram("bad", "erd", "erDiagram\n  users ||--o{ plan_versions : writes")])
    assert v["ok"].verification_status == "CONSISTENT", v["ok"].verification_notes
    assert v["bad"].verification_status == "MISMATCH" and "users - plan_versions" in v["bad"].verification_notes[0]


def test_platform_cicd_diagram_is_compared_with_the_ci_jobs_at_head(platform):
    """W6-098: job ids/names of .github/workflows/ci.yml at the platform HEAD commit."""
    v, _r = _platform_visuals(platform, [
        _diagram("ok", "cicd", "flowchart LR\n  pr[pull_request] --> backend --> frontend --> bootstrap"),
        _diagram("bad", "cicd", "flowchart LR\n  backend --> d[deploy to prod cluster]")])
    assert v["ok"].verification_status == "CONSISTENT", v["ok"].verification_notes
    assert re.search(r"\.github/workflows/ci\.yml", v["ok"].verification_notes[0])
    assert v["bad"].verification_status == "MISMATCH" and "deploy to prod cluster" in v["bad"].verification_notes[0]


def test_data_flow_wiring_names_missing_inventory_items_and_boundaries_in_the_architecture_overview(platform):
    """W6-097: render_version passes the plan's data inventory and threat-model trust boundaries to the check;
    a data-flow diagram that omits one is MISMATCH and the note is shown in 03-architecture-overview."""
    content = sample()
    content["security_privacy"]["threat_model"]["trust_boundaries"] = ["Browser boundary", "Host boundary"]
    content["security_privacy"]["data_inventory"] = [
        {"data_type": "Data tugas", "classification": "INTERNAL", "owner": "x", "storage": "SQLite", "access": "x",
         "retention": "x", "disposal": "x"},
        {"data_type": "Email pengguna", "classification": "CONFIDENTIAL", "owner": "x", "storage": "SQLite",
         "access": "x", "retention": "x", "disposal": "x"}]
    v, result = _platform_visuals(platform, [
        _diagram("flow", "data_flow", "flowchart LR\n  u[Data tugas] --> b[Browser boundary]"),
        _diagram("tb", "trust_boundary", "flowchart LR\n  subgraph h[Host boundary]\n    a[API]\n  end")],
        content=content)
    flow = v["flow"]
    assert flow.verification_status == "MISMATCH"
    notes = " ".join(flow.verification_notes)
    assert "Host boundary" in notes and "Email pengguna" in notes
    assert v["tb"].verification_status == "MISMATCH" and "Browser boundary" in " ".join(v["tb"].verification_notes)
    overview = result.by_id()["03-architecture-overview"].body_markdown or ""
    assert "Verifikasi diagram: **MISMATCH**" in overview and "Email pengguna" in overview


def test_product_data_flow_is_checked_against_its_plan_through_render(settings, db, monkeypatch):
    """W6-097 for a PRODUCT (no checkout needed: the references are the plan's own data inventory and threat model):
    a complete data-flow diagram is CONSISTENT, one that drops a data type is MISMATCH, through render_version."""
    install_fakes(monkeypatch)
    project = make_project(db, key="produk-flow")
    content = sample()
    content["security_privacy"]["threat_model"]["trust_boundaries"] = ["Browser boundary"]
    content["security_privacy"]["data_inventory"] = [
        {"data_type": "Data tugas", "classification": "INTERNAL", "owner": "x", "storage": "SQLite", "access": "x",
         "retention": "x", "disposal": "x"}]
    content["architecture"]["diagrams"] = [
        _diagram("ok", "data_flow", "flowchart LR\n  u[Data tugas] --> b[Browser boundary]"),
        _diagram("bad", "data_flow", "flowchart LR\n  b[Browser boundary] --> s[Server]")]
    _plan, version = make_plan(db, project, content, tasks=TASKS)
    result, _d, _s = service.render_version(db, settings, version, persist=False, render_visuals=False)
    v = {x.key: x for x in result.visuals}
    assert v["ok"].verification_status == "CONSISTENT", v["ok"].verification_notes
    assert v["bad"].verification_status == "MISMATCH" and "Data tugas" in " ".join(v["bad"].verification_notes)
