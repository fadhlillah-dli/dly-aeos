import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import ModelsPage from "./index";

// R28: the Models page must say how many routes are ACTIVE (TRIAL/APPROVED), put them first, show their expiry only
// when the API carries one, and show the free-tier quota for a `:free` route.
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: false, session: null, domain: "PERSONAL", projectId: null }) }));

const route = (over: Record<string, unknown>) => ({
  route_key: "opencode-zen/a", pool: "WORKER", provider: "opencode-zen", endpoint: null, model_id: "opencode/a", alias: null,
  display_name: "Model A", resolved_version: "UNKNOWN", resolved_observed_at: null, identity_status: "PROVIDER_CLAIMED",
  is_free: "NO", effectively_free: false, free_until: null, expires_note: null, pricing: {}, pricing_checked_at: "2026-10-18T00:00:00Z",
  data_terms: {}, terms_checked_at: "2026-10-18T00:00:00Z", eligible_domains: ["PERSONAL"], allowed_data_classes: ["PUBLIC"],
  context_window: null, supports_tools: "YES", status: "CANDIDATE", mode: "DESIGN", origin: "SEED", evidence_level: "E1",
  availability: "UNKNOWN", availability_checked_at: null, harness: "opencode", fallback_chain: [], priority: 10,
  config_hash: "sha256:" + "c".repeat(64), config_changed_at: null,
  eval_summary: { state: "BELUM_DIUJI", roles: {}, stale_roles: [], note: "" }, last_used: null, active_decision: null,
  quota: { state: "UNKNOWN_OR_OK" }, source_refs: ["S07"], notes: "", ...over,
});

const FREE_TIER = {
  provider: "openrouter", scope: "account (shared by every :free model)", rpm_limit: 20, daily_limit: 50, daily_basis: "OWNER_SETTING",
  requests_last_minute: 1, requests_today_utc: 7, state: "OK", reason: null,
  counted: "requests this AEOS instance sent (broker and probes)", checked_at: "2026-10-28T00:00:00Z",
  decision_c: { status: "ELIGIBLE_IF_ACTIVATED", problems: [], data_classes: ["PUBLIC"], domains: ["PERSONAL"], note: "Rute gratis di pool WORKER (keputusan C)" },
};

const TRIAL = route({
  route_key: "opencode-zen/trial", display_name: "Trial Model", status: "TRIAL", mode: "LIVE", priority: 99,
  active_decision: { id: "d-1", kind: "TRIAL", expires_at: "2026-11-15T00:00:00Z" },
});
const CAND1 = route({ route_key: "opencode-zen/c1", display_name: "Candidate One", priority: 1 });
const FREE = route({ route_key: "openrouter/x:free", display_name: "Free Candidate", priority: 2, is_free: "YES", effectively_free: true, free_tier: FREE_TIER });

const STATUS = {
  binary: { found: true, status: "CONFIGURED", path_logical: "claude (PATH)", version: "2.1.302", fingerprint: "sha256:x", reasons: [] },
  auth: { status: "VERIFIED", loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max", email: "owner@example.com", reasons: [] },
  bridge_status: "CONFIGURED",
  env_hygiene: { status: "OK", forbidden_present: [], running_inside_claude_session: false, note: "" },
  invocation: { argv_template: [], forbidden_flags: ["--bare"], prompt_via: "stdin", workspace: "" },
  policy: { allowed_families: ["opus", "fable"], stages: {}, fallback: "NONE", mode: "DESIGN" },
  models: [],
  last_capability_test: null,
  quota: { state: "UNKNOWN", source: "UNKNOWN", observed_at: null, resets_at: null, note: "Sisa kuota Max tidak tersedia secara resmi" },
  extra_usage: { state: "UNKNOWN", note: "manual" },
  checked_at: "2026-10-18T10:00:00Z",
  source: "claude",
  mode: "LIVE",
};

function mountWith(items: unknown[], counts?: { total: number; approved: number }) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      let body: unknown = {};
      if (url === "/api/routes") body = { items, source: "model_routes", checked_at: null, catalog_refresh: null, mode: "DESIGN", counts };
      else if (url.startsWith("/api/claude/status")) body = STATUS;
      else if (url.startsWith("/api/routes/")) {
        const key = decodeURIComponent(url.slice("/api/routes/".length));
        const it = (items as { route_key: string }[]).find((i) => i.route_key === key);
        body = { ...it, config: {}, decisions: [], quota_states: [] };
      }
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Models & Routes: honest active status (R28)", () => {
  it("says Aktif: 1 for 1 TRIAL + 2 CANDIDATE, and lists the TRIAL row first with its expiry", async () => {
    mountWith([CAND1, FREE, TRIAL], { total: 3, approved: 1 });
    const summary = await screen.findByTestId("routes-active-summary");
    expect(summary).toHaveTextContent("Aktif: 1 dari 3 rute (TRIAL/APPROVED) · sisanya katalog kandidat");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("Trial Model")).toBeInTheDocument(); // active first, although its priority is last
    expect(within(rows[0]).getByText("Aktif · TRIAL")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("route-valid-until")).toHaveTextContent(/^s\/d .*2026/);
    expect(within(rows[1]).getByText("Candidate One")).toBeInTheDocument(); // catalogue rows keep the API order
    expect(within(rows[1]).getByText("Katalog, belum diaktifkan")).toBeInTheDocument();
    expect(within(rows[1]).queryByTestId("route-valid-until")).toBeNull(); // never an invented date
  });

  it("says Aktif: 0 without error when nothing is active", async () => {
    mountWith([CAND1, FREE], { total: 2, approved: 0 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 2 rute (TRIAL/APPROVED) · sisanya katalog kandidat");
    expect(screen.queryByText(/Aktif · TRIAL/)).toBeNull();
    expect(screen.queryByTestId("route-valid-until")).toBeNull();
  });

  it("falls back to the list when an older server sends no counts, and shows no date for an active route without one", async () => {
    const noExpiry = { ...TRIAL, active_decision: { id: "d-1", kind: "TRIAL", expires_at: null } };
    mountWith([CAND1, noExpiry]);
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 2 rute");
    expect(screen.queryByTestId("route-valid-until")).toBeNull();
  });

  it("shows a compact free-tier badge for a :free route only", async () => {
    mountWith([CAND1, FREE], { total: 2, approved: 0 });
    await screen.findByTestId("routes-active-summary");
    const badges = screen.getAllByTestId("free-tier-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveTextContent("Gratis: 7/50 hari ini");
  });

  it("renders the free-tier block in the drawer for a free-tier route and not for a route without free_tier", async () => {
    mountWith([CAND1, FREE], { total: 2, approved: 0 });
    fireEvent.click(await screen.findByText("Free Candidate"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute openrouter\/x:free/ });
    const block = await within(drawer).findByTestId("free-tier-block");
    expect(within(block).getByTestId("free-tier-usage")).toHaveTextContent("Hari ini (UTC): 7 dari 50 permintaan");
    expect(within(block).getByTestId("free-tier-decision-c")).toHaveTextContent("Memenuhi syarat bila diaktifkan");
    fireEvent.click(within(drawer).getByRole("button", { name: "Tutup" }));

    fireEvent.click(await screen.findByText("Candidate One"));
    const drawer2 = await screen.findByRole("dialog", { name: /Detail rute opencode-zen\/c1/ });
    await within(drawer2).findByText(/Entri desain/);
    expect(within(drawer2).queryByTestId("free-tier-block")).toBeNull();
  });

  it("flags a free-tier limit that is reached", async () => {
    const hit = { ...FREE, free_tier: { ...FREE_TIER, state: "LIMIT_REACHED", requests_today_utc: 50, reason: "daily cap reached" } };
    mountWith([hit], { total: 1, approved: 0 });
    fireEvent.click(await screen.findByText("Free Candidate"));
    const block = await screen.findByTestId("free-tier-block");
    expect(within(block).getAllByText("Batas harian tercapai").length).toBeGreaterThan(0);
  });
});
