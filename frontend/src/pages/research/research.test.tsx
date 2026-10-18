import { screen } from "@testing-library/react";
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
});
