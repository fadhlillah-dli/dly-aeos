import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AsvsPanel } from "./AsvsPanel";
import type { AsvsTrace } from "./api";

const trace: AsvsTrace = {
  asvs_version: "4.0.3",
  catalog_version: "4.0.3",
  application: "dly-aeos-control-plane",
  items: [
    { id: "V14.4.3", chapter: "V14", title: "CSP header", status: "TESTED", tests: ["tests/unit/test_asvs_controls.py::test_x"] },
    { id: "V3.3.4", chapter: "V3", title: "List sessions", status: "NOT_PROVEN", note: "no session list UI" },
    { id: "V9.1.1", chapter: "V9", title: "TLS", status: "NOT_SELECTED", reason: "loopback only" },
    { id: "V1.4.2", chapter: "V1", title: "[DELETED] not actionable", status: "DELETED", reason: "deleted in ASVS 4.0.3" },
  ],
  chapters: [
    { chapter: "V1", name: "Architecture", tested: 0, not_proven: 0, not_selected: 0, deleted: 1, unlisted: 0 },
    { chapter: "V3", name: "Session Management", tested: 0, not_proven: 1, not_selected: 0, deleted: 0, unlisted: 0 },
    { chapter: "V9", name: "Communication", tested: 0, not_proven: 0, not_selected: 1, deleted: 0, unlisted: 0 },
    { chapter: "V14", name: "Configuration", tested: 1, not_proven: 0, not_selected: 0, deleted: 0, unlisted: 0 },
  ],
  totals: { tested: 1, not_proven: 1, not_selected: 1, deleted: 1 },
  selection_sha256: "sha256:" + "a".repeat(64),
  catalog_sha256: "sha256:" + "b".repeat(64),
  per_project: { status: "UNDECIDED", reason: "owner decision" },
  evidence_note: "TESTED = trace",
  source: "contracts/security/asvs-selection.yaml",
  checked_at: null,
  generated_at: "2026-10-25T00:00:00Z",
  mode: "LIVE",
};

function wrap() {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(trace), { status: 200 })) as typeof fetch;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AsvsPanel />
    </QueryClientProvider>,
  );
}

describe("AsvsPanel", () => {
  it("opens on what is not proven, never labels a traced control as passed, and states the per-project gap", async () => {
    wrap();
    expect(await screen.findByText("V3.3.4")).toBeInTheDocument();
    expect(screen.queryByText("V14.4.3")).not.toBeInTheDocument(); // default filter: not proven
    expect(screen.getAllByText("Belum terbukti").length).toBeGreaterThan(0);
    expect(screen.getByText(/BELUM DIPUTUSKAN/)).toBeInTheDocument();
    expect(screen.queryByText(/Lulus/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Ada test" }));
    expect(screen.getByText("V14.4.3")).toBeInTheDocument();
    expect(screen.getByText("Ada test (terlacak)")).toBeInTheDocument();
    expect(screen.getByText("tests/unit/test_asvs_controls.py::test_x")).toBeInTheDocument();
  });

  it("shows exclusions with their reason and filters by chapter", async () => {
    wrap();
    await screen.findByText("V3.3.4");
    await userEvent.click(screen.getByRole("button", { name: "Semua" }));
    expect(screen.getByText("loopback only")).toBeInTheDocument();
    expect(screen.getByText("Tidak dipilih (alasan tercatat)")).toBeInTheDocument();
    const chapters = screen.getByText("Per bab").closest("section") as HTMLElement;
    await userEvent.click(within(chapters).getByText(/V9 Communication/));
    expect(screen.getByText("Kontrol V9 Communication")).toBeInTheDocument();
    expect(screen.queryByText("V3.3.4")).not.toBeInTheDocument();
  });
});
