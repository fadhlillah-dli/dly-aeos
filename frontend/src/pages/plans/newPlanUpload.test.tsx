import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

const auth = { isAdmin: true, domain: "PERSONAL", projectId: "p1", session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("../../lib/events", () => ({ useEventStream: () => undefined }));

import PlansPage from "./index";

type Call = { url: string; method: string; body: BodyInit | null | undefined };

describe("New plan form uploads files (§01 U-11: every intake surface supports upload, incl. .md)", () => {
  it("uploads the queued .md to the selected project and sends its attachment id with the plan", async () => {
    const calls: Call[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET", body: init?.body });
        const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
        if (url.startsWith("/api/plans")) return json({ items: [], needs_decision: 0, checked_at: new Date().toISOString(), mode: "LIVE" });
        if (url.startsWith("/api/projects/p1/attachments")) return json({ attachment: { id: "att-md-1", scan_findings: [], filename: "ide.md" } });
        if (url.startsWith("/api/projects/p1/plans")) return json({ id: "plan-1" });
        if (url.startsWith("/api/projects")) return json({ items: [{ id: "p1", name: "Demo", project_key: "demo", domain: "PERSONAL" }] });
        if (url.startsWith("/api/intake/policy")) return json({ allowed_extensions: [".md", ".pdf"], max_upload_bytes: 100000, max_archive_members: 5, notice: "DATA" });
        return json({});
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <PlansPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /Rencana baru/ }));
    await waitFor(() => expect(screen.getByRole("option", { name: /Demo \(demo\)/ })).toBeInTheDocument());
    fireEvent.change(screen.getByTestId("file-input"), { target: { files: [new File(["# Ide\nexport CSV"], "ide.md", { type: "text/markdown" })] } });
    expect(await screen.findByText("ide.md")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Judul rencana"), "Ekspor CSV");
    await userEvent.type(screen.getByLabelText(/Ide \/ prompt/), "Tambah ekspor CSV untuk akuntan");
    await userEvent.click(screen.getByRole("button", { name: "Buat rencana" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/projects/p1/plans" && c.method === "POST")).toBe(true));
    const upload = calls.find((c) => c.url === "/api/projects/p1/attachments" && c.method === "POST");
    expect(upload && upload.body instanceof FormData && (upload.body.get("file") as File).name).toBe("ide.md");
    const plan = calls.find((c) => c.url === "/api/projects/p1/plans")!;
    expect(JSON.parse(String(plan.body)).attachment_ids).toEqual(["att-md-1"]);
    vi.unstubAllGlobals();
  });
});
