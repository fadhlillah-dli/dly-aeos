import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

const auth = { isAdmin: false, domain: "PERSONAL" };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

const responses: Record<string, unknown> = {};
vi.mock("../../api/client", () => ({
  get: vi.fn(async (path: string) => {
    if (!(path in responses)) throw new Error(`unmocked ${path}`);
    return responses[path];
  }),
  post: vi.fn(async () => ({})),
  ApiError: class extends Error {},
}));

import { post } from "../../api/client";
import WorkPage from "./index";
import RunDetailPage from "./RunDetailPage";
import OrganizationPage from "../organization/index";

const statuses = { worker_reported: "EXITED_0", verifier: "NOT_RUN", review: "PENDING", acceptance: "PENDING", merged: "NOT_MERGED" };
const row = {
  run_id: "11111111-2222-3333-4444-555555555555",
  run_type: "IMPLEMENTATION",
  attempt: 1,
  mode: "DEMO",
  project: { id: "p", name: "DLY AEOS", key: "dly-aeos" },
  domain: "PERSONAL",
  agent_key: "opencode-implementer",
  role_key: "feature-implementer",
  role_name: "Feature Implementer",
  task: { id: null, key: "demo-doctor-ui", title: null, feature_key: null },
  route: { key: "DEMO-route", model_resolved: "UNKNOWN", engine: "opencode", engine_version: "DEMO" },
  execution_state: "RUNNING",
  task_state: null,
  ui_state: "STALE",
  wait_reason: "",
  reason_code: "",
  current_action: "DEMO: Mengubah file x.tsx",
  current_action_source: "observed tool action (runtime)",
  started_at: new Date().toISOString(),
  finished_at: null,
  last_heartbeat_at: new Date(Date.now() - 600_000).toISOString(),
  heartbeat_age_seconds: 600,
  heartbeat_stale: true,
  touched_paths: ["x.tsx"],
  touched_count: 1,
  statuses,
  usage: { requests: null, prompt_tokens: null, completion_tokens: null, cost_usd: null, cost_known_usd: null, cost_unknown_requests: 0, quota_status: "UNKNOWN", ledger: null },
  caps: { demo: true },
  capability: null,
  evidence_refs: [],
  quarantined: false,
  exit_code: null,
  owner_requirements: ["Merge ke branch utama hanya oleh manusia (U-09)"],
  links: { run: "/runs/11111111-2222-3333-4444-555555555555" },
};
const summary = {
  checked_at: new Date().toISOString(),
  domain: "PERSONAL",
  mode: "LIVE",
  source: "x",
  scheduler: { enabled: false, paused: false, paused_reason: "", paused_by: null, concurrency_cap: 1, cap_decision_id: null, controller_id: "c", loop_running: false },
  task_counts: { QUEUED: 0, READY: 0, LEASED: 0, RUNNING: 0, REVIEW: 0, BLOCKED: 1, DONE: 0, FAILED: 0, CANCELLED: 0 },
  active_runs: 0,
  demo_runs: 1,
  blockers: [{ task_id: "t", task_key: "task-a", state: "BLOCKED", reason_code: "PLAN_NOT_APPROVED", reason: "plan has no ACTIVE approval", ui_state: "BLOCKED", attempts_used: 0, since: null }],
  cost: { source: "ledger", entries: 0, input_tokens: 0, output_tokens: 0, requests: 0, cost_known_usd: null, cost_unknown_entries: 0, cost_status: "NONE", quota: [] },
  sandbox: { status: "BLOCKED", kind: null, reasons: ["container sandbox not available"], remedies: ["record the owner EXCEPTION decision"], residual_risks: [] },
  engine: { status: "MISSING", version: null, pinned_version: "1.19.3", functional: "NOT_PROVEN", evidence: "x", checked_at: "x" },
};

function wrap(ui: ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/" element={ui} />
          <Route path="/runs/:runId" element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Live Work page", () => {
  beforeEach(() => {
    responses["/api/runs"] = { items: [row], source: "x", checked_at: new Date().toISOString(), mode: "DEMO" };
    responses["/api/workforce/summary"] = summary;
    responses["/api/workforce/concurrency"] = {
      items: [],
      concurrency: { cap: 1, decision_id: null, source: "PILOT_DEFAULT", reason: "pilot default", policy_cap: 1, budget_max_parallel_workers: null, budget_note: "not set", budget_limited: false },
      source: "x", checked_at: new Date().toISOString(), mode: "LIVE",
    };
  });

  it("lets the owner switch the scheduler loop on from the Console (W6-137)", async () => {
    auth.isAdmin = true;
    try {
      const user = userEvent.setup({ delay: null });
      wrap(<WorkPage />);
      await user.click(await screen.findByRole("button", { name: /Aktifkan loop scheduler/ }));
      expect(screen.getByText(/tidak memulai run apa pun dengan sendirinya/)).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Konfirmasi" }));
      await waitFor(() => expect(vi.mocked(post)).toHaveBeenCalledWith("/api/workforce/scheduler", { enabled: true, reason: "" }));
      expect(await screen.findByText("Batas efektif: 1 worker")).toBeInTheDocument();
    } finally {
      auth.isAdmin = false;
    }
  });

  it("does not call a budget-limited owner decision the pilot default (W6-145)", async () => {
    responses["/api/workforce/summary"] = {
      ...summary,
      scheduler: { ...summary.scheduler, concurrency_cap: 1, cap_decision_id: null, cap_source: "POLICY_DECISION", budget_max_parallel_workers: 1, cap_reason: "owner POLICY decision d1; limited to 1 by the owner resource budget max_parallel_workers=1" },
    };
    wrap(<WorkPage />);
    expect(await screen.findByText(/keputusan pemilik dibatasi budget resource/)).toBeInTheDocument();
    expect(screen.queryByText(/\(default pilot\)/)).not.toBeInTheDocument();
  });

  it("disables the loop toggle for VIEWER and points the empty state to the Console controls", async () => {
    responses["/api/runs"] = { items: [], source: "x", checked_at: new Date().toISOString(), mode: "LIVE" };
    wrap(<WorkPage />);
    expect(await screen.findByRole("button", { name: /Aktifkan loop scheduler/ })).toBeDisabled();
    expect(screen.getByText(/aeos work execute/)).toBeInTheDocument();
    expect(screen.queryByText(/POST \/api\/plans/)).not.toBeInTheDocument();
  });

  it("labels DEMO rows, shows stale heartbeat and separate statuses", async () => {
    wrap(<WorkPage />);
    expect(await screen.findByText(/adalah fixture untuk pratinjau UI/)).toBeInTheDocument();
    expect(screen.getByText("DEMO (fixture)")).toBeInTheDocument();
    expect(screen.getAllByText("Heartbeat usang").length).toBeGreaterThan(0);
    expect(screen.getByText(/Verifier: belum dijalankan/)).toBeInTheDocument();
    expect(screen.getByText(/Acceptance: belum/)).toBeInTheDocument();
    expect(screen.getByText(/plan has no ACTIVE approval/)).toBeInTheDocument();
    expect(screen.getByText(/Worker tidak dapat berjalan/)).toBeInTheDocument();
  });

  it("shows the current action with the source its writer recorded and the external resources touched", async () => {
    responses["/api/runs"] = {
      items: [{ ...row, current_action: "WAITING_QUOTA: upstream 429", current_action_source: "controller status (bukan aksi tool)",
                current_action_writer: "CONTROLLER", resources: [{ kind: "push_branch", target: "owner/repo:aeos/a", status: "DONE", external_url: null }] }],
      source: "x", checked_at: new Date().toISOString(), mode: "DEMO",
    };
    wrap(<WorkPage />);
    expect(await screen.findByText("WAITING_QUOTA: upstream 429")).toBeInTheDocument();
    expect(screen.getByText(/sumber: controller status/)).toBeInTheDocument();
    expect(screen.getByText(/push_branch: owner\/repo:aeos\/a \(DONE\)/)).toBeInTheDocument();
  });

  it("renders every Live Work contract column from the row (§35 Live Work contract)", async () => {
    responses["/api/runs"] = {
      items: [{ ...row, caps: { max_requests: 50, max_cost_usd: 0 }, evidence_refs: ["ev-1111111111", "ev-2222222222"],
        started_at: new Date(Date.now() - 120_000).toISOString() }],
      source: "x", checked_at: new Date().toISOString(), mode: "DEMO",
    };
    wrap(<WorkPage />);
    expect(await screen.findByText("opencode-implementer")).toBeInTheDocument(); // agent definition id
    expect(screen.getByText("Feature Implementer")).toBeInTheDocument(); // role
    expect(screen.getByText(/IMPLEMENTATION · attempt 1/)).toBeInTheDocument(); // run type + attempt
    expect(screen.getAllByText(/demo-doctor-ui/).length).toBeGreaterThan(0); // task / feature
    expect(screen.getByText("DEMO-route")).toBeInTheDocument(); // exact route
    expect(screen.getByText("model: UNKNOWN")).toBeInTheDocument(); // unresolved model is UNKNOWN, never guessed
    expect(screen.getByText(/opencode DEMO/)).toBeInTheDocument(); // engine + version
    expect(screen.getByText("Mulai")).toBeInTheDocument();
    expect(screen.getByText(/^2 (menit|minutes|min)/)).toBeInTheDocument(); // started_at (2 min ago), relative
    expect(screen.getByText("req ≤ 50 · biaya ≤ $0")).toBeInTheDocument(); // active caps
    const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(links).toContain("/evidence?id=ev-1111111111"); // evidence links open the evidence record
    expect(screen.getByText("2 referensi")).toBeInTheDocument();
  });

  it("disables admin actions for VIEWER (backend still enforces)", async () => {
    wrap(<WorkPage />);
    const cancel = await screen.findByRole("button", { name: /Batalkan/ });
    expect(cancel).toBeDisabled();
    expect(screen.getByRole("button", { name: /Jeda penjadwalan/ })).toBeDisabled();
    expect(screen.queryByText(/force-green/)).toBeInTheDocument(); // only in the "not available" note
  });

  it("explains the remedy when there are no runs", async () => {
    responses["/api/runs"] = { items: [], source: "x", checked_at: new Date().toISOString(), mode: "LIVE" };
    wrap(<WorkPage />);
    expect(await screen.findByText(/aeos work demo-fixtures/)).toBeInTheDocument();
  });
});

describe("Run detail page", () => {
  it("labels observed actions vs AI narration and shows quarantine", async () => {
    responses[`/api/runs/${row.run_id}`] = {
      ...row,
      mode: "LIVE",
      checked_at: new Date().toISOString(),
      quarantined: true,
      quarantine_reasons: [{ path: "policy/x.yaml", rule: "protected-path", detail: "policy/**" }],
      task_spec: null,
      task_execution: null,
      base_commit: "aaaaaaaaaaaa",
      head_commit: "bbbbbbbbbbbb",
      branch: "aeos/demo-1",
      workspace: { sandbox_kind: "process", strategy: "worktree", present: true },
      lease: null,
      diff_summary: { files: 1, added: 1, deleted: 0, paths: [{ path: "policy/x.yaml", status: "M", mode: null }] },
      actions: [
        { seq: 1, at: new Date().toISOString(), kind: "run.action", source: "runner", attempt: 1, lease_id: null, mode: "LIVE", payload: { label: "Mengubah file policy/x.yaml" }, label: "observed" },
        { seq: 2, at: new Date().toISOString(), kind: "run.action", source: "runner", attempt: 1, lease_id: null, mode: "LIVE", payload: { label: "Ringkasan AI", detail: "All tests pass", kind: "narration" }, label: "summary (narasi AI, bukan bukti)" },
      ],
      attempts: [],
      evidence: [],
      reviews: [],
      acceptance: [],
      decisions: [],
      side_effects: [],
      model_calls: [],
      error: "",
      block_reason: "",
    };
    wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    expect(await screen.findByText(/Patch dikarantina/)).toBeInTheDocument();
    expect(screen.getByText("Aksi alat teramati")).toBeInTheDocument();
    expect(screen.getByText("Ringkasan AI (bukan bukti)")).toBeInTheDocument();
    expect(screen.getByText(/Belum ada review atau acceptance/)).toBeInTheDocument();
  });

  it("shows the TaskSpec verifier_policy read-only, and an older server's missing field as not stated", async () => {
    const spec = {
      task_key: "a", title: "API login", goal: "Login API", feature_key: null, acceptance_criteria: ["AC1"], expected_outputs: ["code"],
      allowed_paths: ["src/**"], forbidden_paths: [], tools_allowed: [], egress_allowed: [], limits: {}, stop_conditions: [],
      evidence_requirements: [], risk_class: "R2", data_classification: "INTERNAL", depends_on: [], spec_hash: "sha256:aaaa",
      plan_version_id: "pv1", plan_version_hash: "sha256:bbbb", base_commit: null, capability_ttl_seconds: 600,
      verifier_policy: { required: true, suites: ["unit"], integration_tests: "required", contract_breaking: "approved" },
    };
    const detail = {
      ...row, mode: "LIVE", checked_at: new Date().toISOString(), quarantined: false, quarantine_reasons: [], task_spec: spec,
      task_execution: null, base_commit: "aaaaaaaaaaaa", head_commit: null, branch: "aeos/demo-1",
      workspace: { sandbox_kind: "process", strategy: "worktree", present: true }, lease: null, diff_summary: {}, actions: [],
      attempts: [], evidence: [], reviews: [], acceptance: [], decisions: [], side_effects: [], model_calls: [], error: "", block_reason: "",
    };
    responses[`/api/runs/${row.run_id}`] = detail;
    const { unmount } = wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    const facts = await screen.findByRole("group", { name: "Kebijakan verifier (hanya baca)" });
    expect(screen.getByText("Kebijakan verifier")).toBeInTheDocument();
    expect(facts).toHaveTextContent("wajib (required)");
    expect(facts).toHaveTextContent("disetujui (dinyatakan, tidak memblokir)");
    expect(facts).toHaveTextContent("suite: unit");
    // read-only: the immutable TaskSpec offers no control to change its policy
    expect(facts.querySelector("button, input, select, textarea")).toBeNull();
    unmount();

    const { verifier_policy: _omitted, ...older } = spec;
    void _omitted;
    responses[`/api/runs/${row.run_id}`] = { ...detail, task_spec: older };
    wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    expect(await screen.findByText("tidak dinyatakan API")).toBeInTheDocument();
    expect(screen.queryByText("wajib (required)")).not.toBeInTheDocument();
  });
});

describe("Run detail quarantine release and reconciliation", () => {
  const base = {
    ...row,
    mode: "LIVE",
    execution_state: "REVIEW",
    ui_state: "WAITING_REVIEW",
    checked_at: new Date().toISOString(),
    quarantined: true,
    quarantine_reasons: [{ path: "backend/aeos/broker/x.py", rule: "protected-path", detail: "backend/aeos/broker/**" }],
    task_spec: null,
    task_execution: null,
    base_commit: "aaaaaaaaaaaa",
    head_commit: "bbbbbbbbbbbbbbbb",
    branch: "aeos/demo-1",
    workspace: { sandbox_kind: "process", strategy: "isolated-clone", present: true },
    lease: null,
    diff_summary: {},
    actions: [],
    attempts: [],
    evidence: [],
    reviews: [],
    acceptance: [],
    decisions: [] as unknown[],
    side_effects: [{ id: "se1", kind: "PUSH_BRANCH", status: "UNKNOWN", target: { branch: "aeos/demo-1" }, external_url: null, commit_sha: null, updated_at: null, note: "" }],
    model_calls: [],
    error: "",
    block_reason: "",
  };

  it("offers the owner special-review release (disabled for VIEWER) and flags unreconciled side effects", async () => {
    responses[`/api/runs/${row.run_id}`] = base;
    wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    const btn = await screen.findByRole("button", { name: /Catat hasil review khusus/ });
    expect(btn).toBeDisabled();
    expect(screen.getByRole("button", { name: /Periksa status eksternal/ })).toBeDisabled();
    expect(screen.getByText(/SIDE_EFFECT_UNRECONCILED/)).toBeInTheDocument();
  });

  it("shows a recorded release only when it is bound to the current head", async () => {
    responses[`/api/runs/${row.run_id}`] = {
      ...base,
      decisions: [{ id: "d1", kind: "EXCEPTION", target_type: "run_quarantine", target_id: row.run_id, target_hash: "bbbbbbbbbbbbbbbb", status: "ACTIVE", rationale: "reviewed", created_at: null }],
    };
    wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    expect(await screen.findByText(/Hasil review khusus tercatat/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Catat hasil review khusus/ })).not.toBeInTheDocument();
  });

  it("does not treat a release for another head as valid", async () => {
    responses[`/api/runs/${row.run_id}`] = {
      ...base,
      decisions: [{ id: "d1", kind: "EXCEPTION", target_type: "run_quarantine", target_id: row.run_id, target_hash: "ffffffff", status: "ACTIVE", rationale: "old", created_at: null }],
    };
    wrap(<RunDetailPage />, `/runs/${row.run_id}`);
    expect(await screen.findByRole("button", { name: /Catat hasil review khusus/ })).toBeInTheDocument();
    expect(screen.queryByText(/Hasil review khusus tercatat/)).not.toBeInTheDocument();
  });
});

describe("Organization page", () => {
  it("shows inactive roles as available, never as working, and says org lines grant no permission", async () => {
    responses["/api/workforce/roles"] = {
      items: [
        {
          key: "fe-api-data",
          label: "FE/API/Data",
          roles: [
            { role_key: "frontend-engineer", name: "Frontend Engineer", kind: "EXECUTOR", group: "fe-api-data", description: "", outputs: ["UI"], boundaries: ["Satu TaskSpec"], verification: ["Browser test"], forbidden_actions: ["Mengubah policy/MCI"], deterministic_service: false, engine_hint: "", source_section: "§16", version: "1", revision_hash: "h", eval_status: "NOT_EVALUATED", mode: "DESIGN", active_runs: 0, demo_runs: 0, availability: "AVAILABLE_INACTIVE" },
          ],
        },
        {
          key: "platform-sre",
          label: "Platform & SRE",
          roles: [
            { role_key: "scheduler", name: "Scheduler", kind: "SERVICE", group: "platform-sre", description: "", outputs: ["Lease"], boundaries: [], verification: [], forbidden_actions: [], deterministic_service: true, engine_hint: "", source_section: "§16", version: "1", revision_hash: "h", eval_status: "NOT_EVALUATED", mode: "DESIGN", active_runs: 0, demo_runs: 0, availability: "SERVICE" },
          ],
        },
      ],
      note: "Org view adalah proyeksi katalog: garis organisasi tidak memberi permission atau authority.",
      checked_at: new Date().toISOString(),
      mode: "DESIGN",
    };
    responses["/api/workforce/agents"] = {
      items: [{ agent_key: "opencode-implementer", role_key: "feature-implementer", harness: "opencode", description: "OpenCode", route_policy: {}, tools: ["read"], limits: {}, allowed_scope: {}, enabled: false, enabled_reason: "Nonaktif sampai route worker APPROVED", deterministic: false, revision: "1", revision_hash: "h", mode: "DESIGN", owner: "owner", active_runs: 0, availability: "DISABLED" }],
      checked_at: new Date().toISOString(),
    };
    wrap(<OrganizationPage />);
    expect(await screen.findByText("Tersedia, tidak aktif")).toBeInTheDocument();
    expect(screen.getByText(/garis organisasi tidak memberi permission/)).toBeInTheDocument();
    expect(screen.getByText("Layanan kode, bukan LLM")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Nonaktif")).toBeInTheDocument());
    expect(screen.getAllByText("Belum dievaluasi")).toHaveLength(2);
    // §17: ownership (accountable human) and assignment are projected, never a permission
    expect(screen.getByText(/Pemilik \(manusia yang bertanggung jawab\): owner/)).toBeInTheDocument();
    expect(screen.getByTestId("assigned-frontend-engineer")).toHaveTextContent("0 task aktif");
    // W6-137: the owner control exists on the agent card (VIEWER: disabled; the backend still enforces RBAC)
    expect(screen.getByRole("button", { name: /^Aktifkan$/ })).toBeDisabled();
  });

  it("shows the competence definition (inputs, failure modes, eval cases) and an imported pack as a candidate", async () => {
    const base = { kind: "EXECUTOR", group: "fe-api-data", description: "", boundaries: [], verification: [], forbidden_actions: [], deterministic_service: false, engine_hint: "", source_section: "§16", version: "1", revision_hash: "h", eval_status: "NOT_EVALUATED", active_runs: 0, demo_runs: 0 };
    responses["/api/workforce/roles"] = {
      items: [
        {
          key: "fe-api-data",
          label: "FE/API/Data",
          roles: [
            { ...base, role_key: "integration-engineer", name: "Integration Engineer", outputs: ["Adapter"], mode: "DESIGN", availability: "AVAILABLE_INACTIVE", inputs: ["Kontrak provider"], failure_modes: ["Tanpa idempotensi"], example_outputs: ["Adapter + contract test"], evaluation_cases: [] },
            { ...base, role_key: "ext-api-reviewer", name: "API Contract Helper", outputs: ["Diff kontrak"], mode: "CANDIDATE", availability: "CANDIDATE", evaluation_cases: [{ suite_key: "eval.x", case_ids: ["c1", "c2"] }] },
          ],
        },
      ],
      note: "Org view adalah proyeksi katalog: garis organisasi tidak memberi permission atau authority.",
      checked_at: new Date().toISOString(),
      mode: "DESIGN",
    };
    responses["/api/workforce/agents"] = { items: [], checked_at: new Date().toISOString() };
    wrap(<OrganizationPage />);
    expect(await screen.findByText("Kontrak provider")).toBeInTheDocument();
    expect(screen.getByText("Tanpa idempotensi")).toBeInTheDocument();
    expect(screen.getByText(/belum ada suite evaluasi terproteksi/)).toBeInTheDocument();
    expect(screen.getByText(/eval\.x \(2 kasus\)/)).toBeInTheDocument();
    expect(screen.getByText(/Kandidat impor/)).toBeInTheDocument();
  });
});
