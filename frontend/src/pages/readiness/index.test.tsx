import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: false, domain: "PERSONAL", session: null, projectId: null }),
}));

import ReadinessPage from "./index";

const checks = {
  items: [
    {
      check_id: "runtime.db",
      title: "Database dan status migrasi",
      group: "RUNTIME",
      group_label: "Runtime aplikasi",
      component_id: "sqlite-metadata-store",
      component_version: null,
      scope: {},
      target_identity: null,
      status: "UNKNOWN",
      effective_status: "UNKNOWN",
      stale_reason: "belum pernah diperiksa",
      severity: "LOW",
      summary: "Belum pernah diperiksa.",
      observed_evidence: {},
      last_checked_at: null,
      expires_at: null,
      invalidators: [],
      fingerprint: null,
      error_code: null,
      remediation: {},
      requires_destructive: false,
      requires_paid_write: false,
      never_run: true,
      functional_only: false,
      network: false,
      critical_for: ["CAN_OPEN_DASHBOARD"],
      component_link: "/tools/sqlite-metadata-store",
    },
  ],
  groups: [{ id: "RUNTIME", label: "Runtime aplikasi", order: 2 }],
  last_run: null,
  source: "Readiness Doctor",
  checked_at: null,
  mode: "LIVE",
  status_legend: { CONNECTED: "probe koneksi berhasil; bukan verifikasi izin/keamanan" },
};

const caps = {
  items: [
    {
      capability: "CAN_OPEN_DASHBOARD",
      label: "Membuka dashboard lokal",
      description: "",
      domain: null,
      status: "UNKNOWN",
      reasons: ["runtime.db: UNKNOWN - belum pernah diperiksa"],
      checks: [],
      remedies: [],
    },
  ],
  human_only: [{ label: "Merge ke main", status: "HUMAN_ONLY", note: "" }],
  source: "Capability gates",
  checked_at: null,
  mode: "LIVE",
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("capabilities") ? caps : checks;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("Readiness page", () => {
  it("shows the capability matrix first, unknown is not green, viewer cannot run checks", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <ReadinessPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Membuka dashboard lokal")).toBeInTheDocument();
    expect(screen.getByText(/runtime.db: UNKNOWN/)).toBeInTheDocument();
    expect(screen.getByText(/Doctor belum pernah dijalankan/)).toBeInTheDocument();
    expect(screen.getByText("Merge ke main")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Jalankan pemeriksaan/ })).not.toBeInTheDocument();
    expect(screen.getByText("VIEWER: hanya baca")).toBeInTheDocument();
    expect(screen.getAllByText("Tidak diketahui").length).toBeGreaterThan(0);
  });
});
