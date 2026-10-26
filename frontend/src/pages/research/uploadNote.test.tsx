import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderPage, sessionFor } from "../../components/proposals/testUtils";
import { AddUrlForm, RESEARCH_UPLOAD_ACCEPT } from "./index";

// §01 U-11 (W6-004): research registration accepts file uploads (.md included) through intake, into the selected
// project or this domain's platform project; intake refuses executables; the backend refuses another domain's id.
type Call = { url: string; method: string; body: unknown };

function stubBackend({ researchError }: { researchError?: { status: number; code: string; message: string } } = {}) {
  const calls: Call[] = [];
  const reply = (body: unknown, status = 200) =>
    ({ ok: status < 400, status, statusText: "", text: async () => JSON.stringify(body) }) as Response;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const body = init?.body instanceof FormData ? init.body : typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({ url, method, body });
      if (url.startsWith("/api/auth/session")) return reply(sessionFor("SUPER_ADMIN"));
      if (url.startsWith("/api/proposals/meta")) return reply({ platform_project: { id: "plat-1", name: "DLY AEOS", domain: "PERSONAL" } });
      if (url === "/api/projects/plat-1/attachments" && method === "POST") {
        const file = (body as FormData).get("file") as File;
        if (file.name.endsWith(".exe"))
          return reply({ error: { code: "ATTACHMENT_REJECTED", message: "jenis berkas tidak diizinkan (executable)", details: {} } }, 422);
        return reply({ attachment: { id: `att-${file.name}` } });
      }
      if (url === "/api/research" && method === "POST") {
        if (researchError) return reply({ error: { code: researchError.code, message: researchError.message, details: {} } }, researchError.status);
        return reply({ entry: { name: "widget", identity: "github:acme/widget" }, duplicate: false, note: "Entri DISCOVERED." });
      }
      return reply({ error: { code: "NOT_FOUND", message: `unmocked ${url}`, details: {} } }, 404);
    }),
  );
  return calls;
}

const fileInput = () => screen.findByLabelText("Lampirkan berkas");
const choose = (input: HTMLElement, file: File) => fireEvent.change(input, { target: { files: [file] } });

describe("research registration and file upload (§01 U-11)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uploads a .md brief through intake and sends its id with the entry", async () => {
    const calls = stubBackend();
    renderPage(<AddUrlForm />);
    const input = await fileInput();
    expect(input.getAttribute("accept")).toBe(RESEARCH_UPLOAD_ACCEPT);
    expect(RESEARCH_UPLOAD_ACCEPT.split(",")).toContain(".md");
    choose(input, new File(["# Catatan riset\n"], "brief.md", { type: "text/markdown" }));
    expect(await screen.findByText("brief.md")).toBeInTheDocument();
    const upload = calls.find((c) => c.url === "/api/projects/plat-1/attachments")!;
    expect((upload.body as FormData).get("source")).toBe("research");
    fireEvent.change(screen.getByPlaceholderText("https://github.com/owner/repo"), { target: { value: "https://github.com/acme/widget" } });
    fireEvent.click(screen.getByRole("button", { name: "Tambah ke registry" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/research" && c.method === "POST")).toBe(true));
    const sent = calls.find((c) => c.url === "/api/research" && c.method === "POST")!.body as { attachment_ids: string[] };
    expect(sent.attachment_ids).toEqual(["att-brief.md"]);
    expect(screen.getByTestId("research-upload-note")).not.toHaveTextContent("tidak diunggah di sini");
  });

  it("shows the intake refusal of an executable and does not attach it", async () => {
    const calls = stubBackend();
    renderPage(<AddUrlForm />);
    choose(await fileInput(), new File(["MZ"], "tool.exe", { type: "application/octet-stream" }));
    expect(await screen.findByText(/jenis berkas tidak diizinkan/)).toBeInTheDocument();
    expect(screen.queryByText("tool.exe")).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("https://github.com/owner/repo"), { target: { value: "https://github.com/acme/widget" } });
    fireEvent.click(screen.getByRole("button", { name: "Tambah ke registry" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/research" && c.method === "POST")).toBe(true));
    expect((calls.find((c) => c.url === "/api/research" && c.method === "POST")!.body as { attachment_ids: string[] }).attachment_ids).toEqual([]);
  });

  it("shows the backend refusal of an attachment from another domain", async () => {
    stubBackend({ researchError: { status: 422, code: "ATTACHMENT_NOT_FOUND", message: "attachment att-x not found in this domain" } });
    renderPage(<AddUrlForm />);
    choose(await fileInput(), new File(["# x"], "x.md", { type: "text/markdown" }));
    await screen.findByText("x.md");
    fireEvent.change(screen.getByPlaceholderText("https://github.com/owner/repo"), { target: { value: "https://github.com/acme/widget" } });
    fireEvent.click(screen.getByRole("button", { name: "Tambah ke registry" }));
    expect(await screen.findByText(/not found in this domain/)).toBeInTheDocument();
  });
});
