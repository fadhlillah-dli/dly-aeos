import { render, screen } from "@testing-library/react";
import { RemedyPanel } from "./RemedyPanel";

describe("RemedyPanel (§08 remedy is a proposal)", () => {
  const remedy = {
    operation: "Buat database lokal",
    command: "./aeos db upgrade",
    source: "aeos.core.migrate",
    target: "<data>/aeos.sqlite3",
    privilege: "pengguna lokal",
    cost: "tidak ada",
    rollback: "restore backup",
    evidence_to_collect: "aeos db status",
    executed_by: "MANUSIA (salin dan jalankan sendiri)",
  };

  it("shows every remedy field and a copy button, never a run button", () => {
    render(<RemedyPanel remedy={remedy} />);
    expect(screen.getByText("Buat database lokal")).toBeInTheDocument();
    expect(screen.getByText("./aeos db upgrade")).toBeInTheDocument();
    for (const label of ["Sumber / versi", "Target", "Hak akses", "Biaya", "Rollback", "Bukti yang dikumpulkan", "Dijalankan oleh"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: /Salin perintah/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /jalankan|run|install/i })).not.toBeInTheDocument();
    expect(screen.getByText(/tidak menjalankan perbaikan/)).toBeInTheDocument();
  });

  it("warns when the remedy is destructive or paid", () => {
    render(<RemedyPanel remedy={remedy} requiresDestructive requiresPaidWrite />);
    expect(screen.getByText(/destruktif/)).toBeInTheDocument();
    expect(screen.getByText(/menimbulkan biaya/)).toBeInTheDocument();
  });

  it("handles a result without a remedy", () => {
    render(<RemedyPanel remedy={{}} />);
    expect(screen.getByText(/Tidak ada usulan perbaikan/)).toBeInTheDocument();
  });
});
