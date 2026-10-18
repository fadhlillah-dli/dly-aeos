import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    expect(statusSpec(mapStatus("scan", "CLEAN")[0]).tone).toBe("ok");
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
