import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../../auth/AuthContext";

/** Test-only helper for module group H pages: mocked fetch by path prefix + real providers. */
export function sessionFor(role: "SUPER_ADMIN" | "VIEWER") {
  return {
    user: { id: "u1", username: "owner", display_name: "Owner", role, must_change_password: false },
    csrf_token: "csrf",
    expires_at: "2099-01-01T00:00:00Z",
    allowed_domains: ["PERSONAL", "OFFICE"],
    context: { domain: "PERSONAL", project_id: null },
  };
}

export function mockApi(routes: Record<string, unknown>, role: "SUPER_ADMIN" | "VIEWER" = "SUPER_ADMIN") {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body });
    const all: Record<string, unknown> = { "/api/auth/session": sessionFor(role), ...routes };
    const key = Object.keys(all)
      .filter((k) => url.startsWith(k) || url.startsWith(k.replace(/^(GET|POST) /, "")))
      .filter((k) => !/^(GET|POST) /.test(k) || k.startsWith(method + " "))
      .sort((a, b) => b.length - a.length)[0];
    const body = key ? all[key] : { error: { code: "NOT_FOUND", message: `unmocked ${url}`, details: {} } };
    const status = key ? 200 : 404;
    return { ok: status < 400, status, statusText: status < 400 ? "OK" : "Not Found", text: async () => JSON.stringify(body) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

export function renderPage(ui: ReactElement, { path = "/", route = "/" }: { path?: string; route?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path={path} element={ui} />
            <Route path="*" element={<div>navigated</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
