import { render, screen } from "@testing-library/react";
import { statusSpec } from "../StatusBadge";
import { acceptanceSpec, costText, ExecState, SeparateStatuses, verifierSpec, workerSpec } from "./status";

describe("workforce status helpers (§35)", () => {
  it("renders five separate status badges, never one check mark", () => {
    render(
      <SeparateStatuses statuses={{ worker_reported: "EXITED_0", verifier: "NOT_RUN", review: "PENDING", acceptance: "PENDING", merged: "NOT_MERGED" }} />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText(/Worker: exit 0 \(belum terbukti\)/)).toBeInTheDocument();
    expect(screen.getByText(/Verifier: belum dijalankan/)).toBeInTheDocument();
    expect(screen.getByText(/Merge: belum \(manusia\)/)).toBeInTheDocument();
  });

  it("worker exit 0 and missing verifier data are never shown as pass", () => {
    expect(statusSpec(workerSpec("EXITED_0").status).tone).not.toBe("ok");
    expect(statusSpec(verifierSpec(null).status).tone).toBe("warn");
    expect(statusSpec(verifierSpec("NOT_RUN").status).tone).not.toBe("ok");
    expect(statusSpec(acceptanceSpec("PENDING").status).tone).not.toBe("ok");
    expect(statusSpec(workerSpec("EXITED_3").status).tone).toBe("bad");
  });

  it("understands the verifier's own vocabulary (PASS/FAIL/QUARANTINED)", () => {
    expect(verifierSpec("PASS").label).toBe("Verifier: lulus");
    expect(statusSpec(verifierSpec("FAIL").status).tone).toBe("bad");
    expect(statusSpec(verifierSpec("QUARANTINED").status).tone).not.toBe("ok");
  });

  it("shows canonical state next to the derived UI state", () => {
    render(<ExecState state="RUNNING" uiState="STALE" />);
    expect(screen.getByText("Berjalan")).toBeInTheDocument();
    expect(screen.getByText(/Tampilan: Heartbeat usang/)).toBeInTheDocument();
  });

  it("unknown cost stays UNKNOWN, not $0", () => {
    expect(costText("UNKNOWN")).toBe("UNKNOWN");
    expect(costText(null, 3)).toBe("UNKNOWN");
    expect(costText(0)).toBe("$0.0000");
  });
});
