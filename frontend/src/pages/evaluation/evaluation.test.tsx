import { fireEvent, screen } from "@testing-library/react";
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

  it("keeps topologies apart, shows the baseline delta, regression/terms exclusions and declared-weight ranking", async () => {
    const base = {
      label: "m@r",
      configuration: {},
      runs: 1,
      sample_size: 50,
      final_acceptance_rate: 0.5,
      first_pass_acceptance_rate: 0.4,
      acceptance_ci95: [0.36, 0.64],
      acceptance_variance: 0.25,
      failures_reported: 0,
      metrics: { performance: { latency_p50_s: 60, latency_p95_s: 90 }, economics: { worker_cost_per_accepted_usd: 0.1, worker_cost_status: "COMPUTED" } },
      eligibility: { eligible: true, status: "ELIGIBLE", gates_failed: [], gates_unproven: [] },
      small_sample: false,
      tie: false,
    };
    mockApi({
      "/api/eval/suites": { items: [], checked_at: null, source: "eval_suites" },
      "/api/eval/experiments": { items: [], common_gates: {}, measures: [], not_measured: [], dimensions: [], note: "" },
      "/api/eval/runs": { items: [{ id: "run-1", suite_key: "eval.small-bugfix", role_key: null, topology: null, status: "RUNNING", configuration: { model_id: "m", provider_route: "r" }, configuration_hash: "sha256:x", sample_size: 0, failures: [], created_at: null, is_baseline: true, execution: { status: "COLLECTING", note: "kolektor", fixtures: "NOT_AVAILABLE_IN_BUILD" } }] },
      "/api/eval/leaderboard": {
        ...emptyBoard,
        status: "HAS_RESULTS",
        message: null,
        candidates_not_tested: [],
        combined: { weights: { "eval.small-bugfix": 3 }, source: "decision:d1", rule: "rata-rata berbobot", incomplete: [], ranking: [{ entry_key: "h1|C", label: "m@r", score: 0.9, interval: [0.8, 0.96], sample_size: 50, rank: 1, tie: false }] },
        suites: [
          {
            suite_key: "eval.small-bugfix",
            title: "Small bugfix",
            role_key: "feature-implementer",
            status: "RANKED",
            baseline: "h1|A",
            ranking: [
              { ...base, entry_key: "h1|C", configuration_hash: "sha256:h1", topology: "C", final_acceptance_rate: 0.9, rank: 1, delta_vs_baseline: { acceptance_rate: 0.4, interval: [0.2, 0.55], conclusion: "BETTER" } },
              { ...base, entry_key: "h1|A", configuration_hash: "sha256:h1", topology: "A", rank: 2, is_baseline: true, delta_vs_baseline: null },
            ],
            excluded: [
              { ...base, entry_key: "h2|-", configuration_hash: "sha256:h2", label: "regressed", topology: null, eligibility: { eligible: false, status: "EXCLUDED_REGRESSION", gates_failed: [], gates_unproven: [] } },
              { ...base, entry_key: "h3|-", configuration_hash: "sha256:h3", label: "gemini", topology: null, eligibility: { eligible: false, status: "EXCLUDED_ROUTE_TERMS", gates_failed: [], gates_unproven: [], route_reasons: ["data classification INTERNAL not allowed"] } },
            ],
            not_comparable: [{ ...base, entry_key: "h4|-", configuration_hash: "sha256:h4", label: "more-budget", topology: null }],
          },
        ],
      },
    });
    renderPage(<EvaluationPage />);
    expect(await screen.findByText("+40 pp (lebih baik)")).toBeInTheDocument();
    expect(screen.getAllByText("Baseline").length).toBeGreaterThan(0);
    expect(screen.getAllByText("C").length).toBeGreaterThan(0); // topology C and A are separate entries
    expect(screen.getAllByText("A").length).toBeGreaterThan(0);
    expect(screen.getByText("Dikecualikan: regresi kritis")).toBeInTheDocument();
    expect(screen.getByText("Dikecualikan: rute tidak eligible (terms/data)")).toBeInTheDocument();
    expect(screen.getByText(/data classification INTERNAL not allowed/)).toBeInTheDocument();
    expect(screen.getByText("more-budget")).toBeInTheDocument();
    expect(screen.getByText(/Ranking gabungan \(bobot workload/)).toBeInTheDocument();
  });

  it("states the controlled execution path of a planned run and why it cannot start (§49 M6 arena)", async () => {
    const run = (id: string, route: string, serving: Record<string, unknown>) => ({
      id, suite_key: "eval.small-bugfix", role_key: null, topology: null, status: "PLANNED", configuration: { model_id: "m", provider_route: route },
      configuration_hash: "sha256:x", sample_size: 0, failures: [], created_at: null, is_baseline: false,
      execution: { status: "NOT_STARTED", note: "belum", fixtures: "NOT_AVAILABLE_IN_BUILD", serving },
    });
    mockApi({
      "/api/eval/suites": { items: [], checked_at: null, source: "eval_suites" },
      "/api/eval/experiments": { items: [], common_gates: {}, measures: [], not_measured: [], dimensions: [], note: "" },
      "/api/eval/runs": {
        items: [
          run("r-arena", "arena.free-x", { path: "EVAL_ONLY", pool: "ARENA", reasons: [] }),
          run("r-comp", "comparator.x", { path: "NONE", pool: "COMPARATOR", reasons: ["pool COMPARATOR has no controlled execution path in this build"] }),
          run("r-paid", "arena.paid", { path: "EVAL_ONLY", pool: "ARENA", reasons: ["route is not free (is_free=NO)"] }),
        ],
      },
      "/api/eval/leaderboard": emptyBoard,
    });
    renderPage(<EvaluationPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "Run" }));
    expect(await screen.findByText("Rute ARENA: hanya task yang terikat run ini")).toBeInTheDocument();
    expect(screen.getByText("Tidak dapat dieksekusi (COMPARATOR)")).toBeInTheDocument();
    expect(screen.getByText(/no controlled execution path in this build/)).toBeInTheDocument();
    expect(screen.getByText("Tidak dapat dieksekusi (ARENA)")).toBeInTheDocument();
    expect(screen.getByText(/route is not free/)).toBeInTheDocument();
  });

  it("shows a run excluded because no verifier baseline proved it regression-free as not proven, never as a pass", async () => {
    const { ELIGIBILITY } = await import("../../components/proposals/MappedBadge");
    expect(ELIGIBILITY.EXCLUDED_REGRESSION_NOT_PROVEN[0]).toBe("NOT-PROVEN");
    expect(ELIGIBILITY.EXCLUDED_REGRESSION_NOT_PROVEN[1]).toMatch(/belum terbukti/);
  });

  it("shows the controlled baseline-vs-candidate comparison of a proposal (§40), never as a route change", async () => {
    const side = (rate: number, cost: number) => ({
      status: "RECORDED",
      run_ids: ["r"],
      result: {
        configuration_hash: "sha256:x",
        label: "m@r",
        configuration: {},
        topologies: ["-"],
        runs: 1,
        sample_size: 10,
        final_acceptance_rate: rate,
        first_pass_acceptance_rate: rate,
        acceptance_ci95: [0.2, 0.9],
        acceptance_variance: 0.2,
        failures_reported: 0,
        metrics: { performance: { latency_p50_s: null, latency_p95_s: null }, economics: { worker_cost_per_accepted_usd: cost, worker_cost_status: "COMPUTED" } },
        eligibility: { eligible: true, status: "ELIGIBLE", gates_failed: [], gates_unproven: [] },
        small_sample: false,
      },
    });
    const run = (id: string, isBase: boolean) => ({
      id, suite_key: "eval.small-bugfix", role_key: null, topology: null, status: "COMPLETED", configuration: { model_id: "m", provider_route: "r" },
      configuration_hash: "sha256:" + id, sample_size: 10, failures: [], created_at: null, is_baseline: isBase, proposal_id: "prop-1",
    });
    mockApi({
      "/api/eval/suites": { items: [], checked_at: null, source: "eval_suites" },
      "/api/eval/experiments": { items: [], common_gates: {}, measures: [], not_measured: [], dimensions: [], note: "" },
      "/api/eval/runs": { items: [run("b", true), run("c", false)] },
      "/api/eval/leaderboard": emptyBoard,
      "/api/eval/comparisons/prop-1": {
        proposal_id: "prop-1", status: "RECORDED", route_change: "NONE",
        rule: "Baseline vs kandidat pada suite, limit dan environment yang sama.",
        suites: [{ suite_key: "eval.small-bugfix", baseline: side(0.8, 0.125), candidate: side(0.5, 0.16), equal_conditions: true,
                   delta: { acceptance_rate: -0.3, interval: [-0.7, 0.1], conclusion: "NOT_DISTINGUISHABLE" }, verdict: "NOT_DISTINGUISHABLE" }],
      },
    });
    renderPage(<EvaluationPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "Run" }));
    expect(await screen.findByText(/Perbandingan terkendali per proposal/)).toBeInTheDocument();
    expect(await screen.findByText("Tidak dapat dibedakan (interval tumpang tindih)")).toBeInTheDocument();
    expect(screen.getByText(/accepted 80% · n=10 · biaya\/accepted \$0.125/)).toBeInTheDocument();
    expect(screen.getByText(/accepted 50% · n=10 · biaya\/accepted \$0.16/)).toBeInTheDocument();
    expect(screen.getByText(/Rute tidak berubah otomatis \(NONE\)/)).toBeInTheDocument();
  });
});
