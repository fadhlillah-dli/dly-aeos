import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import detail from "./__fixtures__/detail.json";
import { RemovalClosure } from "./LifecyclePanels";
import type { ProposalDetail, RemovalItem } from "./types";

// §40 W6-201: credential revocation of a REPLACE/UPGRADE is tracked per exact secret ref (progress, not a gate);
// the owner records CLOSED/WAIVED, which PATCHes owner_plan.credentials. A SHARED ref is marked as such.
const p = detail as unknown as ProposalDetail;
const items: RemovalItem[] = [
  {
    id: "credential:personal/github",
    kind: "credential",
    item: "Cabut credential ref personal/github setelah consumer sah dipindahkan",
    secret_ref: "personal/github",
    shared_with: [],
    status: "OPEN",
    note: "",
    evidence_id: null,
  },
  {
    id: "credential:workers/openrouter",
    kind: "credential",
    item: "Cabut credential ref workers/openrouter - workers/openrouter: SHARED - masih dipakai litellm",
    secret_ref: "workers/openrouter",
    shared_with: ["litellm"],
    status: "OPEN",
    note: "",
    evidence_id: null,
  },
];

describe("credential revocation progress (§40, W6-201)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("records a revoked ref under 'credentials' and marks a SHARED ref", async () => {
    const calls = mockApi({ [`/api/proposals/${p.id}/plan`]: { rebinds_candidate: false, proposal: p } });
    renderPage(<RemovalClosure p={p} items={items} isAdmin planKey="credentials" />);
    expect(screen.getByText("Yang harus dicabut")).toBeInTheDocument();
    expect(screen.getAllByText("SHARED (masih dipakai)")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Catatan credential:personal/github"), { target: { value: "dicabut di GitHub" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Tutup" })[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const sent = calls.find((c) => c.method === "PATCH")!;
    expect(sent.url).toBe(`/api/proposals/${p.id}/plan`);
    expect(sent.body).toEqual({ credentials: { "credential:personal/github": { status: "CLOSED", note: "dicabut di GitHub" } } });
  });

  it("keeps REMOVE closure on the 'removal' key and shows no controls to a viewer", () => {
    mockApi({});
    const { unmount } = renderPage(<RemovalClosure p={p} items={items} isAdmin={false} planKey="credentials" />);
    expect(screen.queryByRole("button", { name: "Tutup" })).not.toBeInTheDocument();
    unmount();
    renderPage(<RemovalClosure p={p} items={items} isAdmin />);
    expect(screen.getByText("Yang harus ditutup")).toBeInTheDocument();
  });
});
