import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, vi } from "vitest";
import { AuthProvider } from "../../auth/AuthContext";
import { mockApi, renderPage, sessionFor } from "../../components/proposals/testUtils";
import { SearchBox } from "./SearchBox";

afterEach(() => vi.unstubAllGlobals());

const result = {
  items: [{ kind: "component", id: "claude-code", title: "Claude Code", subtitle: "executable", snippet: "", project_id: null, status: "SELECTED_DESIGN", url: "/tools/claude-code" }],
  total: 1, counts: { component: 1, project: 0 }, limit: 20, offset: 0, has_more: false, query: "claude",
  domain: "PERSONAL", project_id: null, source: "sqlite-like (metadata only, active domain)", checked_at: "2026-10-18T00:00:00Z", mode: "LIVE",
};

describe("Ringkasan metadata search (ADR-0024 /api/search)", () => {
  it("uses the domain-scoped /api/search contract and can narrow to the active project", async () => {
    const session = { ...sessionFor("SUPER_ADMIN"), context: { domain: "PERSONAL", project_id: "p1" } };
    const calls = mockApi({ "/api/auth/session": session, "/api/search": result });
    renderPage(<SearchBox domain="PERSONAL" />);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Kata kunci pencarian" }), "claude");
    await user.click(screen.getByRole("button", { name: /Cari/ }));
    expect(await screen.findByRole("link", { name: "Claude Code" })).toHaveAttribute("href", "/tools/claude-code");
    expect(screen.getByText(/1 hasil: Komponen 1/)).toBeInTheDocument();
    expect(calls.some((c) => c.url.startsWith("/api/search?q=claude") && !c.url.includes("project="))).toBe(true);
    expect(calls.some((c) => c.url.startsWith("/api/control-center/search"))).toBe(false);

    const scope = screen.getByRole("checkbox", { name: /Hanya proyek aktif/ });
    await waitFor(() => expect(scope).toBeEnabled());
    await user.click(scope);
    await waitFor(() => expect(calls.some((c) => c.url.includes("project=p1"))).toBe(true));
  });
});

describe("Ringkasan search never carries rows across domains", () => {
  it("drops PERSONAL rows while the OFFICE search is loading (no placeholder from another domain)", async () => {
    const session = { ...sessionFor("SUPER_ADMIN"), context: { domain: "PERSONAL", project_id: null } };
    let searches = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith("/api/search")) {
          searches += 1;
          if (searches > 1) return new Promise<Response>(() => {}); // the OFFICE answer is still pending
          return { ok: true, status: 200, text: async () => JSON.stringify(result) } as Response;
        }
        return { ok: true, status: 200, text: async () => JSON.stringify(session) } as Response;
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (domain: string) => (
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter>
            <SearchBox domain={domain} />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>
    );
    const view = render(tree("PERSONAL"));
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Kata kunci pencarian" }), "claude");
    await user.click(screen.getByRole("button", { name: /Cari/ }));
    expect(await screen.findByRole("link", { name: "Claude Code" })).toBeInTheDocument();

    view.rerender(tree("OFFICE"));
    await waitFor(() => expect(searches).toBe(2));
    expect(screen.queryByRole("link", { name: "Claude Code" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Mencari");
  });
});
