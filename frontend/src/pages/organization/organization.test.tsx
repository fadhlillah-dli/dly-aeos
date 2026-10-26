import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import OrganizationPage from "./index";

const auth = { isAdmin: true, domain: "PERSONAL", projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("../../components/workforce/useLive", () => ({ useLiveInvalidation: () => undefined }));

function role(over: Record<string, unknown>) {
  return {
    role_key: "r", name: "Peran", kind: "EXECUTOR", group: "build", description: "", outputs: ["kode"], boundaries: [],
    verification: [], forbidden_actions: [], deterministic_service: false, engine_hint: "", source_section: "§16",
    version: "1", revision_hash: null, eval_status: "NOT_EVALUATED", mode: "DESIGN", active_runs: 0, demo_runs: 0,
    assigned_tasks: 0, availability: "IDLE", ...over,
  };
}

function respond(roles: unknown[], agents: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/api/workforce/roles")
      ? { items: [{ key: "build", label: "Build", roles }], note: "catatan", source: "RoleSkillPack catalog", checked_at: new Date().toISOString(), mode: "DESIGN" }
      : { items: agents, checked_at: new Date().toISOString() };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrganizationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Organization page explain buttons (§35 'buka record sumber yang sama')", () => {
  it("builds 'Apa fungsinya?' and 'Mengapa dipilih?' from the role records, not fixed copy", async () => {
    respond(
      [role({ role_key: "impl", name: "Implementer", kind: "EXECUTOR", source_section: "§16" }),
        role({ role_key: "rev", name: "Reviewer kode", kind: "REVIEWER", source_section: "§18" }),
        role({ role_key: "ctl", name: "Controller", kind: "SERVICE", deterministic_service: true, source_section: "§10" })],
      [{ agent_key: "a1", role_key: "impl", harness: "opencode", description: "", route_policy: {}, tools: [], limits: {},
        allowed_scope: {}, enabled: false, enabled_reason: "", deterministic: false, revision: "1", revision_hash: null,
        mode: "DESIGN", owner: "", active_runs: 0, availability: "IDLE" }],
    );
    wrap();
    const user = userEvent.setup();
    await screen.findByText("Implementer");
    await user.click(screen.getByRole("button", { name: /Apa fungsinya\?/ }));
    expect(await screen.findByText(/3 peran dalam 1 kelompok — Build \(3\) — dan 1 definisi agent/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Mengapa dipilih\?/ }));
    const why = await screen.findByText(/Komposisi menurut record/);
    expect(why.textContent).toContain("Pelaksana 1");
    expect(why.textContent).toContain("Reviewer 1");
    expect(why.textContent).toContain("Layanan deterministik (kode, bukan LLM): Controller");
    expect(why.textContent).toContain("§10, §16, §18");
    expect(screen.queryByText(/Pola terpilih: controller deterministik/)).not.toBeInTheDocument();
  });

  it("states that nothing is claimed when no role record exists", async () => {
    respond([], []);
    wrap();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Apa fungsinya\?/ }));
    expect(await screen.findByText(/tidak ada fungsi yang dinyatakan tanpa record/)).toBeInTheDocument();
  });
});

describe("Organization 'impact' and 'Usulkan perubahan' (W6-109)", () => {
  it("derives the impact from roles, agents and active assignments and links to the proposal form", async () => {
    const agent = (over: Record<string, unknown>) => ({
      agent_key: "a", role_key: "impl", harness: "opencode", description: "", route_policy: {}, tools: [], limits: {},
      allowed_scope: {}, enabled: false, enabled_reason: "", deterministic: false, revision: "1", revision_hash: null,
      mode: "DESIGN", owner: "", active_runs: 0, availability: "IDLE", ...over,
    });
    respond(
      [role({ role_key: "impl", name: "Implementer", assigned_tasks: 2, active_runs: 1 }),
        role({ role_key: "rev", name: "Reviewer kode", kind: "REVIEWER" })],
      [agent({ agent_key: "impl-opencode", enabled: true }), agent({ agent_key: "rev-claude", role_key: "rev" })],
    );
    wrap();
    const user = userEvent.setup();
    await screen.findByText("Implementer");
    await user.click(screen.getByRole("button", { name: /Apa dampaknya jika diganti\?/ }));
    const impact = await screen.findByText(/Terdampak sekarang menurut record/);
    expect(impact.textContent).toContain("Implementer (2 task, 1 run)");
    expect(impact.textContent).not.toContain("Reviewer kode (");
    expect(impact.textContent).toContain("2 definisi agent memakai 2 peran, 1 aktif (impl-opencode)");
    await user.click(screen.getByRole("button", { name: /Usulkan perubahan/ }));
    expect(screen.getByRole("link", { name: "Usulkan perubahan peran/agent" })).toHaveAttribute(
      "href", "/proposals?kind=UPGRADE&component=workforce-scheduler");
  });
});
