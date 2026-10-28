import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import type { AeosEvent } from "./events";

// The shared event -> query invalidation map (R28). A decision/proposal/policy event refreshes inventory, routes and
// decisions; route.* refreshes routes and inventory; inventory.* refreshes inventory; a task heartbeat nothing.
let handler: ((e: AeosEvent) => void) | undefined;
vi.mock("./events", () => ({
  useEventStream: (h: (e: AeosEvent) => void) => {
    handler = h;
    return { status: "live", lastSeenAt: null };
  },
}));

import { queryKeysForEvent, useEventInvalidation } from "./eventInvalidation";

const ev = (source: string, kind: string) => ({ source, kind }) as AeosEvent;
const flat = (source: string, kind: string) => queryKeysForEvent({ source, kind }).map((k) => String(k[0]));

describe("queryKeysForEvent", () => {
  it("decision, proposal and policy events invalidate inventory, routes and decisions", () => {
    for (const [source, kind] of [["policy", "decision.recorded"], ["proposals", "proposal.decision"], ["policy", "policy.snapshot.activated"]]) {
      const keys = flat(source, kind);
      expect(keys).toEqual(expect.arrayContaining(["inventory", "routes", "decisions"]));
    }
  });

  it("route.* invalidates routes and inventory; inventory.* only inventory", () => {
    expect(flat("routing", "route.activated")).toEqual(expect.arrayContaining(["routes", "inventory"]));
    expect(flat("routing", "routes.catalog_refreshed")).toEqual(expect.arrayContaining(["routes", "inventory"]));
    expect(flat("inventory", "inventory.reconcile.completed")).toEqual(["inventory"]);
    expect(flat("inventory", "inventory.observation")).toEqual(["inventory"]);
    expect(flat("claude_bridge", "claude.capability_tested")).toEqual(expect.arrayContaining(["claude-status"]));
  });

  it("a task heartbeat or run event invalidates nothing", () => {
    expect(queryKeysForEvent({ source: "runner", kind: "run.heartbeat" })).toEqual([]);
    expect(queryKeysForEvent({ source: "scheduler", kind: "task.state_changed" })).toEqual([]);
    expect(queryKeysForEvent({ source: "cost", kind: "cost.usage.recorded" })).toEqual([]);
  });
});

describe("useEventInvalidation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    handler = undefined;
  });
  afterEach(() => vi.useRealTimers());

  function mount() {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    function Probe() {
      useEventInvalidation();
      return null;
    }
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    );
    return spy;
  }
  const keysOf = (spy: ReturnType<typeof mount>) => spy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);

  it("a decision event invalidates ['inventory']; a task heartbeat does not", () => {
    const spy = mount();
    act(() => handler?.(ev("runner", "run.heartbeat")));
    act(() => handler?.(ev("scheduler", "task.state_changed")));
    act(() => void vi.advanceTimersByTime(1000));
    expect(spy).not.toHaveBeenCalled();
    act(() => handler?.(ev("policy", "decision.recorded")));
    act(() => void vi.advanceTimersByTime(1000));
    expect(keysOf(spy)).toEqual(expect.arrayContaining(["inventory", "routes", "decisions"]));
    expect(spy.mock.calls.some((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey) === JSON.stringify(["inventory"]))).toBe(true);
  });

  it("a burst is coalesced: each query is invalidated once", () => {
    const spy = mount();
    act(() => {
      for (let i = 0; i < 30; i++) handler?.(ev("inventory", "inventory.observation"));
    });
    act(() => void vi.advanceTimersByTime(1000));
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
