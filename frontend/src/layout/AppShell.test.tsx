import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, vi } from "vitest";
import { get } from "../api/client";
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

function api(opts: { mode?: string; queue?: unknown[] } = {}) {
  return mockApi({
    "/api/health": { status: "ok", version: "0.1.0", environment: "local" },
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

describe("AppShell installs the shared event -> query invalidation (R28)", () => {
  class FakeEventSource {
    static last: FakeEventSource | null = null;
    onopen: (() => void) | null = null;
    onmessage: ((m: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;
    constructor() {
      FakeEventSource.last = this;
    }
    close() {}
    send(frame: Record<string, unknown>) {
      this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent<string>);
    }
  }

  it("a decision event refetches an inventory query mounted on any page; a heartbeat does not", async () => {
    const calls = api();
    vi.stubGlobal("EventSource", FakeEventSource);
    function Probe() {
      useQuery({ queryKey: ["inventory", "probe"], queryFn: () => get("/api/inventory/probe") });
      return <h1>Probe page</h1>;
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter>
            <Routes>
              <Route element={<AppShell />}>
                <Route index element={<Probe />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole("heading", { name: "Probe page" });
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    const probeCalls = () => calls.filter((c) => c.url.startsWith("/api/inventory/probe")).length;
    await waitFor(() => expect(probeCalls()).toBe(1));
    const es = FakeEventSource.last as FakeEventSource;
    await act(async () => {
      es.onopen?.();
      es.send({ type: "ready", cursor: 0 });
      es.send({ type: "event", seq: 1, source: "runner", kind: "run.heartbeat" });
    });
    await new Promise((r) => setTimeout(r, 700));
    expect(probeCalls()).toBe(1); // a heartbeat refreshes nothing
    await act(async () => {
      es.send({ type: "event", seq: 2, source: "policy", kind: "decision.recorded" });
    });
    await waitFor(() => expect(probeCalls()).toBe(2), { timeout: 3000 });
  });
});
