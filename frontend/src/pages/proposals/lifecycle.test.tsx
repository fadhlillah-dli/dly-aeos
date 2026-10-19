import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import detail from "./__fixtures__/detail.json";
import timeline from "./__fixtures__/timeline.json";
import { CheckpointCard, OwnerPlanCard, TrialCard } from "./LifecyclePanels";
import ProposalDetailPage from "./ProposalDetailPage";
import type { ProposalDetail } from "./types";

afterEach(() => vi.unstubAllGlobals());

const base = detail as unknown as ProposalDetail;

describe("Proposal lifecycle panels (§40 trial, owner plan, checkpoint)", () => {
  it("renders the owner plan, security assessment, trial launch and checkpoint for a trial-approved proposal", async () => {
    mockApi({ [`/api/proposals/${base.id}/timeline`]: timeline, [`/api/proposals/${base.id}`]: detail });
    renderPage(<ProposalDetailPage />, { path: "/proposals/:proposalId", route: `/proposals/${base.id}` });
    expect(await screen.findByText(/Rencana pemilik \(ditetapkan sebelum trial/)).toBeInTheDocument();
    expect(screen.getByText("Security / Privacy — batas data dan akses kandidat")).toBeInTheDocument();
    expect(screen.getByText("Trial terisolasi (di bawah keputusan TRIAL)")).toBeInTheDocument();
    expect(screen.getByText("Luncurkan trial")).toBeInTheDocument();
    expect(screen.getByText("Checkpoint Claude atas bukti trial (sebelum PROMOTE)")).toBeInTheDocument();
    expect(screen.getByText(base.owner_plan!.acceptance![0].criterion)).toBeInTheDocument();
  });

  it("launch posts to the trial endpoint; stop needs a reason", async () => {
    const calls = mockApi({ [`POST /api/proposals/${base.id}/trial`]: { trial: {} } });
    renderPage(<TrialCard p={base} isAdmin />);
    fireEvent.click(screen.getByText("Luncurkan trial"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === `/api/proposals/${base.id}/trial/launch`)).toBe(true));
    mockApi({});
    const launched = { ...base, status: "IN_TRIAL", trial: { status: "LAUNCHED" as const, plan_id: "plan1", task_ids: ["t1"] } };
    renderPage(<TrialCard p={launched} isAdmin />);
    expect(screen.getByText(/Hentikan trial/).closest("button")).toBeDisabled();
  });

  it("owner plan is locked once promotion is approved and hidden for VIEWER edits", () => {
    mockApi({});
    renderPage(<OwnerPlanCard p={{ ...base, status: "PROMOTION_APPROVED" }} isAdmin />);
    expect(screen.getByText(/Rencana terkunci/)).toBeInTheDocument();
    expect(screen.queryByText(/Simpan rencana/)).not.toBeInTheDocument();
  });

  it("NOT-PROVEN can only be recorded with a rationale and a failing checkpoint is shown as blocked", () => {
    mockApi({});
    const failing = {
      ...base,
      status: "PROMOTION_PENDING",
      checkpoint_review: { status: "COMPLETED", verdict: "FAIL", label: "data", at: "2026-10-19T00:00:00Z", evidence_ids: [], package_hash: "h", gate: "CHECKPOINT_REVIEW_FAILED" },
    };
    renderPage(<CheckpointCard p={failing} isAdmin />);
    expect(screen.getByText(/Gate: CHECKPOINT_REVIEW_FAILED/)).toBeInTheDocument();
    expect(screen.getByText(/Catat NOT-PROVEN/).closest("button")).toBeDisabled();
  });
});
