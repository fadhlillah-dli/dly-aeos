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
  });
});
