import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

vi.mock("../../auth/AuthContext", () => ({ useAuth: () => ({ isAdmin: false, domain: "PERSONAL", session: null, projectId: null }) }));

import { ClaudeReview, ProjectResults } from "./index";

describe("Readiness C4 (§07, §08)", () => {
  it("lists one result per project target", () => {
    render(
      <MemoryRouter>
        <ProjectResults
          items={[{ check_id: "adapter.project_repo", title: "Repo GitHub proyek", project_id: "abcdef123456", effective_status: "FAILED", summary: "me/other FAILED", last_checked_at: null }]}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("project-results")).toHaveTextContent("me/other FAILED");
    expect(screen.getByText("abcdef12")).toBeInTheDocument();
  });

  it("shows the Claude review only as E0 advisory", () => {
    render(
      <MemoryRouter>
        <ClaudeReview
          review={{ at: null, ok: true, summary: "Dua pemeriksaan perlu tindakan.", note: "Saran Claude (E0, advisory): tidak mengubah status", error_code: null, model_resolved: "opus",
            findings: [{ check_id: "host.disk", observation: "disk penuh", suggestion: "kosongkan ruang" }] }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/advisory, E0/)).toBeInTheDocument();
    expect(screen.getByTestId("claude-review-findings")).toHaveTextContent("kosongkan ruang");
  });
});
