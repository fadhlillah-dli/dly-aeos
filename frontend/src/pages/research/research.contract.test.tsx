import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import candidates from "./__fixtures__/candidates.json";
import references from "./__fixtures__/references.json";
import ResearchPage from "./index";

afterEach(() => vi.unstubAllGlobals());

describe("Research page renders real API payloads", () => {
  it("lists seeded candidates and the S-references tab", async () => {
    mockApi({ "/api/research?kind=reference": references, "/api/research": candidates });
    renderPage(<ResearchPage />, { path: "/research", route: "/research" });
    const first = (candidates as { items: { name: string }[] }).items[0].name;
    expect(await screen.findByText(first)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Referensi S01–S64" }));
    expect(await screen.findByText("S01")).toBeInTheDocument();
    expect(screen.getAllByText("E1 resmi").length).toBeGreaterThan(0);
  });
});
