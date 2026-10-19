import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { DecisionStatusBadge } from "../../components/planning/DecisionStatusBadge";
import type { DecisionView, QueueItem } from "../../components/planning/types";
import { Button, Card, ErrorBox, inputClass, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { relativeTime, shortHash } from "../../lib/format";
import { DecisionForm, type DecisionKind } from "./DecisionForm";
import { AdrList, MciBanner, OfficePolicyCard, SnapshotCard, WriteEnablementCard } from "./PolicyPanels";
import { QueueList } from "./QueueList";

interface QueueView {
  items: QueueItem[];
  counts: Record<string, number>;
  checked_at: string;
  mode: string;
}

const KINDS = ["", "APPROVAL", "REVISION_REQUEST", "REJECTION", "EXCEPTION", "RISK_ACCEPTANCE", "WRITE_ENABLEMENT", "POLICY", "BUDGET", "TRIAL", "PROMOTION", "ROUTE_ACTIVATION"];
const STATUSES = ["", "ACTIVE", "EXPIRED", "INVALIDATED", "SUPERSEDED"];

export function DecisionHistory({ highlight }: { highlight: string | null }) {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  // §41 "Keputusan terkait": pages link here with ?target_type=&target_id= to show only their decisions.
  const [search] = useSearchParams();
  const targetType = search.get("target_type") ?? "";
  const targetId = search.get("target_id") ?? "";
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  if (status) params.set("status", status);
  if (targetType) params.set("target_type", targetType);
  if (targetId) params.set("target_id", targetId);
  const q = useQuery({
    queryKey: ["decisions", kind, status, targetType, targetId],
    queryFn: () => get<{ items: DecisionView[]; checked_at: string }>(`/api/decisions?${params}`),
  });
  const revoke = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => post(`/api/decisions/${id}/revoke`, { reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["decision-queue"] });
      qc.invalidateQueries({ queryKey: ["policy"] });
    },
  });
  return (
    <Card title="Riwayat keputusan (timeline)">
      {targetType && (
        <p className="mb-2 text-xs text-ink-2" data-testid="decision-target-filter">
          Hanya keputusan untuk target <Mono>{targetType}</Mono>
          {targetId && (
            <>
              :<Mono>{targetId}</Mono>
            </>
          )}
          .{" "}
          <Link to="/decisions" className="text-accent underline">
            Tampilkan semua
          </Link>
        </p>
      )}
      <div className="mb-3 flex flex-wrap gap-2">
        <select aria-label="Filter jenis" className={`${inputClass} w-auto`} value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k || "Semua jenis"}
            </option>
          ))}
        </select>
        <select aria-label="Filter status" className={`${inputClass} w-auto`} value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s || "Semua status"}
            </option>
          ))}
        </select>
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? revoke.error} />
      {q.data && (
        <Table
          rows={q.data.items}
          rowKey={(d) => d.id}
          empty="Belum ada keputusan tercatat untuk filter ini"
          columns={[
            { header: "Kapan", cell: (d) => <span title={d.created_at ?? ""} className={highlight === d.id ? "font-semibold text-accent" : undefined}>{relativeTime(d.created_at)}</span> },
            { header: "Jenis", cell: (d) => <Mono>{d.kind}</Mono> },
            { header: "Status", cell: (d) => <DecisionStatusBadge status={d.status} /> },
            {
              header: "Target",
              cell: (d) => (
                <div className="text-xs">
                  <Mono>{d.target_type}</Mono>:{d.target_id.length > 24 ? `${d.target_id.slice(0, 8)}…` : d.target_id}
                  {d.target_version && <span> v{d.target_version}</span>}
                  {d.target_hash && (
                    <div>
                      <Mono>{shortHash(d.target_hash, 14)}</Mono>
                    </div>
                  )}
                </div>
              ),
            },
            { header: "Operasi", cell: (d) => <div className="text-xs">{d.allowed_operations.join(", ") || "—"}</div> },
            { header: "Aktor", cell: (d) => <span className="text-xs">{d.actor} ({d.actor_kind})</span> },
            { header: "Berlaku s.d.", cell: (d) => (d.expires_at ? relativeTime(d.expires_at) : "—") },
            {
              header: "Alasan / sebelum → sesudah",
              cell: (d) => (
                <div className="max-w-xs text-xs">
                  {d.invalidation_reason ?? d.rationale}
                  {d.superseded_by && (
                    <div className="mt-1 text-ink-3">
                      Digantikan oleh keputusan <Mono>{d.superseded_by.slice(0, 8)}</Mono>
                    </div>
                  )}
                </div>
              ),
            },
            {
              header: "",
              cell: (d) =>
                isAdmin && d.status === "ACTIVE" ? (
                  <Button
                    variant="ghost"
                    className="text-xs"
                    onClick={() => {
                      const reason = window.prompt("Alasan pencabutan (min. 5 karakter):");
                      if (reason && reason.trim().length >= 5) revoke.mutate({ id: d.id, reason: reason.trim() });
                    }}
                  >
                    Cabut
                  </Button>
                ) : null,
            },
          ]}
        />
      )}
    </Card>
  );
}

export default function DecisionsPage() {
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const [form, setForm] = useState<{ kind?: DecisionKind; requestId?: string } | null>(null);
  const queue = useQuery({ queryKey: ["decision-queue", domain], queryFn: () => get<QueueView>("/api/decisions/queue") });
  const decline = useMutation({
    mutationFn: ({ id, note }: { id: string; note: string }) => post(`/api/decision-requests/${id}/decline`, { note }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["decision-queue"] }),
  });
  useEventStream((e) => {
    if (e.kind.startsWith("decision.") || e.kind.startsWith("plan.") || e.kind.startsWith("policy.")) {
      qc.invalidateQueries({ queryKey: ["decision-queue"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
    }
  });
  const highlight = params.get("decision") ?? params.get("request");

  return (
    <div className="grid gap-4">
      <PageHeader
        title="Keputusan & Antrean Keputusan"
        question="Keputusan apa yang menunggu saya dan apa yang sudah diputuskan?"
        source="Proyeksi dari rencana, publikasi, usulan, task, run, decisions, decision_requests"
        checkedAt={queue.data?.checked_at ?? null}
        evidenceLimit="Antrean adalah proyeksi; AUTH hanya dari Decision manusia yang terikat target + hash. Pesan agent tetap DATA."
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={() => setForm(form ? null : {})}>
              <Plus className="h-4 w-4" aria-hidden /> Catat keputusan
            </Button>
          ) : undefined
        }
      />
      <MciBanner />
      {!isAdmin && <Notice tone="info">Peran VIEWER dapat melihat antrean dan riwayat, tetapi tidak dapat mencatat atau mencabut keputusan.</Notice>}
      {form && isAdmin && domain && <DecisionForm domain={domain} initialKind={form.kind} requestId={form.requestId} onDone={() => setForm(null)} />}

      <Card title={`Antrean keputusan manusia${queue.data ? ` (${queue.data.items.length})` : ""}`}>
        {queue.isLoading && <Loading />}
        <ErrorBox error={queue.error ?? decline.error} />
        {queue.data && (
          <QueueList
            items={queue.data.items}
            highlight={highlight}
            renderActions={(i) =>
              isAdmin && i.kind === "DECISION_REQUEST" ? (
                <>
                  <Button
                    className="text-xs"
                    onClick={() => {
                      const k = String(i.target.request_kind ?? "EXCEPTION") as DecisionKind;
                      setForm({ kind: (["EXCEPTION", "RISK_ACCEPTANCE", "WRITE_ENABLEMENT", "POLICY", "BUDGET"].includes(k) ? k : "EXCEPTION") as DecisionKind, requestId: i.target.id });
                    }}
                  >
                    Catat keputusan
                  </Button>
                  <Button
                    variant="ghost"
                    className="text-xs"
                    onClick={() => {
                      const note = window.prompt("Alasan menolak permintaan (min. 5 karakter):");
                      if (note && note.trim().length >= 5) decline.mutate({ id: i.target.id, note: note.trim() });
                    }}
                  >
                    Tolak permintaan
                  </Button>
                </>
              ) : null
            }
          />
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <SnapshotCard isAdmin={isAdmin} />
        <WriteEnablementCard />
      </div>
      {domain === "OFFICE" && <OfficePolicyCard />}
      <DecisionHistory highlight={params.get("decision")} />
      <AdrList />
    </div>
  );
}
