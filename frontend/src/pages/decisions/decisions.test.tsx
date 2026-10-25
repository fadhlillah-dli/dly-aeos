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

  it("records the paid extra-usage attestation as a POLICY preset (§12, E0, never VERIFIED)", async () => {
    const fn = renderForm("PERSONAL");
    fireEvent.change(screen.getByLabelText("Jenis keputusan"), { target: { value: "POLICY" } });
    fireEvent.change(screen.getByLabelText("Jenis kebijakan"), { target: { value: "claude_extra_usage_off" } });
    expect(screen.queryByText("Jenis target")).not.toBeInTheDocument();
    expect(screen.getByText("claude_extra_usage/PERSONAL")).toBeInTheDocument();
    expect(screen.getByText(/tidak pernah VERIFIED/)).toBeInTheDocument();
    expect(screen.getByText("Berlaku sampai (wajib)")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "akun=Max pribadi" } });
    fireEvent.change(screen.getByLabelText(/Berlaku sampai/), { target: { value: "2027-01-31T10:00" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "extra usage berbayar dimatikan di claude.ai" } });
    fireEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(fn).toHaveBeenCalled());
    const body = JSON.parse(String(fn.mock.calls[0][1]!.body));
    expect(body).toMatchObject({ kind: "POLICY", policy_preset: "claude_extra_usage_off", auth_scope: { akun: "Max pribadi" } });
    expect(body.target_type).toBeUndefined();
    expect(body.expires_at).toBeTruthy();
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
