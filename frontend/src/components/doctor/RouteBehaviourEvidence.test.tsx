import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

// Doctor worker.route_behaviour (W6-021): per worker route the trusted evidence of tool/schema behaviour, and the
// owner's failure-rate threshold (UNDECIDED = reported, never judged; recorded only as a human POLICY decision).

const auth = { isAdmin: false, domain: "PERSONAL" as string | null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("../../api/client", () => ({ post: vi.fn(async () => ({ id: "dec-thr-1" })), get: vi.fn(), ApiError: class extends Error {} }));

import { post } from "../../api/client";
import { RouteBehaviourEvidence } from "./RouteBehaviourEvidence";

const undecided = {
  threshold: { max_failure_rate: null, decision_id: null, status: "UNDECIDED", target: "worker_route_behaviour:tool_schema_failures" },
  installed_engine_version: "1.19.3",
  routes: [
    {
      route_key: "opencode-zen/glm-free",
      route_status: "TRIAL",
      verdict: "UNKNOWN",
      why: "3 kegagalan tool/schema dari 20 sampel; ambang batas pemilik UNDECIDED (dilaporkan, tidak dinilai)",
      tool_schema_failures: 3,
      failure_rate: 0.15,
      sample_size: 20,
      verified_runs: 2,
      tool_errors: 1,
    },
    { route_key: "opencode-zen/other", route_status: "TRIAL", verdict: "STALE", why: "evaluasi terakhir dibuat pada konfigurasi lain", verified_runs: 0, tool_errors: 0 },
  ],
};

function renderIt(evidence: Record<string, unknown>) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <RouteBehaviourEvidence evidence={evidence} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  auth.isAdmin = false;
  auth.domain = "PERSONAL";
  vi.mocked(post).mockClear();
});

describe("worker.route_behaviour evidence", () => {
  it("shows each route's verdict, measured failures and verified runs; an UNDECIDED threshold is never a pass", () => {
    renderIt(undecided);
    expect(screen.getByText("Ambang pemilik UNDECIDED: kegagalan dilaporkan, tidak dinilai")).toBeInTheDocument();
    expect(screen.getByText("opencode-zen/glm-free")).toBeInTheDocument();
    expect(screen.getByText("3/20 sampel · 15%")).toBeInTheDocument();
    expect(screen.getByText(/2 run lulus verifier · 1 error alat teramati runner/)).toBeInTheDocument();
    expect(screen.getByText("Tidak tercatat")).toBeInTheDocument(); // the STALE route has no measurement
    expect(screen.getByText(/Usang/)).toBeInTheDocument();
    expect(screen.queryByText(/Terverifikasi|Lulus/)).toBeNull();
    expect(screen.getByText("OpenCode terpasang: 1.19.3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ambang pemilik/ })).toBeNull();
    expect(screen.getByText(/VIEWER hanya-baca/)).toBeInTheDocument();
  });

  it("states a decided threshold with its decision and no route as NOT-PROVEN", () => {
    renderIt({ threshold: { max_failure_rate: 0.05, decision_id: "dec-9", status: "DECIDED" }, routes: [] });
    expect(screen.getByText("Ambang pemilik: maks 5% kegagalan")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "dec-9" })).toHaveAttribute("href", "/decisions?decision=dec-9");
    expect(screen.getByText(/Belum ada route worker APPROVED\/TRIAL/)).toBeInTheDocument();
  });

  it("lets an admin record the threshold as a POLICY decision with an explicit confirmation", async () => {
    auth.isAdmin = true;
    const user = userEvent.setup({ delay: null });
    renderIt(undecided);
    await user.click(screen.getByRole("button", { name: "Tetapkan ambang pemilik…" }));
    const confirm = screen.getByRole("button", { name: "Konfirmasi keputusan POLICY" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Laju kegagalan maksimum (%)" }), "150");
    expect(screen.getByText("Isi angka 0 sampai 100.")).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Laju kegagalan maksimum (%)" }));
    await user.type(screen.getByRole("textbox", { name: "Laju kegagalan maksimum (%)" }), "5");
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Alasan ambang" }), "pilot: maks 5% sampel gagal");
    expect(vi.mocked(post)).not.toHaveBeenCalled();
    await user.click(confirm);
    await waitFor(() => expect(vi.mocked(post)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(post)).toHaveBeenCalledWith(
      "/api/decisions",
      expect.objectContaining({
        kind: "POLICY",
        target_type: "worker_route_behaviour",
        target_id: "tool_schema_failures",
        details: { max_failure_rate: 0.05 },
        rationale: "pilot: maks 5% sampel gagal",
      }),
    );
    expect(await screen.findByText(/Ambang tercatat sebagai keputusan/)).toBeInTheDocument();
  });

  it("offers no threshold form in an OFFICE session (a platform value)", () => {
    auth.isAdmin = true;
    auth.domain = "OFFICE";
    renderIt(undecided);
    expect(screen.queryByRole("button", { name: /ambang pemilik/ })).toBeNull();
    expect(screen.getByText(/dicatat di sesi PERSONAL/)).toBeInTheDocument();
  });
});
