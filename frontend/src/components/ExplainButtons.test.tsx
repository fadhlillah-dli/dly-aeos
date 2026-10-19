import { fireEvent, render, screen } from "@testing-library/react";
import { ExplainButtons } from "./ExplainButtons";

describe("Explain buttons (§35: the six questions open the same source record)", () => {
  it("renders only the questions with record-derived content and shows that content", () => {
    render(
      <ExplainButtons
        content={{
          what: "Engine worker",
          why: "Alasan keputusan terakhir: trial lulus",
          usedBy: "Terakhir dipakai oleh run r-1",
          propose: "Usulkan perubahan untuk rute ini",
        }}
      />,
    );
    for (const label of ["Apa fungsinya?", "Mengapa dipilih?", "Dipakai oleh siapa?", "Usulkan perubahan"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Lihat bukti" })).not.toBeInTheDocument(); // no content -> no button
    fireEvent.click(screen.getByRole("button", { name: "Dipakai oleh siapa?" }));
    expect(screen.getByText("Terakhir dipakai oleh run r-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mengapa dipilih?" }));
    expect(screen.getByText("Alasan keputusan terakhir: trial lulus")).toBeInTheDocument();
  });
});
