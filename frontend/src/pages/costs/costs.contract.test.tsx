import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import budgets from "./__fixtures__/budgets.json";
import ledger from "./__fixtures__/ledger.json";
import quotas from "./__fixtures__/quotas.json";
import summary from "./__fixtures__/summary.json";
import CostsPage from "./index";

// Real backend payloads (generated through the FastAPI app) guard the API <-> UI contract.
afterEach(() => vi.unstubAllGlobals());

describe("Costs page renders real API payloads", () => {
  it("shows actual vs UNKNOWN, the native subscription and the budget decision", async () => {
    mockApi({ "/api/costs/summary": summary, "/api/budgets": budgets, "/api/costs/ledger": ledger, "/api/quotas": quotas });
    renderPage(<CostsPage />);
    expect(await screen.findByText("Batas langganan berlaku; sisa kuota UNKNOWN")).toBeInTheDocument();
    expect(screen.getAllByText("$0.4200").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1 entri").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/BUDGET oleh/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Langganan (bukan dolar)").length).toBeGreaterThan(0);
    // §42: latency and repairs are monitored separately from money/tokens
    expect(screen.getByText("Latency dan repair pekerja")).toBeInTheDocument();
    expect(screen.getByText("240 s / 600 s (n=3 dari 4 run)")).toBeInTheDocument();
    expect(screen.getByText("2 (1 dari 2 task)")).toBeInTheDocument();
  });

  it("shows the OpenCode Go subscription as plan + allowance per window, not as dollars spent", async () => {
    mockApi({ "/api/costs/summary": summary, "/api/budgets": budgets, "/api/costs/ledger": ledger, "/api/quotas": quotas });
    renderPage(<CostsPage />);
    expect(await screen.findByText("Langganan worker (paket + allowance)")).toBeInTheDocument();
    const sub = summary.subscriptions[0];
    expect(screen.getByText(sub.route_key)).toBeInTheDocument();
    expect(screen.getByText("OpenCode Go · $10.00/bulan")).toBeInTheDocument();
    for (const label of ["5 jam", "Minggu (saran)", "Bulan (saran)"]) expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getAllByText("Dalam paket").length).toBe(4); // route state + three windows
    expect(screen.getByText(/instance AEOS ini saja/)).toBeInTheDocument();
    expect(screen.queryByText(/Dibayar di luar paket/)).toBeNull();
  });
});
