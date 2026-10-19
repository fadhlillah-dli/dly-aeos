import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { ComponentRow, RelationView } from "../../components/inventory/types";

const auth = { isAdmin: false, domain: "PERSONAL" as string | null, session: null, projectId: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

import { applyDiff, foldGraph } from "../system-map/layout";
import { EdgeCard } from "../system-map/Panels";
import { DependencyExplorer } from "./ComponentDetailPage";
import ToolsPage, { Dim, ProjectRecords } from "./index";

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const dim = (value: string, extra: Record<string, unknown> = {}) => ({
  value, effective: value, at: null, expires_at: null, stale: false, source: "s", observation_id: null, version: null, reason: null, ...extra,
});

const row: ComponentRow = {
  component_id: "opencode",
  name: "OpenCode",
  friendly_name: "OpenCode — mesin eksekusi pekerja",
  kind: "worker_engine",
  category: "workforce",
  category_label: "Workforce",
  domains: ["PLATFORM"],
  requirement: "REQUIRED",
  design_label: "SELECTED_DESIGN",
  identity_verified: "VERIFIED",
  source_url: null,
  source_kind: "GITHUB_REPO",
  usage_role: "runtime",
  bootstrap_order: 5,
  version: { approved_pin: null, observed_version: null, status: "NO_PIN", pin_note: null },
  dimensions: {
    admission: dim("DISCOVERED"), installation: dim("UNKNOWN"), configuration: dim("UNKNOWN"), connectivity: dim("NOT_CHECKED"),
    functional: dim("NOT_TESTED"), enablement: dim("DISABLED"), usage: dim("NEVER_OBSERVED"), lifecycle_final: dim("ACTIVE"),
  },
  drift: [],
  config_lens: { present: false, basis: [] },
  tombstoned_at: null,
  mode: "DESIGN",
  why_selected: "Worker engine pertama yang diuji",
  next_action: { code: "RUN_RECONCILE", label: "Jalankan aeos inventory reconcile", command: "aeos inventory reconcile", link: null },
};

let fetchMock: ReturnType<typeof vi.fn>;
afterEach(() => vi.unstubAllGlobals());

describe("Tools & Repos C4 (§35, §37, §38)", () => {
  it("shows the reason used and the next action before the technical id", async () => {
    fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          items: [row], total: 1, page: 1, page_size: 50, view: "default",
          facets: { kinds: [], requirements: [], categories: [], design_labels: [] },
          coverage: { totals: {}, gaps: [], last_reconciled_at: null, last_full_reconciled_at: null },
          manifest: null, source: "x", checked_at: null, mode: "DESIGN",
          project_repositories: [], project_services: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    wrap(<ToolsPage />);
    const why = await screen.findByTestId("why-selected");
    const next = screen.getByTestId("next-action");
    const id = screen.getByTestId("component-id");
    expect(why).toHaveTextContent("Worker engine pertama yang diuji");
    expect(next).toHaveTextContent("Jalankan aeos inventory reconcile");
    expect(why.compareDocumentPosition(id) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(next.compareDocumentPosition(id) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/Unduh SBOM/).closest("a")).toHaveAttribute("href", "/api/inventory/sbom");
  });

  it("renders 'Sedang digunakan oleh task' only with a run reference", () => {
    wrap(<Dim d={dim("IN_USE", { reason: "Sedang digunakan oleh task T-1 (run abc, attempt 1)", refs: { run_id: "abc" } })} />);
    expect(screen.getByText(/Sedang digunakan oleh task T-1/)).toBeInTheDocument();
  });

  it("lists project repositories with their exact id and declared services as DECLARED", () => {
    wrap(
      <ProjectRecords
        repos={[{
          id: "repo:1", repository_id: "1", kind: "project_repository", project_id: "p1", project_name: "P1", domain: "PERSONAL",
          provider: "GITHUB", full_name: "me/app", provider_repo_id: "42", identity: "github:42", identity_status: "VERIFIED", role: "project",
          default_branch: "main", head_commit: "a".repeat(40), dirty: false, verification_status: "VERIFIED", last_verified_at: null, link: "/projects/p1",
        }]}
        services={[{
          id: "svc:1", kind: "project_service", name: "API service", project_id: "p1", project_name: "P1", domain: "PERSONAL",
          status: "DECLARED", observed: "UNKNOWN", observed_reason: "belum teramati", provenance: "plan_version:sha256:x", lifecycle: "ACTIVE",
        }]}
        routes={[{ id: "route:or", kind: "route", route_key: "or-free", status: "APPROVED", pool: "WORKER", provider: "openrouter", provider_component: "openrouter" }]}
        roles={[{ id: "role:impl", kind: "role", role_key: "impl", name: "Implementer", agents: [{ agent_key: "opencode-implementer", harness: "opencode" }], harness_components: ["opencode"] }]}
      />,
    );
    expect(screen.getByTestId("registry-records")).toHaveTextContent("route or-free");
    expect(screen.getByTestId("registry-records")).toHaveTextContent("opencode-implementer");
    expect(screen.getByText("me/app")).toBeInTheDocument();
    expect(screen.getByText("github:42")).toBeInTheDocument();
    expect(screen.getByText("API service")).toBeInTheDocument();
    expect(screen.getByText(/teramati: UNKNOWN/)).toBeInTheDocument();
  });

  it("explores the paginated dependency view with 'digunakan oleh build'", async () => {
    fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          items: [{ name: "mako", version: "1.3.0", relation: "transitive", direct: false, required_by: ["alembic"], parent_path: ["alembic"],
            ecosystem: "pypi", purl: "pkg:pypi/mako@1.3.0", used_by_build: { component_id: "dly-aeos-control-plane", lockfile: "requirements.lock", label: "digunakan oleh build DLY AEOS control plane" } }],
          total: 1, page: 1, page_size: 50, status: "READ", reason: null, counts: { direct: 1, transitive: 1, total: 2 },
          lockfile: { path: "requirements.lock", status: "READ", sha256: "sha256:x", matches_last_reconcile: true },
          coverage: { images: "Image container: coverage gap" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    wrap(<DependencyExplorer componentId="dly-aeos-control-plane" />);
    expect(await screen.findByText(/digunakan oleh build DLY AEOS control plane/)).toBeInTheDocument();
    expect(screen.getByText(/transitif via alembic/)).toBeInTheDocument();
    await waitFor(() => expect(String(fetchMock.mock.calls[0][0])).toContain("/dependencies?page=1"));
  });
});

describe("System Map C4 (§36, §41)", () => {
  it("edge card shows the latest check results of the relation ends", () => {
    const rel: RelationView = {
      id: "e1", source: "publisher", target: "google-drive", source_name: "Publisher", target_name: "Google Drive", relation: "publishes_to",
      lens: "DESIGN", status: "DESIGN", provenance: "install-manifest:x", data_description: "folder", operation: "WRITE", boundary: null,
      identity_ref: null, direction: "publisher -> google-drive", last_observed_at: null, freshness: { at: null, stale: false },
      tombstoned_at: null, evidence: "relasi rancangan",
      tests: [{ check_id: "adapter.google_oauth", status: "CONFIGURED", last_checked_at: null, summary: "", about: "google-drive" }],
    };
    wrap(<EdgeCard rels={[rel]} onClose={() => undefined} />);
    expect(screen.getByTestId("edge-tests")).toHaveTextContent("adapter.google_oauth");
  });

  it("colours added/changed edges and brings removed ones back as ghosts", () => {
    const n = (id: string) => ({ id, label: id, name: id, kind: "service", category: "c", requirement: "REQUIRED", design_label: "D", domains: [], identity_verified: "U", present: true, status: "DESIGN", at: null, stale: false, basis: [], drift: [], function: "" });
    const e = (id: string, s: string, t: string) => ({
      id, source: s, target: t, source_name: s, target_name: t, relation: "calls", lens: "DESIGN", status: "DESIGN", provenance: "", data_description: "",
      operation: "", boundary: null, identity_ref: null, direction: "", last_observed_at: null, freshness: { at: null, stale: false }, tombstoned_at: null, evidence: "",
    });
    const g = foldGraph([n("a"), n("b"), n("c")], [e("e1", "a", "b"), e("e2", "b", "c")], [], new Set());
    const out = applyDiff(g, {
      nodes: { added: [{ id: "c" }], removed: [], changed: [{ key: "a" }] },
      edges: { added: [{ source: "a", target: "b", relation: "calls" }], removed: [{ source: "a", target: "c", relation: "calls" }], changed: [{ key: ["b", "c", "calls", "DESIGN"] }] },
    });
    expect(out.edges.find((x) => x.id === "e1")?.diff).toBe("added");
    expect(out.edges.find((x) => x.id === "e2")?.diff).toBe("changed");
    expect(out.edges.find((x) => x.diff === "removed")?.source).toBe("a");
    expect(out.nodes.find((x) => x.id === "c")?.diff).toBe("added");
    expect(applyDiff(g, null)).toBe(g);
  });
});
