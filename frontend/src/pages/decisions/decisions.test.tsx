import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { QueueItem } from "../../components/planning/types";
import { DecisionForm, parseScope, PROVIDERS } from "./DecisionForm";
import { formatAge, QueueList } from "./QueueList";

const ITEM: QueueItem = {
  id: "PLAN:p1",
  kind: "PLAN",
  title: "Rencana siap",
  target: { type: "plan", id: "p1", version: 2, hash: "sha256:abcdef0123456789" },
  why: "Empat tahap perencanaan selesai",
  requested_decision: "Approve & Publish, Approve, Request Revision, atau Reject",
  link: "/plans/p1",
  created_at: "2026-10-18T00:00:00Z",
  age_seconds: 7200,
  project_id: "x",
  domain: "PERSONAL",
  priority: 1,
};

describe("Human Decision Queue (U-12)", () => {
  it("shows why an item waits, the requested decision, the exact target and the link", () => {
    render(
      <MemoryRouter>
        <QueueList items={[ITEM]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Rencana siap")).toBeInTheDocument();
    expect(screen.getByText(/Empat tahap perencanaan selesai/)).toBeInTheDocument();
    expect(screen.getByText(/Approve & Publish/)).toBeInTheDocument();
    expect(screen.getByText("abcdef0123")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Tindak lanjuti/ })).toHaveAttribute("href", "/plans/p1");
    expect(screen.getByText(/menunggu 2 jam/)).toBeInTheDocument();
  });

  it("explains an empty queue", () => {
    render(
      <MemoryRouter>
        <QueueList items={[]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Tidak ada keputusan yang menunggu Anda")).toBeInTheDocument();
  });

  it("formats ages", () => {
    expect(formatAge(30)).toBe("1 mnt");
    expect(formatAge(90000)).toBe("1 hari");
    expect(formatAge(null)).toBe("—");
  });
});

describe("DecisionForm exact target shapes (ADR-0042/0051)", () => {
  afterEach(() => vi.unstubAllGlobals());

  function renderForm(domain: "PERSONAL" | "OFFICE") {
    const fn = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response("{}", { status: 201, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fn);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DecisionForm domain={domain} initialKind="EXCEPTION" onDone={() => undefined} />
      </QueryClientProvider>,
    );
    return fn;
  }

  it("uses one provider vocabulary (Google Docs live in the google-drive target)", () => {
    expect(PROVIDERS.PERSONAL).toEqual(["google-drive", "github-issues", "github-repo"]);
    expect(PROVIDERS.OFFICE).toEqual(["confluence", "jira", "bitbucket"]);
  });

  it("records the process-sandbox exception as a preset with allow_bash, without a hand-typed target", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis pengecualian"), { target: { value: "worker_sandbox_process" } });
    expect(screen.queryByText("Jenis target")).not.toBeInTheDocument();
    expect(screen.getByText("worker_sandbox/process")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Izinkan bash/));
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "host=laptop" } });
    fireEvent.change(screen.getByLabelText(/Berlaku sampai/), { target: { value: "2026-11-30T10:00" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "Docker belum tersedia di laptop ini" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const body = JSON.parse(String(fn.mock.calls[0][1]!.body));
    expect(body).toMatchObject({ kind: "EXCEPTION", exception: "worker_sandbox_process", allow_bash: true, auth_scope: { host: "laptop" } });
    expect(body.target_type).toBeUndefined();
  });

  it("sends the system-change evaluation and rollback target with a POLICY decision (§41/§43)", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis keputusan"), { target: { value: "POLICY" } });
    fireEvent.change(screen.getByLabelText("Jenis target"), { target: { value: "fallback_chain" } });
    fireEvent.change(screen.getByLabelText("ID target persis"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText(/ID EvalRun bukti/), { target: { value: "run-1, run-2" } });
    fireEvent.change(screen.getByLabelText(/Target rollback/), { target: { value: "fallback chain v1" } });
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "pool=WORKER" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "fallback chain baru setelah evaluasi" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const body = JSON.parse(String(fn.mock.calls[0][1]!.body));
    expect(body).toMatchObject({
      kind: "POLICY",
      target_type: "fallback_chain",
      details: { system_change: { eval_run_ids: ["run-1", "run-2"], rollback_target: "fallback chain v1" } },
    });
  });

  it("binds the evaluation to the change with the new value (target_version)", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis keputusan"), { target: { value: "POLICY" } });
    fireEvent.change(screen.getByLabelText("Jenis target"), { target: { value: "prompt" } });
    fireEvent.change(screen.getByLabelText("ID target persis"), { target: { value: "worker-prompt" } });
    fireEvent.change(screen.getByLabelText(/Nilai baru yang dievaluasi/), { target: { value: " wp-42 " } });
    fireEvent.change(screen.getByLabelText(/ID EvalRun bukti/), { target: { value: "run-9" } });
    fireEvent.change(screen.getByLabelText(/Target rollback/), { target: { value: "prompt wp-41" } });
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "role=worker" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "prompt worker baru sudah dievaluasi" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const body = JSON.parse(String(fn.mock.calls[0][1]!.body));
    expect(body).toMatchObject({ target_type: "prompt", target_id: "worker-prompt", target_version: "wp-42" });
    expect(body.details.system_change).toEqual({ eval_run_ids: ["run-9"], rollback_target: "prompt wp-41" });
  });

  it("records a supervisor stage-model pin on the exact consumer target with its models and evaluation (§15)", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis keputusan"), { target: { value: "POLICY" } });
    // a hash typed before switching to the pin is hidden with its field and never sent (a pin binds by its models)
    fireEvent.change(screen.getByLabelText("Hash target (opsional)"), { target: { value: "sha256:stale" } });
    fireEvent.click(screen.getByLabelText(/Pin model supervisor per tahap/));
    expect(screen.queryByLabelText("Jenis target")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Nilai baru yang dievaluasi/)).not.toBeInTheDocument();
    // stated, never implied: no supervisor evaluation path exists in this build, so the pin is refused for now
    expect(screen.getByText(/belum punya jalur evaluasi untuk model supervisor/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("PRIMARY_PLANNING (opus)"), { target: { value: "claude-opus-4-8" } });
    fireEvent.change(screen.getByLabelText(/ID EvalRun bukti/), { target: { value: "run-opus" } });
    fireEvent.change(screen.getByLabelText(/Target rollback/), { target: { value: "alias baseline (opus)" } });
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "stage=PRIMARY_PLANNING" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "pin Opus setelah evaluasi model persis" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const body = JSON.parse(String(fn.mock.calls[0][1]!.body));
    expect(body).toMatchObject({
      kind: "POLICY",
      target_type: "planning_baseline",
      target_id: "stage_models",
      details: { models: { PRIMARY_PLANNING: "claude-opus-4-8" }, system_change: { eval_run_ids: ["run-opus"], rollback_target: "alias baseline (opus)" } },
    });
    expect(body.target_version).toBeUndefined();
    expect(body.target_hash).toBeUndefined();
  });

  it("refuses an empty stage-model pin locally", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis keputusan"), { target: { value: "POLICY" } });
    fireEvent.click(screen.getByLabelText(/Pin model supervisor per tahap/));
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "stage=x" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "pin tanpa model sama sekali" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    expect(await screen.findByText(/minimal satu ID model persis/)).toBeInTheDocument();
    expect(fn).not.toHaveBeenCalled();
  });

  it("does not offer the PERSONAL-only presets in OFFICE", () => {
    renderForm("OFFICE");
    expect(screen.queryByLabelText("Jenis pengecualian")).not.toBeInTheDocument();
    expect(screen.getByText(/hanya untuk PERSONAL; OFFICE selalu ditolak/)).toBeInTheDocument();
  });
});

describe("parseScope", () => {
  it("accepts key=value pairs and JSON; empty means no scope", () => {
    expect(parseScope("repo=o/r; reason=hotfix")).toEqual({ repo: "o/r", reason: "hotfix" });
    expect(parseScope('{"a": 1}')).toEqual({ a: 1 });
    expect(parseScope("  ")).toEqual({});
    expect(() => parseScope("{bad")).toThrow();
  });
});
