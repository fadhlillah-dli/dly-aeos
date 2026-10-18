import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { DeliveryPanel, ProviderRiskBanner } from "./DeliveryPanel";
import { EvidenceLedger } from "./EvidenceLedger";
import { GatesPanel } from "./GatesPanel";
import { RunStatusPanel } from "./RunStatusPanel";
import type { ProviderCaps } from "./api";

const auth = { isAdmin: true, domain: "PERSONAL", projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

function respond(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (!key) return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "nf" } }), { status: 404 });
    return new Response(JSON.stringify(routes[key]), { status: 200 });
  }) as typeof fetch;
}

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const summaryRow = {
  run_id: "run-1", task_id: "task-1", task_key: "t1", task_title: "Fitur kalkulator", attempt: 1,
  execution_status: "REVIEW", head_commit: "a".repeat(40), mode: "LIVE",
  worker_reported: { status: "DONE", exit_code: 0, trust: "UNTRUSTED_CLAIM" },
  verifier: { status: "FAIL", evidence_id: "ev-1" },
  reviewer: { verdict: null, status: null },
  accepted: { status: null, valid: false },
  merged: { status: null },
};

describe("Evidence & Decisions page", () => {
  beforeEach(() => {
    auth.isAdmin = true;
  });

  it("keeps worker-reported, verifier, reviewer, accepted and merged separate; failed verifier is not green (§35, §50)", async () => {
    respond({ "/api/verification/summary": { items: [summaryRow], source: "s", checked_at: null, mode: "LIVE" } });
    wrap(<RunStatusPanel onSelectTask={() => {}} />);
    const row = (await screen.findByText("Fitur kalkulator")).closest("tr")!;
    const cells = within(row);
    expect(cells.getByText("DONE")).toBeInTheDocument();
    expect(cells.getByText("Klaim (tidak dihitung)")).toBeInTheDocument();
    expect(cells.getByText("Gagal")).toBeInTheDocument();
    expect(cells.getByText("Belum direview")).toBeInTheDocument();
    expect(cells.getByText("Belum diputuskan")).toBeInTheDocument();
    expect(cells.queryByText("Diterima")).not.toBeInTheDocument();
    // review is only possible after a verifier PASS
    expect(cells.getByRole("button", { name: /Minta review/ })).toBeDisabled();
  });

  it("hides admin-only actions for VIEWER (backend still enforces)", async () => {
    auth.isAdmin = false;
    respond({ "/api/verification/summary": { items: [summaryRow], source: "s", checked_at: null, mode: "LIVE" } });
    wrap(<RunStatusPanel onSelectTask={() => {}} />);
    await screen.findByText("Fitur kalkulator");
    expect(screen.queryByRole("button", { name: /Verifikasi/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gate" })).toBeInTheDocument();
  });

  it("empty ledger says what is missing and the exact remedy command", async () => {
    respond({ "/api/evidence": { items: [], source: "s", checked_at: new Date().toISOString(), mode: "LIVE", level_explanations: {}, trusted_collectors: [] } });
    wrap(<EvidenceLedger />);
    expect(await screen.findByText("Belum ada bukti untuk filter ini")).toBeInTheDocument();
    expect(screen.getByText("aeos verify run <run_id>")).toBeInTheDocument();
  });

  it("ledger shows level, trust and collector and opens the redacted detail drawer", async () => {
    const item = {
      id: "ev-9", project_id: "p", domain: "PERSONAL", subject_type: "run", subject_id: "run-1", run_id: "run-1",
      task_id: "task-1", claim: "all tests pass", kind: "TEST_REPORT", origin: "worker", collector_identity: "opencode",
      trusted: false, content_hash: "sha256:" + "1".repeat(64), content_type: "text/plain", classification: "INTERNAL",
      has_content: true, size_bytes: 9, evidence_level: "E0", level_explanation: "Klaim", level_requested: "E2",
      level_reason: "origin 'worker' is not an independent collector", environment: {}, created_at: new Date().toISOString(), mode: "LIVE",
    };
    respond({
      "/api/evidence/ev-9": { ...item, inline: {}, integrity: "VERIFIED", content: "token=[REDACTED]", content_note: null },
      "/api/evidence": { items: [item], source: "s", checked_at: new Date().toISOString(), mode: "LIVE", level_explanations: {}, trusted_collectors: [] },
    });
    wrap(<EvidenceLedger />);
    const cell = await screen.findByText("all tests pass");
    expect(screen.getByText("Tidak tepercaya (input)")).toBeInTheDocument();
    expect(screen.getAllByText("E0").length).toBeGreaterThan(0);
    await userEvent.click(cell);
    const dialog = await screen.findByRole("dialog", { name: "Detail bukti" });
    expect(await within(dialog).findByText("token=[REDACTED]")).toBeInTheDocument();
    expect(within(dialog).getByText("Isi cocok dengan hash")).toBeInTheDocument();
    expect(within(dialog).getByText(/tidak pernah dihitung sebagai kelulusan/)).toBeInTheDocument();
  });

  it("shows the residual-risk banner when branch protection is not enforceable (§31)", () => {
    const caps: ProviderCaps = {
      provider: "GITHUB", repo: "o/r", branch: "main", checked_at: new Date().toISOString(), credential: "AVAILABLE",
      visibility: "private", plan: "free", checks: [], required_status_checks: [], merge_gate: "NOT_ENFORCEABLE",
      enforcement_tested: "NOT-PROVEN", residual_risks: [], source: "s", mode: "LIVE",
    };
    render(<ProviderRiskBanner caps={caps} />);
    expect(screen.getByText("Branch protection tidak dapat ditegakkan pada paket GitHub ini — merge tetap tindakan manusia")).toBeInTheDocument();
  });

  it("delivery panel without a registered repository explains the remedy", async () => {
    respond({ "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE", merge_authority: "HUMAN_ONLY" } });
    wrap(<DeliveryPanel />);
    expect(await screen.findByText("Belum ada repository GitHub terdaftar di domain ini")).toBeInTheDocument();
    expect(screen.getByText("Belum ada PR")).toBeInTheDocument();
  });

  it("gates never render NOT_PROVEN/PENDING as pass and merge stays human", async () => {
    const gate = (g: string, status: string) => ({ gate: g, name: g, status, summary: `${g} summary`, source: "x", evidence_refs: [], reasons: [] });
    respond({
      "/api/delivery/gates": {
        task_id: "task-1", task_key: "t1", title: "Fitur", run_id: "run-1", head_commit: "a".repeat(40),
        gates: [gate("G0", "PASS"), gate("G1", "NOT_PROVEN"), gate("G2", "PASS"), gate("G3", "NOT_APPLICABLE"), gate("G4", "PENDING"),
          gate("G5", "PENDING"), gate("G6", "NOT_APPLICABLE"), gate("G7", "PENDING"), gate("G8", "NOT_APPLICABLE")],
        separate_status: summaryRow, acceptance: null, pull_request: null, ready_for_human_merge: false,
        merge_authority: "HUMAN_ONLY", checked_at: new Date().toISOString(), mode: "LIVE",
      },
    });
    wrap(<GatesPanel taskId="task-1" />);
    const g1 = (await screen.findByText("G1 summary")).closest("tr")!;
    expect(within(g1).getByText("Belum terbukti")).toBeInTheDocument();
    expect(within(g1).queryByText("Lulus")).not.toBeInTheDocument();
    expect(screen.getAllByText("Lulus")).toHaveLength(2);
    expect(screen.getByText(/DLY AEOS tidak memiliki operasi merge atau deploy/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Siapkan PR/ })).toBeDisabled();
  });
});
