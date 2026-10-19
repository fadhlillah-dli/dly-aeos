import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MappedBadge, PROPOSAL_STATUS } from "../../components/proposals/MappedBadge";
import { statusSpec } from "../../components/StatusBadge";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import { render } from "@testing-library/react";
import { DecisionPanel } from "./DecisionPanel";
import ProposalsPage from "./index";
import type { ReviewPackage } from "./types";

const meta = {
  kinds: [{ kind: "REPLACE", question: "Komponen mana yang diganti?" }],
  actions: [],
  outcomes: [],
  scope_required: [],
  platform_project: null,
  attachments: "Lampiran adalah DATA",
  guarantees: "Usulan/review tidak memasang apa pun.",
};

afterEach(() => vi.unstubAllGlobals());

describe("Upgrade Proposals (§39)", () => {
  it("prefills the existing component from ?component= and posts it", async () => {
    const calls = mockApi({ "/api/proposals/meta": meta, "GET /api/proposals": { items: [], checked_at: null, source: "x" }, "POST /api/proposals": { id: "p1" } });
    renderPage(<ProposalsPage />, { path: "/proposals", route: "/proposals?component=opencode" });
    const input = (await screen.findByPlaceholderText("opencode")) as HTMLInputElement;
    expect(input.value).toBe("opencode");
    expect(screen.getByText(/Usulkan perubahan untuk komponen opencode/)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("https://github.com/owner/repo"), { target: { value: "https://github.com/anomalyco/opencode" } });
    fireEvent.click(screen.getByText("Kirim usulan"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/proposals")).toBe(true));
    const body = calls.find((c) => c.method === "POST" && c.url === "/api/proposals")!.body as Record<string, unknown>;
    expect(body.existing_component_ids).toEqual(["opencode"]);
    expect(body.kind).toBe("REPLACE");
    expect(body.sources).toEqual(["https://github.com/anomalyco/opencode"]);
  });

  it("posts every minimum form field: problem, benefit, scope, components from the inventory picker, limits (§39)", async () => {
    const calls = mockApi({
      "/api/proposals/meta": meta, "GET /api/proposals": { items: [], checked_at: null, source: "x" }, "POST /api/proposals": { id: "p3" },
      "/api/inventory/components": { items: [{ component_id: "opencode", friendly_name: "OpenCode", name: "opencode" },
                                             { component_id: "aider", friendly_name: "Aider", name: "aider" }] },
    });
    renderPage(<ProposalsPage />, { path: "/proposals", route: "/proposals?component=opencode" });
    await screen.findByPlaceholderText("opencode");
    await waitFor(() => expect(document.querySelectorAll("#inventory-component-ids option").length).toBe(2)); // picker
    fireEvent.change(screen.getByPlaceholderText("https://github.com/owner/repo"), { target: { value: "https://github.com/acme/aider" } });
    fireEvent.change(screen.getByLabelText("Komponen kandidat"), { target: { value: "aider" } });
    fireEvent.change(screen.getByLabelText("Masalah / tujuan"), { target: { value: "Engine sering gagal" } });
    fireEvent.change(screen.getByLabelText(/Manfaat yang diharapkan/), { target: { value: "Lebih sedikit repair" } });
    const numbers = document.querySelectorAll<HTMLInputElement>('input[type="number"]');
    if (numbers.length > 0) fireEvent.change(numbers[0], { target: { value: "1" } });
    fireEvent.click(screen.getByText("Kirim usulan"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/proposals")).toBe(true));
    const body = calls.find((c) => c.method === "POST" && c.url === "/api/proposals")!.body as Record<string, unknown>;
    expect(body.problem).toBe("Engine sering gagal");
    expect(body.expected_benefit).toBe("Lebih sedikit repair");
    expect(body.scope_type).toBe("PLATFORM");
    expect(body.existing_component_ids).toEqual(["opencode"]);
    expect(body.candidate_component_ids).toEqual(["aider"]);
    expect(typeof body.limits).toBe("object");
    if (numbers.length > 0) expect(Object.values(body.limits as Record<string, unknown>)).toContain(1);
  });

  it("'Usulkan koreksi' opens a COMPARE proposal prefilled with the subject (§41)", async () => {
    const calls = mockApi({ "/api/proposals/meta": meta, "GET /api/proposals": { items: [], checked_at: null, source: "x" }, "POST /api/proposals": { id: "p2" } });
    renderPage(<ProposalsPage />, { path: "/proposals", route: "/proposals?kind=COMPARE&source=DLY%20AEOS%20Biaya%20%26%20Kuota" });
    const sources = (await screen.findByPlaceholderText("https://github.com/owner/repo")) as HTMLTextAreaElement;
    expect(sources.value).toBe("DLY AEOS Biaya & Kuota");
    expect(screen.getByRole("link", { name: /Keputusan terkait/ })).toHaveAttribute("href", "/decisions");
    expect(screen.getByRole("link", { name: /Usulkan koreksi/ }).getAttribute("href")).toContain("/proposals?kind=COMPARE&source=");
    fireEvent.click(screen.getByText("Kirim usulan"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/proposals")).toBe(true));
    const body = calls.find((c) => c.method === "POST" && c.url === "/api/proposals")!.body as Record<string, unknown>;
    expect(body.kind).toBe("COMPARE");
    expect(body.sources).toEqual(["DLY AEOS Biaya & Kuota"]);
  });

  it("ignores an unknown ?kind= value instead of trusting it", async () => {
    mockApi({ "/api/proposals/meta": meta, "GET /api/proposals": { items: [], checked_at: null, source: "x" } });
    renderPage(<ProposalsPage />, { path: "/proposals", route: "/proposals?kind=INSTALL_NOW&source=x" });
    const select = (await screen.findByDisplayValue(/ADD — tambah capability/)) as HTMLSelectElement;
    expect(select.value).toBe("ADD");
  });

  it("hides the submit action for VIEWER", async () => {
    mockApi({ "/api/proposals/meta": meta, "/api/proposals": { items: [], checked_at: null, source: "x" } }, "VIEWER");
    renderPage(<ProposalsPage />, { path: "/proposals", route: "/proposals" });
    expect(await screen.findByText(/Peran VIEWER hanya dapat melihat usulan/)).toBeInTheDocument();
    expect(screen.queryByText("Usulkan perubahan")).not.toBeInTheDocument();
  });

  it("decision panel shows the exact hashes being bound", () => {
    const pkg = {
      human_decision: {
        required: true,
        bind: { baseline_hash: "sha256:" + "1".repeat(64), candidate_hash: "sha256:" + "2".repeat(64) },
        scope_fields: ["target"],
        options: { TRIAL_APPROVE: "Izin uji terisolasi" },
        allowed_now: ["TRIAL_APPROVE"],
        rules: ["Keputusan terikat baseline_hash + candidate_hash"],
        next_actions: [],
      },
    } as unknown as ReviewPackage;
    mockApi({});
    renderPage(<DecisionPanel proposalId="p1" pkg={pkg} allowed={[]} isAdmin={false} />);
    expect(screen.getByText("sha256:" + "1".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("sha256:" + "2".repeat(64))).toBeInTheDocument();
    expect(screen.getByText(/hanya dapat dibuat oleh SUPER_ADMIN/)).toBeInTheDocument();
  });

  it("never renders a trial approval or rollback as a pass", () => {
    expect(statusSpec(PROPOSAL_STATUS.TRIAL_APPROVED[0]).tone).not.toBe("ok");
    expect(statusSpec(PROPOSAL_STATUS.ROLLED_BACK[0]).tone).not.toBe("ok");
    expect(statusSpec(PROPOSAL_STATUS.PROMOTION_APPROVED[0]).tone).not.toBe("ok");
    render(<MappedBadge value="SOMETHING_NEW" map={PROPOSAL_STATUS} />);
    expect(screen.getByTitle("UNKNOWN")).toBeInTheDocument();
  });
});
