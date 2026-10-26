import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useEventStream, type AeosEvent } from "../../lib/events";

// worker_runtime.alert (DB_TRUST_TAMPER / INTEGRITY_HOLD / CONTROLLER_MODIFIED, HIGH) changes admission and run state
const RELEVANT = /^(run\.|task\.|scheduler\.|broker\.grant|worker_runtime\.)/;

/**
 * Invalidate the given query keys when a persisted workforce event arrives (debounced). The event
 * stream is the trigger, the canonical API response is the data — events are never rendered as
 * state on their own. Returns the stream status so pages can show STALE/UNKNOWN when disconnected.
 */
export function useLiveInvalidation(keys: unknown[][], filter?: (e: AeosEvent) => boolean) {
  const qc = useQueryClient();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const keysRef = useRef(keys);
  keysRef.current = keys;
  const stream = useEventStream((e) => {
    if (!RELEVANT.test(e.kind)) return;
    if (filter && !filter(e)) return;
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      for (const k of keysRef.current) qc.invalidateQueries({ queryKey: k });
    }, 750);
  });
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return stream;
}
