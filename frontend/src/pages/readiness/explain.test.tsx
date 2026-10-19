import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ isAdmin: true, domain: "PERSONAL", session: null, projectId: null }),
}));
vi.mock("../../lib/events", () => ({ useEventStream: () => undefined }));

import ReadinessPage from "./index";

const check = {
  check_id: "host.port", title: "Port lokal tersedia", group: "HOST", group_label: "Host", component_id: null,
  component_version: null, scope: {}, target_identity: null, status: "FAILED", effective_status: "FAILED",
  stale_reason: null, severity: "HIGH", summary: "Port dipakai.", observed_evidence: {}, last_checked_at: null,
  expires_at: null, invalidators: [], fingerprint: null, error_code: "PORT_IN_USE", remediation: {},
  requires_destructive: false, requires_paid_write: false, never_run: false, functional_only: false, network: false,
  critical_for: [], ok_statuses: ["VERIFIED"], component_link: null,
};

describe("Readiness: Claude explains a check (§08) as narration only", () => {
  it("posts the check id and shows the answer labelled as narration, not evidence", async () => {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
      if (init?.method === "POST" && url === "/api/claude/explain-check") {
        posts.push(String(init.body));
        return json({ status: "NARRATION", explanation: "Port 8787 dipakai proses lain.", next_steps: ["aeos serve --port 8790"],
          label: "Narasi Claude (DATA, bukan bukti); status pemeriksaan hanya berubah lewat Readiness Doctor." });
      }
      if (url.includes("capabilities")) return json({ items: [], human_only: [], source: "x", checked_at: null, mode: "LIVE" });
      return json({ items: [check], groups: [{ id: "HOST", label: "Host", order: 1 }], last_run: null, source: "x",
        checked_at: null, mode: "LIVE", status_legend: {} });
    }));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <ReadinessPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByText("Port lokal tersedia"));
    await userEvent.click(await screen.findByRole("button", { name: "Jelaskan dengan Claude" }));
    expect(await screen.findByText("Port 8787 dipakai proses lain.")).toBeInTheDocument();
    expect(screen.getByText(/bukan bukti/)).toBeInTheDocument();
    expect(JSON.parse(posts[0])).toEqual({ check_id: "host.port" });
    vi.unstubAllGlobals();
  });
});
