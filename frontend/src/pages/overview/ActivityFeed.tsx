import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { get } from "../../api/client";
import { Provenance } from "../../components/control_center/Provenance";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Loading, Mono } from "../../components/ui";
import { useEventStream, type AeosEvent, type StreamStatus } from "../../lib/events";
import { absoluteTime, relativeTime } from "../../lib/format";
import type { ActivityItem, ActivityPage } from "./types";

const MAX_ITEMS = 60;

export function mergeEvents(current: ActivityItem[], incoming: ActivityItem[]): ActivityItem[] {
  const seen = new Set(current.map((e) => e.seq));
  const merged = [...incoming.filter((e) => !seen.has(e.seq)), ...current];
  return merged.sort((a, b) => b.seq - a.seq).slice(0, MAX_ITEMS);
}

export function StreamState({ status, lastSeenAt }: { status: StreamStatus; lastSeenAt: string | null }) {
  if (status === "live") return <StatusBadge status="LIVE" label="Stream live" />;
  return (
    <span className="inline-flex items-center gap-2">
      <StatusBadge status="STALE" label={status === "reconnecting" ? "Menyambung ulang — data mungkin usang" : "Stream belum tersambung"} />
      <span className="text-xs text-ink-3">terakhir diterima: {lastSeenAt ? relativeTime(lastSeenAt) : "belum pernah"}</span>
    </span>
  );
}

export function ActivityFeed({ domain }: { domain: string }) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [kind, setKind] = useState("");
  const q = useQuery({
    queryKey: ["cc-activity", domain, kind],
    queryFn: () => get<ActivityPage>(`/api/control-center/activity?limit=30${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`),
  });
  useEffect(() => {
    if (q.data) setItems(q.data.items);
  }, [q.data]);
  const onEvent = useCallback(
    (e: AeosEvent) => {
      if (kind && !e.kind.startsWith(kind)) return;
      setItems((cur) => mergeEvents(cur, [e]));
    },
    [kind],
  );
  const { status, lastSeenAt } = useEventStream(onEvent, true);

  return (
    <Card title="Aktivitas terbaru" actions={<StreamState status={status} lastSeenAt={lastSeenAt} />}>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {["", "alert.", "ops.", "task.", "run.", "plan.", "publication."].map((k) => (
          <Button key={k || "all"} variant={kind === k ? "primary" : "secondary"} className="text-xs" onClick={() => setKind(k)}>
            {k || "semua"}
          </Button>
        ))}
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : items.length === 0 ? (
        <EmptyState title="Belum ada aktivitas tercatat">Aktivitas muncul saat controller, doctor atau worker mencatat event.</EmptyState>
      ) : (
        <ol className="max-h-96 space-y-1 overflow-y-auto text-sm" aria-label="Aktivitas">
          {items.map((e) => (
            <li key={e.seq} className="flex flex-wrap items-baseline gap-2 border-b border-line pb-1 last:border-0">
              <span className="w-20 shrink-0 text-xs text-ink-3" title={absoluteTime(e.at)}>
                {relativeTime(e.at)}
              </span>
              <Mono>{e.kind}</Mono>
              <span className="text-ink-2">
                {e.entity_type}
                {e.entity_id ? ` ${e.entity_id.slice(0, 12)}` : ""}
              </span>
              <span className="text-xs text-ink-3">sumber: {e.source}</span>
              {e.mode !== "LIVE" && <StatusBadge status={e.mode} />}
            </li>
          ))}
        </ol>
      )}
      <Provenance
        source="events (dipersist dulu, lalu diproyeksikan; kursor seq)"
        checkedAt={lastSeenAt ?? q.data?.checked_at ?? null}
        evidenceLimit="Tindakan teramati yang tercatat; narasi AI bukan status. Stream putus = STALE."
        staleAfterSeconds={120}
      />
    </Card>
  );
}
