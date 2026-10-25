import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The owner dialogs are filled field by field; under a fully parallel run the default 5 s budget is too tight.
vi.setConfig({ testTimeout: 30_000 });

const responses: Record<string, unknown> = {};
const posts: { path: string; body: unknown }[] = [];
let postError: unknown = null;

vi.mock("../../api/client", () => {
  class ApiError extends Error {
    status: number;
    code: string;
    details: Record<string, unknown>;
    constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }
  return {
    ApiError,
    get: vi.fn(async (path: string) => {
      if (!(path in responses)) throw new Error(`unmocked ${path}`);
      return responses[path];
    }),
    post: vi.fn(async (path: string, body?: unknown) => {
      posts.push({ path, body });
      if (postError) {
        const e = postError;
        postError = null;
        throw e;
      }
      return responses[`POST ${path}`] ?? {};
    }),
  };
});

import { ApiError } from "../../api/client";
import { AgentControls } from "./AgentControls";
import { ConcurrencyPanel } from "./ConcurrencyPanel";
import { ExecutePlanAction } from "./ExecutePlanAction";
import { InvestigationPanel } from "./InvestigationPanel";
import { RoleAdoption } from "./RoleAdoption";
import { TrustedSuitePanel } from "./TrustedSuitePanel";
import type { AgentDef, RoleCard } from "./types";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const agent: AgentDef = {
  agent_key: "opencode-implementer", role_key: "feature-implementer", harness: "opencode", description: "", route_policy: {},
  tools: ["read"], limits: {}, allowed_scope: {}, enabled: false, enabled_reason: "", deterministic: false, revision: "1",
  revision_hash: "h", mode: "DESIGN", owner: "owner", active_runs: 0, availability: "DISABLED",
};

beforeEach(() => {
  posts.length = 0;
  postError = null;
  for (const k of Object.keys(responses)) delete responses[k];
});

describe("Agent enable/disable (W6-137/W6-181/W6-164)", () => {
  it("enables through a confirmed, reasoned owner action and shows the prerequisite reasons when refused", async () => {
    const onChanged = vi.fn();
    const user = userEvent.setup({ delay: null });
    wrap(<AgentControls agent={agent} isAdmin onChanged={onChanged} />);
    await user.click(screen.getByRole("button", { name: /Aktifkan/ }));
    const confirm = screen.getByRole("button", { name: "Konfirmasi" });
    expect(confirm).toBeDisabled(); // a reason is required
    postError = new ApiError(409, "AGENT_PREREQUISITES", "agent cannot be enabled yet", {
      reasons: ["OpenCode MISSING: opencode not found", "process sandbox is WEAK and needs an ACTIVE owner EXCEPTION decision"],
    });
    await user.type(screen.getByLabelText(/Alasan/), "pilot M3");
    await user.click(confirm);
    expect(await screen.findByText("AGENT_PREREQUISITES")).toBeInTheDocument();
    expect(screen.getByText(/OpenCode MISSING/)).toBeInTheDocument();
    expect(posts[0]).toEqual({ path: "/api/workforce/agents/opencode-implementer/enable", body: { reason: "pilot M3" } });
    expect(onChanged).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Konfirmasi" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(posts).toHaveLength(2);
  });

  it("checks prerequisites read-only and disables the owner action for VIEWER", async () => {
    responses["/api/workforce/agents/opencode-implementer/prerequisites"] = {
      agent_key: "opencode-implementer", harness: "opencode", enabled: false, ready: false,
      reasons: ["no APPROVED worker route is eligible for this domain"], sandbox: { status: "BLOCKED", kind: null },
      excluded_check: "worker.agent_definition", checked_at: new Date().toISOString(), source: "sandbox probe + routing + Doctor CAN_RUN_WORKER",
    };
    const user = userEvent.setup({ delay: null });
    wrap(<AgentControls agent={agent} isAdmin={false} onChanged={() => undefined} />);
    expect(screen.getByRole("button", { name: /Aktifkan/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /Periksa prasyarat/ }));
    expect(await screen.findByText("Prasyarat belum siap")).toBeInTheDocument();
    expect(screen.getByText(/no APPROVED worker route/)).toBeInTheDocument();
    expect(posts).toHaveLength(0);
  });

  it("offers disable for an enabled agent and leaves module-gated agents alone", async () => {
    const user = userEvent.setup({ delay: null });
    const { unmount } = wrap(<AgentControls agent={{ ...agent, enabled: true }} isAdmin onChanged={() => undefined} />);
    await user.click(screen.getByRole("button", { name: /Nonaktifkan/ }));
    await user.type(screen.getByLabelText(/Alasan/), "stop pilot");
    await user.click(screen.getByRole("button", { name: "Konfirmasi" }));
    await waitFor(() => expect(posts[0]?.path).toBe("/api/workforce/agents/opencode-implementer/disable"));
    unmount();
    wrap(<AgentControls agent={{ ...agent, harness: "claude-native" }} isAdmin onChanged={() => undefined} />);
    expect(screen.getByText(/mengikuti gate modul pemiliknya/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Aktifkan/ })).not.toBeInTheDocument();
  });
});

const role: RoleCard = {
  role_key: "feature-implementer", name: "Feature Implementer", kind: "EXECUTOR", group: "build", description: "", outputs: [],
  boundaries: [], verification: [], forbidden_actions: [], deterministic_service: false, engine_hint: "", source_section: "§16",
  version: "1", revision_hash: "sha256:abcdef0123456789", eval_status: "PASS", mode: "DESIGN", active_runs: 0, demo_runs: 0,
  availability: "AVAILABLE_INACTIVE", evaluation: { status: "PASS", run_id: "run-pass-1", suite_key: "eval.small-bugfix", basis: "EvalRun run-pass-1 (COMPLETED)" },
  adoption: { decision_id: null, eval_run_id: null, scope: null, current: false }, adoptable: true,
};

describe("Role adoption dialog (W6-123)", () => {
  it("records the DecisionRecord fields and scope for the current PASS evaluation", async () => {
    const onChanged = vi.fn();
    const user = userEvent.setup({ delay: null });
    wrap(<RoleAdoption role={role} isAdmin onChanged={onChanged} />);
    expect(screen.getByText("Belum diadopsi")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Adopsi hasil evaluasi/ }));
    const submit = screen.getByRole("button", { name: "Catat adopsi" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(/Opsi yang dipertimbangkan/), "tetap ambang{enter}adopsi hasil");
    await user.type(screen.getByLabelText("Tujuan"), "eligible dari evaluasi");
    await user.type(screen.getByLabelText(/Trade-off/), "biaya review sama");
    await user.type(screen.getByLabelText(/Batal berlaku bila/), "revisi peran berubah");
    await user.click(screen.getByLabelText("R1"));
    await user.type(screen.getByLabelText(/Alasan/), "hasil PASS ditinjau");
    await user.click(submit);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(posts[0]).toEqual({
      path: "/api/workforce/roles/feature-implementer/adopt",
      body: {
        eval_run_id: "run-pass-1", rationale: "hasil PASS ditinjau",
        decision_record: { options: ["tetap ambang", "adopsi hasil"], goal: "eligible dari evaluasi", cost_quality_tradeoff: "biaya review sama", invalidate_if: "revisi peran berubah" },
        scope: { risk_classes: ["R1"] },
      },
    });
  });

  it("shows an adopted result and never offers adoption without a PASS", () => {
    const { unmount } = wrap(
      <RoleAdoption role={{ ...role, adoptable: false, adoption: { decision_id: "d-1", eval_run_id: "run-pass-1", scope: {}, current: true } }} isAdmin onChanged={() => undefined} />,
    );
    expect(screen.getByText("Hasil diadopsi pemilik")).toBeInTheDocument();
    expect(screen.getByText("d-1")).toBeInTheDocument();
    unmount();
    wrap(<RoleAdoption role={{ ...role, adoptable: false, evaluation: { status: "NOT_EVALUATED", run_id: null, basis: "belum ada EvalRun" } }} isAdmin onChanged={() => undefined} />);
    expect(screen.queryByRole("button", { name: /Adopsi hasil evaluasi/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Belum ada hasil PASS revisi ini/)).toBeInTheDocument();
  });
});

const concurrency = {
  items: [
    {
      eval_run_id: "run-c-1", topology: "C", status: "RUNNING", grant_decision_id: null, grantable: true, problems: [], bound_tasks: 2,
      parallel_execution: { status: "NOT_PROVEN", problems: ["no two bound attempts overlapped in time: two workers never ran at once"], cap2_runs: 0, overlaps: [] },
    },
  ],
  concurrency: {
    cap: 1, decision_id: null, source: "PILOT_DEFAULT", reason: "pilot default: one active implementation worker", policy_cap: 1,
    budget_max_parallel_workers: null, budget_note: "resource budget not set by the owner (aeos doctor budget); UNKNOWN, not guessed", budget_limited: false,
  },
  source: "x", checked_at: new Date().toISOString(), mode: "LIVE",
};

describe("Concurrency panel (W6-122/W6-131/W6-145/W6-123)", () => {
  it("states the cap source, an UNKNOWN budget and grants a two-worker experiment to one RUNNING C run", async () => {
    responses["/api/workforce/concurrency"] = concurrency;
    const user = userEvent.setup({ delay: null });
    wrap(<ConcurrencyPanel domain="PERSONAL" isAdmin />);
    expect(await screen.findByText("Batas efektif: 1 worker")).toBeInTheDocument();
    expect(screen.getByText("Budget resource: belum ditetapkan pemilik")).toBeInTheDocument();
    expect(screen.getByText("Dua worker bersamaan: belum terbukti")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Beri hibah eksperimen/ }));
    const save = screen.getByRole("button", { name: "Catat hibah" });
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText(/Alasan/), "uji bounded parallel suite kecil");
    // no pre-filled expiry: the owner must choose it (an EXCEPTION always expires)
    expect(screen.getByLabelText(/Kedaluwarsa/)).toHaveValue("");
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText(/Kedaluwarsa/), "45");
    expect(save).toBeDisabled(); // above the 30-day limit
    await user.clear(screen.getByLabelText(/Kedaluwarsa/));
    await user.type(screen.getByLabelText(/Kedaluwarsa/), "5");
    await user.click(save);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ path: "/api/workforce/concurrency/experiment", body: { eval_run_id: "run-c-1", rationale: "uji bounded parallel suite kecil", days: 5 } });
  });

  it("returns to the pilot mode without a decision and posts a full two-worker DecisionRecord", async () => {
    responses["/api/workforce/concurrency"] = concurrency;
    responses["POST /api/workforce/concurrency"] = { effective_cap: 1, cap_reason: "pilot default" };
    const user = userEvent.setup({ delay: null });
    wrap(<ConcurrencyPanel domain="PERSONAL" isAdmin />);
    await user.click(await screen.findByRole("button", { name: /Ubah mode konkurensi/ }));
    await user.selectOptions(screen.getByLabelText("Mode"), "1");
    await user.click(screen.getByRole("button", { name: "Catat" }));
    await waitFor(() => expect(posts[0]?.body).toEqual({ max_active: 1, rationale: "" }));
    expect(await screen.findByText(/Batas efektif sekarang 1/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Ubah mode konkurensi/ }));
    await user.selectOptions(screen.getByLabelText("Mode"), "2");
    const save = screen.getByRole("button", { name: "Catat" });
    expect(save).toBeDisabled(); // two workers need evidence, scope and the DecisionRecord
    await user.type(screen.getByLabelText(/topologi C/), "run-c-done");
    await user.type(screen.getByLabelText(/baseline topologi A/), "run-a-done");
    await user.click(screen.getByLabelText("R1"));
    await user.type(screen.getByLabelText(/Opsi yang dipertimbangkan/), "1 worker{enter}2 worker");
    await user.type(screen.getByLabelText("Tujuan"), "throughput");
    await user.type(screen.getByLabelText(/Trade-off/), "dua slot");
    await user.type(screen.getByLabelText(/Batal berlaku bila/), "gate FAIL");
    await user.type(screen.getByLabelText(/Alasan/), "manfaat terbukti vs baseline");
    await user.click(save);
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].body).toEqual({
      max_active: 2, rationale: "manfaat terbukti vs baseline", eval_run_id: "run-c-done", baseline_eval_run_id: "run-a-done",
      scope: { risk_classes: ["R1"] },
      decision_record: { options: ["1 worker", "2 worker"], goal: "throughput", cost_quality_tradeoff: "dua slot", invalidate_if: "gate FAIL" },
    });
  });
});

describe("Execute an approved plan (W6-164/W6-200/W6-119)", () => {
  it("is offered only for an approved plan and reports what was queued", async () => {
    responses["POST /api/plans/p1/execute"] = { plan_version_id: "v1", tasks: 2, ready: 1, blocked: 1, scheduler_enabled: false, paused: false, note: "" };
    const user = userEvent.setup({ delay: null });
    const { unmount } = wrap(<ExecutePlanAction planId="p1" approved={false} isAdmin />);
    expect(screen.queryByRole("button", { name: /Jalankan rencana/ })).not.toBeInTheDocument();
    unmount();
    wrap(<ExecutePlanAction planId="p1" approved isAdmin />);
    await user.click(screen.getByRole("button", { name: /Jalankan rencana/ }));
    await user.type(screen.getByLabelText(/Alasan/), "slice pertama");
    await user.click(screen.getByRole("button", { name: "Konfirmasi" }));
    expect(await screen.findByText("2 task diantrikan")).toBeInTheDocument();
    expect(screen.getByText("Loop scheduler nonaktif")).toBeInTheDocument();
    expect(posts[0]).toEqual({ path: "/api/plans/p1/execute", body: { reason: "slice pertama" } });
  });

  it("shows the CAN_RUN_WORKER reasons when execution is refused", async () => {
    postError = new ApiError(409, "CAPABILITY_NOT_READY", "CAN_RUN_WORKER is not READY", { reasons: ["CAN_RUN_WORKER: worker.contract UNKNOWN"] });
    const user = userEvent.setup({ delay: null });
    wrap(<ExecutePlanAction planId="p1" approved isAdmin />);
    await user.click(screen.getByRole("button", { name: /Jalankan rencana/ }));
    await user.click(screen.getByRole("button", { name: "Konfirmasi" }));
    expect(await screen.findByText("CAPABILITY_NOT_READY")).toBeInTheDocument();
    expect(screen.getByText(/worker.contract UNKNOWN/)).toBeInTheDocument();
  });
});

describe("Read-only investigation integrator step (W6-052)", () => {
  const view = {
    task_id: "t-inv", task_key: "inv", state: "REVIEW", reason_code: "INVESTIGATION_COMPLETE", repair_task_key: "fix", timebox_seconds: 120,
    evidenced: ["h1"], complete: true, choice: null,
    hypotheses: [
      { key: "h1", statement: "VALUE constant is wrong", index: 1, investigated: true, run_id: "r1", outcome_code: "INVESTIGATION_REPORTED", verdict: "SUPPORTED", evidenced: true, evidence_refs: [{ ref: "src/app.py:1", exists: true }], summary: "x", proposed_fix: "set VALUE = 2" },
      { key: "h2", statement: "import path broken", index: 2, investigated: true, run_id: "r2", outcome_code: "INVESTIGATION_REPORTED", verdict: "REFUTED", evidenced: false, evidence_refs: [{ ref: "src/missing.py:3", exists: false }], summary: null, proposed_fix: null },
    ],
  };

  it("compares the reports and records only an evidenced choice", async () => {
    responses["/api/tasks/t-inv/investigation"] = view;
    const user = userEvent.setup({ delay: null });
    wrap(<InvestigationPanel taskId="t-inv" isAdmin />);
    expect(await screen.findByText("Berbukti (referensi ada di repo)")).toBeInTheDocument();
    expect(screen.getByText("Belum berbukti")).toBeInTheDocument();
    expect(screen.getByText("(tidak ditemukan)")).toBeInTheDocument();
    expect(screen.queryByLabelText("h2")).not.toBeInTheDocument(); // a refuted/unevidenced report is never offered
    const save = screen.getByRole("button", { name: "Catat pilihan integrator" });
    expect(save).toBeDisabled();
    await user.click(screen.getByLabelText("h1"));
    await user.type(screen.getByLabelText(/Alasan/), "h1 didukung src/app.py:1");
    await user.click(save);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ path: "/api/tasks/t-inv/investigation/choose", body: { hypothesis_key: "h1", rationale: "h1 didukung src/app.py:1" } });
  });
});

describe("Trusted suite registration (W6-119)", () => {
  it("registers the project's suite from a reviewed commit as an owner decision", async () => {
    responses["/api/projects/proj-1/trusted-suite"] = { project_id: "proj-1", status: "BLOCKED", code: "TRUSTED_SUITE_MISSING", reason: "no registered suite", checked_at: new Date().toISOString() };
    const user = userEvent.setup({ delay: null });
    wrap(<TrustedSuitePanel projectId="proj-1" isPlatform={false} isAdmin />);
    expect(await screen.findByText("Belum terdaftar / tidak valid")).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "Daftarkan suite tepercaya" });
    expect(btn).toBeDisabled();
    await user.type(screen.getByLabelText(/Commit atau branch/), "abc123");
    await user.type(screen.getByLabelText(/Alasan/), "grader fixture ditinjau");
    await user.click(btn);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ path: "/api/projects/proj-1/trusted-suite", body: { rationale: "grader fixture ditinjau", commit: "abc123" } });
  });

  it("states that the platform suite is the controller checkout and keeps VIEWER read-only", async () => {
    responses["/api/projects/plat/trusted-suite"] = { project_id: "plat", status: "CONTROLLER", kind: "CONTROLLER", suite_hash: "sha256:0011223344556677889900", checked_at: new Date().toISOString() };
    const { unmount } = wrap(<TrustedSuitePanel projectId="plat" isPlatform isAdmin />);
    expect(await screen.findByText(/Checkout controller/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Daftarkan suite tepercaya" })).not.toBeInTheDocument();
    unmount();
    responses["/api/projects/proj-2/trusted-suite"] = { project_id: "proj-2", status: "BLOCKED", code: "TRUSTED_SUITE_MISSING", checked_at: new Date().toISOString() };
    wrap(<TrustedSuitePanel projectId="proj-2" isPlatform={false} isAdmin={false} />);
    expect(await screen.findByRole("button", { name: "Daftarkan suite tepercaya" })).toBeDisabled();
  });
});
