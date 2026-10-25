import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { EvidenceChart } from "../../components/control_center/EvidenceChart";
import { RetentionBadge, retentionStatus, type RetentionMarker } from "../../components/control_center/RetentionBadge";
import { AuditTab } from "./AuditTab";
import { RetentionView } from "./RetentionTab";

const marker = (over: Partial<RetentionMarker>): RetentionMarker => ({
  rule: "RETAIN", retain_days: null, legal_hold: false, expired_by_policy: false, disposal: "NONE", label: "Disimpan", ...over,
});

describe("Evidence chart (§29 recorded data only, never a verdict)", () => {
  it("plots only recorded values, shows UNKNOWN gaps and draws nothing without data", () => {
    const { container, rerender } = render(
      <EvidenceChart
        data={{ status: "EVIDENCE_BOUND", title: "Lease usang", unit: "count", source: "ops_sli_samples", unknown_points: 1, refs: ["a", "c"],
          points: [{ ref: "a", at: "2026-10-19T08:00:00Z", value: 1 }, { ref: "b", at: "2026-10-19T09:00:00Z", value: null }, { ref: "c", at: "2026-10-19T10:00:00Z", value: 3 }] }}
      />,
    );
    expect(container.querySelectorAll("circle")).toHaveLength(2);
    expect(screen.getByText(/2 nilai tercatat .*1 UNKNOWN \(celah, bukan 0\)/)).toBeInTheDocument();
    expect(screen.getByText(/bukan verdict/)).toBeInTheDocument();
    rerender(<EvidenceChart data={{ status: "NO_DATA", title: "x", unit: "count", source: "s", unknown_points: 0, refs: [], points: [], reason: "Belum ada nilai tercatat" }} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});

describe("Retention markers (§35 audit/evidence retention follows policy)", () => {
  it("maps a marker to a status that never claims deletion", () => {
    expect(retentionStatus(marker({}))).toBe("NO_POLICY");
    expect(retentionStatus(marker({ retain_days: 30, rule: "RETAIN_DAYS" }))).toBe("KEEP");
    expect(retentionStatus(marker({ retain_days: 30, expired_by_policy: true, disposal: "MANUAL_ONLY" }))).toBe("EXPIRED_BY_POLICY");
    expect(retentionStatus(marker({ retain_days: 30, legal_hold: true }))).toBe("LEGAL_HOLD");
    render(<RetentionBadge marker={marker({ retain_days: 30, expired_by_policy: true })} />);
    expect(screen.getByText("Lewat retensi (hapus manual saja)")).toBeInTheDocument();
  });

  it("marks expired audit records in the audit timeline", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      items: [
        { seq: 2, at: new Date().toISOString(), actor_type: "user", actor_id: "u", action: "plan.approve", target_type: "plan",
          target_id: "p", result: "SUCCESS", reason: "", details: {}, retention: marker({ retain_days: 30, expired_by_policy: true }) },
        { seq: 1, at: new Date().toISOString(), actor_type: "user", actor_id: "u", action: "plan.create", target_type: "plan",
          target_id: "p", result: "SUCCESS", reason: "", details: {}, retention: marker({}) },
      ],
      next_before: null, source: "audit_events", checked_at: new Date().toISOString(), mode: "LIVE",
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AuditTab />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Lewat retensi (hapus manual saja)")).toBeInTheDocument();
    expect(screen.getByText("Belum ada kebijakan retensi")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("shows the owner's rules, the stored records and the expired count per data class", () => {
    render(
      <RetentionView
        data={{
          status: "OWNER_DECISION_REQUIRED", legal_hold_blocks_disposal: true,
          notice: "AEOS tidak pernah menghapus data; kedaluwarsa = disposal manual saja.",
          source: "policy/retention.yaml", checked_at: new Date().toISOString(),
          items: [
            { data_class: "audit_events", domain: "PERSONAL", rule: "RETAIN_DAYS", retain_days: 30, label: "Simpan 30 hari",
              records: 12, expired_by_policy: 3, evaluated: true },
            { data_class: "evidence", domain: "PERSONAL", rule: "RETAIN", retain_days: null, label: "Belum ada aturan",
              records: 4, expired_by_policy: 0, evaluated: true },
            { data_class: "backups", domain: "PERSONAL", rule: "RETAIN", retain_days: null, label: "Belum ada aturan",
              records: null, expired_by_policy: null, evaluated: false },
          ],
        }}
      />,
    );
    expect(screen.getByText("Perlu keputusan pemilik")).toBeInTheDocument();
    expect(screen.getByText("3 record (disposal manual saja)")).toBeInTheDocument();
    expect(screen.getAllByText("Simpan 30 hari").length).toBeGreaterThan(0);
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("tidak dihitung di sini")).toBeInTheDocument();
    expect(screen.getByText(/tidak pernah menghapus data/)).toBeInTheDocument();
  });
});
