import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PageHeader } from "./ui";

// §41 (W6-113): every page offers 'Sumber data', 'Diperiksa pada', 'Batas bukti', 'Keputusan terkait' and
// 'Usulkan koreksi'. This renders the five from PageHeader's props (the static guard in pages/pageProvenance.test.ts
// checks that each page passes them).
describe("PageHeader provenance (§41)", () => {
  const props = {
    title: "Biaya",
    question: "Berapa biaya per rute?",
    source: "usage_ledger",
    evidenceLimit: "Estimasi bukan tagihan provider",
    relatedDecisionsHref: "/decisions?target_type=budget",
  };

  it("renders all five labels and both links inside a router", () => {
    const checked = new Date(Date.now() - 5 * 60_000).toISOString();
    render(
      <MemoryRouter>
        <PageHeader {...props} checkedAt={checked} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Sumber data:")).toBeInTheDocument();
    expect(screen.getByText("usage_ledger")).toBeInTheDocument();
    expect(screen.getByText("Diperiksa pada:")).toBeInTheDocument();
    const when = screen.getByText("Diperiksa pada:").nextElementSibling!;
    expect(when.textContent).not.toBe("belum pernah");
    expect(when.getAttribute("title")).toBeTruthy(); // absolute time on hover
    expect(screen.getByText("Batas bukti:")).toBeInTheDocument();
    expect(screen.getByText("Estimasi bukan tagihan provider")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Keputusan terkait" })).toHaveAttribute("href", "/decisions?target_type=budget");
    expect(screen.getByRole("link", { name: "Usulkan koreksi" })).toHaveAttribute(
      "href", `/proposals?kind=COMPARE&source=${encodeURIComponent("DLY AEOS Biaya")}`);
  });

  it("says 'belum pernah' when nothing was ever checked, and links work outside a router", () => {
    render(<PageHeader {...props} checkedAt={null} />);
    expect(screen.getByText("Diperiksa pada:").nextElementSibling).toHaveTextContent("belum pernah");
    expect(screen.getByText("Batas bukti:")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Keputusan terkait" })).toHaveAttribute("href", "/decisions?target_type=budget");
    expect(screen.getByRole("link", { name: "Usulkan koreksi" })).toBeInTheDocument();
  });

  it("hides only what a page explicitly opts out of", () => {
    render(<PageHeader title="Login" question="Masuk" relatedDecisionsHref={null} correctionHref={null} />);
    expect(screen.queryByText("Sumber data:")).not.toBeInTheDocument();
    expect(screen.queryByText("Diperiksa pada:")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
