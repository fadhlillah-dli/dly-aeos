import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { statusSpec } from "../../components/StatusBadge";
import { mapStatus } from "./badges";
import { precheck } from "./UploadZone";

const auth = { domain: "OFFICE", isAdmin: true, projectId: null, session: null, selectContext: vi.fn() };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

type Handler = (url: string, init?: RequestInit) => unknown;
function mockFetch(handler: Handler) {
  const fn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const body = handler(String(url), init);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  auth.isAdmin = true;
  auth.domain = "OFFICE";
});

describe("module status mapping (§34/§35 evidence honesty)", () => {
  it("never renders unverified / unscanned / flagged as a pass tone", () => {
    for (const [kind, value] of [
      ["verify", "UNVERIFIED"],
      ["scan", "UNSCANNED"],
      ["scan", "FLAGGED"],
      ["config", "CONFIGURED"],
      ["config", "NOT_CONFIGURED"],
      ["freshness", "STALE"],
      ["freshness", "UNKNOWN"],
      ["local", "MISMATCH"],
      ["credential", "UNKNOWN"],
    ] as const) {
      expect(statusSpec(mapStatus(kind, value)[0]).tone).not.toBe("ok");
    }
    expect(statusSpec(mapStatus("verify", "VERIFIED")[0]).tone).toBe("ok");
    // a scanner without findings is not a pass: the attachment stays untrusted DATA
    expect(statusSpec(mapStatus("scan", "CLEAN")[0]).tone).not.toBe("ok");
    expect(mapStatus("scan", undefined)[0]).toBe("UNKNOWN");
  });
});

describe("upload pre-check (convenience only; backend enforces)", () => {
  const policy = { allowed_extensions: [".md", ".pdf"], max_upload_bytes: 10, max_archive_members: 5, notice: "" };
  it("rejects executables, unknown types and oversize files", () => {
    expect(precheck(new File(["MZ"], "tool.exe"), policy)).toMatch(/tidak diterima/);
    expect(precheck(new File(["x".repeat(20)], "big.md"), policy)).toMatch(/melebihi/);
    expect(precheck(new File([""], "empty.md"), policy)).toMatch(/kosong/);
    expect(precheck(new File(["# ok"], "ide.md"), policy)).toBeNull();
  });
});

describe("NewProjectPage", () => {
  it("shows the session domain read-only and sends it explicitly with the domain's provider", async () => {
    const { default: NewProjectPage } = await import("./NewProjectPage");
    let created: Record<string, unknown> | null = null;
    mockFetch((url, init) => {
      if (url.startsWith("/api/intake/policy")) return { allowed_extensions: [".md"], max_upload_bytes: 1000, max_archive_members: 5, notice: "DATA" };
      if (url === "/api/projects" && init?.method === "POST") {
        created = JSON.parse(String(init.body));
        return { project: { id: "p1", name: "Svc", project_key: "svc" }, verification: null };
      }
      return {};
    });
    wrap(<NewProjectPage />);
    expect(screen.getAllByText("OFFICE").length).toBeGreaterThan(0);
    expect(screen.getByText(/tidak dapat diubah di sini/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: /Proyek existing/ }));
    const submit = screen.getByRole("button", { name: /Buat proyek di OFFICE/ });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Nama proyek"), "Svc");
    expect(submit).toBeDisabled(); // EXISTING requires an explicit repository
    expect((screen.getByLabelText("Provider") as HTMLInputElement).value).toBe("BITBUCKET");
    await userEvent.type(screen.getByLabelText("Workspace"), "company");
    await userEvent.type(screen.getByLabelText("Nama repository"), "svc");
    await userEvent.click(submit);
    await waitFor(() => expect(created).not.toBeNull());
    expect(created!.domain).toBe("OFFICE");
    expect(created!.project_kind).toBe("EXISTING");
    expect((created!.repository as Record<string, unknown>).provider).toBe("BITBUCKET");
    expect((created!.repository as Record<string, unknown>).verify).toBe(false);
    expect(await screen.findByText(/Buka detail proyek/)).toBeInTheDocument();
  });

  it("disables creation for VIEWER", async () => {
    auth.isAdmin = false;
    const { default: NewProjectPage } = await import("./NewProjectPage");
    mockFetch(() => ({ allowed_extensions: [".md"], max_upload_bytes: 1000, max_archive_members: 5, notice: "" }));
    wrap(<NewProjectPage />);
    expect(screen.getByText(/hanya dapat membaca/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Nama proyek"), { target: { value: "x" } });
    expect(screen.getByRole("button", { name: /Buat proyek/ })).toBeDisabled();
  });
});

describe("ProjectsPage", () => {
  it("lists only what the backend returns for the active domain with honest statuses", async () => {
    auth.domain = "PERSONAL";
    auth.isAdmin = false;
    const { default: ProjectsPage } = await import("./index");
    mockFetch((url) => {
      if (url === "/api/projects")
        return {
          items: [
            {
              id: "p1",
              name: "DLY AEOS",
              project_key: "dly-aeos",
              domain: "PERSONAL",
              lifecycle: "ACTIVE",
              data_classification: "INTERNAL",
              project_kind: "EXISTING",
              is_platform: true,
              mode: "LIVE",
              repositories: [{ id: "r1", full_name: "fadhlillah-dli/dly-aeos", provider: "GITHUB", role: "platform", verification_status: "UNVERIFIED" }],
              integrations: [],
              integration_state: { docs: "MISSING", issues: "CONFIGURED" },
              links: {},
            },
          ],
          checked_at: new Date().toISOString(),
          source: "test",
          mode: "LIVE",
        };
      return { items: [], total: 0, counts: {} };
    });
    wrap(<ProjectsPage />);
    expect(await screen.findByText("Platform DLY AEOS")).toBeInTheDocument();
    expect(screen.getByText("Belum diverifikasi")).toBeInTheDocument();
    expect(screen.getByText("Terkonfigurasi (belum diuji)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Proyek baru/ })).toBeDisabled();
  });

  it("shows the remedy command when the domain has no projects", async () => {
    auth.domain = "PERSONAL";
    const { default: ProjectsPage } = await import("./index");
    mockFetch(() => ({ items: [], checked_at: new Date().toISOString(), mode: "LIVE" }));
    wrap(<ProjectsPage />);
    expect(await screen.findByText(/Belum ada proyek di domain PERSONAL/)).toBeInTheDocument();
    expect(screen.getByText("aeos seed")).toBeInTheDocument();
  });
});

describe("SearchPanel", () => {
  it("scopes the backend search to the project when a projectId is given", async () => {
    auth.domain = "PERSONAL";
    const { SearchPanel } = await import("./SearchPanel");
    const fn = mockFetch(() => ({ items: [], total: 0, counts: {}, has_more: false, limit: 20, offset: 0, checked_at: new Date().toISOString(), source: "t" }));
    wrap(<SearchPanel projectId="proj-1" />);
    expect(screen.getByText("Cari metadata proyek ini")).toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText(/Cari proyek/), "billing");
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const url = String(fn.mock.calls.at(-1)?.[0]);
    expect(url).toContain("q=billing");
    expect(url).toContain("&project=proj-1");
  });
});

describe("IntegrationsPanel", () => {
  it("never labels a configuration-only integration as connected or verified", async () => {
    auth.domain = "PERSONAL";
    auth.isAdmin = false;
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    mockFetch(() => ({
      items: [
        {
          id: "i1",
          project_id: "p1",
          domain: "PERSONAL",
          provider: "google-drive",
          purpose: "docs",
          tenant_id: null,
          root_id: null,
          root_id_name: "GOOGLE_DRIVE_FOLDER_ID",
          credential_ref: "personal/google-oauth-token",
          config: {},
          target_url: null,
          last_verified_at: null,
          updated_at: null,
          capability_status: {
            configuration: "MISSING",
            credential: { status: "MISSING" },
            connectivity: "NOT_CHECKED",
            functional: "NOT_TESTED",
            remedies: [{ issue: "GOOGLE_DRIVE_FOLDER_ID belum diatur", command: "aeos publish google-connect" }],
          },
        },
      ],
      allowed_providers: { "google-drive": ["docs"] },
      checked_at: new Date().toISOString(),
      mode: "LIVE",
    }));
    const project = { id: "p1", domain: "PERSONAL" } as unknown as import("./types").ProjectView;
    wrap(<IntegrationsPanel project={project} isAdmin={false} />);
    expect(await screen.findByText("Koneksi belum diperiksa")).toBeInTheDocument();
    expect(screen.getByText("Fungsi belum diuji")).toBeInTheDocument();
    expect(screen.queryByText(/Terverifikasi/)).not.toBeInTheDocument();
    expect(screen.getAllByText("aeos publish google-connect").length).toBeGreaterThan(0);
    expect(screen.queryByText("Pengaturan integrasi publikasi")).not.toBeInTheDocument(); // VIEWER: no admin form
    expect(screen.queryByText("Atur integrasi lain")).not.toBeInTheDocument();
  });

  it("PERSONAL settings save Google Drive (optional folder) and GitHub Issues with credential refs only", async () => {
    auth.domain = "PERSONAL";
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    const fn = mockFetch((_url, init) =>
      init?.method === "PUT"
        ? { integration: JSON.parse(String(init.body)) }
        : { items: [], allowed_providers: { "google-drive": ["docs"], "github-issues": ["issues"] }, checked_at: new Date().toISOString(), mode: "LIVE" },
    );
    const project = {
      id: "p1",
      domain: "PERSONAL",
      repositories: [{ provider: "GITHUB", full_name: "owner/app" }],
    } as unknown as import("./types").ProjectView;
    wrap(<IntegrationsPanel project={project} isAdmin />);
    const drive = await screen.findByRole("form", { name: /Google Drive/ });
    await userEvent.click(within(drive).getByRole("button", { name: /Simpan/ }));
    await waitFor(() => expect(fn.mock.calls.some(([, i]) => i?.method === "PUT")).toBe(true));
    const driveBody = JSON.parse(String(fn.mock.calls.find(([, i]) => i?.method === "PUT")![1]!.body));
    expect(driveBody).toEqual({ provider: "google-drive", purpose: "docs", root_id: null, credential_ref: "personal/google-oauth-token", config: {} });
    const issues = screen.getByRole("form", { name: /GitHub Issues/ });
    expect(within(issues).getByDisplayValue("owner/app")).toBeInTheDocument(); // defaults to the project's repository
    await userEvent.click(within(issues).getByRole("button", { name: /Simpan/ }));
    await waitFor(() => expect(fn.mock.calls.filter(([, i]) => i?.method === "PUT").length).toBe(2));
    const issuesBody = JSON.parse(String(fn.mock.calls.filter(([, i]) => i?.method === "PUT")[1][1]!.body));
    expect(issuesBody).toMatchObject({ provider: "github-issues", purpose: "issues", root_id: "owner/app", credential_ref: "personal/github" });
    expect(JSON.stringify(issuesBody)).not.toMatch(/token\W*[:=]|secret\W*[:=]/i);
  });

  it("OFFICE settings require the Confluence parent and space and keep the adapter disabled by default", async () => {
    auth.domain = "OFFICE";
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    const fn = mockFetch((_url, init) =>
      init?.method === "PUT"
        ? { integration: JSON.parse(String(init.body)) }
        : { items: [], allowed_providers: { confluence: ["docs"], jira: ["issues", "roadmap"] }, checked_at: new Date().toISOString(), mode: "LIVE" },
    );
    const project = { id: "p2", domain: "OFFICE", repositories: [] } as unknown as import("./types").ProjectView;
    wrap(<IntegrationsPanel project={project} isAdmin />);
    const conf = await screen.findByRole("form", { name: /Confluence/ });
    expect(within(conf).getByRole("button", { name: /Simpan/ })).toBeDisabled();
    fireEvent.change(within(conf).getByPlaceholderText("perusahaan.atlassian.net"), { target: { value: "acme.atlassian.net" } });
    fireEvent.change(within(conf).getByPlaceholderText("ENG"), { target: { value: "ENG" } });
    fireEvent.change(within(conf).getByLabelText(/CONFLUENCE_PROJECTS_PARENT_ID/), { target: { value: "123456" } });
    fireEvent.change(within(conf).getByLabelText(/Deployment/), { target: { value: "CLOUD" } });
    await userEvent.click(within(conf).getByRole("button", { name: /Simpan/ }));
    await waitFor(() => expect(fn.mock.calls.some(([, i]) => i?.method === "PUT")).toBe(true));
    const body = JSON.parse(String(fn.mock.calls.find(([, i]) => i?.method === "PUT")![1]!.body));
    expect(body).toEqual({
      provider: "confluence",
      purpose: "docs",
      tenant_id: "acme.atlassian.net",
      root_id: "123456",
      credential_ref: "office/atlassian",
      config: { space_id: "ENG", deployment: "CLOUD", enabled: false },
    });
    expect(screen.getAllByText(/OFFICE default deny/).length).toBeGreaterThan(0);
  });
});

describe("ProjectDetailPage read-only lifecycle", () => {
  it("states that an ARCHIVED project is read-only and only offers lifecycle actions", async () => {
    auth.domain = "PERSONAL";
    auth.isAdmin = true;
    const { default: ProjectDetailPage } = await import("./ProjectDetailPage");
    mockFetch((url) => {
      if (url.startsWith("/api/projects/"))
        return {
          project: {
            id: "p1",
            name: "Arsip",
            project_key: "arsip",
            domain: "PERSONAL",
            lifecycle: "ARCHIVED",
            lifecycle_reason: "jeda",
            data_classification: "INTERNAL",
            project_kind: "NEW",
            is_platform: false,
            mode: "LIVE",
            owner_user_id: "u1",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            repositories: [],
            integrations: [],
            integration_state: { docs: "NOT_CONFIGURED", issues: "NOT_CONFIGURED" },
            links: { plans: "/plans?project=p1", publications: "/publications?project=p1", dag: "/dag?project=p1", detail: "/projects/p1" },
            description: "",
            idea: "ide",
            input_findings: [],
            attachments: { total: 0 },
          },
          checked_at: new Date().toISOString(),
          source: "aeos-portfolio",
          mode: "LIVE",
        };
      return { items: [] };
    });
    wrap(<ProjectDetailPage />);
    expect(await screen.findByText(/diarsipkan dan hanya-baca/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ubah" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aktifkan" })).toBeInTheDocument();
  });
});

describe("RepositoriesPanel joined repository view (§37)", () => {
  it("renders PR/CI links, docs targets and capability from GET /api/repositories/{id}", async () => {
    auth.domain = "PERSONAL";
    const { RepositoriesPanel } = await import("./RepositoriesPanel");
    const fn = mockFetch((url) => {
      if (url === "/api/repositories/r1")
        return {
          repository: {
            id: "r1",
            full_name: "octo/view",
            provider: "GITHUB",
            role: "project",
            verification_status: "VERIFIED",
            base_branch: "main",
            pull_requests: [{ number: 7, url: "https://github.com/octo/view/pull/7", state: "OPEN", head_ref: "aeos/t1", base_ref: "main", last_synced_at: null }],
            ci: [{ provider: "github-actions", integration_id: "i-ci", configuration: "CONFIGURED" }],
            docs_targets: [{ provider: "google-drive", root_id: "1AbCdEfGh", integration_id: "i-docs" }],
            capability: {
              write_enablement: [
                { integration_id: "i-docs", provider: "google-drive", purpose: "docs", decision_id: "d1", status: "ENABLED" },
                { integration_id: "i-ci", provider: "github-actions", purpose: "ci", decision_id: null, status: "NOT_ENABLED" },
              ],
              merge: "HUMAN_ONLY (AEOS never merges)",
            },
          },
        };
      return {};
    });
    const project = {
      id: "p1",
      domain: "PERSONAL",
      project_kind: "NEW",
      repositories: [
        { id: "r1", full_name: "octo/view", provider: "GITHUB", role: "project", verification_status: "VERIFIED", local_status: "NOT_CHECKED", dirty: null },
      ],
    } as unknown as import("./types").ProjectView;
    wrap(<RepositoriesPanel project={project} isAdmin={false} />);
    await userEvent.click(screen.getByRole("button", { name: /PR, CI, dokumen/ }));
    const section = await screen.findByRole("region", { name: /Tautan repository octo\/view/ });
    expect(within(section).getByRole("link", { name: /#7/ })).toHaveAttribute("href", "https://github.com/octo/view/pull/7");
    expect(within(section).getByText("Terbuka")).toBeInTheDocument();
    expect(within(section).getByText("1AbCdEfGh")).toBeInTheDocument();
    expect(within(section).getByText("Diizinkan (keputusan)")).toBeInTheDocument();
    expect(within(section).getByText("Belum diizinkan")).toBeInTheDocument();
    expect(within(section).getByText(/HUMAN_ONLY/)).toBeInTheDocument();
    expect(fn.mock.calls.some(([u]) => String(u) === "/api/repositories/r1")).toBe(true);
  });
});

describe("SearchPanel status honesty", () => {
  it("renders module statuses through the honest mapping (a CLEAN scan is not a pass)", async () => {
    auth.domain = "PERSONAL";
    const { SearchPanel } = await import("./SearchPanel");
    mockFetch(() => ({
      items: [
        { kind: "attachment", id: "a1", title: "brief.md", subtitle: "text/plain", snippet: "", project_id: "p1", status: "CLEAN", url: "/projects/p1#attachments", updated_at: null },
        { kind: "repository", id: "r1", title: "octo/app", subtitle: "GITHUB", snippet: "", project_id: "p1", status: "UNVERIFIED", url: "/projects/p1#repositories", updated_at: null },
      ],
      total: 2,
      counts: { attachment: 1, repository: 1 },
      has_more: false,
      limit: 20,
      offset: 0,
      checked_at: new Date().toISOString(),
      source: "t",
    }));
    wrap(<SearchPanel />);
    await userEvent.type(screen.getByPlaceholderText(/Cari proyek/), "brief");
    expect(await screen.findByText("Tidak ada temuan (tetap DATA)")).toBeInTheDocument();
    expect(screen.getByText("Belum diverifikasi")).toBeInTheDocument();
  });
});
