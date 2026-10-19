import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import detail from "./__fixtures__/detail.json";
import timeline from "./__fixtures__/timeline.json";
import ProposalDetailPage from "./ProposalDetailPage";

// Fixtures are REAL backend responses (generated through the FastAPI app with a trial decision), so
// this test guards the API <-> UI contract of the 10 review-package sections.
afterEach(() => vi.unstubAllGlobals());

describe("Proposal detail renders a real review package", () => {
  it("shows all ten §39 sections, the bound hashes and the trial-is-not-promotion state", async () => {
    const id = (detail as { id: string }).id;
    mockApi({ [`/api/proposals/${id}/timeline`]: timeline, [`/api/proposals/${id}`]: detail });
    renderPage(<ProposalDetailPage />, { path: "/proposals/:proposalId", route: `/proposals/${id}` });
    expect(await screen.findByText("Ringkasan fungsi, masalah dan relevansi")).toBeInTheDocument();
    for (const title of [
      "Identitas sumber (terverifikasi / belum) dengan tanggal",
      "Kondisi baseline saat ini",
      "Alternatif",
      "Dampak up/downstream",
      "Rencana eksperimen",
      "Biaya (aktual / estimasi / UNKNOWN)",
      "Rekomendasi Claude — REVIEW / VERIFY / VERDICT",
      "Keputusan manusia yang diperlukan",
      "Hasil setelah implementasi",
    ]) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    const bind = (detail as { review_package: { human_decision: { bind: { baseline_hash: string; candidate_hash: string } } } }).review_package.human_decision.bind;
    expect(screen.getAllByText(bind.baseline_hash).length).toBeGreaterThan(0);
    expect(screen.getAllByText(bind.candidate_hash).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Trial disetujui (bukan promosi)").length).toBeGreaterThan(0);
    expect(screen.getByText("Rencana penggantian dan rollback (§40)")).toBeInTheDocument();
    expect(await screen.findByText("Sebelum (baseline)")).toBeInTheDocument();
    expect(screen.getByText("ARCHITECTURE_SOURCE_UPDATED")).toBeInTheDocument();
    // §41 detail: added/candidate components and "kapan aktif" (never the approval time)
    expect(screen.getByText("Komponen kandidat (ditambah/pengganti)")).toBeInTheDocument();
    expect(screen.getByText("Kapan aktif")).toBeInTheDocument();
    expect(screen.getByText(/belum aktif \(tidak ada observasi ENABLED/)).toBeInTheDocument();
  });

  it("shows the candidate components and the observed activation time when the inventory saw them ENABLED", async () => {
    const id = (detail as { id: string }).id;
    const at = "2026-10-19T08:00:00+00:00";
    const active = {
      ...(detail as Record<string, unknown>),
      candidate_component_ids: ["aider"],
      delivery_dimensions: { ...((detail as { delivery_dimensions: Record<string, unknown> }).delivery_dimensions),
        ACTIVE: { status: "ENABLED_IN_CONFIG", note: "inventaris", at } },
    };
    mockApi({ [`/api/proposals/${id}/timeline`]: timeline, [`/api/proposals/${id}`]: active });
    renderPage(<ProposalDetailPage />, { path: "/proposals/:proposalId", route: `/proposals/${id}` });
    expect(await screen.findByRole("link", { name: "aider" })).toHaveAttribute("href", "/tools/aider");
    expect(screen.queryByText(/belum aktif \(tidak ada observasi ENABLED/)).not.toBeInTheDocument();
  });
});
