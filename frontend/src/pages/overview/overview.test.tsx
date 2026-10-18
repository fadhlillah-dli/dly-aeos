import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { opsBadgeSpec } from "../../components/control_center/OpsBadge";
import { statusSpec } from "../../components/StatusBadge";
import { mergeEvents, StreamState } from "./ActivityFeed";
import { OverviewView } from "./OverviewView";
import type { ActivityItem, Overview, Section } from "./types";

// Test fixture builder: the payload shape is checked by the components, not by this helper.
function sec(status: string, data: unknown = null, reason = ""): Section<never> {
  return { status, source: "test", checked_at: null, reason, evidence_limit: "", data } as Section<never>;
}

function overview(partial: Partial<Overview> = {}): Overview {
  return {
    generated_at: new Date().toISOString(),
    domain: "PERSONAL",
    project: null,
    mode: "LIVE",
    contains_mock: false,
    mock_rows: {},
    status_counts: { UNKNOWN: 5 },
    source: "test",
    evidence_limit: "",
    sections: {
      capabilities: sec("UNKNOWN", null, "modul belum tersedia: aeos.doctor.capabilities"),
      active_plan: sec("UNKNOWN"),
      policy: sec("MISSING"),
      blockers: sec("UNKNOWN", { items: [] }, "Doctor belum pernah berjalan"),
      health: sec("UNKNOWN", { items: [] }),
      cost: sec("UNKNOWN", { window_days: 30, actual_usd: 0, estimated_usd: 0, unknown_cost_entries: 2, mock_entries_excluded: 0, budgets: 0, quotas: [] }),
      decisions: sec("UNKNOWN"),
      stream: sec("UNKNOWN"),
      secrets: sec("UNKNOWN"),
      recovery: sec("NOT-PROVEN", { backups: 0 }, "Restore backup belum pernah diuji"),
      emergency: sec("NONE_RECORDED"),
      project_hint: sec("UNKNOWN", { projects_in_domain: 0 }, "Belum ada proyek di domain ini. Jalankan `aeos seed`"),
    },
    ...partial,
  };
}

function renderView(data: Overview) {
  return render(
    <MemoryRouter>
      <OverviewView data={data} />
    </MemoryRouter>,
  );
}

describe("Overview projection rendering (§35)", () => {
  it("shows UNKNOWN sections as unknown, never as ready", () => {
    renderView(overview());
    expect(screen.queryByText("Siap")).not.toBeInTheDocument();
    expect(screen.getAllByText("Tidak diketahui").length).toBeGreaterThan(3);
    expect(screen.getByText(/modul belum tersedia/)).toBeInTheDocument();
    expect(screen.getByText(/aeos seed/)).toBeInTheDocument();
  });

  it("shows recovery as not proven and cost UNKNOWN entries", () => {
    renderView(overview());
    expect(screen.getByText("Restore backup belum pernah diuji")).toBeInTheDocument();
    expect(screen.getByText("2 entri")).toBeInTheDocument();
  });

  it("labels MOCK/DEMO data with a banner", () => {
    renderView(overview({ mode: "MIXED", contains_mock: true, mock_rows: { events: 3 } }));
    expect(screen.getByText(/Data MOCK\/DEMO terdeteksi/)).toBeInTheDocument();
    expect(screen.getByText(/events: 3/)).toBeInTheDocument();
  });

  it("renders capability rows with text + icon badges", () => {
    const data = overview();
    data.sections.capabilities = sec("BLOCKED", {
      items: [
        { capability: "CAN_RUN_WORKER", status: "BLOCKED", reasons: ["sandbox not proven"], remedies: [], checked_at: null },
        { capability: "CAN_OPEN_DASHBOARD", status: "READY", reasons: [], remedies: [], checked_at: null },
      ],
      counts: { BLOCKED: 1, READY: 1 },
    });
    renderView(data);
    const list = screen.getByLabelText("Kesiapan capability");
    expect(within(list).getByText("Jalankan worker")).toBeInTheDocument();
    expect(within(list).getByText("sandbox not proven")).toBeInTheDocument();
    expect(within(list).getByText("Siap")).toBeInTheDocument();
  });
});

describe("Ops status vocabulary", () => {
  it("never maps no-target / untested / unrecorded to a pass tone", () => {
    for (const s of ["NO_TARGET", "NOT_RESTORE_TESTED", "RESTORE_TEST_UNRECORDED", "PASSED_UNRECORDED", "NONE_RECORDED"]) {
      expect(statusSpec(opsBadgeSpec(s).status).tone).not.toBe("ok");
    }
    expect(statusSpec(opsBadgeSpec("RESTORE_TESTED").status).tone).toBe("ok");
    expect(statusSpec(opsBadgeSpec("BREACH").status).tone).toBe("bad");
  });
});

describe("Activity stream", () => {
  const ev = (seq: number): ActivityItem => ({
    seq, at: new Date().toISOString(), source: "t", kind: "k", entity_type: "e", entity_id: "x", project_id: null, run_id: null, mode: "LIVE", payload: {},
  });

  it("dedupes by seq and keeps newest first", () => {
    const merged = mergeEvents([ev(2), ev(1)], [ev(3), ev(2)]);
    expect(merged.map((e) => e.seq)).toEqual([3, 2, 1]);
  });

  it("shows STALE with last observed time when disconnected", () => {
    render(<StreamState status="reconnecting" lastSeenAt={null} />);
    expect(screen.getByText(/Menyambung ulang/)).toBeInTheDocument();
    expect(screen.getByText(/belum pernah/)).toBeInTheDocument();
  });
});
