import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

const auth = { isAdmin: true, domain: "PERSONAL", projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

import { AdrList, MciBanner } from "./PolicyPanels";

const MCI = {
  name: "MCI_V2_SOURCE_OF_TRUTH",
  declared_status: "UNAVAILABLE",
  status: "UNVERIFIED",
  verification_code: "NOT_SUPPLIED",
  compliance: "NOT-PROVEN",
  version: null,
  content_hash: null,
  source_location: null,
  source_placement: "LOCAL_ONLY",
  local_directory: "<data dir>/policy/canonical",
  note: "",
  verification_note: "MCI_V2 source of truth not supplied; alignment UNVERIFIED.",
  packs: ["ARCH", "QA"],
  pack_mappings: ["ARCH", "QA"].map((key) => ({
    key,
    mci_controls: [],
    mapping_status: "UNVERIFIED",
    evidence_contract_status: "UNDECIDED",
    mapping_source: "NONE",
    note: "MCI_V2 source is not VERIFIED",
    decision_id: null,
    binding: { kind: "POLICY", target_type: "mci_pack_mapping", target_id: key, target_hash: `sha256:${key.toLowerCase()}0123456789abcdef` },
  })),
  pack_mapping_file: "<data dir>/policy/canonical/mci-pack-mapping.yaml",
  pack_mapping_state: "ABSENT",
  packs_missing: [],
  packs_mapping_status: "UNVERIFIED",
  risk_classes: [{ key: "R0", label: "read", threshold_status: "UNVERIFIED" }],
  checked_at: "2026-10-26T00:00:00Z",
};

const ADRS = {
  items: [
    {
      key: "ADR-0004",
      title: "First-admin bootstrap hardening",
      status: "PROPOSED",
      file_status: "PROPOSED",
      owner_status: null,
      owner_status_state: "NOT_RECORDED",
      owner_decision: null,
      effective_status: "PROPOSED",
      status_source: "FILE (builder-written)",
      source_path: "docs/decisions/ADR-0004-first-admin-bootstrap-hardening.md",
      content_hash: "sha256:abcdef0123456789abcdef",
      decision_id: null,
      supersedes: null,
      date: null,
      deciders: null,
      updated_at: null,
    },
    {
      key: "ADR-0107",
      title: "Verifier isolated runner",
      status: "PROPOSED",
      file_status: "PROPOSED",
      owner_status: "ACCEPTED",
      owner_status_state: "STALE",
      owner_decision: { id: "d1", outcome: "ACCEPTED", target_hash: "sha256:old", decided_at: null, superseded_by: null },
      effective_status: "PROPOSED",
      status_source: "FILE (builder-written)",
      source_path: "docs/decisions/ADR-0107-verifier-isolated-runner.md",
      content_hash: "sha256:fedcba9876543210",
      decision_id: null,
      supersedes: null,
      date: null,
      deciders: null,
      updated_at: null,
    },
  ],
  remedy: null,
  owner_status_counts: { NOT_RECORDED: 1, STALE: 1 },
};

function renderWith(node: React.ReactNode, routes: Record<string, unknown>) {
  const fn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST") return new Response(JSON.stringify({ ok: true }), { status: 201, headers: { "Content-Type": "application/json" } });
    const key = Object.keys(routes).find((r) => u.startsWith(r));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);
  return fn;
}

describe("MCI banner (§02, §34)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("states the local-only source and every pack slot with its mapping status, never compliance", async () => {
    renderWith(<MciBanner />, { "/api/policy/mci": MCI });
    expect(await screen.findByTestId("mci-local-only")).toHaveTextContent("<data dir>/policy/canonical");
    expect(screen.getByTestId("mci-local-only")).toHaveTextContent("repositori publik hanya memuat referensi");
    const packs = screen.getByTestId("mci-packs");
    expect(packs).toHaveTextContent("Pemetaan 2 pack");
    expect(packs).toHaveTextContent("0/2 disetujui pemilik");
    expect(packs).toHaveTextContent("ARCH");
    expect(packs).toHaveTextContent("mci-pack-mapping.yaml");
    expect(screen.getByText("NOT_SUPPLIED")).toBeInTheDocument();
    expect(screen.queryByText(/Lulus|Patuh/)).not.toBeInTheDocument();
  });
});

describe("ADR register: owner decision vs builder-written status (§02)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows both statuses and records the owner's decision bound to the shown content hash", async () => {
    const fn = renderWith(<AdrList />, { "/api/adrs/ADR-0004": { ...ADRS.items[0], sections: {}, meta: {}, linked_decisions: [] }, "/api/adrs": ADRS });
    expect(await screen.findAllByText("File: PROPOSED", { exact: false })).toHaveLength(2);
    expect(screen.getByText("Pemilik belum memutuskan")).toBeInTheDocument();
    expect(screen.getByText(/Keputusan pemilik usang/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /ADR-0004/ }));
    const form = await screen.findByTestId("adr-decide-ADR-0004");
    expect(form).toHaveTextContent("abcdef01234567"); // the content hash the decision binds (shortened)
    const submit = screen.getByRole("button", { name: "Catat keputusan ADR" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Keputusan ADR-0004"), { target: { value: "SUPERSEDED" } });
    fireEvent.change(screen.getByLabelText("Alasan ADR-0004"), { target: { value: "digantikan oleh ADR yang lebih baru" } });
    expect(submit).toBeDisabled(); // SUPERSEDED needs the successor
    fireEvent.change(screen.getByLabelText("Pengganti ADR-0004"), { target: { value: "ADR-0107" } });
    fireEvent.click(submit);
    await waitFor(() => expect(fn.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const [url, init] = fn.mock.calls.find(([, i]) => i?.method === "POST")!;
    expect(String(url)).toBe("/api/adrs/ADR-0004/decision");
    expect(JSON.parse(String(init!.body))).toEqual({
      outcome: "SUPERSEDED",
      rationale: "digantikan oleh ADR yang lebih baru",
      content_hash: "sha256:abcdef0123456789abcdef",
      superseded_by: "ADR-0107",
    });
  });

  it("offers no decision form to a VIEWER", async () => {
    auth.isAdmin = false;
    try {
      renderWith(<AdrList />, { "/api/adrs/ADR-0004": { ...ADRS.items[0], sections: {}, meta: {}, linked_decisions: [] }, "/api/adrs": ADRS });
      fireEvent.click(await screen.findByRole("button", { name: /ADR-0004/ }));
      await waitFor(() => expect(screen.queryByTestId("adr-decide-ADR-0004")).not.toBeInTheDocument());
    } finally {
      auth.isAdmin = true;
    }
  });
});
