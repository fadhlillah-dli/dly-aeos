import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, vi } from "vitest";
import { CheckpointPanel } from "./CheckpointPanel";
import type { CheckpointCandidates, CheckpointView } from "./types";

const auth = { isAdmin: false, domain: "PERSONAL", projectId: "p1", session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

const PACKAGE: CheckpointView = {
  id: "cp1",
  kind: "MILESTONE",
  ref: "M1",
  package_hash: "sha256:pkg",
  plan_version_id: "v1",
  material: true,
  finalised: true,
  changed_documents: [{ logical_doc_id: "03-architecture-overview", change: "CHANGED", sections: ["Context"] }],
  removed_documents: [],
  diagram_delta: ["context"],
  contract_deltas: [],
  architecture_delta: null,
  writer_task: { status: "DRAFT_FOR_NEXT_PLAN_VERSION", key: "DOC-MILE-M1", title: "Perbarui narasi M1", goal: "Susun narasi", role_key: "technical-writer" },
  previous_checkpoint_id: null,
  created_at: new Date().toISOString(),
  writer_task_planned: null,
};

const CANDIDATES: CheckpointCandidates = {
  plan_version: { id: "v1", version: 1, manifest_hash: "sha256:v1" },
  milestones: [{ key: "M2", title: "Rilis", tasks_total: 2, tasks_accepted: 2, accepted: true, checkpoint_id: null }],
  snapshots: [],
  offers: [{ kind: "MILESTONE", ref: "M2", plan_version_id: "v1", reason: "Semua 2 task milestone M2 ACCEPTED; catat checkpoint untuk memfinalisasi dokumentasinya." }],
  source: "approved PlanVersion + AcceptanceRecord",
  checked_at: new Date().toISOString(),
};

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function stubApi() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.endsWith("/documents/checkpoints/candidates")) return json(CANDIDATES);
    if (url.endsWith("/documents/checkpoints") && (!init || !init.method || init.method === "GET")) {
      return json({ items: [PACKAGE], source: "document_revisions", checked_at: PACKAGE.created_at });
    }
    if (url === "/api/claude/review-docs-checkpoint") {
      return json({ status: "NARRATION", assessment: "Narasi perlu menyebut openbao.", concerns: ["diagram belum diperbarui"], questions_for_owner: [], label: "Tinjauan penasihat Claude (DATA, bukan keputusan)" });
    }
    return json({ id: "cp2", created: true });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  auth.isAdmin = false;
  vi.unstubAllGlobals();
});

describe("CheckpointPanel", () => {
  it("shows the package, the writer draft and the offer, without actions for a VIEWER", async () => {
    stubApi();
    render(wrap(<CheckpointPanel projectId="p1" />));
    expect(await screen.findByText(/Semua 2 task milestone M2 ACCEPTED/)).toBeInTheDocument();
    expect(screen.getByText("Perubahan material")).toBeInTheDocument();
    expect(screen.getByText("Perbarui narasi M1")).toBeInTheDocument();
    expect(screen.getByText(/dijalankan hanya setelah pemilik menyetujui versi rencana/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Minta review Claude/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Catat checkpoint/ })).toBeNull();
    expect(screen.getByText("Pencatatan dilakukan oleh SUPER_ADMIN.")).toBeInTheDocument();
  });

  it("lets an admin record the offered checkpoint and ask Claude for an advisory review", async () => {
    auth.isAdmin = true;
    const fetchMock = stubApi();
    render(wrap(<CheckpointPanel projectId="p1" />));
    fireEvent.click(await screen.findByRole("button", { name: "Catat checkpoint M2" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => u === "/api/projects/p1/documents/checkpoints" && (i as RequestInit)?.method === "POST")).toBe(true));
    const recordCall = fetchMock.mock.calls.find(([u, i]) => u === "/api/projects/p1/documents/checkpoints" && (i as RequestInit)?.method === "POST")!;
    expect(JSON.parse(String((recordCall[1] as RequestInit).body))).toEqual({ plan_version_id: "v1", kind: "MILESTONE", ref: "M2" });

    fireEvent.click(screen.getByRole("button", { name: /Minta review Claude/ }));
    expect(await screen.findByText("Narasi perlu menyebut openbao.")).toBeInTheDocument();
    expect(screen.getByText(/Narasi Claude — Tinjauan penasihat Claude \(DATA, bukan keputusan\)/)).toBeInTheDocument();
    const reviewCall = fetchMock.mock.calls.find(([u]) => u === "/api/claude/review-docs-checkpoint")!;
    expect(JSON.parse(String((reviewCall[1] as RequestInit).body))).toEqual({ revision_id: "cp1" });
    expect(screen.getByRole("button", { name: /Tambahkan ke versi rencana berikutnya/ })).toBeInTheDocument();
  });
});
