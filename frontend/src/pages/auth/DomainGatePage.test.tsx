import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ selectContext: vi.fn(), session: { allowed_domains: ["PERSONAL", "OFFICE"] } }),
}));

import DomainGatePage from "./DomainGatePage";

describe("Domain gate (§05 step 1: after login the first page shows readiness)", () => {
  it("shows platform readiness before a domain is chosen, never domain rows, unknown is not green", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      items: [
        { capability: "CAN_OPEN_DASHBOARD", label: "Membuka dashboard lokal", domain: null, status: "READY", reasons: [] },
        { capability: "CAN_RUN_WORKER", label: "Menjalankan worker dalam sandbox", domain: null, status: "BLOCKED",
          reasons: ["worker.opencode: MISSING"] },
        { capability: "CAN_PLAN_OFFICE", label: "Merencanakan proyek OFFICE", domain: "OFFICE", status: "BLOCKED", reasons: [] },
      ],
      checked_at: new Date().toISOString(),
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <DomainGatePage />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Membuka dashboard lokal")).toBeInTheDocument();
    expect(screen.getByText("Menjalankan worker dalam sandbox")).toBeInTheDocument();
    expect(screen.getByText(/worker\.opencode: MISSING/)).toBeInTheDocument();
    expect(screen.queryByText("Merencanakan proyek OFFICE")).not.toBeInTheDocument(); // domain rows wait for the choice
    expect(screen.getByRole("heading", { name: "Pilih domain kerja" })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
