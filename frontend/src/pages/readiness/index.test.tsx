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
      ok_statuses: ["VERIFIED"],
      component_link: "/tools/sqlite-metadata-store",
    },
    {
      check_id: "policy.mci",
      title: "Referensi MCI kanonik",
      group: "CANONICAL_POLICY",
      group_label: "Canonical policy",
      component_id: null,
      component_version: "2",
      scope: {},
      target_identity: null,
      status: "CONFIGURED",
      effective_status: "CONFIGURED",
      stale_reason: null,
      severity: "INFO",
      summary: "Referensi MCI tercatat; keselarasan isi tetap UNVERIFIED oleh Doctor.",
      observed_evidence: { alignment: "UNVERIFIED" },
      last_checked_at: "2026-10-18T00:00:00+00:00",
      expires_at: "2026-10-19T00:00:00+00:00",
      invalidators: [],
      fingerprint: null,
      error_code: null,
      remediation: {},
      requires_destructive: false,
      requires_paid_write: false,
      never_run: false,
      functional_only: false,
      network: false,
      critical_for: [],
      ok_statuses: [],
      component_link: null,
    },
  ],
  groups: [
    { id: "RUNTIME", label: "Runtime aplikasi", order: 2 },
    { id: "CANONICAL_POLICY", label: "Canonical policy", order: 4 },
  ],
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

let checksBody: unknown = checks;
let capsBody: unknown = caps;

beforeEach(() => {
  checksBody = checks;
  capsBody = caps;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("capabilities") ? capsBody : checksBody;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ReadinessPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Readiness page", () => {
  it("shows the startup run, the live sandbox gate and residual risks without turning them green", async () => {
    checksBody = {
      ...checks,
      last_run: {
        id: "r1", started_at: "2026-10-18T01:00:00+00:00", finished_at: "2026-10-18T01:00:05+00:00",
        trigger: "startup", functional: false, groups: [], capability: null, counts: { VERIFIED: 27, MISSING: 18 },
      },
    };
    capsBody = {
      ...caps,
      items: [
        {
          capability: "CAN_RUN_WORKER",
          label: "Menjalankan worker dalam sandbox",
          description: "",
          domain: null,
          status: "READY",
          reasons: ["catatan: worker_runtime.sandbox_readiness: process sandbox WEAK diizinkan keputusan pengecualian"],
          checks: [
            { check_id: "worker_runtime.sandbox_readiness", title: "Syarat sandbox worker (live, ADR-0051)", tier: "critical",
              status: "READY", effective_status: "VERIFIED", ok: true, last_checked_at: null, component_id: "worker-runtime",
              live: true },
          ],
          remedies: [],
          residual_risks: ["Tidak ada penegakan egress jaringan: proses worker dapat menghubungi host selain broker.",
                           "Penulisan di luar worktree tidak dicegah OS."],
        },
      ],
    };
    renderPage();
    expect(await screen.findByText("Menjalankan worker dalam sandbox")).toBeInTheDocument();
    expect(screen.getByTestId("last-run")).toHaveTextContent("otomatis saat server mulai");
    expect(screen.getByText(/Syarat sandbox worker \(live, ADR-0051\)/)).toBeInTheDocument();
    const risks = screen.getByTestId("residual-risks");
    expect(risks).toHaveTextContent("Risiko residual (2)");
    expect(risks).toHaveTextContent("bukan tanda lulus");
    expect(risks).toHaveTextContent(/egress/);
  });

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
    // CONFIGURED is not a pass for a check whose ok-set excludes it (MCI alignment is never automatic)
    expect(screen.getByTestId("not-ok-policy.mci")).toHaveTextContent(/belum memenuhi syarat/);
  });
});
