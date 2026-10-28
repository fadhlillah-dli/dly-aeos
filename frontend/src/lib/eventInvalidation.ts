import { useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useEventStream, type AeosEvent } from "./events";

/**
 * ONE event -> query invalidation map for the whole app (R28 "buat semuanya selalu uptodate"). Before, every page kept
 * its own filter (Tools: `inventory.reconcile.completed` only, Models: source `routing` only), so a decision, a
 * proposal or a policy change left Tools & Repos and Models & Routes showing the old state until a manual reload.
 *
 * The names are the backend's: `source` and `kind` as emitted by `aeos.events.service.emit` (see AeosEvent). Query
 * keys are the prefixes the pages use: ["inventory", ...] (components, map, blast, snapshots), ["routes"] and
 * ["route", key] (Models), ["decisions", ...], ["claude-status"], ["doctor", ...] and ["doctor-capabilities", ...]
 * (Readiness; secret events carry only a reference name or a store transition, never a value). High-frequency kinds (`run.heartbeat`,
 * `task.*`, `cost.usage.recorded`, ...) match nothing here, so a heartbeat never refetches a page.
 *
 * It refreshes on events and on window focus (the QueryClient default) with staleTime 15 s; there is no polling.
 */
export function queryKeysForEvent(e: Pick<AeosEvent, "source" | "kind">): QueryKey[] {
  const { source, kind } = e;
  const keys: QueryKey[] = [];
  if (/^(decision|proposal|policy)\./.test(kind)) {
    // A decision/proposal/policy change moves admission, enablement and route activation at once. The System Map's
    // queries live under ["inventory", "map", ...], so the "inventory" prefix covers them.
    keys.push(["inventory"], ["routes"], ["route"], ["decisions"]);
  }
  if (/^routes?\./.test(kind) || source === "routing") keys.push(["routes"], ["route"], ["inventory"]);
  if (kind.startsWith("inventory.")) keys.push(["inventory"]);
  if (source === "claude_bridge" || kind.startsWith("claude.")) keys.push(["claude-status"], ["routes"]);
  if (source === "secrets" && (kind === "secret.presence_changed" || kind === "secret.store_changed")) {
    // A key stored/deleted or the vault sealed/unsealed moves the routes' credential block (Models: "Aktif, tidak
    // dapat dipakai"), route eligibility and the Readiness checks (["doctor", ...], ["doctor-capabilities", ...]).
    keys.push(["routes"], ["route"], ["doctor"], ["doctor-capabilities"]);
  }
  return keys;
}

/** Coalescing window: a burst (a reconcile emits dozens of inventory.* events) refetches each query once. */
const FLUSH_MS = 400;

export function invalidateForEvents(qc: QueryClient, events: Pick<AeosEvent, "source" | "kind">[]): void {
  const seen = new Set<string>();
  for (const e of events) {
    for (const key of queryKeysForEvent(e)) {
      const id = JSON.stringify(key);
      if (seen.has(id)) continue;
      seen.add(id);
      void qc.invalidateQueries({ queryKey: key });
    }
  }
}

/** Install once (AppShell). Pages do not subscribe for invalidation themselves. */
export function useEventInvalidation(enabled = true): void {
  const qc = useQueryClient();
  const pending = useRef<Pick<AeosEvent, "source" | "kind">[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEventStream((e) => {
    if (queryKeysForEvent(e).length === 0) return;
    pending.current.push({ source: e.source, kind: e.kind });
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      const batch = pending.current;
      pending.current = [];
      invalidateForEvents(qc, batch);
    }, FLUSH_MS);
  }, enabled);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    },
    [],
  );
}
