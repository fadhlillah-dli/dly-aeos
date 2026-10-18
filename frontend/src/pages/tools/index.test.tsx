import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

const auth = { isAdmin: true, domain: null as string | null, session: null, projectId: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

import SystemMapPage from "../system-map/index";
import ToolsPage from "./index";

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("domain-first inventory pages", () => {
  it("Tools & Repos asks for an explicit domain and requests nothing until one is chosen", () => {
    auth.domain = null;
    wrap(<ToolsPage />);
    expect(screen.getByText(/Pilih domain PERSONAL atau OFFICE/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Segarkan/ })).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("System Map asks for an explicit domain and requests nothing until one is chosen", () => {
    auth.domain = null;
    wrap(<SystemMapPage />);
    expect(screen.getByText(/Pilih domain PERSONAL atau OFFICE/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
