import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { QueueItem } from "../../components/planning/types";
import { parseScope } from "./DecisionForm";
import { formatAge, QueueList } from "./QueueList";

const ITEM: QueueItem = {
  id: "PLAN:p1",
  kind: "PLAN",
  title: "Rencana siap",
  target: { type: "plan", id: "p1", version: 2, hash: "sha256:abcdef0123456789" },
  why: "Empat tahap perencanaan selesai",
  requested_decision: "Approve & Publish, Approve, Request Revision, atau Reject",
  link: "/plans/p1",
  created_at: "2026-10-18T00:00:00Z",
  age_seconds: 7200,
  project_id: "x",
  domain: "PERSONAL",
  priority: 1,
};

describe("Human Decision Queue (U-12)", () => {
  it("shows why an item waits, the requested decision, the exact target and the link", () => {
    render(
      <MemoryRouter>
        <QueueList items={[ITEM]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Rencana siap")).toBeInTheDocument();
    expect(screen.getByText(/Empat tahap perencanaan selesai/)).toBeInTheDocument();
    expect(screen.getByText(/Approve & Publish/)).toBeInTheDocument();
    expect(screen.getByText("abcdef0123")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Tindak lanjuti/ })).toHaveAttribute("href", "/plans/p1");
    expect(screen.getByText(/menunggu 2 jam/)).toBeInTheDocument();
  });

  it("explains an empty queue", () => {
    render(
      <MemoryRouter>
        <QueueList items={[]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Tidak ada keputusan yang menunggu Anda")).toBeInTheDocument();
  });

  it("formats ages", () => {
    expect(formatAge(30)).toBe("1 mnt");
    expect(formatAge(90000)).toBe("1 hari");
    expect(formatAge(null)).toBe("—");
  });
});

describe("parseScope", () => {
  it("accepts key=value pairs and JSON; empty means no scope", () => {
    expect(parseScope("repo=o/r; reason=hotfix")).toEqual({ repo: "o/r", reason: "hotfix" });
    expect(parseScope('{"a": 1}')).toEqual({ a: 1 });
    expect(parseScope("  ")).toEqual({});
    expect(() => parseScope("{bad")).toThrow();
  });
});
