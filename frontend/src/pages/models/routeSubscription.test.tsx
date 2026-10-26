import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import free from "./__fixtures__/route-detail.json";
import go from "./__fixtures__/route-detail-go.json";
import { RouteDrawer } from "./RouteDrawer";

// ADR-0080 amendment (2026-10-26): the drawer of a SUBSCRIPTION route (OpenCode Go) shows the plan, the allowance per
// provider window and the beyond-plan state - never per-token spend. Fixtures are real GET /api/routes/<key> responses.

vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: true, session: null, domain: "PERSONAL", projectId: null }) }));

afterEach(() => vi.unstubAllGlobals());

function open(detail: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(detail), { status: 200, headers: { "Content-Type": "application/json" } })));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const key = (detail as { route_key: string }).route_key;
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RouteDrawer routeKey={key} onClose={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RouteDrawer subscription block", () => {
  it("shows plan, budget note and allowance per window for a subscription route", async () => {
    open(go);
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText("Langganan — biaya paket + allowance, bukan per token")).toBeInTheDocument();
    expect(within(drawer).getByText(go.budget_note)).toBeInTheDocument();
    expect(within(drawer).getByText("OpenCode Go · $10.00/bulan")).toBeInTheDocument();
    expect(within(drawer).getAllByText("$60.00").length).toBe(2); // allowance per month + the month window limit
    for (const label of ["5 jam", "Minggu (saran)", "Bulan (saran)"]) expect(within(drawer).getByText(label)).toBeInTheDocument();
    expect(within(drawer).getAllByText("Dalam paket").length).toBe(4); // route state + three windows
    expect(within(drawer).getByText(/instance AEOS ini saja/)).toBeInTheDocument();
    expect(within(drawer).queryByText(/Ditolak \(SUBSCRIPTION_BEYOND_PLAN\)/)).toBeNull();
  });

  it("shows a refused beyond-plan (Extra Usage) route with its remedy", async () => {
    const sub = {
      ...go.subscription,
      state: "BEYOND_PLAN",
      beyond_plan_usd: 0.31,
      beyond_plan: { entry_id: "e1", at: "2026-10-18T10:00:00+00:00", amount_usd: "0.31", billing_source: "balance", activated_at: null, remedy: "Pastikan Extra Usage OFF lalu aktifkan ulang rute." },
    };
    open({ ...go, subscription: sub });
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText("Di luar paket (Extra Usage) — ditolak")).toBeInTheDocument();
    expect(within(drawer).getByText(/Dibayar di luar paket: \$0\.3100/)).toBeInTheDocument();
    expect(within(drawer).getByText(/Pastikan Extra Usage OFF/)).toBeInTheDocument();
  });

  it("hides the window figures from a principal who cannot see every domain", async () => {
    const { windows: _windows, problems: _p, warnings: _w, beyond_plan: _b, ...rest } = go.subscription;
    open({ ...go, subscription: { ...rest, windows: null, spend_hidden: true, spend_note: "Allowance rute mencakup domain lain yang tidak dapat Anda lihat." } });
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    expect(await within(drawer).findByText(/Allowance disembunyikan: Allowance rute mencakup domain lain/)).toBeInTheDocument();
    expect(within(drawer).queryByText("5 jam")).toBeNull();
  });

  it("renders no subscription block for a free route", async () => {
    open(free);
    const drawer = await screen.findByRole("dialog", { name: /Detail rute/ });
    await within(drawer).findByText("Keputusan BUDGET");
    expect(within(drawer).queryByText("Penagihan")).toBeNull();
    expect(within(drawer).queryByText(/Langganan — biaya paket/)).toBeNull();
  });
});
