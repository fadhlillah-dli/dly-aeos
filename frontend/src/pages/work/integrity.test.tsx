import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

// Wave 7b UI lane 2 (F worker integrity): the host-wide integrity hold, the runtime's tamper windows and caps, the
// DB-trust quarantine reasons, the HIGH runtime alerts and the broker's reasoning levels.

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
import { changeLabel, IntegrityHoldBanner, neutralisationSpec, reconcileCommand } from "./IntegrityHold";
import RunDetailPage from "./RunDetailPage";
import { quarantineRuleLabel, reasoningRequestedLabel, reasoningSentLabel } from "./runtimeCaps";

const RUN_ID = "11111111-2222-3333-4444-555555555555";
const HOLD_ID = "9f0c3a52-1111-4222-8333-444455556666";
const statuses = { worker_reported: "EXITED_0", verifier: "NOT_RUN", review: "PENDING", acceptance: "PENDING", merged: "NOT_MERGED" };
const row = {
  run_id: RUN_ID,
  run_type: "IMPLEMENTATION",
  attempt: 1,
  mode: "LIVE",
  project: { id: "p", name: "DLY AEOS", key: "dly-aeos" },
  domain: "PERSONAL",
  agent_key: "opencode-implementer",
  role_key: "feature-implementer",
  role_name: "Feature Implementer",
  task: { id: null, key: "task-a", title: null, feature_key: null },
  route: { key: "worker.r", model_resolved: "UNKNOWN", engine: "opencode", engine_version: "1.19.3" },
  execution_state: "REVIEW",
  task_state: null,
  ui_state: "BLOCKED",
  wait_reason: "",
  reason_code: "",
  current_action: "",
  current_action_source: "",
  started_at: new Date().toISOString(),
  finished_at: null,
  last_heartbeat_at: new Date().toISOString(),
  heartbeat_age_seconds: 1,
  heartbeat_stale: false,
  touched_paths: [],
  touched_count: 0,
  statuses,
  usage: { requests: null, prompt_tokens: null, completion_tokens: null, cost_usd: null, cost_known_usd: null, cost_unknown_requests: 0, quota_status: "UNKNOWN", ledger: null },
  caps: { integrity_hold: true } as Record<string, unknown>,
  capability: null,
  evidence_refs: [],
  quarantined: true,
  exit_code: 0,
  owner_requirements: [],
  links: { run: `/runs/${RUN_ID}` },
};

const holdSandbox = {
  status: "BLOCKED",
  kind: null,
  code: "INTEGRITY_HOLD",
  reasons: [`integrity hold ${HOLD_ID.slice(0, 8)}: the controller changed; no worker starts until the owner reconciles`],
  remedies: [
    "Review the findings (aeos worker status), restore the controller checkout (git status / git checkout -- <path>) and data, then record the reconciliation: " +
      `aeos policy decide --kind RISK_ACCEPTANCE --target-type worker_integrity_hold --target-id ${HOLD_ID} --as <SUPER_ADMIN> --domain <PERSONAL|OFFICE> --days 1 --rationale "<what you checked and restored>"`,
  ],
  residual_risks: [],
  integrity_hold: {
    id: HOLD_ID,
    detected_at: "2026-10-26T08:00:00+00:00",
    source: "worker run",
    subject_id: RUN_ID,
    findings: [
      { kind: "controller", path: "backend/aeos/core/config.py", change: "modified", detail: "", source: "worker run", subject_id: RUN_ID, domain: "PERSONAL" },
      { kind: "controller", path: "config:settings.toml", change: "modified", detail: "owner config file changed while the worker ran; restored to the pre-run content", source: "worker run", subject_id: RUN_ID, domain: "PERSONAL" },
      { kind: "db", path: "decisions:abc", change: "added", source: "worker run", subject_id: RUN_ID, domain: "PERSONAL" },
    ],
    other_domain_findings: 2,
  },
};

const summary = {
  checked_at: new Date().toISOString(),
  domain: "PERSONAL",
  mode: "LIVE",
  source: "x",
  scheduler: { enabled: false, paused: false, paused_reason: "", paused_by: null, concurrency_cap: 1, cap_decision_id: null, controller_id: "c", loop_running: false },
  task_counts: {},
  active_runs: 0,
  demo_runs: 0,
  blockers: [],
  cost: { source: "ledger", entries: 0, input_tokens: 0, output_tokens: 0, requests: 0, cost_known_usd: null, cost_unknown_entries: 0, cost_status: "NONE", quota: [] },
  sandbox: holdSandbox,
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

beforeEach(() => {
  auth.isAdmin = false;
  auth.domain = "PERSONAL";
  for (const k of Object.keys(responses)) delete responses[k];
  responses["/api/runs"] = { items: [row], source: "x", checked_at: new Date().toISOString(), mode: "LIVE" };
  responses["/api/workforce/summary"] = summary;
  responses["/api/workforce/concurrency"] = {
    items: [],
    concurrency: { cap: 1, decision_id: null, source: "PILOT_DEFAULT", reason: "pilot default", policy_cap: 1, budget_max_parallel_workers: null, budget_note: "not set", budget_limited: false },
    source: "x",
    checked_at: new Date().toISOString(),
    mode: "LIVE",
  };
  vi.mocked(post).mockClear();
});

describe("Integrity hold on the Live Work page", () => {
  it("shows the hold prominently: every domain blocked, findings, other-domain count and the CLI remedy (VIEWER read-only)", async () => {
    wrap(<WorkPage />);
    const panel = await screen.findByRole("region", { name: "Integrity hold aktif" });
    expect(panel).toHaveTextContent("semua worker di semua domain diblokir");
    expect(panel).toHaveTextContent("PERSONAL maupun OFFICE");
    expect(panel).toHaveTextContent("INTEGRITY_HOLD");
    expect(panel).toHaveTextContent("backend/aeos/core/config.py");
    expect(panel).toHaveTextContent("Config dipulihkan ke isi sebelum run");
    expect(panel).toHaveTextContent("Tabel DB tepercaya");
    expect(panel).toHaveTextContent("+ 2 temuan dari domain lain");
    expect(panel).toHaveTextContent(`--target-id ${HOLD_ID}`);
    expect(panel).toHaveTextContent("<PERSONAL|OFFICE>"); // the domain is the owner's choice, never filled in
    expect(screen.getByRole("link", { name: RUN_ID })).toHaveAttribute("href", `/runs/${RUN_ID}`);
    expect(screen.getByRole("link", { name: "Keputusan terkait hold ini" })).toHaveAttribute(
      "href",
      `/decisions?target_type=worker_integrity_hold&target_id=${HOLD_ID}`,
    );
    expect(screen.queryByRole("button", { name: /Catat rekonsiliasi/ })).toBeNull();
    expect(panel).toHaveTextContent("peran VIEWER hanya-baca");
    expect(screen.getByText("Sandbox: terblokir (integrity hold)")).toBeInTheDocument();
    expect(screen.queryByText(/sandbox yang disyaratkan belum tersedia/)).toBeNull(); // not the generic notice
    expect(screen.getByText("Memicu integrity hold")).toBeInTheDocument(); // the run row's cap column
  });

  it("hides another domain's subject run: only that the hold exists and how many findings it has", async () => {
    responses["/api/workforce/summary"] = {
      ...summary,
      sandbox: { ...holdSandbox, integrity_hold: { ...holdSandbox.integrity_hold, subject_id: null, findings: [], other_domain_findings: 3 } },
    };
    wrap(<WorkPage />);
    const panel = await screen.findByRole("region", { name: "Integrity hold aktif" });
    expect(panel).toHaveTextContent("Di domain lain (detail tidak ditampilkan di domain ini)");
    expect(panel).toHaveTextContent("Tidak ada temuan dari domain ini");
    expect(panel).toHaveTextContent("+ 3 temuan dari domain lain");
  });

  it("records the owner's reconciliation only through the Decision API, after two explicit steps", async () => {
    auth.isAdmin = true;
    responses["/api/workforce/agents"] = { items: [] };
    vi.mocked(post).mockResolvedValueOnce({ id: "dec-hold-1" });
    const user = userEvent.setup({ delay: null });
    wrap(<WorkPage />);
    await user.click(await screen.findByRole("button", { name: /Catat rekonsiliasi/ }));
    const form = screen.getByRole("group", { name: "Rekonsiliasi integrity hold" });
    expect(form).toHaveTextContent(`worker_integrity_hold:${HOLD_ID}`);
    expect(form).toHaveTextContent("domain sesi PERSONAL");
    const confirm = screen.getByRole("button", { name: /Konfirmasi keputusan RISK_ACCEPTANCE/ });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Apa yang Anda periksa dan pulihkan" }), "git checkout -- config.py; data diperiksa");
    expect(confirm).toBeDisabled(); // the review statement is required too
    await user.click(screen.getByRole("checkbox", { name: /Saya sudah meninjau temuan/ }));
    expect(confirm).not.toBeDisabled();
    expect(vi.mocked(post)).not.toHaveBeenCalled();
    await user.click(confirm);
    await waitFor(() => expect(vi.mocked(post)).toHaveBeenCalledTimes(1));
    const [url, body] = vi.mocked(post).mock.calls[0] as unknown as [string, Record<string, unknown>];
    expect(url).toBe("/api/decisions");
    expect(body).toMatchObject({
      kind: "RISK_ACCEPTANCE",
      target_type: "worker_integrity_hold",
      target_id: HOLD_ID,
      rationale: "git checkout -- config.py; data diperiksa",
      auth_scope: { integrity_hold: HOLD_ID },
    });
    expect(new Date(String(body.expires_at)).getTime()).toBeGreaterThan(Date.now());
    expect(await screen.findByText(/Rekonsiliasi tercatat sebagai keputusan/)).toBeInTheDocument();
    expect(screen.getByText("dec-hold-1")).toBeInTheDocument();
  });

  it("keeps the recorded reconciliation confirmed after the hold is gone, and a new hold gets a fresh form", async () => {
    auth.isAdmin = true;
    responses["/api/workforce/agents"] = { items: [] };
    vi.mocked(post).mockResolvedValueOnce({ id: "dec-hold-2" });
    const user = userEvent.setup({ delay: null });
    wrap(<WorkPage />);
    const open = await screen.findByRole("button", { name: /Catat rekonsiliasi/ });
    await user.click(open);
    // the form's first control takes focus; cancelling returns focus to the opener
    expect(screen.getByRole("checkbox", { name: /Saya sudah meninjau temuan/ })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Batal" }));
    expect(screen.getByRole("button", { name: /Catat rekonsiliasi/ })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: /Catat rekonsiliasi/ }));
    await user.click(screen.getByRole("checkbox", { name: /Saya sudah meninjau temuan/ }));
    await user.type(screen.getByRole("textbox", { name: "Apa yang Anda periksa dan pulihkan" }), "checkout dipulihkan dan diperiksa");
    // the backend re-reads admission: a NEW finding raised a new hold meanwhile
    const NEW_HOLD = "aaaaaaaa-1111-4222-8333-444455556666";
    responses["/api/workforce/summary"] = {
      ...summary,
      sandbox: { ...holdSandbox, integrity_hold: { ...holdSandbox.integrity_hold, id: NEW_HOLD } },
    };
    await user.click(screen.getByRole("button", { name: /Konfirmasi keputusan RISK_ACCEPTANCE/ }));
    expect(await screen.findByText(/Hold baru aktif \(temuan lain\)/)).toBeInTheDocument();
    expect(screen.getByText("dec-hold-2")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("region", { name: "Integrity hold aktif" })).toHaveTextContent(NEW_HOLD));
    // the new hold's form starts over: the earlier result never stands for it
    expect(screen.getByRole("button", { name: /Catat rekonsiliasi/ })).toBeInTheDocument();
    // and once no hold is active, the confirmation stays on the page
    responses["/api/workforce/summary"] = { ...summary, sandbox: { status: "BLOCKED", kind: null, reasons: [], remedies: [], residual_risks: [], code: null, integrity_hold: null } };
    await user.click(screen.getByRole("button", { name: /Catat rekonsiliasi/ }));
    await user.click(screen.getByRole("checkbox", { name: /Saya sudah meninjau temuan/ }));
    await user.type(screen.getByRole("textbox", { name: "Apa yang Anda periksa dan pulihkan" }), "checkout dipulihkan dan diperiksa");
    vi.mocked(post).mockResolvedValueOnce({ id: "dec-hold-3" });
    await user.click(screen.getByRole("button", { name: /Konfirmasi keputusan RISK_ACCEPTANCE/ }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Integrity hold aktif" })).toBeNull());
    expect(screen.getByText(/Status sandbox di bawah dibaca ulang dari backend/)).toBeInTheDocument();
    expect(screen.getByText("dec-hold-3")).toBeInTheDocument();
    expect(screen.getByText(NEW_HOLD)).toBeInTheDocument();
  });

  it("the Ringkasan banner appears only while a hold is active and links to the owner steps", async () => {
    responses["/api/worker-runtime/sandbox"] = holdSandbox;
    const { unmount } = wrap(<IntegrityHoldBanner />);
    const panel = await screen.findByRole("region", { name: "Integrity hold aktif" });
    expect(panel).toHaveTextContent("semua worker di semua domain diblokir");
    expect(screen.getByRole("link", { name: /Lihat temuan dan langkah pemilik/ })).toHaveAttribute("href", "/work");
    unmount();
    responses["/api/worker-runtime/sandbox"] = { status: "BLOCKED", kind: null, reasons: ["container not available"], remedies: [], residual_risks: [] };
    const { container } = wrap(<IntegrityHoldBanner />);
    await waitFor(() => expect(container.querySelector("[data-testid=integrity-hold]")).toBeNull());
    expect(screen.queryByRole("region", { name: "Integrity hold aktif" })).toBeNull();
  });

  it("labels findings and runtime actions without ever calling them a pass", () => {
    expect(changeLabel("incomplete")).toMatch(/tidak lengkap/);
    expect(changeLabel(undefined)).toBe("UNKNOWN");
    expect(neutralisationSpec("cache file could NOT be removed")).toEqual({ status: "FAILED", label: "Netralisasi gagal - pulihkan manual" });
    expect(neutralisationSpec("owner config ...; NOT restored (no pre-run copy kept)")?.status).toBe("FAILED");
    expect(neutralisationSpec("owner config file changed while the worker ran; not restored (no config path)")?.status).toBe("FAILED");
    expect(neutralisationSpec("owner config file changed while the worker ran; restored to the pre-run content")?.status).toBe("UPDATED");
    expect(neutralisationSpec("")).toBeNull();
    expect(reconcileCommand(holdSandbox.remedies)).toMatch(/^aeos policy decide --kind RISK_ACCEPTANCE/);
    expect(reconcileCommand(["install docker"])).toBeNull();
    expect(quarantineRuleLabel("db-trust-check-unknown")).toMatch(/UNKNOWN, bukan lulus/);
    expect(quarantineRuleLabel("some-new-rule")).toBe("some-new-rule");
    expect(reasoningSentLabel(null).status).toBe("UNKNOWN");
    expect(reasoningRequestedLabel("NOT_SENT", null).label).toBe("Pekerja tidak meminta");
    expect(reasoningRequestedLabel(null, null).status).toBe("UNKNOWN");
  });
});

describe("Run detail: runtime integrity, DB-trust quarantine, alerts and reasoning", () => {
  const detail = {
    ...row,
    checked_at: new Date().toISOString(),
    quarantine_reasons: [
      { path: "db:decisions:abc", rule: "db-trust-table-modified", detail: "added" },
      { path: "db:*", rule: "db-trust-check-unknown", detail: "window not opened" },
      { path: "controller:*", rule: "controller-modified", detail: "incomplete: more than 20000 checkout files: not every controller file was compared (fail closed)" },
      {
        path: "controller:backend/aeos/__pycache__/x.pyc",
        rule: "controller-modified",
        detail: "added: bytecode not equal to a fresh compile of its source (MISMATCH); cache file removed (recompiled from source on next import)",
      },
    ],
    caps: {
      integrity_hold: true,
      controller_integrity: { checked_files: 812, changed: 2 },
      db_trust_window: { status: "FAIL", foreign_count: 1, tables: 9, rows: 120, own_writes: 3, tolerated_count: 4 },
      cpu_cap: { enforced: false, max_cpus: 2, job_cpu_rate: null, by: "none (reservation only)" },
      command_logs: { present: true, files: 3, bytes: 2048, truncated: false, skipped: 0, removed_from_candidate: true, evidence_ref: "evidence:ev-cmdlog-1" },
      context_pack: {
        used: true,
        items: 4,
        token_budget: 6000,
        pack_hash: "sha256:" + "e".repeat(64),
        freshness: "CURRENT",
        artifacts_in_pack: true,
        artifacts_omitted: 1,
        build: { built: true, items: 4, token_budget: 6000, token_estimate: 4100, omitted: 1, warnings: [] },
      },
    },
    task_spec: null,
    task_execution: null,
    base_commit: "aaaaaaaaaaaa",
    head_commit: "bbbbbbbbbbbb",
    branch: "aeos/demo-1",
    workspace: { sandbox_kind: "process", strategy: "worktree", present: true },
    lease: null,
    diff_summary: {},
    actions: [
      { seq: 1, at: new Date().toISOString(), kind: "worker_runtime.alert", source: "worker_runtime", attempt: 1, lease_id: null, mode: "LIVE", payload: { severity: "HIGH", code: "DB_TRUST_TAMPER", rows: 1 }, label: "observed" },
      {
        seq: 2,
        at: new Date().toISOString(),
        kind: "run.model_call",
        source: "broker",
        attempt: 1,
        lease_id: null,
        mode: "LIVE",
        payload: { outcome: "OK", prompt_tokens: 10, completion_tokens: 5, cost_usd: 0, reasoning_effort: "NOT_SENT", reasoning_requested_dropped: "high" },
        label: "observed",
      },
    ],
    attempts: [],
    evidence: [],
    reviews: [],
    acceptance: [],
    decisions: [],
    side_effects: [],
    model_calls: [
      { at: new Date().toISOString(), route_key: "w", outcome: "OK", status: 200, prompt_tokens: 10, completion_tokens: 5, cost_usd: 0, model_resolved: "m", latency_ms: 5, reasoning_effort: "NOT_SENT", reasoning_requested: "high" },
      { at: new Date(Date.now() - 1000).toISOString(), route_key: "w", outcome: "OK", status: 200, prompt_tokens: 11, completion_tokens: 5, cost_usd: 0, model_resolved: "m", latency_ms: 5, reasoning_effort: "medium", reasoning_requested: "UNRECOGNISED" },
      { at: new Date(Date.now() - 2000).toISOString(), route_key: "w", outcome: "OK", status: 200, prompt_tokens: 12, completion_tokens: 5, cost_usd: 0, model_resolved: "m", latency_ms: 5 },
    ],
    error: "",
    block_reason: "",
  };

  it("shows the runtime's tamper windows, the hold, the CPU cap, runner logs and the context pack", async () => {
    responses[`/api/runs/${RUN_ID}`] = detail;
    wrap(<RunDetailPage />, `/runs/${RUN_ID}`);
    expect(await screen.findByText("Run ini memicu integrity hold (semua domain diblokir)")).toBeInTheDocument();
    expect(screen.getByText("2 temuan dari 812 file diperiksa")).toBeInTheDocument();
    expect(screen.getByText("Perubahan asing pada tabel tepercaya")).toBeInTheDocument();
    expect(screen.getByText(/1 baris asing · 4 ditoleransi/)).toBeInTheDocument();
    expect(screen.getByText("Tidak ditegakkan: none (reservation only)")).toBeInTheDocument();
    expect(screen.getByText("Dari sisi worker (E0, tidak tepercaya)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Bukti log perintah/ })).toHaveAttribute("href", "/evidence?id=ev-cmdlog-1");
    expect(screen.getByText(/ContextPack dipakai \(4 item, budget 6000 token\)/)).toBeInTheDocument();
    expect(screen.getByText(/1 artefak dihilangkan/)).toBeInTheDocument();
  });

  it("explains the DB-trust and controller quarantine rules, including an incomplete comparison", async () => {
    responses[`/api/runs/${RUN_ID}`] = detail;
    wrap(<RunDetailPage />, `/runs/${RUN_ID}`);
    expect(await screen.findByText(/Baris tabel DB tepercaya diubah proses lain/)).toBeInTheDocument();
    expect(screen.getByText(/tidak dapat dievaluasi \(UNKNOWN, bukan lulus\)/)).toBeInTheDocument();
    expect(screen.getByText("Perbandingan tidak lengkap (batas snapshot)")).toBeInTheDocument();
    expect(screen.getByText("File dihapus oleh runtime")).toBeInTheDocument();
  });

  it("marks a HIGH runtime alert in the timeline and states the reasoning the broker sent and dropped", async () => {
    responses[`/api/runs/${RUN_ID}`] = detail;
    wrap(<RunDetailPage />, `/runs/${RUN_ID}`);
    expect(await screen.findByText("Peringatan HIGH: DB_TRUST_TAMPER")).toBeInTheDocument();
    expect(screen.getByText(/Tabel DB tepercaya diubah proses lain saat worker berjalan \(1 baris\)/)).toBeInTheDocument();
    expect(screen.getByText(/reasoning: Tidak dikirim \(default provider\) · permintaan reasoning pekerja dibuang: high/)).toBeInTheDocument();
    // the model-call table: sent level, dropped request, and UNKNOWN for a row recorded before the columns existed
    expect(screen.getAllByText("Tidak dikirim (default provider)").length).toBeGreaterThan(0);
    expect(screen.getByText("high - dibuang, tidak diteruskan")).toBeInTheDocument();
    expect(screen.getByText("medium (konfigurasi rute)")).toBeInTheDocument();
    expect(screen.getByText(/Nilai tak dikenal - dibuang/)).toBeInTheDocument();
    expect(screen.getAllByText("Tidak tercatat (UNKNOWN)").length).toBe(2);
  });

  it("never shows an empty controller comparison, a refused context pack or a local-only log as a pass", async () => {
    responses[`/api/runs/${RUN_ID}`] = {
      ...detail,
      caps: {
        controller_integrity: { checked_files: 0, changed: 0 },
        command_logs: { present: true, files: 1, bytes: 10, truncated: false, skipped: 0, removed_from_candidate: true, evidence_ref: "local-unverified:worker-command-logs.json" },
        context_pack: { used: false, pack_id: "pack-1", pack_hash: "sha256:" + "f".repeat(64), freshness: "STALE", reason: "context pack is STALE" },
      },
    };
    wrap(<RunDetailPage />, `/runs/${RUN_ID}`);
    expect(await screen.findByText("Tidak ada file yang dibandingkan (bukan lulus)")).toBeInTheDocument();
    expect(screen.queryByText(/^Tidak ada perubahan/)).toBeNull();
    expect(screen.getByText(/ContextPack ditolak \(STALE\)/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Bukti log perintah/ })).toBeNull();
    expect(screen.getByText(/hanya tersimpan lokal \(collector tidak tersedia, UNVERIFIED\)/)).toBeInTheDocument();
  });

  it("states that nothing was recorded for a run without runtime integrity caps", async () => {
    responses[`/api/runs/${RUN_ID}`] = { ...detail, quarantined: false, quarantine_reasons: [], caps: { demo: true }, actions: [], model_calls: [] };
    wrap(<RunDetailPage />, `/runs/${RUN_ID}`);
    expect(await screen.findByText("Belum dicatat runtime")).toBeInTheDocument();
  });
});
