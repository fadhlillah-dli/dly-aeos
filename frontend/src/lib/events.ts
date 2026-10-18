import { useEffect, useRef, useState } from "react";

export interface AeosEvent {
  seq: number;
  event_id: string;
  at: string;
  source: string;
  kind: string;
  entity_type: string;
  entity_id: string;
  entity_version: string | null;
  project_id: string | null;
  domain: string | null;
  run_id: string | null;
  attempt: number | null;
  lease_id: string | null;
  mode: string;
  payload: Record<string, unknown>;
}

export type StreamStatus = "connecting" | "live" | "reconnecting" | "offline";

type Listener = (e: AeosEvent) => void;
type StatusListener = () => void;

/** Backend heartbeat is 15 s; three missed beats means the connection is dead even if not closed. */
const WATCHDOG_MS = 45_000;
const RETRY_MS = 3_000;
/** Keep the connection across a route change (one page unmounts before the next one mounts). */
const IDLE_CLOSE_MS = 2_000;
const STATUS_THROTTLE_MS = 1_000;

/**
 * ONE persisted-event stream per browser tab (§35 "Sumber data dan mekanisme update"), shared by
 * every subscriber. A per-component EventSource held 2-4 long-lived connections per page against the
 * browser's 6-per-origin limit and replayed the whole history on every mount, so each navigation
 * fired an invalidation storm. Semantics:
 *
 * - a fresh start opens at the current head (`latest_seq` of GET /api/events): pages fetched fresh
 *   data on mount, so history is neither downloaded nor re-dispatched. If the head lookup fails it
 *   falls back to cursor 0 and the backlog before the "ready" frame only advances the cursor;
 * - a reconnect sends the last cursor and DOES dispatch the backlog (those are missed live events;
 *   the backend replays every event after the cursor);
 * - events are deduped by seq; status drops to "reconnecting" on error or a silent connection, so
 *   the UI shows STALE/UNKNOWN instead of staying green;
 * - `restartEventStream()` (domain/project switch) closes the stream and starts over, because the
 *   backend applies the session's domain scope when the stream is opened (and ends a stream whose
 *   session changed elsewhere); `stopEventStream()` (logout) closes it and forgets the cursor.
 */
class SharedEventStream {
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  private es: EventSource | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private idleTimer: ReturnType<typeof setTimeout> | undefined;
  private watchdog: ReturnType<typeof setTimeout> | undefined;
  private notifyTimer: ReturnType<typeof setTimeout> | undefined;
  private lastNotify = 0;
  private cursor = 0;
  private live = false; // false while the initial (history) backlog is being replayed
  /** Bumped by restart/stop so a pending head lookup can never open a stream for an old scope. */
  private generation = 0;
  private starting = false;
  status: StreamStatus = "connecting";
  lastSeenAt: string | null = null;

  subscribe(listener: Listener, onStatus: StatusListener): () => void {
    this.listeners.add(listener);
    this.statusListeners.add(onStatus);
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = undefined;
    }
    if (!this.es && !this.retryTimer && !this.starting) this.start();
    return () => {
      this.listeners.delete(listener);
      this.statusListeners.delete(onStatus);
      if (this.listeners.size === 0 && !this.idleTimer) {
        this.idleTimer = setTimeout(() => {
          this.idleTimer = undefined;
          if (this.listeners.size === 0) this.close();
        }, IDLE_CLOSE_MS);
      }
    };
  }

  /** Start over at the head (new scope). History is skipped again; subscribers stay attached. */
  restart(): void {
    this.reset();
    if (this.listeners.size > 0) this.start();
  }

  /** Logout: close and forget the cursor. The next subscriber (after a new login) starts fresh. */
  stop(): void {
    this.reset();
  }

  private reset(): void {
    this.close();
    this.generation += 1;
    this.starting = false;
    this.cursor = 0;
    this.live = false;
    this.lastSeenAt = null;
    this.setStatus("connecting", true);
  }

  private start(): void {
    if (typeof EventSource === "undefined") return;
    if (this.cursor > 0) {
      this.open();
      return;
    }
    const gen = this.generation;
    this.starting = true;
    void headSeq().then((head) => {
      if (gen !== this.generation) return; // restarted/stopped meanwhile
      this.starting = false;
      if (this.es || this.listeners.size === 0) return;
      if (head !== null) this.cursor = Math.max(this.cursor, head);
      this.open();
    });
  }

  private open(): void {
    if (typeof EventSource === "undefined") return;
    this.retryTimer = undefined;
    const resuming = this.cursor > 0;
    // On a resume, everything after the cursor is a missed live event and must be dispatched.
    if (resuming) this.live = true;
    const es = new EventSource(`/api/events/stream?cursor=${this.cursor}`, { withCredentials: true });
    this.es = es;
    es.onopen = () => {
      this.armWatchdog();
      this.setStatus("live");
    };
    es.onmessage = (msg: MessageEvent<string>) => {
      this.armWatchdog();
      this.lastSeenAt = new Date().toISOString();
      let data: { type?: string; seq?: number; cursor?: number } & Partial<AeosEvent>;
      try {
        data = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (data.type === "event" && typeof data.seq === "number") {
        if (data.seq <= this.cursor) return; // dedupe on reconnect
        this.cursor = data.seq;
        if (this.live) {
          for (const l of [...this.listeners]) {
            try {
              l(data as AeosEvent);
            } catch (err) {
              console.warn("event listener failed", err);
            }
          }
        }
      } else if (typeof data.cursor === "number") {
        this.cursor = Math.max(this.cursor, data.cursor);
        if (data.type === "ready") this.live = true;
      }
      this.setStatus("live");
    };
    es.onerror = () => {
      if (this.es !== es) return;
      this.dropConnection();
      if (this.listeners.size > 0) this.retryTimer = setTimeout(() => this.open(), RETRY_MS);
    };
  }

  private armWatchdog(): void {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => {
      // No frame (not even a heartbeat) for 3 beats: treat as disconnected and resume from the cursor.
      this.dropConnection();
      if (this.listeners.size > 0) this.open();
    }, WATCHDOG_MS);
  }

  private dropConnection(): void {
    this.es?.close();
    this.es = null;
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = undefined;
    this.setStatus("reconnecting", true);
  }

  private close(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.watchdog) clearTimeout(this.watchdog);
    this.retryTimer = undefined;
    this.watchdog = undefined;
    this.es?.close();
    this.es = null;
  }

  private setStatus(status: StreamStatus, force = false): void {
    const changed = status !== this.status;
    this.status = status;
    const now = Date.now();
    // Status changes notify immediately; repeated "live" frames (heartbeats, replay bursts) are throttled.
    if (changed || force || now - this.lastNotify >= STATUS_THROTTLE_MS) {
      if (this.notifyTimer) clearTimeout(this.notifyTimer);
      this.notifyTimer = undefined;
      this.lastNotify = now;
      for (const s of [...this.statusListeners]) s();
    } else if (!this.notifyTimer) {
      this.notifyTimer = setTimeout(() => {
        this.notifyTimer = undefined;
        this.lastNotify = Date.now();
        for (const s of [...this.statusListeners]) s();
      }, STATUS_THROTTLE_MS);
    }
  }
}

/** Current head of the event log; null when unknown (then the stream starts at 0 and skips history). */
async function headSeq(): Promise<number | null> {
  try {
    const r = await fetch("/api/events?limit=1", { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!r.ok) return null;
    const body = (await r.json()) as { latest_seq?: unknown };
    return typeof body.latest_seq === "number" && body.latest_seq >= 0 ? body.latest_seq : null;
  } catch {
    return null;
  }
}

const shared = new SharedEventStream();

/** Domain/project switch: the backend scopes a stream when it is opened, so start over. */
export function restartEventStream(): void {
  shared.restart();
}

/** Logout: close the stream now (not after the idle delay) and forget the previous user's cursor. */
export function stopEventStream(): void {
  shared.stop();
}

/**
 * Subscribe to the persisted event stream (§35). Events trigger cache invalidation; the canonical
 * API response stays the data. Exposes freshness so the UI shows STALE/UNKNOWN when disconnected.
 * Every frame is a default SSE "message": {type: "event"|"ready"|"heartbeat", ...}.
 */
export function useEventStream(onEvent: (e: AeosEvent) => void, enabled = true) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [snap, setSnap] = useState<{ status: StreamStatus; lastSeenAt: string | null }>(() => ({
    status: shared.status,
    lastSeenAt: shared.lastSeenAt,
  }));

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const unsubscribe = shared.subscribe(
      (e) => handler.current(e),
      () => setSnap({ status: shared.status, lastSeenAt: shared.lastSeenAt }),
    );
    setSnap({ status: shared.status, lastSeenAt: shared.lastSeenAt });
    return unsubscribe;
  }, [enabled]);

  return snap;
}
