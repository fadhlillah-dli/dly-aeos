import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import ResearchPage from "./index";

const base = {
  entry_key: "",
  category: "orchestration",
  source_refs: [],
  placement: "",
  upstream_status: "NOT_CHECKED",
  checked_at: null,
  freshness: "NOT_CHECKED",
  research_date: "2026-10-17/18",
  release_date: null,
  component_id: null,
  source_kind: "GITHUB_REPO",
  mode: "DESIGN",
  section_refs: ["§45"],
  is_reference: false,
  latest_tag: null,
  update_available: null,
};

afterEach(() => vi.unstubAllGlobals());

describe("Research registry (§45, §47)", () => {
  it("shows unverified identities without a guessed URL and offers check-upstream only when checkable", async () => {
    mockApi({
      "/api/research?kind=reference": { items: [], source: "r", checked_at: null, mode: "DESIGN", lifecycles: [], evidence_limit: "x" },
      "/api/research": {
        items: [
          { ...base, id: "1", name: "DDorchestrator", identity: "name:ddorchestrator", urls: [], evidence_level: "E0", lifecycle: "DISCOVERED", identity_status: "UNVERIFIED", checkable: false },
          { ...base, id: "2", name: "Paperclip", identity: "github:paperclipai/paperclip", urls: ["https://github.com/paperclipai/paperclip"], evidence_level: "E1", lifecycle: "REFERENCE_ONLY", identity_status: "STATED_BY_MASTER", checkable: true },
        ],
        source: "research_entries",
        checked_at: null,
        mode: "DESIGN",
        lifecycles: ["DISCOVERED", "REFERENCE_ONLY"],
        evidence_limit: "Tahap riset bukan bukti instalasi/koneksi/penggunaan",
      },
    });
    renderPage(<ResearchPage />, { path: "/research", route: "/research" });
    expect(await screen.findByText("DDorchestrator")).toBeInTheDocument();
    expect(screen.getByText(/Tanpa URL — identitas belum diverifikasi/)).toBeInTheDocument();
    expect(screen.getAllByText("Belum diverifikasi").length).toBeGreaterThan(0);
    const buttons = screen.getAllByText("Check upstream").map((el) => el.closest("button")!);
    expect(buttons[0]).toBeDisabled();
    expect(buttons[1]).not.toBeDisabled();
    expect(screen.getAllByText(/Belum dicek live/).length).toBe(2);
  });

  it("shows the §47 conclusion/coverage limits, the platform result column and the owner fields of an entry", async () => {
    const entry = {
      ...base,
      id: "3",
      name: "Widget",
      identity: "github:acme/widget",
      urls: ["https://github.com/acme/widget"],
      evidence_level: "E0",
      lifecycle: "DISCOVERED",
      identity_status: "CHECKED",
      upstream_status: "OK",
      checkable: true,
      platform_result: null,
    };
    const calls = mockApi({
      "/api/research?kind=reference": { items: [], source: "r", checked_at: null, mode: "DESIGN", lifecycles: [], evidence_limit: "x" },
      "/api/research/3": {
        ...entry,
        relevance: "",
        claims: [],
        limitations: "",
        license: "MIT",
        source_version: null,
        maintainer: "acme",
        data_terms: "",
        upstream: {},
        history: [],
        advisories: { status: "AFFECTED", affected: ["GHSA-1"], count: 1, note: "Versi kandidat berada dalam rentang rentan" },
        update_notice: { from: "1.0", to: "1.1", summary: "Widget: 1.0 -> 1.1", draft_link: "/proposals?kind=UPGRADE&component=widget" },
        editable_fields: ["maintainer", "source_version", "data_terms"],
        can_confirm_source: true,
        test_results: [],
      },
      "POST /api/research/3/confirm-source": { ...entry, lifecycle: "SOURCE_VERIFIED" },
      "/api/research": {
        items: [entry],
        source: "research_entries",
        checked_at: null,
        mode: "DESIGN",
        lifecycles: ["DISCOVERED"],
        evidence_limit: "Tahap riset bukan bukti",
        conclusion: "Kesimpulan 18 Okt 2026: belum ada bukti DLY AEOS mengungguli seluruh metode",
        coverage_limit: "Peringkat GitHub Trending tidak dapat diverifikasi langsung",
      },
    });
    renderPage(<ResearchPage />, { path: "/research", route: "/research?entry=3" });
    expect(await screen.findByText(/belum ada bukti DLY AEOS mengungguli/)).toBeInTheDocument();
    expect(screen.getByText(/GitHub Trending tidak dapat diverifikasi/)).toBeInTheDocument();
    expect(screen.getByText("Belum diuji di platform ini")).toBeInTheDocument();
    expect(await screen.findByText("acme")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Buka draf usulan UPGRADE/ })).toHaveAttribute("href", "/proposals?kind=UPGRADE&component=widget");
    fireEvent.click(screen.getByText(/Konfirmasi sumber/));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/research/3/confirm-source")).toBe(true));
  });
});
