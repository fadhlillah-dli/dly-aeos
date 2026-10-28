import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { ComponentRow } from "../../components/inventory/types";

vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: false, domain: "PERSONAL", session: null, projectId: null }) }));

import ToolsPage from "./index";

// R28: in the default view the Admission column is hidden (advanced only), so a TRIAL_APPROVED tool looked merely
// DISABLED. The admission badge now sits in the component cell. Change-proposal TRIAL decisions carry no expires_at,
// so no date is shown for tools.
const dim = (value: string) => ({
  value, effective: value, at: null, expires_at: null, stale: false, source: "s", observation_id: null, version: null, reason: null,
});

const row = (id: string, admission: string): ComponentRow => ({
  component_id: id, name: id, friendly_name: `Tool ${id}`, kind: "worker_engine", category: "workforce", category_label: "Workforce",
  domains: ["PLATFORM"], requirement: "OPTIONAL", design_label: "CANDIDATE", identity_verified: "VERIFIED", source_url: null,
  source_kind: "GITHUB_REPO", usage_role: "runtime", bootstrap_order: null,
  version: { approved_pin: null, observed_version: null, status: "NO_PIN", pin_note: null },
  dimensions: {
    admission: dim(admission), installation: dim("UNKNOWN"), configuration: dim("UNKNOWN"), connectivity: dim("NOT_CHECKED"),
    functional: dim("NOT_TESTED"), enablement: dim("DISABLED"), usage: dim("NEVER_OBSERVED"), lifecycle_final: dim("ACTIVE"),
  },
  drift: [], config_lens: { present: false, basis: [] }, tombstoned_at: null, mode: "DESIGN", why_selected: null, next_action: undefined,
}) as unknown as ComponentRow;

function mount(rows: ComponentRow[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(
        JSON.stringify({
          items: rows, total: rows.length, page: 1, page_size: 50, view: "default",
          facets: { kinds: [], requirements: [], categories: [], design_labels: [] },
          coverage: { totals: {}, gaps: [], last_reconciled_at: null, last_full_reconciled_at: null },
          manifest: null, source: "x", checked_at: null, mode: "DESIGN", project_repositories: [], project_services: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Tools & Repos default view: admission badge (R28)", () => {
  it("shows TRIAL for TRIAL_APPROVED, Disetujui for APPROVED and nothing for DISCOVERED", async () => {
    mount([row("trialled", "TRIAL_APPROVED"), row("approved", "APPROVED"), row("found", "DISCOVERED")]);
    await screen.findByText("Tool trialled");
    const rowOf = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;
    expect(within(rowOf("Tool trialled")).getByTestId("admission-badge")).toHaveTextContent("TRIAL");
    expect(within(rowOf("Tool approved")).getByTestId("admission-badge")).toHaveTextContent("Disetujui");
    expect(within(rowOf("Tool found")).queryByTestId("admission-badge")).toBeNull();
    // no date for tools: a change-proposal TRIAL decision has no expires_at
    expect(within(rowOf("Tool trialled")).queryByText(/s\/d /)).toBeNull();
  });
});
