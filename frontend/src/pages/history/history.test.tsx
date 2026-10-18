import { fireEvent, render, screen } from "@testing-library/react";
import { LastRestoreTest, RecoveryNotice } from "./BackupsTab";
import { RetirementView } from "./RetirementTab";
import { ServicesView } from "./ServicesTab";
import { CONFIRM_PHRASE, StepList } from "./EmergencyTab";
import { IntFlagEditor } from "./OpsTab";

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

describe("Backups table: last restore test (§33)", () => {
  it("never shows a passed-but-unrecorded test as ready evidence", () => {
    render(
      <LastRestoreTest
        row={{
          backup_id: "b1",
          readiness: "RESTORE_TEST_UNRECORDED",
          ready: false,
          last_restore_test: {
            status: "PASSED_UNRECORDED",
            started_at: new Date().toISOString(),
            finished_at: new Date().toISOString(),
            problems: [],
            warnings: [],
            evidence_id: null,
            evidence_note: "verification module unavailable",
            stats: {},
          },
        }}
      />,
    );
    expect(screen.getByText("Lulus tanpa bukti tercatat")).toBeInTheDocument();
    expect(screen.getByText("verification module unavailable")).toBeInTheDocument();
  });

  it("says when a backup was never restore-tested", () => {
    render(<LastRestoreTest row={{ backup_id: "b2", readiness: "NOT_RESTORE_TESTED", ready: false, last_restore_test: null }} />);
    expect(screen.getByText("Belum pernah diuji")).toBeInTheDocument();
  });
});

describe("Emergency report scope (§35)", () => {
  it("shows active-domain counts and the other-domain note", () => {
    render(
      <StepList
        scope="PERSONAL"
        steps={[
          {
            step: "cancel_runs",
            status: "PARTIAL",
            reason: "Domain ini tuntas, tetapi langkah tidak tuntas di domain lain",
            active_runs: 2,
            cancelled: 2,
            other_domains: "Langkah ini juga mencakup domain lain",
          },
        ]}
      />,
    );
    expect(screen.getByText("Rincian untuk domain PERSONAL.")).toBeInTheDocument();
    expect(screen.getByText(/run aktif: 2, dibatalkan: 2/)).toBeInTheDocument();
    expect(screen.getByText("Langkah ini juga mencakup domain lain")).toBeInTheDocument();
    expect(screen.getByText("Sebagian")).toBeInTheDocument();
  });
});

describe("Services and runbooks (§33)", () => {
  it("labels a reachable service as connected, not verified", () => {
    render(
      <ServicesView
        services={{
          items: [{ service: "openbao", kind: "external", status: "CONNECTED", detail: "health endpoint reachable (sealed)" }],
          source: "probe",
          checked_at: new Date().toISOString(),
        }}
        runbooks={{ items: [{ id: "backup-restore", title: "Runbook: Backup", trigger: "before upgrade", path: "docs/runbooks/backup-restore.md" }] }}
      />,
    );
    expect(screen.getByText("Terhubung (belum diverifikasi fungsi)")).toBeInTheDocument();
    expect(screen.getByText("Runbook: Backup")).toBeInTheDocument();
  });
});

describe("Retirement checklist (§33)", () => {
  it("is read-only and lists blocking items with owner actions", () => {
    render(
      <RetirementView
        data={{
          project: { id: "p1", name: "Alpha", domain: "PERSONAL", lifecycle: "ACTIVE" },
          items: [
            { key: "jobs", title: "Pekerjaan dan job aktif", status: "BLOCKING", detail: { open_tasks: 1 }, owner_action: "Selesaikan task." },
            { key: "final_verification", title: "Verifikasi akhir pemilik", status: "PENDING_OWNER", detail: {}, owner_action: "Catat keputusan." },
          ],
          blocking: ["jobs"],
          notice: "Daftar periksa read-only.",
          checked_at: new Date().toISOString(),
        }}
      />,
    );
    expect(screen.getByText("Memblokir")).toBeInTheDocument();
    expect(screen.getByText("Menunggu pemilik")).toBeInTheDocument();
    expect(screen.getByText(/Masih memblokir: jobs/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Integer feature flag editor (§35 feature flags)", () => {
  const flag = { key: "ops.sli_loop_interval_seconds", kind: "int", value: 300, default: 300, is_default: true, description: "", consumer: "loop" };

  it("only saves a changed integer value", () => {
    const saved: number[] = [];
    render(<IntFlagEditor flag={flag} disabled={false} busy={false} onSave={(v) => saved.push(v)} />);
    const button = screen.getByRole("button", { name: "Simpan" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Nilai ops.sli_loop_interval_seconds"), { target: { value: "600" } });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(saved).toEqual([600]);
    fireEvent.change(screen.getByLabelText("Nilai ops.sli_loop_interval_seconds"), { target: { value: "1.5" } });
    expect(button).toBeDisabled();
  });

  it("is disabled for VIEWER sessions", () => {
    render(<IntFlagEditor flag={flag} disabled busy={false} onSave={() => undefined} />);
    expect(screen.getByLabelText("Nilai ops.sli_loop_interval_seconds")).toBeDisabled();
  });
});
