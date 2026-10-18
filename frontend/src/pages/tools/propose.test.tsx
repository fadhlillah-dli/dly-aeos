import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, vi } from "vitest";

const auth = { isAdmin: true, domain: "PERSONAL" as string | null, session: null, projectId: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

import ToolsPage from "./index";

const dim = { effective: "NOT_CHECKED", source: "seed", reason: null, at: null };
const row = {
  component_id: "claude-code", name: "Claude Code", friendly_name: "Claude Code (official)", kind: "executable",
  category: "planning", category_label: "Perencanaan & tata kelola", domains: ["PLATFORM"], requirement: "REQUIRED",
  design_label: "SELECTED_DESIGN", identity_verified: "VERIFIED", source_url: null, source_kind: "OFFICIAL_DOCS",
  usage_role: "runtime", bootstrap_order: 1, version: { approved_pin: null, observed_version: null, status: "NO_PIN" },
  dimensions: Object.fromEntries(["admission", "installation", "configuration", "connectivity", "functional", "enablement", "usage", "lifecycle_final"].map((k) => [k, dim])),
  drift: [], config_lens: { present: true, basis: [] }, tombstoned_at: null, mode: "DESIGN",
};
const list = {
  items: [row], total: 1, page: 1, page_size: 50, view: "default",
  facets: { kinds: [], requirements: [], categories: [], design_labels: [] },
  coverage: { totals: {}, gaps: [], last_reconciled_at: null }, manifest: null, source: "InstallManifest", checked_at: null, mode: "DESIGN",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(list), { status: 200, headers: { "Content-Type": "application/json" } })));
});
afterEach(() => vi.unstubAllGlobals());

function wrap() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Tools & Repos 'Usulkan perubahan' (§39)", () => {
  it("links each component to the proposal form for that component (SUPER_ADMIN)", async () => {
    auth.isAdmin = true;
    wrap();
    const link = await screen.findByRole("link", { name: "Usulkan perubahan" });
    expect(link).toHaveAttribute("href", "/proposals?component=claude-code");
    expect(screen.getByRole("link", { name: /Usulkan komponen baru/ })).toHaveAttribute("href", "/proposals?new=1&kind=ADD");
    // The seeded design label keeps its canonical code in the tooltip.
    expect(screen.getByTitle("SELECTED_DESIGN")).toBeInTheDocument();
  });

  it("offers no proposal or refresh controls to a VIEWER", async () => {
    auth.isAdmin = false;
    wrap();
    await screen.findByText("Claude Code (official)");
    // ("Usulkan koreksi" in the PageHeader stays: §41 offers it on every page, submitting needs SUPER_ADMIN.)
    expect(screen.queryByRole("link", { name: /Usulkan (perubahan|komponen)/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Segarkan/ })).not.toBeInTheDocument();
  });
});
