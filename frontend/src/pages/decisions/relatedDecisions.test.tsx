import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: false, domain: "PERSONAL", projectId: null, session: null }) }));
vi.mock("../../lib/events", () => ({ useEventStream: () => undefined }));

import { PageHeader } from "../../components/ui";
import { DecisionHistory } from "./index";

describe("'Keputusan terkait' is page-specific (§41)", () => {
  it("a page links its own decisions and the register filters by that target", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (u: string) => {
      urls.push(String(u));
      const body = String(u).startsWith("/api/decisions/queue") ? { items: [], checked_at: null }
        : String(u).startsWith("/api/decisions") ? { items: [], checked_at: new Date().toISOString(), mode: "LIVE" } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(
      <MemoryRouter>
        <PageHeader title="Detail usulan" question="q" relatedDecisionsHref="/decisions?target_type=change_proposal&target_id=cp-1" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Keputusan terkait" })).toHaveAttribute(
      "href", "/decisions?target_type=change_proposal&target_id=cp-1");
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/decisions?target_type=change_proposal&target_id=cp-1"]}>
          <DecisionHistory highlight={null} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("decision-target-filter")).toHaveTextContent("change_proposal");
    await waitFor(() => expect(urls.some((u) => u.includes("target_type=change_proposal") && u.includes("target_id=cp-1"))).toBe(true));
    vi.unstubAllGlobals();
  });
});
