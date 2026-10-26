import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { VerifierPolicyFacts } from "./VerifierPolicy";

const auth = { domain: "PERSONAL", isAdmin: true, projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

afterEach(() => vi.unstubAllGlobals());

describe("TaskSpec verifier_policy (read-only)", () => {
  it("states the opt-ins as stored and an absent opt-in as what the verifier then does", () => {
    const { rerender } = render(<VerifierPolicyFacts policy={{ required: true, suites: ["unit", "acceptance"], negative_tests_required: true }} />);
    const facts = screen.getByLabelText("Kebijakan verifier (hanya baca)");
    expect(within(facts).getByText("tidak diaktifkan")).toBeInTheDocument();
    expect(within(facts).getByText("tidak disetujui (breaking diblokir)")).toBeInTheDocument();
    expect(within(facts).getByText("suite: unit, acceptance")).toBeInTheDocument();
    rerender(<VerifierPolicyFacts policy={{ integration_tests: "required", contract_breaking: "approved", migration_roundtrip: "optional" }} />);
    expect(screen.getByText("wajib (required)")).toBeInTheDocument();
    expect(screen.getByText("disetujui (dinyatakan, tidak memblokir)")).toBeInTheDocument();
    expect(screen.getByText(/migration_roundtrip=optional/)).toBeInTheDocument();
    // read-only: no control to change an immutable TaskSpec
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("the DAG table shows each task's verifier policy with the planner-contract note", async () => {
    const node = {
      key: "T1", id: "t-1", title: "API login", feature_key: null, goal: "", role_key: "backend", risk_class: "R2", allowed_paths: ["src/**"],
      forbidden_paths: [], depends_on: [], spec_hash: "sha256:x", limits: {}, data_classification: "INTERNAL", quarantine: false,
      acceptance_criteria: [], mode: "LIVE", verifier_policy: { required: true, integration_tests: "run" },
      runtime: { state: "NOT_SCHEDULED", reason: "", attempts_used: 0, active_run_id: null }, lease: null, acceptance: { status: "NONE", valid: false },
      warnings: [], accepted_prerequisites: [], dependencies_ready: true, conflicts_with: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.startsWith("/api/plans/pl1/dag")
          ? { plan_id: "pl1", compiled: true, approved: true, mode: "LIVE", nodes: [node], edges: [], critical_blockers: [], shared_write_conflicts: [], validation_errors: [], checked_at: new Date().toISOString() }
          : { items: [{ id: "pl1", title: "Rencana", current_version: { version: 1 }, approved_version: { version: 1 } }] };
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
    const { default: DagPage } = await import("../../pages/dag/index");
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/dag?plan=pl1&task=T1"]}>
          <DagPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/Kontrak TaskSpec planner v1 belum dapat mengisi/)).toBeInTheDocument();
    const row = screen.getByText("API login").closest("tr")!;
    expect(within(row).getByText("dijalankan (run)")).toBeInTheDocument();
  });
});
