import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import IntegrationsPage from "./index";

const CARDS = {
  items: [
    { id: "claude-native", name: "Claude Code resmi (pengawas Max)", domain: "PERSONAL", status: "NOT_TESTED", capability: "Perencanaan", scope: "Supervisor", account: { authMethod: "claude.ai", subscriptionType: "max" }, credential_refs: [], credential_note: "Kredensial native dikelola binary resmi", expiry: null, proof_time: null, proof_source: "uji", remedies: [{ command: "aeos bridge test", why: "Uji" }] },
    { id: "secret-store", name: "OpenBao (vault runtime)", domain: null, status: "BLOCKED", capability: "Vault", scope: "ns", credential_refs: [], expiry: null, proof_time: "2026-10-18T10:00:00Z", proof_source: "health", remedies: [{ command: "aeos vault install", why: "Pasang" }, { command: "aeos vault init", why: "Init" }], warning: null },
    { id: "github", name: "GitHub (PERSONAL)", domain: "PERSONAL", status: "UNKNOWN", capability: "Broker PR", scope: "repo", credential_refs: [{ ref: "personal/github", state: "UNKNOWN" }], expiry: null, proof_time: null, proof_source: "UNKNOWN", remedies: [{ command: "gh auth token | aeos secrets set personal/github --stdin", why: "Simpan" }] },
    { id: "google", name: "Google Drive / Docs (PERSONAL)", domain: "PERSONAL", status: "MISSING", capability: "Publikasi", scope: "drive.file", credential_refs: [{ ref: "personal/google-oauth", state: "MISSING" }], expiry: null, proof_time: null, proof_source: "UNKNOWN", remedies: [{ command: "aeos publish google-connect", why: "OAuth" }] },
    { id: "openrouter", name: "OpenRouter (rute pekerja)", domain: null, status: "MISSING", capability: "Model", scope: "approved", credential_refs: [{ ref: "workers/openrouter", state: "MISSING" }], expiry: null, proof_time: null, proof_source: "UNKNOWN", remedies: [{ command: "aeos secrets set workers/openrouter", why: "Simpan" }], quota: null },
    { id: "atlassian", name: "Atlassian (OFFICE)", domain: "OFFICE", status: "BLOCKED", capability: "Office", scope: "deny", credential_refs: [{ ref: "office/atlassian", state: "HIDDEN" }], expiry: null, proof_time: null, proof_source: "UNKNOWN", remedies: [], blocked_reason: "OFFICE: external_worker_allowed=false" },
  ],
  office_hidden: true,
  checked_at: "2026-10-18T10:00:00Z",
  source: "status vault",
  mode: "LIVE",
};
const STATUS = {
  items: [{ ref: "personal/github", namespace: "personal", exists: null, state: "UNKNOWN", backend: "none", updated_at: null, expires_at: null, scope: {}, note: "", purpose: "Token GitHub", used_by: "broker", integration: "github", remedy: "aeos secrets set personal/github" }],
  backend: "none", store_error: null, reasons: ["OpenBao unreachable"], office_hidden: true, source: "none", checked_at: "2026-10-18T10:00:00Z", mode: "LIVE",
};
const HEALTH = {
  status: "BLOCKED", backend: "none",
  openbao: { addr: "127.0.0.1:8200", reachable: false, initialized: null, sealed: null, version: null, login: "NOT_OK", approle_in_keyring: false, binary_installed: false, server_pid_alive: false },
  keyring: { available: true, backend: "WinVaultKeyring", platform_store: "Windows Credential Manager" },
  interim: { active: false, decision: null, residual_risk: null },
  reasons: ["OpenBao unreachable"], remedies: [{ command: "aeos vault start", why: "Jalankan" }], checked_at: "2026-10-18T10:00:00Z", source: "health", mode: "LIVE",
  evidence_limit: "CONNECTED bukan VERIFIED",
};
// OpenBao up and logged in (CONNECTED), office namespace unbound, last functional proof is history (stale)
const STATUS_BAO = {
  ...STATUS, backend: "openbao", reasons: [],
  items: [
    { ...STATUS.items[0], state: "PRESENT", backend: "openbao", exists: true, updated_at: "2026-10-18T03:00:00Z" },
    { ref: "office/atlassian", namespace: "office", exists: null, state: "NO_ACCESS", backend: "openbao", updated_at: null, expires_at: null, scope: {}, note: "", purpose: "Token Atlassian", used_by: "publisher", integration: "atlassian", remedy: null, remedy_note: "OFFICE: kebijakan office tidak terikat" },
  ],
};
const HEALTH_BAO = {
  ...HEALTH, status: "CONNECTED", backend: "openbao", reasons: [],
  openbao: { ...HEALTH.openbao, reachable: true, initialized: true, sealed: false, version: "2.7.1", login: "OK", approle_in_keyring: true, binary_installed: true, token: { present: true, ttl_seconds: 3600, renewable: true, expires_in_seconds: 3000, policies: ["aeos-personal", "aeos-workers", "default"] } },
  functional_check: { status: "VERIFIED", checked_at: "2026-10-01T00:00:00Z", stale: true },
  remedies: [{ command: "aeos vault status --verify", why: "Buktikan" }],
};
let vault = "down";

beforeEach(() => {
  vault = "down";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const up = vault === "up";
      const body = url.startsWith("/api/secrets/integrations") ? CARDS : url.startsWith("/api/secrets/status") ? (up ? STATUS_BAO : STATUS) : up ? HEALTH_BAO : HEALTH;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <IntegrationsPage />
    </QueryClientProvider>,
  );
}

describe("Integrations & Access page", () => {
  it("renders every integration card with copyable remedies", async () => {
    renderPage();
    expect(await screen.findByText("Claude Code resmi (pengawas Max)")).toBeInTheDocument();
    for (const cmd of ["aeos vault install", "aeos secrets set workers/openrouter", "aeos publish google-connect", "aeos bridge test"]) {
      expect(screen.getAllByText(cmd).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByRole("button", { name: /Salin perintah/ }).length).toBeGreaterThan(3);
  });

  it("shows UNKNOWN (not missing) when the vault is unavailable and blocks OFFICE", async () => {
    renderPage();
    expect(await screen.findByText(/Vault tidak tersedia/)).toBeInTheDocument();
    expect(screen.getByText(/external_worker_allowed=false/)).toBeInTheDocument();
    expect(screen.getByText(/Disembunyikan di sesi ini/)).toBeInTheDocument();
    expect(screen.getAllByText(/Belum ada bukti/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Belum diuji \(aeos vault status --verify\)/)).toBeInTheDocument();
  });

  it("shows token TTL/scope, a stale functional proof as stale and NO_ACCESS without a bogus command", async () => {
    vault = "up";
    renderPage();
    expect(await screen.findByText(/TTL 60 menit, sisa 50 menit, dapat diperpanjang/)).toBeInTheDocument();
    expect(screen.getByText("aeos-personal, aeos-workers, default")).toBeInTheDocument();
    expect(screen.getByText(/Usang \(hasil terakhir VERIFIED\)/)).toBeInTheDocument();
    expect(screen.queryByText("Terverifikasi")).toBeNull(); // a past roundtrip is never shown as a current pass
    expect(await screen.findByText("Tanpa akses")).toBeInTheDocument();
    expect(screen.getByText(/kebijakan office tidak terikat/)).toBeInTheDocument();
    expect(screen.queryByText("aeos secrets set office/atlassian")).toBeNull();
  });
});
