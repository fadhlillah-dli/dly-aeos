import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import EvaluationPage from "./index";

const emptyBoard = {
  role: null,
  source: "eval_runs",
  checked_at: "2026-10-18T00:00:00Z",
  status: "BELUM_DIUJI",
  message: "Semua kandidat BELUM DIUJI dalam platform ini",
  suites: [],
  candidates_not_tested: [{ label: "GLM-5.3 Flash Free", route_key: "opencode-zen/glm", status: "BELUM_DIUJI", source: "model registry (ModelRoute)" }],
  combined_note: "Ranking gabungan hanya memakai bobot workload yang dideklarasikan",
  route_change: "NONE - hasil evaluasi tidak mengubah route",
  ranking_rule: "Hanya konfigurasi yang lulus gate",
  dimensions: [],
};

afterEach(() => vi.unstubAllGlobals());

describe("Evaluation page (§43)", () => {
  it("shows BELUM DIUJI empty state and untested candidates when no runs exist", async () => {
    mockApi({
      "/api/eval/suites": { items: [], checked_at: "2026-10-18T00:00:00Z", source: "eval_suites" },
      "/api/eval/experiments": { items: [], common_gates: {}, measures: [], not_measured: [], dimensions: [], note: "" },
      "/api/eval/runs": { items: [] },
      "/api/eval/leaderboard": emptyBoard,
    });
    renderPage(<EvaluationPage />);
    expect(await screen.findByText("Semua kandidat BELUM DIUJI dalam platform ini")).toBeInTheDocument();
    expect(screen.getByText("GLM-5.3 Flash Free")).toBeInTheDocument();
    expect(screen.getAllByText("BELUM DIUJI").length).toBeGreaterThan(0);
    expect(screen.getByText(/tidak mengubah route/)).toBeInTheDocument();
  });

  it("renders ranking with ties, sample size and excluded gate failures", async () => {
    const entry = (hash: string, label: string, rank: number, tie: boolean) => ({
      configuration_hash: hash,
      label,
      configuration: {},
      topologies: ["A"],
      runs: 1,
      sample_size: 10,
      final_acceptance_rate: 0.6,
      first_pass_acceptance_rate: 0.5,
      acceptance_ci95: [0.31, 0.83],
      acceptance_variance: 0.24,
      failures_reported: 2,
      metrics: { performance: { latency_p50_s: 60, latency_p95_s: 90 }, economics: { worker_cost_per_accepted_usd: null, worker_cost_status: "UNKNOWN" } },
      eligibility: { eligible: true, status: "ELIGIBLE", gates_failed: [], gates_unproven: [] },
      small_sample: false,
      rank,
      tie,
    });
    mockApi({
      "/api/eval/suites": { items: [], checked_at: null, source: "eval_suites" },
      "/api/eval/experiments": { items: [], common_gates: {}, measures: [], not_measured: [], dimensions: [], note: "" },
      "/api/eval/runs": { items: [] },
      "/api/eval/leaderboard": {
        ...emptyBoard,
        status: "HAS_RESULTS",
        message: null,
        candidates_not_tested: [],
        suites: [
          {
            suite_key: "eval.small-bugfix",
            title: "Small bugfix",
            role_key: "feature-implementer",
            status: "RANKED",
            ranking: [entry("sha256:aaaa", "model-a", 1, true), entry("sha256:bbbb", "model-b", 1, true)],
            excluded: [{ ...entry("sha256:cccc", "fast-but-unsafe", 0, false), eligibility: { eligible: false, status: "EXCLUDED_GATE_FAILED", gates_failed: ["security"], gates_unproven: [] } }],
          },
        ],
      },
    });
    renderPage(<EvaluationPage />);
    expect(await screen.findAllByText("#1 (seri)")).toHaveLength(2);
    expect(screen.getByText("fast-but-unsafe")).toBeInTheDocument();
    expect(screen.getByText("Dikecualikan: gate gagal")).toBeInTheDocument();
    expect(screen.getByText("security: FAIL")).toBeInTheDocument();
  });
});
