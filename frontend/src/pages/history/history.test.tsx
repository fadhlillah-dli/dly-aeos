import { render, screen } from "@testing-library/react";
import { RecoveryNotice } from "./BackupsTab";
import { CONFIRM_PHRASE, StepList } from "./EmergencyTab";

describe("Backups recovery notice (§33, §50)", () => {
  it("warns that restore is untested when no backup is ready", () => {
    render(<RecoveryNotice data={{ items: [], recovery_ready: false, latest_restore_tested: null }} />);
    expect(screen.getByText("Restore belum diuji")).toBeInTheDocument();
    expect(screen.getByText(/pemulihan belum terbukti/)).toBeInTheDocument();
  });

  it("names the restore-tested backup when ready", () => {
    render(<RecoveryNotice data={{ items: [], recovery_ready: true, latest_restore_tested: "20261018T000000Z-x-1" }} />);
    expect(screen.getByText("20261018T000000Z-x-1")).toBeInTheDocument();
  });
});

describe("Emergency stop report", () => {
  it("shows every step with its own status and reason", () => {
    render(
      <StepList
        steps={[
          { step: "pause_scheduling", status: "SUCCEEDED", detail: "scheduling paused" },
          { step: "cancel_runs", status: "FAILED", reason: "module unavailable: aeos.workforce.scheduler" },
        ]}
      />,
    );
    expect(screen.getByText("pause_scheduling")).toBeInTheDocument();
    expect(screen.getByText(/module unavailable/)).toBeInTheDocument();
    expect(screen.getByText("Gagal")).toBeInTheDocument();
  });

  it("requires a strong typed confirmation", () => {
    expect(CONFIRM_PHRASE).toBe("HENTIKAN");
  });
});
