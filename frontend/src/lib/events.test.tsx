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

/** GET /api/events?limit=1 answers with the head seq; `null` = the lookup fails (network error). */
let head: number | null = null;
const fetchMock = vi.fn(async (url: string) => {
  if (head === null) throw new TypeError("network down");
  expect(url).toBe("/api/events?limit=1");
  return { ok: true, json: async () => ({ events: [], latest_seq: head }) } as unknown as Response;
});

/** Let the async head lookup resolve (microtasks only; fake timers stay frozen). */
async function flush() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.all = [];
  head = null;
  fetchMock.mockClear();
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", fetchMock);
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
  it("opens one shared connection at the current head and dispatches only newer events", async () => {
    head = 41;
    const { useEventStream } = await load();
    const seen: number[][] = [[], []];
    function Sub({ i }: { i: number }) {
      useEventStream((e) => seen[i].push(e.seq));
      return null;
    }
    render(
      <>
        <Sub i={0} />
        <Sub i={1} />
      </>,
    );
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.all).toHaveLength(1);
    const es = FakeEventSource.all[0];
    expect(es.url).toContain("cursor=41"); // no history download on page load
    act(() => {
      es.onopen?.();
      es.send(ev(42)); // happened between the head lookup and the stream opening: a real new event
      es.send({ type: "ready", cursor: 42 });
      es.send(ev(43));
      es.send(ev(43)); // duplicate
    });
    expect(seen).toEqual([
      [42, 43],
      [42, 43],
    ]);
  });

  it("falls back to cursor 0 when the head is unknown and skips the history backlog", async () => {
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
    await flush();
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
    await flush();
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
    await flush();
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

  it("restarts at the new head on a domain switch and closes when nobody listens", async () => {
    head = 9;
    const { useEventStream, restartEventStream } = await load();
    function Sub() {
      useEventStream(() => {});
      return null;
    }
    const view = render(<Sub />);
    await flush();
    act(() => {
      FakeEventSource.all[0].send({ type: "ready", cursor: 9 });
    });
    head = 12;
    act(() => restartEventStream());
    expect(FakeEventSource.all[0].closed).toBe(true);
    await flush();
    expect(FakeEventSource.all).toHaveLength(2);
    expect(FakeEventSource.all[1].url).toContain("cursor=12");
    view.unmount();
    act(() => {
      vi.advanceTimersByTime(2500);
    });
    expect(FakeEventSource.all[1].closed).toBe(true);
  });

  it("a restart while the head lookup is pending never opens a stream for the old scope", async () => {
    head = 3;
    const { useEventStream, restartEventStream } = await load();
    function Sub() {
      useEventStream(() => {});
      return null;
    }
    render(<Sub />);
    act(() => restartEventStream()); // before the first lookup resolved
    await flush();
    expect(FakeEventSource.all).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("logout closes the stream at once, does not reconnect and forgets the previous cursor", async () => {
    head = 20;
    const { useEventStream, stopEventStream } = await load();
    const seen: number[] = [];
    function Sub() {
      const s = useEventStream((e) => seen.push(e.seq));
      return <span data-testid="s">{s.status}</span>;
    }
    const first = render(<Sub />);
    await flush();
    act(() => {
      FakeEventSource.all[0].onopen?.();
      FakeEventSource.all[0].send({ type: "ready", cursor: 25 });
      stopEventStream();
    });
    expect(FakeEventSource.all[0].closed).toBe(true);
    expect(first.getByTestId("s").textContent).toBe("connecting");
    act(() => {
      FakeEventSource.all[0].onerror?.(); // late error from the closed connection
      vi.advanceTimersByTime(10_000);
    });
    expect(FakeEventSource.all).toHaveLength(1);
    first.unmount();

    // Next login: a fresh start at the head, not a resume from the previous user's cursor 25.
    head = 30;
    render(<Sub />);
    await flush();
    expect(FakeEventSource.all).toHaveLength(2);
    expect(FakeEventSource.all[1].url).toContain("cursor=30");
  });
});
