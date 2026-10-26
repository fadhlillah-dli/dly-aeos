import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { TargetView } from "./deliveryTypes";
import type { IntegrationView, ProjectView } from "./types";

/**
 * W7b UI lane 1 (cluster B + E1): deployment-target checks editor, read-only observation, the owner's environment
 * decisions, credential references, health_path clearing and the project test baseline. Bodies are compared with the
 * exact backend keys (delivery router TargetChecksIn, POST /api/decisions, portfolio integrations).
 */

const auth = { domain: "PERSONAL", isAdmin: true, projectId: null, session: null, selectContext: vi.fn() };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

type Handler = (url: string, method: string, body: unknown) => unknown;
let calls: { url: string; method: string; body: unknown }[] = [];
function mockFetch(handler: Handler) {
  calls = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    return new Response(JSON.stringify(handler(url, method, body) ?? {}), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
const sent = (method: string, prefix: string) => calls.filter((c) => c.method === method && c.url.startsWith(prefix)).map((c) => c.body as Record<string, unknown>);

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  auth.isAdmin = true;
  auth.domain = "PERSONAL";
});

const project = { id: "p1", domain: "PERSONAL", repositories: [] } as unknown as ProjectView;

function integration(over: Partial<IntegrationView>): IntegrationView {
  return {
    id: "i-stg", project_id: "p1", domain: "PERSONAL", provider: "deployment-target", purpose: "staging", tenant_id: null,
    root_id: "https://staging.proyek.example", root_id_name: "DEPLOYMENT_TARGET_URL", credential_ref: null, config: {},
    capability_status: { configuration: "CONFIGURED", credential: { status: "MISSING" } }, last_verified_at: null,
    updated_at: "2026-10-25T08:00:00Z", target_url: null, ...over,
  } as IntegrationView;
}

function target(over: Partial<TargetView> = {}): TargetView {
  return {
    integration_id: "i-stg", project_id: "p1", domain: "PERSONAL", environment: "staging",
    settings: {
      integration_id: "i-stg", base_url: "https://staging.proyek.example", health_path: "/api/health",
      smoke_checks: [{ name: "status", path: "/api/status", expect_status: 200, json_keys: ["ok"], kind: "smoke" }],
      sli_checks: [{ name: "p95", path: "/metrics", json_key: "latency.p95_ms", max: null, min: null }],
      rollback_on: [], rollback_criteria: "UNDECIDED", require_artifact_digest: false,
    },
    config_problem: null, target_config_hash: "sha256:" + "a".repeat(64),
    latest_observation: { status: "STALE", evidence_id: "ev-obs", reason: "target or its checks changed since this observation; observe again", commit: "1234567890abcdef", failure_conditions: [], observed_at: new Date().toISOString() },
    observe_only: true, ...over,
  };
}

describe("checksBody (PUT /api/delivery/targets/{id}/checks)", () => {
  it("sends every key with owner thresholds as numbers or null and rollback_on in the backend order", async () => {
    const { checksBody } = await import("./TargetChecksEditor");
    const { body, problems } = checksBody({
      health_path: "",
      smoke: [{ name: "", path: "/api/status", expect_status: "200", json_keys: "ok, data.items", kind: "critical_flow" }],
      sli: [{ name: "p95", path: "/metrics", json_key: "latency.p95_ms", min: "", max: "250" }],
      rollback_on: ["sli_breach", "health_fail"],
      require_artifact_digest: true,
    });
    expect(problems).toEqual([]);
    expect(body).toEqual({
      health_path: "",
      smoke_checks: [{ path: "/api/status", expect_status: 200, json_keys: ["ok", "data.items"], kind: "critical_flow" }],
      sli_checks: [{ name: "p95", path: "/metrics", json_key: "latency.p95_ms", min: null, max: 250 }],
      rollback_on: ["health_fail", "sli_breach"],
      require_artifact_digest: true,
    });
  });

  it("refuses paths that leave the target, bad JSON keys, min above max and duplicate names", async () => {
    const { checksBody } = await import("./TargetChecksEditor");
    const { problems } = checksBody({
      health_path: "//evil.example/health",
      smoke: [
        { name: "", path: "/a/../b", expect_status: "700", json_keys: "bad key!", kind: "smoke" },
        { name: "x", path: "/x", expect_status: "200", json_keys: "", kind: "smoke" },
        { name: "x", path: "/y", expect_status: "200", json_keys: "", kind: "smoke" },
      ],
      sli: [{ name: "", path: "metrics", json_key: "", min: "5", max: "1" }],
      rollback_on: [],
      require_artifact_digest: false,
    });
    const text = problems.join(" | ");
    expect(text).toMatch(/Path health/);
    expect(text).toMatch(/Smoke check 1: path/);
    expect(text).toMatch(/Smoke check 1: status HTTP/);
    expect(text).toMatch(/Smoke check 1: kunci JSON/);
    expect(text).toMatch(/Nama smoke check harus unik/);
    expect(text).toMatch(/SLI 1: path/);
    expect(text).toMatch(/SLI 1: kunci JSON/);
    expect(text).toMatch(/SLI 1: min di atas max/);
  });

  it("an unnamed check whose backend default name would be invalid asks for a name", async () => {
    const { checksBody } = await import("./TargetChecksEditor");
    const { problems } = checksBody({
      health_path: "",
      // the backend names an unnamed check after its path (".well-known-health") or json_key ("_p95"): both refused
      smoke: [{ name: "", path: "/.well-known/health", expect_status: "200", json_keys: "", kind: "smoke" }],
      sli: [{ name: "", path: "/metrics", json_key: "_p95", min: "", max: "" }],
      rollback_on: [],
      require_artifact_digest: false,
    });
    expect(problems.join(" | ")).toMatch(/Smoke check 1: path ini tidak menghasilkan nama yang valid/);
    expect(problems.join(" | ")).toMatch(/SLI 1: kunci ini tidak menjadi nama yang valid/);
    const named = checksBody({
      health_path: "",
      smoke: [{ name: "well-known", path: "/.well-known/health", expect_status: "200", json_keys: "", kind: "smoke" }],
      sli: [{ name: "p95", path: "/metrics", json_key: "_p95", min: "", max: "" }],
      rollback_on: [],
      require_artifact_digest: false,
    });
    expect(named.problems).toEqual([]);
  });
});

describe("deployment targets panel (G6/G8, observe-only)", () => {
  function routes(t: TargetView, extra: Handler = () => undefined): Handler {
    return (url, method, body) => {
      const x = extra(url, method, body);
      if (x !== undefined) return x;
      if (url.startsWith("/api/delivery/targets?")) return { items: [t], source: "s", checked_at: null, mode: "LIVE" };
      if (url.startsWith("/api/decisions?")) return { items: [] };
      if (url.startsWith("/api/delivery/targets/") && method === "PUT") return { ...t, settings: { ...t.settings, ...(body as object) } };
      return {};
    };
  }

  async function panel(t: TargetView, extra?: Handler, integrations = [integration({})]) {
    mockFetch(routes(t, extra));
    const { DeploymentTargetsPanel } = await import("./DeploymentTargetsPanel");
    wrap(<DeploymentTargetsPanel project={project} integrations={integrations} isAdmin={auth.isAdmin} />);
    return screen.findByRole("region", { name: "Lingkungan Staging" });
  }

  it("shows the target's checks, an SLI without owner threshold as report-only and undecided rollback criteria", async () => {
    const stg = await panel(target());
    expect(await within(stg).findByText("/api/health")).toBeInTheDocument();
    expect(within(stg).getByText("Tanpa ambang pemilik (hanya laporan)")).toBeInTheDocument();
    expect(within(stg).getAllByText("Belum diputuskan pemilik").length).toBeGreaterThan(0);
    // a stale observation is never shown as a pass
    expect(within(stg).getByText("Usang — perlu dicek ulang")).toBeInTheDocument();
    expect(within(stg).queryByText("Lulus")).not.toBeInTheDocument();
  });

  it("the checks editor PUTs health_path, checks, owner thresholds, rollback_on and the digest rule", async () => {
    const stg = await panel(target());
    await userEvent.click(await within(stg).findByRole("button", { name: /Ubah pemeriksaan target/ }));
    const form = within(stg).getByRole("form", { name: "Pemeriksaan target staging" });
    await userEvent.click(within(form).getByRole("button", { name: /Tambah smoke check/ }));
    const second = within(form).getByRole("group", { name: "Smoke check 2" });
    fireEvent.change(within(second).getByLabelText("Path"), { target: { value: "/checkout" } });
    fireEvent.change(within(second).getByLabelText("Jenis"), { target: { value: "critical_flow" } });
    const sli = within(form).getByRole("group", { name: "SLI 1" });
    fireEvent.change(within(sli).getByLabelText("Max (pemilik)"), { target: { value: "300" } });
    await userEvent.click(within(form).getByLabelText(/Alur kritis gagal/));
    await userEvent.click(within(form).getByLabelText(/Wajibkan digest artefak/));
    await userEvent.click(within(form).getByRole("button", { name: /Simpan pemeriksaan staging/ }));
    await waitFor(() => expect(sent("PUT", "/api/delivery/targets/i-stg/checks")).toHaveLength(1));
    expect(sent("PUT", "/api/delivery/targets/i-stg/checks")[0]).toEqual({
      health_path: "/api/health",
      smoke_checks: [
        { name: "status", path: "/api/status", expect_status: 200, json_keys: ["ok"], kind: "smoke" },
        { path: "/checkout", expect_status: 200, json_keys: [], kind: "critical_flow" },
      ],
      sli_checks: [{ name: "p95", path: "/metrics", json_key: "latency.p95_ms", min: null, max: 300 }],
      rollback_on: ["critical_flow_fail"],
      require_artifact_digest: true,
    });
  });

  it("an invalid check path keeps the editor from saving and says why (aria-live)", async () => {
    const stg = await panel(target());
    await userEvent.click(await within(stg).findByRole("button", { name: /Ubah pemeriksaan target/ }));
    const form = within(stg).getByRole("form", { name: "Pemeriksaan target staging" });
    fireEvent.change(within(form).getByLabelText(/Path health\/version/), { target: { value: "@other.host/health" } });
    expect(within(form).getByLabelText(/Path health\/version/)).toHaveAttribute("aria-invalid", "true");
    expect(within(form).getByRole("button", { name: /Simpan pemeriksaan staging/ })).toBeDisabled();
    expect(within(form).getByText(/Path health harus path absolut/)).toBeInTheDocument();
  });

  it("observes the target read-only and shows parts, digest and the human post-release response", async () => {
    const result = {
      status: "FAIL", reason: "target serves commit 1234567890ab and reports healthy; checks not passing: status", evidence_id: "ev-o2",
      target: "https://staging.proyek.example/api/health", environment: "production",
      checks: [{ name: "status", kind: "smoke", path: "/api/status", status: "FAIL", reason: "HTTP 500, expected 200", http_status: 500, evidence_id: "ev-c1" }],
      slis: [{ name: "p95", status: "NO_TARGET", reason: "no owner threshold recorded", value: 120, evidence_id: "ev-s1" }],
      artifact: { status: "NOT_RECORDED", reported_digest: "sha256:" + "9".repeat(64), recorded_digests: [] },
      failure_conditions: ["smoke_fail"],
      post_release: { alert_id: "al-1", incident_id: "inc-1", decision_request_id: "dr-1", recommendation: { status: "UNDECIDED", summary: "Kriteria rollback belum ditetapkan pemilik" }, runbook: "docs/runbooks/post-release-failure.md" },
    };
    const prod = target({ integration_id: "i-prd", environment: "production", settings: { ...target().settings, integration_id: "i-prd", base_url: "https://app.proyek.example" } });
    mockFetch((url, method) => {
      if (url.startsWith("/api/delivery/targets/observe") && method === "POST") return result;
      if (url.startsWith("/api/delivery/targets?")) return { items: [prod] };
      if (url.startsWith("/api/decisions?")) return { items: [] };
      return {};
    });
    const { DeploymentTargetsPanel } = await import("./DeploymentTargetsPanel");
    wrap(<DeploymentTargetsPanel project={project} integrations={[integration({ id: "i-prd", purpose: "production", root_id: "https://app.proyek.example" })]} isAdmin />);
    const region = await screen.findByRole("region", { name: "Lingkungan Produksi" });
    const sha = await within(region).findByLabelText(/Commit yang seharusnya berjalan/);
    fireEvent.change(sha, { target: { value: "xyz" } });
    expect(within(region).getByRole("button", { name: /Observasi target/ })).toBeDisabled();
    fireEvent.change(sha, { target: { value: "1234567890abcdef" } });
    await userEvent.click(within(region).getByRole("button", { name: /Observasi target/ }));
    await waitFor(() => expect(sent("POST", "/api/delivery/targets/observe")).toHaveLength(1));
    expect(sent("POST", "/api/delivery/targets/observe")[0]).toEqual({ project_id: "p1", environment: "production", sha: "1234567890abcdef" });
    const out = await within(region).findByTestId("observe-result");
    expect(within(out).getByText("HTTP 500, expected 200")).toBeInTheDocument();
    expect(within(out).getAllByText("Smoke check gagal").length).toBeGreaterThan(0);
    expect(within(out).getByText("Digest CI belum tercatat")).toBeInTheDocument();
    expect(within(out).getByText("Belum diputuskan pemilik")).toBeInTheDocument();
    expect(within(out).getByText(/AEOS tidak pernah deploy atau rollback/)).toBeInTheDocument();
    expect(within(out).getByRole("link", { name: "Antrean keputusan" })).toHaveAttribute("href", "/decisions");
  });

  it("VIEWER sees targets and decisions read-only: no observe, edit or decision form", async () => {
    auth.isAdmin = false;
    const stg = await panel(target());
    await within(stg).findByText("/api/health");
    expect(within(stg).queryByRole("button", { name: /Observasi target|Ubah pemeriksaan|Tinjau keputusan/ })).not.toBeInTheDocument();
    expect(within(stg).getByText(/peran VIEWER hanya-baca/)).toBeInTheDocument();
  });
});

describe("environment owner decisions (POST /api/decisions, kind POLICY)", () => {
  async function form(env: "staging" | "production", targetUrl: string | null, items: unknown[] = []) {
    mockFetch((url, method, body) => (method === "POST" ? { id: "d-new", ...(body as object) } : url.startsWith("/api/decisions?") ? { items } : {}));
    const { EnvironmentDecisionForm } = await import("../decisions/EnvironmentDecisionForm");
    wrap(<EnvironmentDecisionForm projectId="p1" environment={env} targetUrl={targetUrl} isAdmin={auth.isAdmin} domain="PERSONAL" />);
    return screen.findByTestId(`env-decision-${env}`);
  }

  it("authorises staging deploys to the exact registered target only after an explicit confirmation", async () => {
    const box = await form("staging", "https://staging.proyek.example/");
    expect(await within(box).findByText(/Belum ada keputusan aktif/)).toBeInTheDocument();
    const review = within(box).getByRole("button", { name: "Tinjau keputusan" });
    expect(review).toBeDisabled();
    await userEvent.type(within(box).getByLabelText(/Rationale/), "staging dipakai untuk uji G6");
    await userEvent.click(review);
    const confirm = within(box).getByRole("group", { name: "Konfirmasi keputusan staging" });
    expect(within(confirm).getByText("staging=AUTHORISED, target=https://staging.proyek.example")).toBeInTheDocument();
    expect(within(confirm).getByText(/tanpa kedaluwarsa/)).toBeInTheDocument();
    expect(sent("POST", "/api/decisions")).toHaveLength(0);
    await userEvent.click(within(confirm).getByRole("button", { name: /Konfirmasi & catat keputusan/ }));
    await waitFor(() => expect(sent("POST", "/api/decisions")).toHaveLength(1));
    expect(sent("POST", "/api/decisions")[0]).toEqual({
      kind: "POLICY",
      target_type: "staging_environment",
      target_id: "p1",
      project_id: "p1",
      auth_scope: { staging: "AUTHORISED", target: "https://staging.proyek.example" },
      allowed_operations: [],
      rationale: "staging dipakai untuk uji G6",
    });
    expect(await within(box).findByText(/Keputusan tercatat/)).toBeInTheDocument();
  });

  it("without a registered target only staging=NONE can be decided; production offers production=NONE", async () => {
    const box = await form("staging", null);
    const authorised = within(box).getByRole("radio", { name: /Deploy staging/ });
    expect(authorised).toBeDisabled();
    expect(within(box).getByRole("radio", { name: /tanpa lingkungan staging/ })).toBeChecked();
    await userEvent.type(within(box).getByLabelText(/Rationale/), "proyek ini tanpa staging");
    await userEvent.click(within(box).getByRole("button", { name: "Tinjau keputusan" }));
    await userEvent.click(within(box).getByRole("button", { name: /Konfirmasi & catat keputusan/ }));
    await waitFor(() => expect(sent("POST", "/api/decisions")).toHaveLength(1));
    expect(sent("POST", "/api/decisions")[0]).toMatchObject({ target_type: "staging_environment", auth_scope: { staging: "NONE" } });
  });

  it("production=NONE and the active decision with its scope", async () => {
    const active = { id: "d1", kind: "POLICY", status: "ACTIVE", target_type: "production_environment", target_id: "p1", auth_scope: { production: "NONE" }, actor: "owner", actor_kind: "HUMAN", domain: "PERSONAL", created_at: "2026-10-20T10:00:00Z", expires_at: null };
    const box = await form("production", null, [active]);
    expect(await within(box).findByText("production=NONE")).toBeInTheDocument();
    expect(within(box).getByText("Aktif")).toBeInTheDocument();
    expect(within(box).queryByText(/tidak menghitungnya/)).not.toBeInTheDocument();
    await userEvent.type(within(box).getByLabelText(/Rationale/), "tidak ada produksi");
    await userEvent.click(within(box).getByRole("button", { name: "Tinjau keputusan" }));
    await userEvent.click(within(box).getByRole("button", { name: /Konfirmasi & catat keputusan/ }));
    await waitFor(() => expect(sent("POST", "/api/decisions")).toHaveLength(1));
    expect(sent("POST", "/api/decisions")[0]).toMatchObject({ kind: "POLICY", target_type: "production_environment", target_id: "p1", auth_scope: { production: "NONE" } });
  });

  it("warns when the active staging authorisation names another target than the registered one", async () => {
    const active = { id: "d2", kind: "POLICY", status: "ACTIVE", target_type: "staging_environment", target_id: "p1", auth_scope: { staging: "AUTHORISED", target: "https://old.proyek.example" }, actor: "owner", actor_kind: "HUMAN", domain: "PERSONAL", created_at: "2026-10-20T10:00:00Z" };
    const box = await form("staging", "https://staging.proyek.example", [active]);
    expect(await within(box).findByText(/mengotorisasi target lain/)).toBeInTheDocument();
  });

  it("an active decision the gate does not count (not HUMAN, or another domain) is stated as such", async () => {
    const active = { id: "d3", kind: "POLICY", status: "ACTIVE", target_type: "staging_environment", target_id: "p1", auth_scope: { staging: "NONE" }, actor: "system", actor_kind: "SYSTEM", domain: "PERSONAL", created_at: "2026-10-20T10:00:00Z" };
    const box = await form("staging", null, [active]);
    expect(await within(box).findByText(/tidak menghitungnya/)).toBeInTheDocument();
  });

  it("an edit after the review closes the confirmation, so only the reviewed binding can be recorded", async () => {
    const box = await form("staging", "https://staging.proyek.example");
    await userEvent.type(await within(box).findByLabelText(/Rationale/), "staging dipakai untuk uji G6");
    await userEvent.click(within(box).getByRole("button", { name: "Tinjau keputusan" }));
    expect(within(box).getByRole("group", { name: "Konfirmasi keputusan staging" })).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("radio", { name: /tanpa lingkungan staging/ }));
    expect(within(box).queryByRole("group", { name: "Konfirmasi keputusan staging" })).not.toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "Tinjau keputusan" }));
    expect(within(box).getByText("staging=NONE")).toBeInTheDocument();
    expect(sent("POST", "/api/decisions")).toHaveLength(0);
  });
});

describe("integration settings: credential references and health_path clearing", () => {
  function mockIntegrations(items: IntegrationView[]) {
    return mockFetch((url, method, body) => {
      if (method === "PUT") return { integration: body };
      if (url.startsWith("/api/projects/p1/integrations"))
        return { items, allowed_providers: { "deployment-target": ["production", "staging"], "github-issues": ["issues"] }, checked_at: new Date().toISOString(), mode: "LIVE" };
      if (url.startsWith("/api/delivery/targets?")) return { items: [] };
      if (url.startsWith("/api/decisions?")) return { items: [] };
      return {};
    });
  }

  it("sends migration/runtime credential references (never values) and refuses a value-like entry", async () => {
    mockIntegrations([]);
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    wrap(<IntegrationsPanel project={project} isAdmin />);
    const form = await screen.findByRole("form", { name: /Staging — Deployment target/ });
    fireEvent.change(within(form).getByLabelText(/URL dasar target/), { target: { value: "https://staging.proyek.example" } });
    const migration = within(form).getByLabelText(/Referensi credential migrasi/);
    fireEvent.change(migration, { target: { value: "postgres://user:pw@db" } });
    const save = within(form).getByRole("button", { name: /Simpan Deployment target \(Staging\)/ });
    expect(save).toBeDisabled();
    expect(migration).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(migration, { target: { value: "projects/p1/db-migrator" } });
    fireEvent.change(within(form).getByLabelText(/Referensi credential runtime/), { target: { value: "projects/p1/db-app" } });
    await userEvent.click(save);
    await waitFor(() => expect(sent("PUT", "/api/projects/p1/integrations")).toHaveLength(1));
    expect(sent("PUT", "/api/projects/p1/integrations")[0]).toEqual({
      provider: "deployment-target",
      purpose: "staging",
      root_id: "https://staging.proyek.example",
      config: { migration_credential_ref: "projects/p1/db-migrator", runtime_credential_ref: "projects/p1/db-app" },
    });
  });

  it("emptying a stored health_path sends \"\" (an omitted key would keep it); the stored checks are not resent", async () => {
    mockIntegrations([
      integration({ config: { health_path: "/api/health", smoke_checks: [{ name: "s", path: "/s" }], rollback_on: ["health_fail"], require_artifact_digest: true, root_id_name: "DEPLOYMENT_TARGET_URL" } }),
    ]);
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    wrap(<IntegrationsPanel project={project} isAdmin />);
    const form = await screen.findByRole("form", { name: /Staging — Deployment target/ });
    const hp = within(form).getByLabelText(/Path health/);
    expect(hp).toHaveValue("/api/health");
    fireEvent.change(hp, { target: { value: "" } });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Deployment target \(Staging\)/ }));
    await waitFor(() => expect(sent("PUT", "/api/projects/p1/integrations")).toHaveLength(1));
    expect(sent("PUT", "/api/projects/p1/integrations")[0]).toEqual({
      provider: "deployment-target",
      purpose: "staging",
      root_id: "https://staging.proyek.example",
      config: { health_path: "", require_artifact_digest: true },
    });
  });

  it("the generic form offers health_path for a deployment target and clears it explicitly", async () => {
    mockIntegrations([integration({ config: { health_path: "/api/health" } })]);
    const { IntegrationsPanel } = await import("./IntegrationsPanel");
    wrap(<IntegrationsPanel project={project} isAdmin />);
    await screen.findByText("Atur integrasi lain");
    const form = screen.getByRole("button", { name: "Simpan konfigurasi" }).closest("form") as HTMLFormElement;
    fireEvent.change(within(form).getByLabelText(/^Provider/), { target: { value: "deployment-target" } });
    fireEvent.change(within(form).getByLabelText(/^Tujuan/), { target: { value: "staging" } });
    const hp = await within(form).findByLabelText(/Path health\/version/);
    await waitFor(() => expect(hp).toHaveValue("/api/health"));
    fireEvent.change(hp, { target: { value: "/a/../b" } });
    expect(within(form).getByRole("button", { name: "Simpan konfigurasi" })).toBeDisabled();
    fireEvent.change(hp, { target: { value: "" } });
    await userEvent.click(within(form).getByRole("button", { name: "Simpan konfigurasi" }));
    await waitFor(() => expect(sent("PUT", "/api/projects/p1/integrations")).toHaveLength(1));
    expect(sent("PUT", "/api/projects/p1/integrations")[0]).toMatchObject({ provider: "deployment-target", purpose: "staging", config: { health_path: "" } });
  });
});

describe("project test baseline (§05 step 6)", () => {
  const baseline = {
    status: "FAIL", commit: "a".repeat(40), evidence_id: "ev-bl", evidence_level: "E2", checked_at: new Date().toISOString(),
    suite_identity: { selection: ["tests"], files: 12, files_hash: "sha256:" + "b".repeat(64) }, passed: 40, failed: 2, skipped: 1,
    failing_sample: ["tests/test_api.py::test_login"], javascript: { status: "NOT_RUN", passed: null, failed: null }, runner: "process", isolation: "WEAK",
  };

  it("shows status, counts, failing sample and the evidence link; the admin action asks for confirmation", async () => {
    mockFetch((url, method) =>
      url === "/api/projects/p1/test-baseline" ? (method === "POST" ? { ...baseline, status: "PASS", integrity: "PASS", failed: 0, failing_sample: [] } : baseline) : {},
    );
    const { TestBaselinePanel } = await import("./TestBaselinePanel");
    wrap(<TestBaselinePanel projectId="p1" isAdmin />);
    const facts = await screen.findByTestId("test-baseline");
    expect(within(facts).getByText("Gagal")).toBeInTheDocument();
    expect(within(facts).getByText("40 lulus · 2 gagal · 1 dilewati")).toBeInTheDocument();
    expect(within(facts).getByText("tests/test_api.py::test_login")).toBeInTheDocument();
    expect(within(facts).getByRole("link", { name: /E2/ })).toHaveAttribute("href", "/evidence?id=ev-bl");
    await userEvent.click(screen.getByRole("button", { name: /Jalankan baseline tes/ }));
    expect(sent("POST", "/api/projects/p1/test-baseline")).toHaveLength(0);
    await userEvent.click(within(screen.getByRole("group", { name: "Konfirmasi baseline tes" })).getByRole("button", { name: /Konfirmasi & jalankan/ }));
    await waitFor(() => expect(sent("POST", "/api/projects/p1/test-baseline")).toHaveLength(1));
    expect(await screen.findByText("Baseline tercatat: PASS.")).toBeInTheDocument();
  });

  it("a PASS whose tamper window did not prove integrity is never shown as a pass", async () => {
    mockFetch((url, method) =>
      url === "/api/projects/p1/test-baseline"
        ? method === "POST"
          ? { ...baseline, status: "PASS", integrity: "UNKNOWN", failed: 0, failing_sample: [] }
          : { ...baseline, status: "PASS", integrity: "UNKNOWN", failed: 0, failing_sample: [] }
        : {},
    );
    const { TestBaselinePanel } = await import("./TestBaselinePanel");
    wrap(<TestBaselinePanel projectId="p1" isAdmin />);
    const facts = await screen.findByTestId("test-baseline");
    expect(within(facts).getByText("PASS, integritas UNKNOWN (bukan lulus terbukti)")).toBeInTheDocument();
    expect(within(facts).getByText("Tidak terbukti (UNKNOWN)")).toBeInTheDocument();
    expect(within(facts).queryByText("Lulus")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Jalankan baseline tes/ }));
    await userEvent.click(within(screen.getByRole("group", { name: "Konfirmasi baseline tes" })).getByRole("button", { name: /Konfirmasi & jalankan/ }));
    expect(await screen.findByText("Baseline tercatat: PASS, integritas UNKNOWN (bukan lulus terbukti).")).toBeInTheDocument();
  });

  it("a PASS with a proven tamper window reads as a pass; an older server without the field is not proven", async () => {
    const { BaselineFacts } = await import("./TestBaselinePanel");
    const { unmount } = wrap(<BaselineFacts b={{ ...baseline, status: "PASS", integrity: "PASS", failed: 0, failing_sample: [] }} />);
    expect(screen.getByText("Tidak ada perubahan terdeteksi di jendela tamper")).toBeInTheDocument();
    expect(screen.queryByText(/bukan lulus terbukti/)).not.toBeInTheDocument();
    unmount();
    wrap(<BaselineFacts b={{ ...baseline, status: "PASS", failed: 0, failing_sample: [] }} />);
    expect(screen.getByText("PASS, integritas tidak dinyatakan (bukan lulus terbukti)")).toBeInTheDocument();
    expect(screen.getByText("Tidak dinyatakan server (tidak terbukti)")).toBeInTheDocument();
  });

  it("without an inventoried commit the admin run action is disabled and says why", async () => {
    mockFetch(() => ({ status: "NOT_RUN", commit: null, reason: "inventory has no commit" }));
    const { TestBaselinePanel } = await import("./TestBaselinePanel");
    wrap(<TestBaselinePanel projectId="p1" isAdmin />);
    expect(await screen.findByText(/Inventaris repository dulu/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Jalankan baseline tes/ })).toBeDisabled();
  });

  it("NOT_RUN states the reason; VIEWER has no run action", async () => {
    auth.isAdmin = false;
    mockFetch(() => ({ status: "NOT_RUN", commit: "c".repeat(40), reason: "the project's tests have not been run at this commit" }));
    const { TestBaselinePanel } = await import("./TestBaselinePanel");
    wrap(<TestBaselinePanel projectId="p1" isAdmin={false} />);
    expect(await screen.findByText("Baseline belum dijalankan")).toBeInTheDocument();
    expect(screen.getByText(/have not been run at this commit/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Jalankan baseline tes/ })).not.toBeInTheDocument();
  });

  it("the inventory facts cite tests.baseline instead of a fixed 'not run' label", async () => {
    const { InventoryFacts } = await import("./InventoryFacts");
    const inv = {
      commit: "a".repeat(40), branch: "main", checked_at: new Date().toISOString(), source: "s", files: 10, total_bytes: 100, truncated: false,
      working_tree: { dirty: false }, languages: [], dependency_manifests: [], lockfiles: [],
      tests: { directories: ["tests"], files_sample: [], frameworks: ["pytest"], baseline },
      ci: {}, deployment: {}, contracts: {}, docs: {}, symlinks: [], submodules: [], sensitive_paths: [], coverage_gaps: [], inventory_hash: "h", evidence_level: "E1",
    };
    wrap(<InventoryFacts inv={inv as never} />);
    expect(screen.getByText("40 lulus · 2 gagal · 1 dilewati")).toBeInTheDocument();
    expect(screen.queryByText("Baseline belum dijalankan")).not.toBeInTheDocument();
  });
});
