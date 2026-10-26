import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: false, domain: "PERSONAL", session: null, projectId: null }),
}));

import ReadinessPage from "./index";

// The Readiness check detail renders worker.route_behaviour evidence as a per-route table (not only raw JSON).
const check = {
  check_id: "worker.route_behaviour",
  title: "Perilaku tool/schema route worker nyata",
  group: "WORKER",
  group_label: "Worker",
  component_id: "opencode",
  component_version: null,
  scope: {},
  target_identity: null,
  status: "UNKNOWN",
  effective_status: "UNKNOWN",
  stale_reason: null,
  severity: "HIGH",
  summary: "Perilaku tool/schema route worker NOT-PROVEN",
  observed_evidence: {
    threshold: { max_failure_rate: null, decision_id: null, status: "UNDECIDED", target: "worker_route_behaviour:tool_schema_failures" },
    installed_engine_version: null,
    routes: [{ route_key: "opencode-zen/glm-free", verdict: "UNKNOWN", why: "belum ada evaluasi tepercaya", verified_runs: 0, tool_errors: 0 }],
  },
  last_checked_at: "2026-10-26T00:00:00+00:00",
  expires_at: null,
  invalidators: [],
  fingerprint: null,
  error_code: "ROUTE_BEHAVIOUR_NOT_PROVEN",
  remediation: {},
  requires_destructive: false,
  requires_paid_write: false,
  never_run: false,
  functional_only: false,
  network: false,
  critical_for: [],
  ok_statuses: ["VERIFIED"],
  component_link: null,
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("capabilities")
        ? { items: [], human_only: [], source: "x", checked_at: null, mode: "LIVE" }
        : { items: [check], groups: [{ id: "WORKER", label: "Worker", order: 1 }], last_run: null, source: "Doctor", checked_at: null, mode: "LIVE", status_legend: {} };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

it("opens worker.route_behaviour with its per-route evidence and the UNDECIDED owner threshold", async () => {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ReadinessPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByText("Perilaku tool/schema route worker nyata"));
  expect(await screen.findByTestId("route-behaviour-evidence")).toBeInTheDocument();
  expect(screen.getByText("Ambang pemilik UNDECIDED: kegagalan dilaporkan, tidak dinilai")).toBeInTheDocument();
  expect(screen.getByText("belum ada evaluasi tepercaya")).toBeInTheDocument();
});
