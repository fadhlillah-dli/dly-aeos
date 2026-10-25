import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { IncidentDetail, type IncidentView } from "./IncidentsTab";

const base: IncidentView = {
  id: "inc-12345678", title: "Alert: Lease kedaluwarsa", status: "OPEN", source: "alert", source_ref: "alert-1",
  owner: "Ops Lead", domain: "PERSONAL", impact: "", root_cause: "", root_cause_evidence_refs: [], corrective_actions: [],
  regression_test_ids: [], eval_case_refs: [], follow_ups: [], timeline: [],
  resolution_requirements: ["root_cause_with_evidence", "corrective_action", "regression_test_or_eval_case"],
  created_at: new Date().toISOString(), resolved_at: null,
};

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
}

describe("Incident follow-up on the dashboard (§05 step 15, §33)", () => {
  it("shows what blocks closing and never claims an unevidenced root cause", () => {
    wrap(<IncidentDetail inc={base} isAdmin />);
    expect(screen.getByText(/UNKNOWN — belum didukung bukti/)).toBeInTheDocument();
    expect(screen.getByText(/Belum dapat ditutup: root cause dengan bukti tepercaya, tindakan korektif, regression test atau eval case/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tutup insiden" })).toBeDisabled();
  });

  it("links regression tests / eval cases and files a candidate eval case through the API", async () => {
    const calls: { url: string; body: unknown }[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ candidate_id: "cand-1" }), { status: 200, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    wrap(<IncidentDetail inc={base} isAdmin />);
    fireEvent.change(screen.getByLabelText(/Tautkan regression test/), { target: { value: "tests/unit/test_x.py::test_y, ops#lease" } });
    fireEvent.click(screen.getByRole("button", { name: "Tautkan" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/regression"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/regression"))!.body).toEqual({ test_ids: ["tests/unit/test_x.py::test_y"], eval_case_refs: ["ops#lease"] });
    fireEvent.change(screen.getByPlaceholderText("Judul"), { target: { value: "Lease basi tidak dipakai" } });
    fireEvent.change(screen.getByPlaceholderText("Perilaku yang diharapkan"), { target: { value: "Scheduler menandai lease basi dulu" } });
    fireEvent.click(screen.getByRole("button", { name: "Ajukan eval case" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/eval-case"))).toBe(true));
  });

  it("is read-only for VIEWER sessions", () => {
    wrap(<IncidentDetail inc={base} isAdmin={false} />);
    expect(screen.getByRole("button", { name: "Ajukan usulan" })).toBeDisabled();
    expect(screen.getByLabelText(/Tautkan regression test/)).toBeDisabled();
  });
});
