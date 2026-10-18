import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderPage } from "../../components/proposals/testUtils";
import experiments from "./__fixtures__/experiments.json";
import leaderboard from "./__fixtures__/leaderboard.json";
import runs from "./__fixtures__/runs.json";
import suites from "./__fixtures__/suites.json";
import EvaluationPage from "./index";

afterEach(() => vi.unstubAllGlobals());

describe("Evaluation page renders real API payloads", () => {
  it("ranks eligible configurations and excludes the gate failure", async () => {
    mockApi({ "/api/eval/suites": suites, "/api/eval/experiments": experiments, "/api/eval/runs": runs, "/api/eval/leaderboard": leaderboard });
    renderPage(<EvaluationPage />);
    expect(await screen.findByText("Dikecualikan: gate gagal")).toBeInTheDocument();
    expect(screen.getAllByText(/glm-5\.3-flash@opencode-zen\/glm-free/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("tab", { name: "Eksperimen A–D" }));
    expect(await screen.findByText("A — Sequential")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Dimensi metrik" }));
    expect(await screen.findByText("Economics")).toBeInTheDocument();
  });
});
