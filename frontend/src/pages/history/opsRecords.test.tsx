import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { ReleasesTab } from "./ReleasesTab";
import { RetirementView } from "./RetirementTab";
import type { RetirementChecklist } from "./types";

const withClient = (ui: ReactElement) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("Releases tab (§09 zones, W6-030)", () => {
  it("shows the recorded UI commit correspondence per release, with the reason when it is not VERIFIED", async () => {
    const row = { version: "1.0.0", commit: "c".repeat(40), artifact_sha256: "s".repeat(64), schema_head: null, built_at: new Date().toISOString(), integrity: "OK", role: "AVAILABLE" };
    vi.stubGlobal("fetch", vi.fn(async () => json({
      items: [
        { ...row, release_id: "1.0.0-verified", ui: { sha256: "u".repeat(64), commit_correspondence: "VERIFIED", reason: null, frontend_tree: "f".repeat(40) } },
        { ...row, release_id: "1.0.0-unverified", ui: { sha256: "v".repeat(64), commit_correspondence: "UNVERIFIED", reason: "built from a frontend tree with uncommitted changes", frontend_tree: null } },
        { ...row, release_id: "1.0.0-noui", ui: null },
      ],
      active: null, previous: null, history: [], notice: "Promote/rollback hanya lewat CLI lokal.",
      source: "release provenance manifests", checked_at: new Date().toISOString(), mode: "LIVE",
    })));
    withClient(<ReleasesTab />);
    expect(await screen.findByText("UI = commit (terverifikasi)")).toBeInTheDocument();
    expect(screen.getByText("UI belum terverifikasi")).toBeInTheDocument();
    expect(screen.getByText("built from a frontend tree with uncommitted changes")).toBeInTheDocument();
    expect(screen.getByText("Tanpa bundle UI")).toBeInTheDocument();
    expect(screen.getByText("Bundle UI")).toBeInTheDocument();
  });
});

describe("Retirement steps (§33, W6-197)", () => {
  const data: RetirementChecklist = {
    project: { id: "p1", name: "Alpha", domain: "PERSONAL", lifecycle: "RETIRING" },
    items: [
      {
        key: "communication", title: "Komunikasi", status: "OWNER_ACTION", detail: {}, owner_action: "Informasikan stakeholder.",
        recorded: { status: "DONE", owner: "pemilik", evidence_ids: ["ev-1"], note: "stakeholder sudah diberi tahu", by: "u1", at: new Date().toISOString() },
      },
      { key: "data", title: "Retensi dan disposal data", status: "OWNER_DECISION_REQUIRED", detail: {}, owner_action: "Disposal manual." },
      { key: "final_verification", title: "Verifikasi akhir pemilik", status: "PENDING_OWNER", detail: {}, owner_action: "Catat keputusan." },
    ],
    blocking: [],
    checklist_hash: "sha256:abc",
    notice: "Daftar periksa read-only.",
    checked_at: new Date().toISOString(),
  };

  it("shows what the owner recorded for a step", () => {
    withClient(<RetirementView data={data} />);
    expect(screen.getByLabelText("Langkah tercatat")).toHaveTextContent("stakeholder sudah diberi tahu");
    expect(screen.getByLabelText("Langkah tercatat")).toHaveTextContent("oleh pemilik");
    expect(screen.getByText("ev-1")).toBeInTheDocument();
    expect(screen.getByText("Selesai")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Catat langkah" })).not.toBeInTheDocument(); // read-only without admin
  });

  it("offers 'Catat langkah' per step (not for the final verification) and PUTs the step record", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), method: String(init.method), body: init.body ? JSON.parse(String(init.body)) : null });
      return json({ project_id: "p1", step: "data", recorded: {} });
    }));
    withClient(<RetirementView data={data} canRecord />);
    const buttons = screen.getAllByRole("button", { name: "Catat langkah" });
    expect(buttons).toHaveLength(2); // communication + data; final verification is the RETIREMENT decision
    fireEvent.click(buttons[1]);
    fireEvent.change(screen.getByLabelText("Status langkah data"), { target: { value: "IN_PROGRESS" } });
    fireEvent.change(screen.getByLabelText("Catatan langkah data"), { target: { value: "retensi lampiran diputuskan" } });
    fireEvent.change(screen.getByLabelText("Bukti langkah data"), { target: { value: "ev-2, ev-3" } });
    fireEvent.click(screen.getByRole("button", { name: "Simpan langkah" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      url: "/api/ops/retirement/p1/steps/data",
      method: "PUT",
      body: { status: "IN_PROGRESS", owner: "", note: "retensi lampiran diputuskan", evidence_ids: ["ev-2", "ev-3"] },
    });
  });
});
