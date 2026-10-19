import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ModelsPage from "./index";

const authState = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: authState.isAdmin, session: null, domain: "PERSONAL", projectId: null }),
}));

const route = (over: Record<string, unknown>) => ({
  route_key: "opencode-zen/deepseek-v4.1-flash-free",
  pool: "WORKER",
  provider: "opencode-zen",
  endpoint: "https://opencode.ai/zen",
  model_id: "opencode/deepseek-v4.1-flash-free",
  alias: null,
  display_name: "DeepSeek V4.1 Flash Free",
  resolved_version: "UNKNOWN",
  resolved_observed_at: null,
  identity_status: "PROVIDER_CLAIMED",
  is_free: "YES",
  effectively_free: true,
  free_until: null,
  expires_note: "free for a limited time per S07",
  pricing: {},
  pricing_checked_at: "2026-10-18T00:00:00Z",
  data_terms: { may_use_inputs_for_training: false },
  terms_checked_at: "2026-10-18T00:00:00Z",
  eligible_domains: ["PERSONAL"],
  allowed_data_classes: ["PUBLIC", "INTERNAL"],
  context_window: null,
  supports_tools: "YES",
  status: "CANDIDATE",
  mode: "DESIGN",
  origin: "SEED",
  evidence_level: "E1",
  availability: "UNKNOWN",
  availability_checked_at: null,
  harness: "opencode",
  fallback_chain: ["opencode-zen/glm-5.3-flash-free"],
  priority: 10,
  config_hash: "sha256:" + "c".repeat(64),
  config_changed_at: null,
  eval_summary: { state: "BELUM_DIUJI", roles: {}, stale_roles: [], note: "" },
  last_used: null,
  active_decision: null,
  quota: { state: "UNKNOWN_OR_OK" },
  source_refs: ["S07"],
  notes: "Pilot priority",
  ...over,
});

const PAID = route({ route_key: "opencode-zen/qwen3.8-flash", model_id: "opencode/qwen3.8-flash", display_name: "Qwen3.8 Flash", pool: "WATCHLIST", is_free: "NO", effectively_free: false, fallback_chain: [] });

const STATUS = {
  binary: { found: true, status: "CONFIGURED", path_logical: "claude.exe (PATH)", version: "2.1.302", fingerprint: "sha256:x", reasons: [] },
  auth: { status: "VERIFIED", loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max", email: "owner@example.com", reasons: [] },
  bridge_status: "CONFIGURED",
  env_hygiene: { status: "OK", forbidden_present: [], running_inside_claude_session: false, note: "" },
  invocation: { argv_template: [], forbidden_flags: ["--bare"], prompt_via: "stdin", workspace: "" },
  policy: { allowed_families: ["opus", "fable"], stages: { "planning.primary": "opus" }, fallback: "NONE", mode: "DESIGN" },
  models: [{ route_key: "claude-native/fable", alias: "fable", model_id: "claude-fable-5-1", resolved_version: "UNKNOWN", resolved_observed_at: null, status: "CANDIDATE", mode: "DESIGN" }],
  last_capability_test: null,
  quota: { state: "UNKNOWN", source: "UNKNOWN", observed_at: null, resets_at: null, note: "Sisa kuota Max tidak tersedia secara resmi" },
  extra_usage: { state: "UNKNOWN", note: "manual" },
  checked_at: "2026-10-18T10:00:00Z",
  source: "claude",
  mode: "LIVE",
};

const DS_CONFIG = { harness: "opencode", harness_version: null, prompt_revision: null, policy_version: null, context_strategy: null, mode_params: {}, tool_set: ["opencode-default"], region: null };
const decision = (over: Record<string, unknown>) => ({
  id: "d-1", kind: "TRIAL", status: "ACTIVE", target_hash: "sha256:" + "c".repeat(64), matches_current_config: true,
  effective: false, actor_kind: "HUMAN", created_at: "2026-10-01T00:00:00Z", expires_at: "2026-10-10T00:00:00Z", rationale: "pilot", ...over,
});
// an expired ACTIVE decision bound to the current hash, and one recorded by an agent: neither authorises the route
const DS_DECISIONS = [decision({}), decision({ id: "d-2", actor_kind: "AGENT", expires_at: null })];

const ROUTES = { items: [route({}), PAID], source: "model_routes", checked_at: null, catalog_refresh: null, mode: "DESIGN", counts: { total: 2, approved: 0 } };

let calls: { url: string; method: string; body?: string }[] = [];

beforeEach(() => {
  calls = [];
  authState.isAdmin = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
      let body: unknown = {};
      if (url.startsWith("/api/claude/status")) body = STATUS;
      else if (url === "/api/routes") body = ROUTES;
      else if (url.startsWith("/api/claude/capability-test")) body = { status: "VERIFIED", model_resolved: "claude-opus-5-5", steps: [] };
      else if (url.startsWith("/api/routes/opencode-zen/qwen3.8-flash")) body = { ...PAID, config: {}, decisions: [], quota_states: [] };
      else if (url.startsWith("/api/routes/")) body = { ...route({}), config: DS_CONFIG, decisions: DS_DECISIONS, quota_states: [] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("Models & Routes page", () => {
  it("shows native Claude status and unknown quota, never as pass", async () => {
    renderPage();
    expect(await screen.findByText("claude.exe (PATH)")).toBeInTheDocument();
    expect(screen.getByText("2.1.302")).toBeInTheDocument();
    expect(screen.getByText(/claude\.ai/)).toBeInTheDocument();
    expect(screen.getByText(/Sisa kuota Max tidak tersedia/)).toBeInTheDocument();
    expect(screen.getByText(/Belum pernah diuji/)).toBeInTheDocument();
  });

  it("lists registry rows as DESIGN, UNKNOWN version and BELUM DIUJI", async () => {
    renderPage();
    expect(await screen.findByText("opencode/deepseek-v4.1-flash-free")).toBeInTheDocument();
    expect(screen.getAllByText("BELUM DIUJI").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Berbayar").length).toBeGreaterThan(0);
    expect(screen.getByText(/Tidak ada rute yang disetujui oleh seed/)).toBeInTheDocument();
  });

  it("runs the capability test for admins only", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Uji kemampuan/ }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/claude/capability-test" && c.method === "POST")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url === "/api/claude/capability-test")!.body!)).toEqual({ model: "opus" });
  });

  it("hides admin actions for VIEWER", async () => {
    authState.isAdmin = false;
    renderPage();
    await screen.findByText("claude.exe (PATH)");
    expect(screen.queryByRole("button", { name: /Uji kemampuan/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Perbarui katalog/ })).toBeNull();
  });

  it("never shows an expired or non-human decision as approving, and unknown identity fields as UNKNOWN", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("DeepSeek V4.1 Flash Free"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText(/kedaluwarsa$/)).toBeInTheDocument();
    expect(within(drawer).getByText("bukan keputusan manusia")).toBeInTheDocument();
    expect(within(drawer).getAllByText(/\(tidak berlaku\)/).length).toBe(2);
    expect(within(drawer).queryByText("Disetujui")).toBeNull();
    expect(within(drawer).getByText("opencode · versi UNKNOWN")).toBeInTheDocument();
    expect(within(drawer).getByText("UNKNOWN · UNKNOWN")).toBeInTheDocument(); // prompt revision / policy version
  });

  it("activation dialog states the decision and requires a budget decision for paid routes", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("Qwen3.8 Flash"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText("sha256:" + "c".repeat(64))).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: /Aktifkan rute/ }));
    const dialog = await screen.findByRole("dialog", { name: "Aktivasi rute" });
    expect(within(dialog).getByText(/Keputusan yang akan dicatat/)).toBeInTheDocument();
    expect(within(dialog).getByText("ID keputusan BUDGET")).toBeInTheDocument();
    expect(within(dialog).getByText(/tidak ada fallback berbayar otomatis/)).toBeInTheDocument();
    const submit = within(dialog).getByRole("button", { name: /Catat keputusan/ });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Alasan/ }), { target: { value: "uji rute berbayar dengan anggaran" } });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByPlaceholderText("uuid keputusan BUDGET"), { target: { value: "budget-1" } });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/activate") && c.method === "POST")).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.url.endsWith("/activate"))!.body!);
    expect(sent).toMatchObject({ mode: "TRIAL", budget_decision_id: "budget-1", expected_config_hash: "sha256:" + "c".repeat(64) });
  });

  it("an APPROVED (production) activation cannot be sent without EvalRun evidence (§23 telah diuji)", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("DeepSeek V4.1 Flash Free"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    fireEvent.click(await within(drawer).findByRole("button", { name: /Aktifkan rute/ }));
    const dialog = await screen.findByRole("dialog", { name: "Aktivasi rute" });
    fireEvent.change(within(dialog).getByRole("combobox"), { target: { value: "APPROVED" } });
    expect(within(dialog).getByText(/telah diuji/)).toBeInTheDocument();
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Alasan/ }), { target: { value: "lulus evaluasi held-out" } });
    const submit = within(dialog).getByRole("button", { name: /Catat keputusan ROUTE_ACTIVATION/ });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByPlaceholderText("uuid EvalRun"), { target: { value: "run-1, run-2" } });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/activate") && c.method === "POST")).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.url.endsWith("/activate"))!.body!);
    expect(sent).toMatchObject({ mode: "APPROVED", eval_run_ids: ["run-1", "run-2"] });
  });

  it("shows a changed configuration as needing evaluation and an unwired arena route honestly", async () => {
    const changed = route({
      eval_summary: { state: "NEEDS_EVALUATION", roles: {}, stale_roles: ["feature-implementer"], note: "Konfigurasi berubah; hasil lama tidak diwariskan" },
      execution_path: { status: "NOT_WIRED", note: "broker hanya melayani route WORKER" },
      runtime_binding: { bound: {}, current: {}, status: "DRIFT", drift: ["policy_version changed since activation"] },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, method: init?.method ?? "GET" });
        let body: unknown = {};
        if (url.startsWith("/api/claude/status")) body = STATUS;
        else if (url === "/api/routes") body = { ...ROUTES, items: [changed] };
        else if (url.startsWith("/api/routes/")) body = { ...changed, config: DS_CONFIG, decisions: [], quota_states: [] };
        return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByText("DeepSeek V4.1 Flash Free"));
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText("Perlu evaluasi ulang")).toBeInTheDocument();
    expect(within(drawer).getByText(/policy_version changed since activation/)).toBeInTheDocument();
    expect(within(drawer).getByText("Belum tersambung")).toBeInTheDocument();
  });
});
