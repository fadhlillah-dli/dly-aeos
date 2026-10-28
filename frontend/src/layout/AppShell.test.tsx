import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, vi } from "vitest";
import { AuthProvider } from "../auth/AuthContext";
import { mockApi } from "../components/proposals/testUtils";
import { THEME_STORAGE_KEY } from "../lib/theme";
import { AppShell } from "./AppShell";

const overview = (mode: string) => ({
  generated_at: new Date().toISOString(),
  domain: "PERSONAL",
  environment: "local",
  project: null,
  mode,
  contains_mock: mode !== "LIVE",
  mock_rows: { events: 4, worker_runs: 4 },
  status_counts: {},
  source: "test",
  evidence_limit: "",
  sections: {},
});

function api(opts: { mode?: string; queue?: unknown[]; health?: Record<string, unknown> } = {}) {
  return mockApi({
    "/api/health": { status: "ok", version: "0.1.0", environment: "local", ...opts.health },
    "/api/control-center/overview": overview(opts.mode ?? "LIVE"),
    "/api/decisions/queue": { items: opts.queue ?? [], counts: {}, checked_at: new Date().toISOString(), mode: "LIVE" },
    "/api/projects": { items: [{ id: "p1", name: "DLY AEOS", project_key: "dly-aeos", domain: "PERSONAL" }] },
    "/api/search": {
      items: [{ kind: "project", id: "p1", title: "DLY AEOS", subtitle: "dly-aeos", snippet: "", project_id: "p1", status: "ACTIVE", url: "/projects/p1" }],
      total: 1, counts: { project: 1 }, has_more: false, domain: "PERSONAL", source: "sqlite-like (metadata only, active domain)",
    },
  });
}

function renderShell(route = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<h1>Ringkasan</h1>} />
              <Route path="tools" element={<h1>Tools page</h1>} />
              <Route path="projects/:id" element={<h1>Project page</h1>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  try {
    window.localStorage.removeItem(THEME_STORAGE_KEY);
  } catch {
    /* storage blocked */
  }
  delete document.documentElement.dataset.theme;
});

describe("AppShell header (§35: domain, project, environment, freshness, LIVE/MOCK)", () => {
  it("shows environment and an honest MIXED mode instead of a hard-coded LIVE", async () => {
    api({ mode: "MIXED" });
    renderShell();
    const header = await screen.findByRole("banner");
    await waitFor(() => expect(within(header).getByTestId("data-mode")).toHaveTextContent("LIVE + MOCK/DEMO"));
    expect(within(header).getByTestId("environment")).toHaveTextContent("local");
    // Without a live stream the header never claims fresh data.
    expect(within(header).getByTestId("freshness")).toHaveTextContent(/kesegaran tidak diketahui|terputus/);
    expect(within(header).getByRole("button", { name: /PERSONAL/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("highlights the pending human decision count from /api/decisions/queue", async () => {
    api({ queue: [{ kind: "CHANGE_PROPOSAL" }, { kind: "PLAN_APPROVAL" }] });
    renderShell();
    const badge = await screen.findByTestId("nav-decisions-count");
    expect(badge.textContent).toMatch(/^2/);
  });

  it("shows no decision badge when the queue is empty", async () => {
    const calls = api({ queue: [] });
    renderShell();
    await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/decisions/queue"))).toBe(true));
    expect(screen.queryByTestId("nav-decisions-count")).not.toBeInTheDocument();
  });
});

describe("mobile navigation drawer", () => {
  it("opens from the menu button, closes on Escape and after navigating", async () => {
    api();
    renderShell();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Buka menu navigasi" }));
    const drawer = screen.getByRole("dialog", { name: "Navigasi" });
    expect(within(drawer).getByRole("link", { name: "Tools & Repos" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Navigasi" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Buka menu navigasi" }));
    await user.click(within(screen.getByRole("dialog", { name: "Navigasi" })).getByRole("link", { name: "Tools & Repos" }));
    expect(await screen.findByRole("heading", { name: "Tools page" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Navigasi" })).not.toBeInTheDocument();
  });
});

describe("global metadata search (GET /api/search)", () => {
  it("queries the domain-scoped search endpoint and links to the record", async () => {
    const calls = api();
    renderShell();
    const user = userEvent.setup();
    const box = await screen.findByRole("searchbox", { name: "Cari metadata" });
    // Disabled until the human-chosen domain is known (domain-first, U-05).
    await waitFor(() => expect(box).toBeEnabled());
    await user.type(box, "DLY{Enter}");
    const hit = await screen.findByRole("link", { name: /DLY AEOS/ });
    expect(calls.some((c) => c.url.startsWith("/api/search?q=DLY"))).toBe(true);
    expect(screen.getByText(/1 hasil metadata di domain PERSONAL/)).toBeInTheDocument();
    await user.click(hit);
    expect(await screen.findByRole("heading", { name: "Project page" })).toBeInTheDocument();
  });
});

describe("theme toggle", () => {
  it("switches data-theme and persists the choice in localStorage", async () => {
    api();
    renderShell();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Tema terang aktif/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(screen.getByRole("button", { name: /^Tema gelap aktif/ })).toBeInTheDocument();
  });
});

describe("freshness banner (R28: the page or the server is out of date)", () => {
  const TREE_A = "a".repeat(40);
  const TREE_B = "b".repeat(40);
  const health = (o: { served?: string | null; drift?: string; ui?: string; judged?: string | null } = {}) => {
    const served = o.served === undefined ? TREE_A : o.served;
    return {
      ui_build: { status: o.ui ?? "FRESH", reason: null, bundle_frontend_tree: o.judged === undefined ? served : o.judged },
      freshness: {
        served_bundle: { frontend_tree: served },
        code_drift: { status: o.drift ?? "CURRENT", loaded_commit: "0123456789abcdef", head_commit: "fedcba9876543210" },
        checked_at: new Date().toISOString(),
      },
    };
  };

  async function healthLoaded(calls: { url: string }[]) {
    await waitFor(() => expect(calls.some((c) => c.url === "/api/health")).toBe(true));
    await screen.findByTestId("environment");
    await waitFor(() => expect(screen.getByTestId("environment")).toHaveTextContent("local"));
  }

  it("stays hidden when the bundle, the served bundle and the server code are current", async () => {
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", TREE_A);
    const calls = api({ health: health() });
    renderShell();
    await healthLoaded(calls);
    expect(screen.queryByTestId("freshness-banner")).not.toBeInTheDocument();
  });

  it("asks for a reload when the bundle in the browser differs from the one the server serves", async () => {
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", TREE_A);
    api({ health: health({ served: TREE_B }) });
    renderShell();
    const banner = await screen.findByTestId("freshness-banner");
    expect(banner).toHaveTextContent("Tampilan lebih lama dari server: muat ulang halaman");
    expect(within(banner).getByRole("button", { name: "Muat ulang" })).toBeInTheDocument();
    expect(banner).not.toHaveTextContent("restart");
  });

  it("asks for a restart when the server runs older backend code", async () => {
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", TREE_A);
    api({ health: health({ drift: "DRIFT" }) });
    renderShell();
    const banner = await screen.findByTestId("freshness-banner");
    expect(banner).toHaveTextContent("Server menjalankan kode lama (0123456789ab): restart ./aeos serve");
    expect(within(banner).queryByRole("button", { name: "Muat ulang" })).not.toBeInTheDocument();
  });

  it("says when the server serves a STALE bundle (the page is the bundle ui_build judged)", async () => {
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", TREE_A);
    api({ health: health({ ui: "STALE" }) });
    renderShell();
    expect(await screen.findByTestId("freshness-banner")).toHaveTextContent("menyajikan bundle tampilan usang (STALE)");
  });

  it("shows no STALE notice in the Vite dev server (no tree baked in)", async () => {
    const calls = api({ health: health({ ui: "STALE" }) });
    renderShell();
    await healthLoaded(calls);
    expect(screen.queryByTestId("freshness-banner")).not.toBeInTheDocument();
  });

  it("shows no STALE notice after a manual rebuild while serving (served tree differs from the judged one)", async () => {
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", TREE_A);
    const calls = api({ health: health({ ui: "STALE", served: TREE_A, judged: TREE_B }) });
    renderShell();
    await healthLoaded(calls);
    expect(screen.queryByTestId("freshness-banner")).not.toBeInTheDocument();
  });

  it("compares nothing in dev mode (no tree baked in) and with an older server without the block", async () => {
    const calls = api({ health: { ...health({ served: TREE_B }), freshness: undefined } });
    renderShell();
    await healthLoaded(calls);
    expect(screen.queryByTestId("freshness-banner")).not.toBeInTheDocument();
  });
});
