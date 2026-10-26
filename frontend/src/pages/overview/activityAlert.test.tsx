import { render, screen } from "@testing-library/react";
import { ActivityRow, FEED_FILTERS } from "./ActivityFeed";
import type { ActivityItem } from "./types";

// Wave 7b UI lane 2: the HIGH worker_runtime.alert events (DB_TRUST_TAMPER, INTEGRITY_HOLD) stand out in the live
// feed with text + icon + colour, and the feed can be filtered to them.

const item = (over: Partial<ActivityItem>): ActivityItem => ({
  seq: 1,
  at: new Date().toISOString(),
  source: "worker_runtime",
  kind: "worker_runtime.alert",
  entity_type: "run",
  entity_id: "11111111-2222-3333-4444-555555555555",
  project_id: null,
  run_id: null,
  mode: "LIVE",
  payload: {},
  ...over,
});

function renderRow(e: ActivityItem) {
  return render(
    <ol>
      <ActivityRow e={e} />
    </ol>,
  );
}

describe("Live feed runtime alerts", () => {
  it("marks a DB_TRUST_TAMPER alert as HIGH with an Indonesian explanation", () => {
    renderRow(item({ payload: { severity: "HIGH", code: "DB_TRUST_TAMPER", rows: 2 } }));
    expect(screen.getByText("HIGH: DB_TRUST_TAMPER")).toBeInTheDocument();
    expect(screen.getByText("Tabel DB tepercaya diubah proses lain saat worker berjalan (2 baris)")).toBeInTheDocument();
  });

  it("marks an INTEGRITY_HOLD alert and says every domain is blocked", () => {
    renderRow(item({ entity_type: "worker_integrity_hold", payload: { severity: "HIGH", code: "INTEGRITY_HOLD", findings: 3 } }));
    expect(screen.getByText("HIGH: INTEGRITY_HOLD")).toBeInTheDocument();
    expect(screen.getByText(/semua worker di semua domain diblokir sampai pemilik merekonsiliasi \(3 temuan\)/)).toBeInTheDocument();
  });

  it("leaves ordinary events plain and offers a filter for runtime alerts", () => {
    renderRow(item({ kind: "run.state_changed", source: "scheduler", payload: { severity: "HIGH" } }));
    expect(screen.queryByText(/^HIGH:/)).toBeNull();
    expect(screen.getByText("run.state_changed")).toBeInTheDocument();
    expect(FEED_FILTERS).toContain("worker_runtime.alert");
  });
});
