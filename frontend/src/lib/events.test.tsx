import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

class FakeEventSource {
  static all: FakeEventSource[] = [];
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((m: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.all.push(this);
  }
  close() {
    this.closed = true;
  }
  send(frame: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent<string>);
  }
}

const ev = (seq: number, kind = "run.state_changed") => ({ type: "event", seq, kind, entity_id: `e${seq}` });

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.all = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.resetModules();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function load() {
  return await import("./events");
}

describe("shared event stream (one EventSource per tab)", () => {
  it("shares one connection, skips the initial history backlog and dedupes by seq", async () => {
    const { useEventStream } = await load();
    const seen: number[][] = [[], [], []];
    function Sub({ i }: { i: number }) {
      useEventStream((e) => seen[i].push(e.seq));
      return null;
    }
    render(
      <>
        <Sub i={0} />
        <Sub i={1} />
        <Sub i={2} />
      </>,
    );
    expect(FakeEventSource.all).toHaveLength(1);
    const es = FakeEventSource.all[0];
    expect(es.url).toContain("cursor=0");
    act(() => {
      es.onopen?.();
      es.send(ev(1));
      es.send(ev(2)); // history: pages fetched fresh data on mount
      es.send({ type: "ready", cursor: 2 });
      es.send(ev(3));
      es.send(ev(3)); // duplicate
    });
    expect(seen).toEqual([[3], [3], [3]]);
  });

  it("resumes from the cursor after an error and dispatches the missed events", async () => {
    const { useEventStream } = await load();
    const seen: number[] = [];
    function Sub() {
      const s = useEventStream((e) => seen.push(e.seq));
      return <span data-testid="s">{s.status}</span>;
    }
    const view = render(<Sub />);
    const first = FakeEventSource.all[0];
    act(() => {
      first.onopen?.();
      first.send({ type: "ready", cursor: 5 });
      first.onerror?.();
    });
    expect(view.getByTestId("s").textContent).toBe("reconnecting");
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    const second = FakeEventSource.all[1];
    expect(second.url).toContain("cursor=5");
    act(() => {
      second.onopen?.();
      second.send(ev(6)); // missed while disconnected
      second.send({ type: "ready", cursor: 6 });
    });
    expect(seen).toEqual([6]);
    expect(view.getByTestId("s").textContent).toBe("live");
  });

  it("marks a silent connection as reconnecting (no green without heartbeats)", async () => {
    const { useEventStream } = await load();
    function Sub() {
      const s = useEventStream(() => {});
      return <span data-testid="s">{s.status}</span>;
    }
    const view = render(<Sub />);
    act(() => {
      FakeEventSource.all[0].onopen?.();
    });
    expect(view.getByTestId("s").textContent).toBe("live");
    act(() => {
      vi.advanceTimersByTime(46_000);
    });
    expect(FakeEventSource.all[0].closed).toBe(true);
    expect(FakeEventSource.all.length).toBe(2);
    expect(view.getByTestId("s").textContent).toBe("reconnecting");
  });

  it("restarts from cursor 0 on a domain switch and closes when nobody listens", async () => {
    const { useEventStream, restartEventStream } = await load();
    function Sub() {
      useEventStream(() => {});
      return null;
    }
    const view = render(<Sub />);
    act(() => {
      FakeEventSource.all[0].send({ type: "ready", cursor: 9 });
      restartEventStream();
    });
    expect(FakeEventSource.all[0].closed).toBe(true);
    expect(FakeEventSource.all[1].url).toContain("cursor=0");
    view.unmount();
    act(() => {
      vi.advanceTimersByTime(2500);
    });
    expect(FakeEventSource.all[1].closed).toBe(true);
  });
});
