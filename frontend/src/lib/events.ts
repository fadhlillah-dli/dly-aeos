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

/**
 * Subscribe to the persisted event stream (§35). Reconnects with the last cursor, dedupes by seq,
 * and exposes freshness so the UI shows STALE/UNKNOWN instead of staying green when disconnected.
 * Every frame is a default SSE "message": {type: "event"|"ready"|"heartbeat", ...}.
 */
export function useEventStream(onEvent: (e: AeosEvent) => void, enabled = true) {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [lastSeenAt, setLastSeenAt] = useState<string | null>(null);
  const cursor = useRef(0);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let closed = false;

    const open = () => {
      es = new EventSource(`/api/events/stream?cursor=${cursor.current}`, { withCredentials: true });
      es.onopen = () => setStatus("live");
      es.onmessage = (msg: MessageEvent<string>) => {
        setLastSeenAt(new Date().toISOString());
        setStatus("live");
        let data: { type?: string; seq?: number; cursor?: number } & Partial<AeosEvent>;
        try {
          data = JSON.parse(msg.data);
        } catch {
          return;
        }
        if (data.type === "event" && typeof data.seq === "number") {
          if (data.seq <= cursor.current) return; // dedupe on reconnect
          cursor.current = data.seq;
          handler.current(data as AeosEvent);
        } else if (typeof data.cursor === "number") {
          cursor.current = Math.max(cursor.current, data.cursor);
        }
      };
      es.onerror = () => {
        es?.close();
        if (closed) return;
        setStatus("reconnecting");
        retry = setTimeout(open, 3000);
      };
    };
    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      es?.close();
    };
  }, [enabled]);

  return { status, lastSeenAt };
}
