import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import CostsPage from "./index";

const bucket = { actual_usd: 0, estimate_usd: 0, unknown_count: 0, entries: 0, input_tokens: 0, output_tokens: 0, requests: 0, quantity: {} };
const budget = {
  id: null,
  scope_type: "GLOBAL",
  scope_id: "*",
  period: "MONTH",
  limit_usd: 0,
  limit_tokens: null,
  limit_requests: null,
  hard_stop: true,
  status: "ACTIVE",
  is_default: true,
  implicit: true,
  state: "HARD_STOP_ZERO",
  trust_reasons: [],
  decision: null,
  spent_usd: 0,
  held_usd: 0,
  headroom_usd: 0,
  notes: "",
};

const summary = {
  source: "usage_ledger",
  checked_at: "2026-10-18T00:00:00Z",
  window: { period: "MONTH", start: "2026-10-01T00:00:00Z" },
  totals: { actual_usd: 1.5, estimate_usd: 0.25, unknown_count: 2, entries: 4, note: "Actual, estimasi dan UNKNOWN tidak dijumlahkan menjadi satu angka." },
  by_category: [
    { ...bucket, category: "WORKER_API", label: "API worker", billing: "METERED", actual_usd: 1.5 },
    { ...bucket, category: "NATIVE_CLAUDE", label: "Claude native (langganan Max)", billing: "SUBSCRIPTION", actual_usd: null, estimate_usd: null },
  ],
  by_project: [],
  by_route: [],
  native_subscription: {
    billing: "SUBSCRIPTION",
    dollar_cost: null,
    dollar_note: "Penggunaan Claude native tidak dikonversi ke dolar atau persentase kuota Max (§42).",
    remaining_quota: "UNKNOWN",
    quota_state: "UNKNOWN",
    quota_source: "no official remaining-quota source",
    quota_observed_at: null,
    requests: 3,
    input_tokens: 10,
    output_tokens: 5,
    entries: 3,
    note: "Batas langganan berlaku; sisa kuota UNKNOWN",
  },
  stop_caps: [budget],
  per_accepted_outcome: { accepted_outcomes: 0, cost_per_accepted_usd: null, status: "NO_ACCEPTED_OUTCOMES", basis: "x", note: "Belum ada accepted outcome", total_known_usd: 0, unknown_entries: 0, per_task: [] },
  excluded_non_live: { entries: 1, modes: ["MOCK"], note: "Entri MOCK tidak dihitung" },
};

afterEach(() => vi.unstubAllGlobals());

describe("Costs & Quotas page (§42)", () => {
  it("separates actual/estimate/UNKNOWN and shows the native subscription honestly", async () => {
    mockApi({
      "/api/costs/summary": summary,
      "/api/budgets": { items: [budget] },
      "/api/costs/ledger": { items: [], checked_at: "2026-10-18T00:00:00Z" },
      "/api/quotas": { items: [{ id: null, provider: "claude-native", scope: "*", state: "UNKNOWN", source: "no official remaining-quota source", observed_at: null, resets_at: null, official: false, implicit: true }], note: "n" },
    });
    renderPage(<CostsPage />);
    expect(await screen.findByText("Batas langganan berlaku; sisa kuota UNKNOWN")).toBeInTheDocument();
    expect(screen.getAllByText("$1.50").length).toBeGreaterThan(0);
    expect(screen.getAllByText("$0.2500").length).toBeGreaterThan(0);
    expect(screen.getByText("2 entri")).toBeInTheDocument();
    expect(screen.getAllByText("$0 hard stop — hanya rute gratis").length).toBeGreaterThan(0);
    expect(screen.getByText(/Entri MOCK tidak dihitung/)).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("labels MOCK ledger rows as MOCK, links the deciding Decision and offers a correction (§41, §50)", async () => {
    const decided = { ...budget, id: "b1", is_default: false, implicit: false, limit_usd: 5, state: "OK",
      decision: { id: "dec-123456789", kind: "BUDGET", actor_user_id: "owner", status: "ACTIVE", created_at: "2026-10-18T00:00:00Z", rationale: "pilot" } };
    const row = { id: "l1", at: "2026-10-18T00:00:00Z", category: "WORKER_API", provider: "openrouter", model: "m", route_key: null,
      project_id: null, run_id: null, input_tokens: 1, output_tokens: 1, requests: 1, cost_usd: 9.99, cost_kind: "MOCK",
      charged_usd: 9.99, quantity: null, unit: null, source: "fixture", mode: "MOCK" };
    mockApi({
      "/api/costs/summary": { ...summary, stop_caps: [decided] },
      "/api/budgets": { items: [decided] },
      "/api/costs/ledger": { items: [row], checked_at: "2026-10-18T00:00:00Z" },
      "/api/quotas": { items: [], note: "n" },
    });
    renderPage(<CostsPage />);
    expect(await screen.findByText("MOCK — bukan usage aktual")).toBeInTheDocument();
    const decisionLink = await screen.findByRole("link", { name: /dec-1234/ });
    expect(decisionLink).toHaveAttribute("href", "/decisions?decision=dec-123456789");
    expect(screen.getByRole("link", { name: /Usulkan koreksi/ }).getAttribute("href")).toMatch(/^\/proposals\?kind=COMPARE&source=/);
  });

  it("offers the budget form only to SUPER_ADMIN", async () => {
    mockApi(
      {
        "/api/costs/summary": summary,
        "/api/budgets": { items: [budget] },
        "/api/costs/ledger": { items: [], checked_at: null },
        "/api/quotas": { items: [], note: "n" },
      },
      "VIEWER",
    );
    renderPage(<CostsPage />);
    await screen.findByText("Batas langganan berlaku; sisa kuota UNKNOWN");
    expect(screen.queryByText("Ubah anggaran")).not.toBeInTheDocument();
    expect(screen.queryByText(/Jalankan estimator ccusage/)).not.toBeInTheDocument();
  });
});
