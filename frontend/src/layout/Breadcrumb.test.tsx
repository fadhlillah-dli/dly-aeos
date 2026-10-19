import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Breadcrumb, crumbsFor } from "./Breadcrumb";

describe("Breadcrumb (§35 navigation alternatives)", () => {
  it("derives group -> page -> detail from the navigation", () => {
    const tools = crumbsFor("/tools/claude-code");
    expect(tools).toHaveLength(3);
    expect(tools[0].to).toBeUndefined(); // the navigation group
    expect(tools.slice(1)).toEqual([{ label: "Tools & Repos", to: "/tools" }, { label: "claude-code" }]);
    expect(crumbsFor("/runs/abc").at(1)).toEqual({ label: "Pekerjaan Live", to: "/work" });
    expect(crumbsFor("/plans/p1").at(1)?.to).toBe("/plans");
    expect(crumbsFor("/proposals/x").at(1)?.to).toBe("/proposals");
    expect(crumbsFor("/nowhere")).toEqual([]);
  });

  it("renders a trail with a link back to the list page on detail routes only", () => {
    const { unmount } = render(
      <MemoryRouter initialEntries={["/runs/run-1"]}>
        <Breadcrumb detailLabel="Run run-1" />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(nav).toHaveTextContent("Pekerjaan Live");
    expect(screen.getByRole("link", { name: "Pekerjaan Live" })).toHaveAttribute("href", "/work");
    expect(screen.getByText("Run run-1")).toHaveAttribute("aria-current", "page");
    unmount();
    render(
      <MemoryRouter initialEntries={["/work"]}>
        <Breadcrumb />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).not.toBeInTheDocument();
  });
});
