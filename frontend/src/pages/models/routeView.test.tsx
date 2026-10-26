import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ModelsPage from "./index";
import { evaluationPath, reasoningSentText } from "./RouteDrawer";
import type { RouteItem } from "./types";

// Wave 7b UI lane 2 (C1 routing): the route view states the upstream request, the provider client restriction, the
// evaluation path and whether a BUDGET decision is needed - each as text + icon + colour, never as a pass.

const authState = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: authState.isAdmin, session: null, domain: "PERSONAL", projectId: null }),
}));

const route = (over: Record<string, unknown> = {}) => ({
  route_key: "opencode-zen/deepseek-v4.1-flash-free",
  pool: "WORKER",
  provider: "opencode-zen",
  endpoint: "https://opencode.ai/inference/openai/v1",
  model_id: "opencode/deepseek-v4.1-flash-free",
  alias: null,
  display_name: "DeepSeek V4.1 Flash Free",
  resolved_version: "UNKNOWN",
  resolved_observed_at: null,
  identity_status: "PROVIDER_CLAIMED",
  is_free: "YES",
  effectively_free: true,
  free_until: null,
  expires_note: null,
  pricing: {},
  pricing_checked_at: "2026-10-18T00:00:00Z",
  data_terms: { may_use_inputs_for_training: false },
  terms_checked_at: "2026-10-18T00:00:00Z",
  eligible_domains: ["PERSONAL"],
  allowed_data_classes: ["PUBLIC"],
  context_window: null,
  supports_tools: "YES",
  status: "CANDIDATE",
  mode: "DESIGN",
  origin: "SEED",
  evidence_level: "E1",
  availability: "UNKNOWN",
  availability_checked_at: null,
  harness: "opencode",
  fallback_chain: [],
  priority: 10,
  config_hash: "sha256:" + "d".repeat(64),
  config_changed_at: null,
  eval_summary: { state: "BELUM_DIUJI", roles: {}, stale_roles: [], note: "" },
  last_used: null,
  active_decision: null,
  quota: { state: "UNKNOWN_OR_OK" },
  source_refs: ["S07"],
  notes: "",
  ...over,
});

const STATUS = {
  binary: { found: true, status: "INSTALLED", path_logical: "claude.exe (PATH)", version: "2.1.302", fingerprint: null, reasons: [] },
  auth: { status: "UNKNOWN", loggedIn: null, authMethod: null, apiProvider: null, subscriptionType: null, email: null, reasons: [] },
  bridge_status: "UNKNOWN",
  env_hygiene: { status: "OK", forbidden_present: [], running_inside_claude_session: false, note: "" },
  invocation: { argv_template: [], forbidden_flags: [], prompt_via: "stdin", workspace: "" },
  policy: { allowed_families: [], stages: {}, fallback: "NONE", mode: "DESIGN" },
  models: [],
  last_capability_test: null,
  quota: { state: "UNKNOWN", source: "UNKNOWN", observed_at: null, resets_at: null, note: "" },
  extra_usage: { state: "UNKNOWN", note: "" },
  checked_at: "2026-10-18T10:00:00Z",
  source: "claude",
  mode: "LIVE",
};

let calls: { url: string; method: string; body?: string }[] = [];

function serve(item: ReturnType<typeof route>, config: Record<string, unknown> = {}, endpointReply: unknown = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
      let body: unknown = {};
      if (url.startsWith("/api/claude/status")) body = STATUS;
      else if (url === "/api/routes") body = { items: [item], source: "model_routes", checked_at: null, catalog_refresh: null, mode: "DESIGN", counts: { total: 1, approved: 0 } };
      else if (url.endsWith("/endpoint")) body = endpointReply;
      else if (url.endsWith("/activate")) body = { decision_id: "dec-1", decision_kind: "TRIAL" };
      else if (url.startsWith("/api/routes/")) body = { ...item, config, decisions: [], quota_states: [] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
}

beforeEach(() => {
  calls = [];
  authState.isAdmin = true;
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

async function openDrawer(name: string) {
  fireEvent.click(await screen.findByText(name));
  return screen.findByRole("dialog", { name: /Detail rute/ });
}

describe("Route view (C1 routing fields)", () => {
  it("gives every evaluation path its own badge and never shows a wired path as a result", () => {
    const cases: [string, string, string][] = [
      ["EVAL_RUN", "EvalRun lewat task pekerja (jalur tersambung, bukan hasil)", "Terkonfigurasi"],
      ["EVAL_ONLY", "Khusus evaluasi: hanya task terikat EvalRun, hasil belum terbukti", "NOT-PROVEN"],
      ["PLANNING_BASELINE", "Baseline perencanaan (bukan EvalRun pekerja)", "Terkonfigurasi"],
      ["NOT_WIRED", "Evaluasi/scorecard belum tersambung", "NOT-PROVEN"],
    ];
    for (const [status, label] of cases) {
      const { unmount } = render(<>{evaluationPath(route({ evaluation_path: { status, note: `catatan ${status}` } }) as unknown as RouteItem)}</>);
      const badge = screen.getByText(label);
      expect(badge.closest("span[title]")).toHaveAttribute("title", status);
      expect(screen.getByText(`catatan ${status}`)).toBeInTheDocument();
      expect(screen.queryByText(/Lulus|Terverifikasi/)).toBeNull();
      unmount();
    }
    const { unmount } = render(<>{evaluationPath(route({ evaluation_path: { status: "SOMETHING_NEW", note: "n" } }) as unknown as RouteItem)}</>);
    expect(screen.getByText("Jalur evaluasi: SOMETHING_NEW").closest("span[title]")).toHaveAttribute("title", "SOMETHING_NEW");
    unmount();
    render(<>{evaluationPath(route() as unknown as RouteItem)}</>);
    expect(screen.getByText("Jalur evaluasi tidak diketahui")).toBeInTheDocument();
  });

  it("states the upstream API, the catalog id, the reasoning level sent and the client restriction scope", async () => {
    serve(
      route({
        pool: "ARENA",
        evaluation_path: { status: "EVAL_ONLY", note: "hanya task terikat EvalRun RUNNING" },
        budget_required: false,
        upstream_request: {
          url: "https://opencode.ai/inference/openai/v1/chat/completions",
          model: "deepseek-v4.1-flash-free",
          catalog_model_id: "opencode/deepseek-v4.1-flash-free",
          api: "openai.chat_completions",
          reasoning_effort: "high",
          code: null,
          problem: null,
          status: "READY_SHAPE",
          note: "E1",
        },
        client_restriction: {
          status: "OWNER_DECISION_REQUIRED",
          statement: "Free-tier chat models require OpenCode",
          source: "S07",
          applies_to: "broker",
          owner_note: null,
          note: "klien HTTP sendiri",
        },
      }),
    );
    renderPage();
    const drawer = await openDrawer("DeepSeek V4.1 Flash Free");
    expect(await within(drawer).findByText("openai.chat_completions")).toBeInTheDocument();
    expect(within(drawer).getByText(/ID katalog/)).toBeInTheDocument(); // catalog id differs from the bare upstream model
    expect(within(drawer).getAllByText("opencode/deepseek-v4.1-flash-free", { selector: "code" }).length).toBe(2);
    expect(within(drawer).getByText(/reasoning: high \(dari konfigurasi rute\)/)).toBeInTheDocument();
    expect(within(drawer).getByText("Berlaku untuk:")).toBeInTheDocument();
    expect(within(drawer).getByText("broker", { selector: "code" })).toBeInTheDocument();
    expect(within(drawer).getByText("Khusus evaluasi: hanya task terikat EvalRun, hasil belum terbukti")).toBeInTheDocument();
    expect(within(drawer).getByText("Tidak perlu (gratis atau LOKAL loopback)")).toBeInTheDocument();
  });

  it("labels a required BUDGET decision, a refused request with its code, and an unknown request shape", async () => {
    serve(
      route({
        is_free: "NO",
        effectively_free: false,
        budget_required: true,
        upstream_request: { status: "REFUSED", code: "ROUTE_ENDPOINT", problem: "not https", note: "ROUTE_ENDPOINT: not https" },
      }),
    );
    renderPage();
    const drawer = await openDrawer("DeepSeek V4.1 Flash Free");
    expect(await within(drawer).findByText("Wajib: keputusan BUDGET terpisah saat aktivasi")).toBeInTheDocument();
    expect(within(drawer).getByText("Ditolak broker")).toBeInTheDocument();
    expect(within(drawer).getByText("ROUTE_ENDPOINT", { selector: "code" })).toBeInTheDocument();
    vi.unstubAllGlobals();
    expect(reasoningSentText("NOT_SENT")).toBe("tidak dikirim (default provider)");
    expect(reasoningSentText(undefined)).toBe("UNKNOWN");
  });

  it("an older server without budget_required shows UNKNOWN, never 'not needed'", async () => {
    serve(route({ upstream_request: { status: "UNKNOWN", note: "Broker tidak tersedia" } }));
    renderPage();
    const drawer = await openDrawer("DeepSeek V4.1 Flash Free");
    expect(await within(drawer).findByText(/Tidak diketahui \(server lama\)/)).toBeInTheDocument();
    expect(within(drawer).getByText("Bentuk permintaan tidak diketahui")).toBeInTheDocument();
    expect(within(drawer).getByText("Broker tidak tersedia")).toBeInTheDocument();
  });

  it("an owner-accepted client restriction is stated in the activation dialog and asks for no second decision", async () => {
    serve(
      route({
        budget_required: false,
        client_restriction: {
          status: "OWNER_ACCEPTED",
          statement: "Free-tier chat models require OpenCode",
          source: "S07",
          applies_to: "broker",
          owner_note: "membaca S07; menerima risiko ditolak",
          note: "NOT-PROVEN sampai probe",
        },
      }),
    );
    renderPage();
    const drawer = await openDrawer("DeepSeek V4.1 Flash Free");
    expect(await within(drawer).findByText("Diputuskan pemilik; penerimaan provider NOT-PROVEN")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: /Aktifkan rute/ }));
    const dialog = await screen.findByRole("dialog", { name: "Aktivasi rute" });
    expect(within(dialog).getByText(/sudah Anda putuskan pada konfigurasi ini/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox", { name: /broker AEOS boleh memanggil rute ini/ })).toBeNull();
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Alasan/ }), { target: { value: "uji kelayakan rute gratis" } });
    const submit = within(dialog).getByRole("button", { name: /Catat keputusan TRIAL/ });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/activate") && c.method === "POST")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url.endsWith("/activate"))!.body!)).toMatchObject({ client_terms: null });
  });

  it("the LOCAL endpoint form shows the recorded build and announces an unchanged configuration", async () => {
    const local = route({
      route_key: "local/qwen",
      provider: "local",
      pool: "LOCAL",
      endpoint: "http://127.0.0.1:8080/v1",
      display_name: "Qwen lokal",
      budget_required: false,
    });
    serve(local, { mode_params: { quantization: "Q4_K_M", inference_backend: "llama.cpp-b5000" } }, { config_changed: false, needs_new_trial: false });
    renderPage();
    const drawer = await openDrawer("Qwen lokal");
    expect(await within(drawer).findByText(/Tercatat: Q4_K_M/)).toBeInTheDocument();
    expect(within(drawer).getByText(/Tercatat: llama\.cpp-b5000/)).toBeInTheDocument();
    expect(within(drawer).getAllByText(/Tercatat: belum dicatat\./).length).toBe(2); // model name and hardware never guessed
    fireEvent.change(within(drawer).getByRole("textbox", { name: "Alasan perubahan endpoint" }), { target: { value: "server lokal tetap sama" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Tetapkan endpoint" }));
    expect(await within(drawer).findByText(/Tidak ada perubahan: endpoint dan build sama/)).toBeInTheDocument();
    expect(JSON.parse(calls.find((c) => c.url.endsWith("/endpoint"))!.body!)).toMatchObject({
      endpoint: "http://127.0.0.1:8080/v1",
      upstream_model: null,
      quantization: null,
      inference_backend: null,
      hardware: null,
    });
  });

  it("VIEWER sees the route fields but no endpoint form or activation", async () => {
    authState.isAdmin = false;
    serve(route({ pool: "LOCAL", provider: "local", display_name: "Qwen lokal", budget_required: false }));
    renderPage();
    const drawer = await openDrawer("Qwen lokal");
    expect(await within(drawer).findByText("Tidak perlu (gratis atau LOKAL loopback)")).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Tetapkan endpoint" })).toBeNull();
    expect(within(drawer).queryByRole("button", { name: /Aktifkan rute/ })).toBeNull();
  });
});
