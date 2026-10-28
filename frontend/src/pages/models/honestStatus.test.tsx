import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { EXPIRY_REFETCH_MAX_MS, nextExpiryRefetchMs } from "./badges";
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

const ZEN_REF = "workers/opencode-zen";
const KEYED = { state: "PRESENT", ref: ZEN_REF, reason: null, domain: "PERSONAL" };
const TRIAL = route({
  route_key: "opencode-zen/trial", display_name: "Trial Model", status: "TRIAL", mode: "LIVE", priority: 99,
  active_decision: { id: "d-1", kind: "TRIAL", expires_at: "2026-11-15T00:00:00Z" }, credential: KEYED,
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

  it("shows no date for an active route without an expiry", async () => {
    const noExpiry = { ...TRIAL, active_decision: { id: "d-1", kind: "TRIAL", expires_at: null } };
    mountWith([CAND1, noExpiry]);
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 2 rute");
    expect(screen.queryByTestId("route-valid-until")).toBeNull();
  });

  // Review of dea380c: nothing resets route.status when a decision expires or is invalidated, so status TRIAL with
  // active_decision null is a route the router rejects. It must neither be counted nor drawn as active.
  it("does not show or count a TRIAL route whose decision expired (active_decision null) as Aktif", async () => {
    const lapsed = { ...TRIAL, route_key: "opencode-zen/lapsed", display_name: "Lapsed Trial", active_decision: null };
    // counts as the fixed server sends them: the lapsed route is not in approved
    mountWith([CAND1, lapsed], { total: 2, approved: 0 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 2 rute");
    expect(screen.queryByText("Aktif · TRIAL")).toBeNull();
    expect(screen.queryByTestId("route-valid-until")).toBeNull();
    const rows = screen.getAllByRole("row").slice(1);
    const row = rows.find((r) => within(r).queryByText("Lapsed Trial"))!;
    expect(within(row).getByText("TRIAL · keputusan kedaluwarsa/tidak berlaku")).toBeInTheDocument();
    expect(rows[0]).toBe(rows.find((r) => within(r).queryByText("Candidate One"))); // not sorted first
  });

  it("counts a lapsed route as not active when the API sends no counts", async () => {
    const lapsed = { ...TRIAL, display_name: "Lapsed Trial", active_decision: null };
    mountWith([CAND1, lapsed]);
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 2 rute");
    expect(screen.queryByText("Aktif · TRIAL")).toBeNull();
  });

  // Round 2: the header must count the population the table shows. The API counts include SUPERVISOR routes; the
  // table hides them. Bite fixture: 2 SUPERVISOR (one active) + 1 active TRIAL + 2 CANDIDATE -> "Aktif: 1 dari 3".
  it("counts only the rows the table shows: 2 SUPERVISOR + 1 TRIAL + 2 CANDIDATE is Aktif: 1 dari 3", async () => {
    const sup1 = route({ route_key: "claude/sup1", pool: "SUPERVISOR", display_name: "Supervisor One", status: "APPROVED", mode: "LIVE", active_decision: { id: "d-s", kind: "TRIAL", expires_at: null } });
    const sup2 = route({ route_key: "claude/sup2", pool: "SUPERVISOR", display_name: "Supervisor Two" });
    const cand2 = route({ route_key: "opencode-zen/c2", display_name: "Candidate Two", priority: 3 });
    // API counts as the server sends them: all 5 routes, 2 approved (TRIAL + the active supervisor)
    mountWith([sup1, sup2, CAND1, cand2, TRIAL], { total: 5, approved: 2 });
    const summary = await screen.findByTestId("routes-active-summary");
    expect(summary).toHaveTextContent("Aktif: 1 dari 3 rute (TRIAL/APPROVED) · sisanya katalog kandidat");
    expect(screen.getAllByRole("row").slice(1)).toHaveLength(3); // header total = table rows
  });

  it("near miss: with no SUPERVISOR route the header equals the table (Aktif: 1 dari 3)", async () => {
    const cand2 = route({ route_key: "opencode-zen/c2", display_name: "Candidate Two", priority: 3 });
    mountWith([CAND1, cand2, TRIAL], { total: 3, approved: 1 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 3 rute");
    expect(screen.getAllByRole("row").slice(1)).toHaveLength(3);
  });

  it("flags an APPROVED route without an active decision in the drawer and shows no date", async () => {
    const lapsed = route({ route_key: "opencode-zen/ap", display_name: "Lapsed Approved", status: "APPROVED", mode: "LIVE", active_decision: null });
    mountWith([lapsed], { total: 1, approved: 0 });
    fireEvent.click(await screen.findByText("Lapsed Approved"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute opencode-zen\/ap/ });
    expect(await within(drawer).findByText("APPROVED · keputusan kedaluwarsa/tidak berlaku")).toBeInTheDocument();
    expect(within(drawer).queryByTestId("route-valid-until")).toBeNull();
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

// Round 3 (incident 2026-10-28): a TRIAL route with an ACTIVE decision but no stored key is rejected by the router
// (CAN_RUN_WORKER BLOCKED) while the page drew it green, counted it and sorted it first.
describe("Models & Routes: a route the router cannot authenticate is not active (R28 round 3)", () => {
  const keyless = route({
    route_key: "opencode-zen/keyless", display_name: "Keyless Trial", status: "TRIAL", mode: "LIVE", priority: 0,
    active_decision: { id: "d-k", kind: "TRIAL", expires_at: "2026-11-15T00:00:00Z" },
    credential: { state: "MISSING", ref: ZEN_REF, reason: `credential ${ZEN_REF} not stored: aeos secrets set ${ZEN_REF}`, domain: "PERSONAL" },
  });

  it("bite: a keyless TRIAL route is not counted, warns 'kunci belum disimpan' and the drawer names the remedy", async () => {
    mountWith([keyless, CAND1], { total: 2, approved: 0 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 2 rute");
    expect(screen.queryByText("Aktif · TRIAL")).toBeNull();
    const rows = screen.getAllByRole("row").slice(1);
    const row = rows.find((r) => within(r).queryByText("Keyless Trial"))!;
    expect(within(row).getByText("Aktif, tidak dapat dipakai: kunci belum disimpan")).toBeInTheDocument();
    expect(within(row).queryByTestId("route-valid-until")).toBeNull();
    fireEvent.click(within(row).getByText("Keyless Trial"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute opencode-zen\/keyless/ });
    const remedy = await within(drawer).findByTestId("route-credential-remedy");
    expect(remedy).toHaveTextContent(`aeos secrets set ${ZEN_REF}`);
    expect(remedy).toHaveTextContent("kunci belum disimpan");
  });

  it("bite: a keyless TRIAL route is not sorted before an active one", async () => {
    mountWith([keyless, CAND1, TRIAL], { total: 3, approved: 1 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 3 rute");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("Trial Model")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Keyless Trial")).toBeInTheDocument();
  });

  it("near miss: the same route with its key stored is active", async () => {
    mountWith([{ ...keyless, credential: { ...KEYED } }, CAND1], { total: 2, approved: 1 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 2 rute");
    expect(screen.getByText("Aktif · TRIAL")).toBeInTheDocument();
    expect(screen.queryByTestId("route-credential-blocked")).toBeNull();
  });

  it("a local route that needs no key (NOT_NEEDED) is active", async () => {
    const local = { ...keyless, route_key: "local/q", pool: "LOCAL", provider: "local", display_name: "Local Trial",
      credential: { state: "NOT_NEEDED", ref: null, reason: null, domain: "PERSONAL" } };
    mountWith([local, CAND1], { total: 2, approved: 1 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 1 dari 2 rute");
    expect(screen.getByText("Aktif · TRIAL")).toBeInTheDocument();
  });

  it("bite: a sealed store (UNAVAILABLE) warns 'vault terkunci' with the unseal remedy, never active", async () => {
    const sealed = { ...keyless, display_name: "Sealed Trial", credential: { state: "UNAVAILABLE", ref: ZEN_REF,
      reason: `credential ${ZEN_REF} cannot be checked: secret store unavailable (sealed, unreachable or unknown; fail closed)`, domain: "PERSONAL" } };
    mountWith([sealed], { total: 1, approved: 0 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 1 rute");
    expect(screen.getByText("Aktif, tidak dapat dipakai: vault terkunci")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Sealed Trial"));
    const remedy = await screen.findByTestId("route-credential-remedy");
    expect(remedy).toHaveTextContent("aeos vault unseal");
  });

  it("bite: an OFFICE-only active route in a PERSONAL session is not 'tidak dapat dipakai' and offers no remedy", async () => {
    const office = { ...keyless, route_key: "opencode-zen/office", display_name: "Office Trial", eligible_domains: ["OFFICE"],
      credential: { state: "NOT_ELIGIBLE_IN_DOMAIN", ref: null, domain: "PERSONAL",
        reason: "route not eligible in PERSONAL (eligible_domains ['OFFICE']): the router never serves it to a task of this domain; its key is checked only in a session of an eligible domain" } };
    mountWith([office, CAND1], { total: 2, approved: 0 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 2 rute");
    expect(screen.getByText("TRIAL · aktif hanya di domain lain")).toBeInTheDocument();
    expect(screen.queryByTestId("route-credential-blocked")).toBeNull();
    expect(screen.queryByText(/tidak dapat dipakai/)).toBeNull();
    fireEvent.click(screen.getByText("Office Trial"));
    await screen.findByRole("dialog", { name: /Detail rute opencode-zen\/office/ });
    expect(screen.queryByTestId("route-credential-remedy")).toBeNull();
    expect(screen.queryByText(/aeos secrets set/)).toBeNull();
  });

  it("a WITHHELD project reference shows neither its name nor a remedy", async () => {
    const hidden = { ...keyless, display_name: "Project Trial", credential: { state: "WITHHELD", ref: null, domain: "PERSONAL",
      reason: "credential reference private to a project not visible in this session: name and key status withheld" } };
    mountWith([hidden], { total: 1, approved: 0 });
    expect(await screen.findByText("TRIAL · status kunci tidak ditampilkan")).toBeInTheDocument();
    expect(screen.queryByTestId("route-credential-blocked")).toBeNull();
    expect(screen.getByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 1 rute");
  });

  it("fails closed on an older server that sends no credential block", async () => {
    const { credential: _drop, ...noBlock } = TRIAL as Record<string, unknown>;
    void _drop;
    mountWith([noBlock], { total: 1, approved: 1 });
    expect(await screen.findByTestId("routes-active-summary")).toHaveTextContent("Aktif: 0 dari 1 rute");
    expect(screen.queryByText("Aktif · TRIAL")).toBeNull();
  });
});

describe("Models & Routes: header vs filters (R28 round 3)", () => {
  it("bite: with Status=CANDIDATE the registry-wide counts stay and the label adds the rows shown", async () => {
    mountWith([CAND1, FREE, TRIAL], { total: 3, approved: 1 });
    const summary = await screen.findByTestId("routes-active-summary");
    expect(summary).not.toHaveTextContent("ditampilkan"); // near miss: no filter, no suffix
    fireEvent.change(screen.getByLabelText("Filter status"), { target: { value: "CANDIDATE" } });
    expect(summary).toHaveTextContent("Aktif: 1 dari 3 rute (TRIAL/APPROVED) · sisanya katalog kandidat · ditampilkan: 2");
    expect(summary.getAttribute("title")).toContain("mengabaikan filter");
    expect(screen.getAllByRole("row").slice(1)).toHaveLength(2);
  });
});

describe("Models & Routes: one refetch at the nearest decision expiry (R28 round 3)", () => {
  it("nextExpiryRefetchMs picks the nearest future expiry and clamps to [1 s, 24 h]", () => {
    const now = Date.parse("2026-10-28T00:00:00Z");
    const at = (iso: string | null) => ({ active_decision: iso ? { id: "d", kind: "TRIAL", expires_at: iso } : null });
    expect(nextExpiryRefetchMs([at(null), at("2026-10-28T00:00:30Z"), at("2026-10-28T00:10:00Z")], now)).toBe(31_000);
    expect(nextExpiryRefetchMs([at("2026-10-27T23:00:00Z"), at(null)], now)).toBeNull(); // past or none: no timer
    expect(nextExpiryRefetchMs([at("2026-10-28T00:00:00.100Z")], now)).toBe(1_100);
    expect(nextExpiryRefetchMs([at("2026-12-01T00:00:00Z")], now)).toBe(EXPIRY_REFETCH_MAX_MS);
  });

  afterEach(() => vi.useRealTimers());

  it("bite: the timer refetches the routes at the expiry, not before; unmount clears it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-10-28T00:00:00Z"));
    const soon = { ...TRIAL, active_decision: { id: "d-1", kind: "TRIAL", expires_at: "2026-10-28T00:00:30Z" } };
    const view = mountWith([soon, CAND1], { total: 2, approved: 1 });
    await screen.findByTestId("routes-active-summary");
    const fetches = () => (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.filter((c) => c[0] === "/api/routes").length;
    const before = fetches();
    await act(async () => void (await vi.advanceTimersByTimeAsync(20_000)));
    expect(fetches()).toBe(before); // not before the expiry
    await act(async () => void (await vi.advanceTimersByTimeAsync(12_000)));
    expect(fetches()).toBe(before + 1); // exactly one refetch at expires_at (+1 s margin)
    view.unmount();
    const after = fetches();
    await act(async () => void (await vi.advanceTimersByTimeAsync(EXPIRY_REFETCH_MAX_MS)));
    expect(fetches()).toBe(after);
  });
});
