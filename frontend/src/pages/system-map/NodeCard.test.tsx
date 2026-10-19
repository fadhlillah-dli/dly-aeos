import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: true, domain: "PERSONAL" }) }));

import type { MapNode } from "../../components/inventory/types";
import { NodeCard } from "./Panels";

const node = {
  id: "opencode", label: "OpenCode", name: "opencode", kind: "worker-engine", category: "worker", requirement: "REQUIRED",
  design_label: "DESIGN", domains: ["PERSONAL"], identity_verified: "VERIFIED", present: true, status: "INSTALLED", at: null,
  stale: false, basis: [], drift: [], function: "Engine worker",
} as unknown as MapNode;

const detail = {
  usage_role: { why_selected: "Engine worker pertama (§16)", used_by: [{ component_id: "workforce", name: "Workforce", relation: "calls", lens: "DESIGN" }] },
  scope: { level: "platform", domains: ["PERSONAL"], environment: "local", owner: "owner" },
  source: { source_url: "https://github.com/anomalyco/opencode" },
  versions: { approved_pin: "1.19.3", observed_version: "1.19.3" },
  cost: { design: { model: "PER_TOKEN" } },
  access: { egress: ["opencode.ai"], credential_ref: "workers/openrouter" },
  relations: {
    outgoing: [{ id: "r1", source: "opencode", target: "evidence-store", source_name: "OpenCode", target_name: "Evidence store",
      relation: "writes", lens: "DESIGN" }],
    incoming: [],
  },
  lifecycle: { installation: { effective: "INSTALLED" } },
  evidence: { claims: [] },
  changes: { proposals: [] },
};

describe("System map node card (§36 node card fields)", () => {
  it("shows function, why, users, domain, project scope, source, pin, cost, data/access and dependencies", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(detail), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <NodeCard node={node} lensLabel="Desain" onClose={() => undefined} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Engine worker pertama (§16)")).toBeInTheDocument();
    expect(screen.getByText("Workforce")).toBeInTheDocument();
    expect(screen.getByText(/platform · local/)).toBeInTheDocument();
    expect(screen.getByText(/egress: opencode\.ai · credential_ref: workers\/openrouter/)).toBeInTheDocument();
    expect(screen.getByText(/writes → Evidence store/)).toBeInTheDocument();
    expect(screen.getByText("1.19.3 / 1.19.3")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
