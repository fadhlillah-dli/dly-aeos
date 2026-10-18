import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { ExternalLink, Eye, RefreshCw, RotateCcw, Send, ShieldCheck, XCircle } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import { DIMENSION_LABELS, DIMENSION_ORDER, label, needsAttention, safeHref, shortId } from "./labels";
import type { Candidate, ListResponse, OperationView, PublicationView } from "./types";

function Dimensions({ pub }: { pub: PublicationView }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
      {DIMENSION_ORDER.map((k) => {
        const d = pub.status_dimensions[k];
        return (
          <div key={k} className="rounded-md border border-line p-2">
            <div className="text-xs text-ink-3">{DIMENSION_LABELS[k]}</div>
            <StatusBadge status={d?.status ?? "UNKNOWN"} label={label(d?.status)} />
            <div className="mt-1 text-xs text-ink-3" title={d?.source}>
              {d?.detail}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ExecuteBox({ pub, onDone }: { pub: PublicationView; onDone: () => void }) {
  const { isAdmin } = useAuth();
  const [confirmed, setConfirmed] = useState(false);
  const m = useMutation({
    mutationFn: () =>
      post<PublicationView>(`/api/publications/${pub.id}/execute`, {
        plan_version_id: pub.plan_version_id,
        approved_hash: pub.approved_hash,
        preview_hash: pub.preview_hash ?? "",
      }),
    onSuccess: onDone,
  });
  const reject = useMutation({
    mutationFn: () => post<PublicationView>(`/api/publications/${pub.id}/reject`, { reason: "" }),
    onSuccess: onDone,
  });
  const blocked = pub.blocked_targets.length > 0;
  return (
    <Card title={<span className="flex items-center gap-2"><ShieldCheck className="h-4 w-4" aria-hidden /> Approve &amp; Publish</span>}>
      <div className="space-y-3 text-sm">
        <KeyValue
          items={[
            ["PlanVersion", <span key="v">v{pub.plan_version_number} <Mono>{shortId(pub.plan_version_id, 12)}</Mono></span>],
            ["Approved hash", <Mono key="a">{pub.approved_hash}</Mono>],
            ["Preview hash", <Mono key="p">{pub.preview_hash}</Mono>],
            ["Keputusan rencana", <Mono key="d">{shortId(pub.decision_id, 12)}</Mono>],
          ]}
        />
        <Notice tone="info">{pub.approval_statement}</Notice>
        {blocked && (
          <Notice tone="warn">
            Target belum siap — eksekusi ditolak sampai diperbaiki atau preview dibuat tanpa target ini:
            <ul className="mt-1 list-disc pl-5">
              {pub.blocked_targets.map((t) => (
                <li key={t.family}>
                  {t.label}: {t.reasons.join("; ")}
                  {t.remedies.map((r) => (
                    <div key={r}>
                      Remedy: <Mono>{r}</Mono>
                    </div>
                  ))}
                </li>
              ))}
            </ul>
          </Notice>
        )}
        {isAdmin ? (
          <>
            <label className="flex items-start gap-2">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-1" />
              <span>
                Saya menyetujui publikasi operasi di atas untuk versi dan hash yang tertera. Interlock tetap memeriksa
                setiap operasi; versi lain memerlukan preview dan persetujuan baru.
              </span>
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={!confirmed || blocked} busy={m.isPending} onClick={() => m.mutate()}>
                <Send className="h-4 w-4" aria-hidden /> Approve &amp; Publish
              </Button>
              <Button busy={reject.isPending} onClick={() => reject.mutate()}>
                <XCircle className="h-4 w-4" aria-hidden /> Tolak preview
              </Button>
              {pub.plan_id && (
                <Link to={`/plans/${pub.plan_id}`} className="text-sm text-accent underline">
                  Minta revisi rencana
                </Link>
              )}
            </div>
            <p className="text-xs text-ink-3">
              Tolak preview tidak menulis apa pun ke provider. Revisi rencana membuat PlanVersion baru yang perlu disetujui
              lagi sebelum dipublikasikan.
            </p>
          </>
        ) : (
          <Notice tone="info">Hanya SUPER_ADMIN yang dapat menyetujui publikasi (backend tetap menegakkan).</Notice>
        )}
        <ErrorBox error={m.error || reject.error} />
      </div>
    </Card>
  );
}

const DELTA_LABELS: Record<string, string> = {
  created: "Dibuat",
  changed: "Berubah",
  unchanged: "Tidak berubah",
  superseded: "Digantikan (tidak dihapus di provider)",
};

/** §05 langkah 9: pemilik melihat target publikasi dan delta dokumen sebelum Approve & Publish. */
function TargetsAndDelta({ pub }: { pub: PublicationView }) {
  const delta = pub.preview.documents_delta ?? {};
  const visuals = pub.preview.visuals ?? [];
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <div className="space-y-2">
        <div className="text-sm font-medium">Target publikasi</div>
        {pub.targets.length === 0 ? (
          <p className="text-xs text-ink-3">Tidak ada target yang diminta.</p>
        ) : (
          <ul className="space-y-1 text-xs">
            {pub.targets.map((t) => (
              <li key={`${t.purpose}-${t.family}`} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{t.purpose === "docs" ? "Dokumen" : "Work item"}: {t.label}</span>
                <StatusBadge
                  status={t.status === "READY" ? "CONFIGURED" : t.status}
                  label={t.status === "READY" ? "Terkonfigurasi (belum diverifikasi)" : label(t.status)}
                />
                {t.reasons.length > 0 && <span className="text-ink-3">{t.reasons.join("; ")}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="space-y-2">
        <div className="text-sm font-medium">Delta dokumen dari versi yang disetujui</div>
        {Object.keys(delta).length === 0 ? (
          <p className="text-xs text-ink-3">Delta tidak tersedia untuk publication ini.</p>
        ) : (
          <div className="space-y-1 text-xs">
            {Object.entries(delta).map(([k, ids]) => (
              <details key={k}>
                <summary className="cursor-pointer">
                  {DELTA_LABELS[k] ?? k}: <span className="font-medium">{ids.length}</span>
                </summary>
                <ul className="mt-1 max-h-40 overflow-auto pl-4">
                  {ids.map((id) => (
                    <li key={id}>
                      <Mono>{id}</Mono>
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        )}
        {visuals.length > 0 && (
          <div className="space-y-1 text-xs">
            <div className="font-medium">Visual (desain, bukan bukti runtime)</div>
            {visuals.map((v) => (
              <div key={v.key} className="flex flex-wrap items-center gap-2">
                <span>{v.title}</span>
                <StatusBadge status={v.status === "RENDERED" ? "DESIGN" : v.status === "VISUAL_PENDING" ? "PENDING" : v.status} label={label(v.status) ?? v.status} />
                {v.reason && <span className="text-ink-3">{v.reason}</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function RetryButton({ pub, onDone }: { pub: PublicationView; onDone: () => void }) {
  // The retry re-confirms the SAME bound version/hash/preview; when the run's approval expired, the backend
  // records a new PUBLICATION decision for exactly these values (never for another version).
  const retry = useMutation({
    mutationFn: () =>
      post(`/api/publications/${pub.id}/retry`, {
        plan_version_id: pub.plan_version_id,
        approved_hash: pub.approved_hash,
        preview_hash: pub.preview_hash ?? "",
      }),
    onSuccess: onDone,
  });
  return (
    <>
      <Button
        busy={retry.isPending}
        disabled={pub.status === "PUBLISHED"}
        title={`Retry terbatas untuk v${pub.plan_version_number} (${pub.approved_hash}); UNKNOWN direkonsiliasi dulu`}
        onClick={() => retry.mutate()}
      >
        <RotateCcw className="h-4 w-4" aria-hidden /> Retry terbatas
      </Button>
      <ErrorBox error={retry.error} />
    </>
  );
}

function OperationsTable({ ops }: { ops: OperationView[] }) {
  return (
    <Table
      rows={ops}
      rowKey={(o) => o.id}
      empty="Tidak ada operasi"
      columns={[
        { header: "#", cell: (o) => <span className="text-ink-3">{o.order_index}</span> },
        {
          header: "Operasi",
          cell: (o) => (
            <div className={clsx(needsAttention(o.status) && "font-medium")}>
              <div>{o.title || o.logical_id}</div>
              <div className="text-xs text-ink-3">
                {o.provider} · {o.operation} · <Mono>{o.logical_id}</Mono>
              </div>
            </div>
          ),
        },
        { header: "Status", cell: (o) => <StatusBadge status={o.status} label={label(o.status)} /> },
        { header: "Percobaan", cell: (o) => `${o.attempts}/${o.max_attempts}` },
        {
          header: "Hasil eksternal",
          cell: (o) =>
            safeHref(o.external_url) ? (
              <a href={safeHref(o.external_url)} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-accent">
                <ExternalLink className="h-3.5 w-3.5" aria-hidden /> {shortId(o.external_id, 14)}
              </a>
            ) : (
              o.external_id ?? "—"
            ),
        },
        {
          header: "Catatan",
          cell: (o) => (
            <div className="max-w-md text-xs">
              {o.error && <div className={needsAttention(o.status) ? "text-bad" : "text-ink-3"}>{o.error}</div>}
              {(o.payload_summary?.lossy_update ?? []).length > 0 && (
                <div className="text-warn">
                  Update inkremental tanpa: {o.payload_summary!.lossy_update!.join(", ")} (snapshot DOCX baru perlu keputusan terpisah)
                </div>
              )}
              {o.next_attempt_at && <div className="text-ink-3">Berikutnya: {absoluteTime(o.next_attempt_at)}</div>}
              {o.reconciled_at && <div className="text-ink-3">Direkonsiliasi {relativeTime(o.reconciled_at)}</div>}
            </div>
          ),
        },
      ]}
    />
  );
}

function PublicationDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const q = useQuery({
    queryKey: ["publication", id],
    queryFn: () => get<PublicationView & { checked_at: string }>(`/api/publications/${id}`),
    refetchInterval: (query) => (query.state.data?.status === "RUNNING" ? 5000 : false),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["publication", id] });
    qc.invalidateQueries({ queryKey: ["publications"] });
    qc.invalidateQueries({ queryKey: ["project-documents"] });
  };
  const reconcile = useMutation({ mutationFn: () => post(`/api/publications/${id}/reconcile`), onSuccess: refresh });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const pub = q.data!;
  const ops = pub.operations ?? [];
  const attention = ops.filter((o) => needsAttention(o.status));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge status={pub.status} label={label(pub.status)} />
        <Freshness at={pub.checked_at} label="Diperiksa" staleAfterSeconds={120} />
        {Object.entries(pub.counts).map(([k, v]) => (
          <StatusBadge key={k} status={k} label={`${label(k) ?? k}: ${v}`} />
        ))}
      </div>
      {pub.status_reason && <Notice tone="warn">{pub.status_reason}</Notice>}
      {(pub.status === "PARTIAL" || pub.status === "CONFLICT" || pub.status === "FAILED" || attention.length > 0) && (
        <Notice tone="warn">
          {attention.length} operasi butuh perhatian (PARTIAL/CONFLICT/UNKNOWN/FAILED). Hasil yang sukses tetap tercatat dan
          tidak dibuat ulang. UNKNOWN direkonsiliasi (dicari lewat marker) sebelum retry; CONFLICT berarti dokumen diedit
          manusia dan tidak ditimpa.
        </Notice>
      )}
      <Dimensions pub={pub} />
      <TargetsAndDelta pub={pub} />
      {pub.status === "PREVIEW" && <ExecuteBox pub={pub} onDone={refresh} />}
      {isAdmin && pub.status !== "PREVIEW" && pub.status !== "CANCELLED" && (
        <div className="flex flex-wrap items-start gap-2">
          <Button busy={reconcile.isPending} onClick={() => reconcile.mutate()}>
            <RefreshCw className="h-4 w-4" aria-hidden /> Rekonsiliasi
          </Button>
          <RetryButton pub={pub} onDone={refresh} />
        </div>
      )}
      <ErrorBox error={reconcile.error} />
      {(pub.preview.notes ?? []).length > 0 && (
        <ul className="list-disc pl-5 text-xs text-ink-3">
          {(pub.preview.notes ?? []).map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      <OperationsTable ops={ops} />
    </div>
  );
}

export function PublicationPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const { isAdmin, domain } = useAuth();
  const [selected, setSelected] = useState<string | null>(null);
  const [targets, setTargets] = useState<{ docs: boolean; issues: boolean }>({ docs: true, issues: true });
  const [granularity, setGranularity] = useState<"task" | "feature">("task");
  const candidates = useQuery({
    queryKey: ["publication-candidates", projectId],
    queryFn: () => get<ListResponse<Candidate>>(`/api/projects/${projectId}/publication-candidates`),
  });
  const pubs = useQuery({
    queryKey: ["publications", projectId],
    queryFn: () => get<ListResponse<PublicationView>>(`/api/publications?project_id=${encodeURIComponent(projectId)}`),
  });
  const preview = useMutation({
    mutationFn: (planId: string) =>
      post<PublicationView>(`/api/plans/${planId}/publications/preview`, {
        targets: (["docs", "issues"] as const).filter((t) => targets[t]),
        issue_granularity: granularity,
      }),
    onSuccess: (p) => {
      setSelected(p.id);
      qc.invalidateQueries({ queryKey: ["publications", projectId] });
      qc.invalidateQueries({ queryKey: ["project-documents", projectId] });
    },
  });
  const docsLabel = domain === "OFFICE" ? "Dokumen (Confluence)" : "Dokumen (Google Drive/Docs)";
  const issuesLabel = domain === "OFFICE" ? "Work item (Jira)" : "Issue (GitHub Issues)";
  return (
    <div className="space-y-4">
      <Card title="Rencana yang dapat dipublikasikan" actions={<Freshness at={candidates.data?.checked_at} />}>
        {candidates.isLoading ? (
          <Loading />
        ) : candidates.error ? (
          <ErrorBox error={candidates.error} />
        ) : (candidates.data?.items ?? []).length === 0 ? (
          <EmptyState title="Belum ada rencana untuk proyek ini">Buat dan setujui rencana di halaman Rencana terlebih dahulu.</EmptyState>
        ) : (
          <div className="space-y-3">
            {isAdmin && (
              <div className="flex flex-wrap items-end gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={targets.docs} onChange={(e) => setTargets({ ...targets, docs: e.target.checked })} />
                  {docsLabel}
                </label>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={targets.issues} onChange={(e) => setTargets({ ...targets, issues: e.target.checked })} />
                  {issuesLabel}
                </label>
                <Field label="Granularitas issue">
                  <select className="rounded-md border border-line bg-surface px-2 py-1 text-sm" value={granularity}
                    onChange={(e) => setGranularity(e.target.value as "task" | "feature")}>
                    <option value="task">Satu issue per TaskSpec</option>
                    <option value="feature">Satu issue per fitur</option>
                  </select>
                </Field>
              </div>
            )}
            <Table
              rows={candidates.data!.items}
              rowKey={(c) => c.plan_id}
              columns={[
                { header: "Rencana", cell: (c) => c.title },
                { header: "State rencana", cell: (c) => <StatusBadge status={c.state} label={label(c.state)} /> },
                {
                  header: "Versi disetujui",
                  cell: (c) =>
                    c.approved ? (
                      <span>
                        v{c.approved.version} <Mono>{shortHash(c.approved.approved_hash)}</Mono>
                        {!c.approved.is_latest && <span className="ml-1 text-xs text-warn">(ada versi lebih baru yang belum disetujui — diabaikan)</span>}
                      </span>
                    ) : (
                      <StatusBadge status={c.approval_status === "UNKNOWN" ? "UNKNOWN" : "PENDING"} label={c.approval_reason ?? "Belum ada versi disetujui"} />
                    ),
                },
                {
                  header: "",
                  cell: (c) =>
                    isAdmin && c.approved ? (
                      <Button busy={preview.isPending && preview.variables === c.plan_id} disabled={!targets.docs && !targets.issues}
                        onClick={() => preview.mutate(c.plan_id)}>
                        <Eye className="h-4 w-4" aria-hidden /> Preview publikasi
                      </Button>
                    ) : null,
                },
              ]}
            />
            <ErrorBox error={preview.error} />
          </div>
        )}
      </Card>
      <Card title="Publication jobs" actions={<Freshness at={pubs.data?.checked_at} />}>
        {pubs.isLoading ? (
          <Loading />
        ) : pubs.error ? (
          <ErrorBox error={pubs.error} />
        ) : (
          <Table
            rows={pubs.data?.items ?? []}
            rowKey={(p) => p.id}
            empty="Belum ada publication. Buat preview dari versi yang disetujui."
            onRowClick={(p) => setSelected(p.id)}
            columns={[
              { header: "Dibuat", cell: (p) => <span title={absoluteTime(p.created_at)}>{relativeTime(p.created_at)}</span> },
              { header: "Versi", cell: (p) => `v${p.plan_version_number ?? "?"}` },
              { header: "Status", cell: (p) => <StatusBadge status={p.status} label={label(p.status)} /> },
              { header: "Operasi", cell: (p) => Object.entries(p.counts).map(([k, v]) => `${k}:${v}`).join(" · ") || "—" },
              { header: "Target", cell: (p) => p.targets.map((t) => t.label).join(", ") },
            ]}
          />
        )}
      </Card>
      {selected && (
        <Card title={`Publication ${shortId(selected)}`}>
          <PublicationDetail id={selected} />
        </Card>
      )}
    </div>
  );
}
