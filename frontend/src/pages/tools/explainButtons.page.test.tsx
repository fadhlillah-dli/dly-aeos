import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RouteDrawer } from "../models/RouteDrawer";
import routeDetail from "../models/__fixtures__/route-detail.json";
import blastRadius from "./__fixtures__/blast-radius.json";
import componentDetail from "./__fixtures__/component-detail.json";
import ComponentDetailPage from "./ComponentDetailPage";

// §35 W6-109: the six explain buttons open the SAME source record. Fixtures are REAL backend responses
// (GET /api/inventory/components/opencode, /api/inventory/blast-radius/opencode and /api/routes/<key> on the seeded
// InstallManifest + model registry; blast-radius items trimmed to the 12 the page lists).
const auth = { isAdmin: true, domain: "PERSONAL", projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

function serve(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const key = Object.keys(routes).filter((k) => url.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    const body = key ? routes[key] : { items: [], source: "test", checked_at: null, mode: "LIVE", total: 0 };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

async function open(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  await user.click(screen.getByRole("button", { name: label }));
}

describe("ComponentDetailPage explain buttons show the component record (W6-109)", () => {
  it("each button opens the record-derived text and 'Usulkan perubahan' links to the proposal form", async () => {
    serve({
      "/api/inventory/components/opencode/dependencies": { items: [], total: 0, source: "test", checked_at: null, mode: "DESIGN" },
      "/api/inventory/components/opencode": componentDetail,
      "/api/inventory/blast-radius/opencode": blastRadius,
    });
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter initialEntries={["/tools/opencode"]}>
          <Routes>
            <Route path="/tools/:componentId" element={<ComponentDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await screen.findByRole("button", { name: /Apa fungsinya\?/ });
    const panel = () => screen.getByRole("button", { name: /Apa fungsinya\?/ }).closest("div")!.parentElement!;

    await open(user, /Apa fungsinya\?/);
    expect(within(panel()).getByText(componentDetail.identity.function)).toBeInTheDocument();

    await open(user, /Mengapa dipilih\?/);
    expect(within(panel()).getByText(componentDetail.usage_role.why_selected)).toBeInTheDocument();
    expect(within(panel()).getByText(`Penempatan: ${componentDetail.usage_role.placement}`)).toBeInTheDocument();

    await open(user, /Dipakai oleh siapa\?/);
    for (const u of componentDetail.usage_role.used_by) {
      expect(within(panel()).getByRole("link", { name: u.name })).toHaveAttribute("href", `/tools/${u.component_id}`);
    }

    await open(user, /Lihat bukti/);
    for (const c of componentDetail.evidence.claims) expect(within(panel()).getAllByText(new RegExp(c.claim.replace(/[()]/g, "\\$&"))).length).toBeGreaterThan(0);

    await open(user, /Apa dampaknya jika diganti\?/);
    expect(await within(panel()).findByText(new RegExp(`${blastRadius.summary.affected} komponen terdampak`))).toBeInTheDocument();
    expect(within(panel()).getByRole("link", { name: blastRadius.items[0].name })).toHaveAttribute("href", `/tools/${blastRadius.items[0].component_id}`);

    await open(user, /Usulkan perubahan/);
    expect(within(panel()).getByRole("link", { name: "Buka formulir usulan untuk komponen ini" })).toHaveAttribute("href", "/proposals?component=opencode");
  });
});

describe("RouteDrawer explain buttons show the route record (W6-109)", () => {
  it("derives what/why/used-by/evidence/impact from the route record and links 'Usulkan perubahan'", async () => {
    const key = routeDetail.route_key;
    serve({ [`/api/routes/${key.split("/").map(encodeURIComponent).join("/")}`]: routeDetail });
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter>
          <RouteDrawer routeKey={key} onClose={() => undefined} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await screen.findByRole("button", { name: /Apa fungsinya\?/ });
    const panel = () => screen.getByRole("button", { name: /Apa fungsinya\?/ }).closest("div")!.parentElement!;

    await open(user, /Apa fungsinya\?/);
    expect(within(panel()).getByText(new RegExp(`Model ${routeDetail.model_id} lewat ${routeDetail.provider}`))).toBeInTheDocument();

    await open(user, /Mengapa dipilih\?/);
    const why = within(panel()).getByText(/Asal record:/);
    expect(why.textContent).toContain(`Asal record: ${routeDetail.origin} (bukti ${routeDetail.evidence_level}; sumber ${routeDetail.source_refs.join(", ")})`);
    expect(why.textContent).toContain(`Status ${routeDetail.status}; domain yang memenuhi syarat: ${routeDetail.eligible_domains.join(", ")}`);
    expect(why.textContent).toContain(`evaluasi: ${routeDetail.eval_summary.state}`);
    expect(why.textContent).toContain("Belum ada keputusan pemilik"); // no decision recorded for this seeded route

    await open(user, /Dipakai oleh siapa\?/);
    expect(within(panel()).getByText(/Belum pernah dipakai oleh run mana pun/)).toBeInTheDocument();

    await open(user, /Lihat bukti/);
    expect(within(panel()).getByText(/"pricing"/)).toBeInTheDocument();

    await open(user, /Apa dampaknya jika diganti\?/);
    const impact = within(panel()).getByText(/hash konfigurasi baru/);
    expect(impact.textContent).toContain(routeDetail.config_hash.replace(/^sha256:/, "").slice(0, 10));
    expect(impact.textContent).toContain("belum ada aktivasi yang terdampak");

    await open(user, /Usulkan perubahan/);
    expect(within(panel()).getByRole("link", { name: "Usulkan perubahan untuk rute ini" })).toHaveAttribute(
      "href", `/proposals?kind=REPLACE&source=${encodeURIComponent(key)}`);
  });
});
