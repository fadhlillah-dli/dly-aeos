import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, FileText, Folder } from "lucide-react";
import { useMemo, useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";
import { CONTENT_TYPE_LABELS, buildTree, label, safeHref, type TreeNode } from "./labels";
import { MarkdownPreview } from "./MarkdownPreview";
import type { DiffResponse, DocItem, DocumentsResponse, ListResponse, RenderResponse, RevisionItem } from "./types";

function Row({ node, depth, selected, onSelect }: { node: TreeNode; depth: number; selected: string | null; onSelect: (d: DocItem) => void }) {
  const [open, setOpen] = useState(depth < 1 || node.doc.logical_doc_id === "root");
  const d = node.doc;
  const isFolder = d.kind === "FOLDER";
  const ext = d.external.find((e) => safeHref(e.url));
  const extHref = safeHref(ext?.url);
  return (
    <li>
      <div
        className={clsx(
          "flex flex-wrap items-center gap-2 rounded px-2 py-1 text-sm hover:bg-surface-2",
          selected === d.id && "bg-surface-3",
        )}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {isFolder ? (
          <button aria-label={open ? "Tutup folder" : "Buka folder"} onClick={() => setOpen(!open)} className="text-ink-3">
            {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
          </button>
        ) : (
          <span className="w-4" />
        )}
        {isFolder ? <Folder className="h-4 w-4 text-ink-3" aria-hidden /> : <FileText className="h-4 w-4 text-ink-3" aria-hidden />}
        <button className="min-w-0 truncate text-left font-medium hover:underline" onClick={() => onSelect(d)}>
          {d.title}
        </button>
        <StatusBadge status={d.status} label={label(d.status)} />
        {!isFolder && <span className="text-xs text-ink-3">{CONTENT_TYPE_LABELS[d.content_type] ?? d.content_type}</span>}
        {d.documentation_pending && (
          <StatusBadge status="STALE" label={`documentation pending: ${d.pending_sections.length}`} />
        )}
        {d.open_revision_inputs > 0 && <StatusBadge status="CONFLICT" label={`edit manusia: ${d.open_revision_inputs}`} />}
        {d.freshness.external_stale && <StatusBadge status="STALE" label="sinkron eksternal usang" />}
        {ext && extHref && (
          <a href={extHref} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-accent">
            <ExternalLink className="h-3.5 w-3.5" aria-hidden /> {ext.provider}
          </a>
        )}
      </div>
      {isFolder && open && node.children.length > 0 && (
        <ul>
          {node.children.map((c) => (
            <Row key={c.doc.id} node={c} depth={depth + 1} selected={selected} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function DocumentTreeView({ items, selected, onSelect }: { items: DocItem[]; selected: string | null; onSelect: (d: DocItem) => void }) {
  const tree = useMemo(() => buildTree(items), [items]);
  return (
    <ul aria-label="Struktur Product 1" className="space-y-0.5">
      {tree.map((n) => (
        <Row key={n.doc.id} node={n} depth={0} selected={selected} onSelect={onSelect} />
      ))}
    </ul>
  );
}

function DiffView({ docId }: { docId: string }) {
  const q = useQuery({ queryKey: ["document-diff", docId], queryFn: () => get<DiffResponse>(`/api/documents/${docId}/diff`) });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data?.changed) return <p className="text-xs text-ink-3">Belum ada snapshot sebelumnya untuk dibandingkan.</p>;
  return (
    <pre className="mono max-h-80 overflow-auto rounded bg-surface-3 p-2 text-xs">
      {q.data.diff.split("\n").map((l, i) => (
        <div key={i} className={l.startsWith("+") ? "text-ok" : l.startsWith("-") ? "text-bad" : ""}>
          {l || " "}
        </div>
      ))}
    </pre>
  );
}

function HumanEdits({ doc }: { doc: DocItem }) {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const q = useQuery({
    queryKey: ["document-revisions", doc.id],
    queryFn: () => get<ListResponse<RevisionItem>>(`/api/documents/${doc.id}/revisions`),
  });
  const ack = useMutation({
    mutationFn: (revId: string) => post(`/api/documents/${doc.id}/revision-inputs/${revId}/acknowledge`, { note: "" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["document-revisions", doc.id] });
      qc.invalidateQueries({ queryKey: ["project-documents"] });
    },
  });
  const inputs = (q.data?.items ?? []).filter((r) => r.kind === "HUMAN_EDIT_INPUT");
  if (!inputs.length) return null;
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">Edit manusia (input revisi)</div>
      {inputs.map((r) => (
        <div key={r.id} className="rounded border border-line p-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={r.status === "OPEN" ? "CONFLICT" : "ACCEPTED"} label={r.status === "OPEN" ? "Belum ditinjau" : "Sudah ditinjau"} />
            <span>revisi teramati <Mono>{r.observed_revision ?? "?"}</Mono> (AEOS terakhir <Mono>{r.expected_revision ?? "?"}</Mono>)</span>
            {isAdmin && r.status === "OPEN" && (
              <Button className="text-xs" busy={ack.isPending} onClick={() => ack.mutate(r.id)}>
                Tandai sudah ditinjau
              </Button>
            )}
          </div>
          {r.excerpt && <pre className="mono mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-ink-2">{r.excerpt}</pre>}
        </div>
      ))}
      <p className="text-xs text-ink-3">
        Menandai sudah ditinjau tidak menimpa dokumen; hanya PlanVersion baru yang disetujui (lebih baru dari edit ini) yang
        boleh memperbaruinya.
      </p>
      <ErrorBox error={ack.error} />
    </div>
  );
}

const REVIEW_TONE: Record<string, string> = { REVIEWED: "CONFIGURED", REVIEW_STALE: "STALE", NOT_REVIEWED: "NOT_CHECKED" };

/** §27 "last reviewed": a human confirms having read THIS content hash. Review metadata only — not an approval. */
export function ReviewBox({ doc }: { doc: DocItem }) {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const review = doc.review ?? { reviewed_at: doc.last_reviewed_at, reviewed_by: null, content_hash: null, status: doc.last_reviewed_at ? "REVIEWED" : "NOT_REVIEWED" };
  const m = useMutation({
    mutationFn: () => post(`/api/documents/${doc.id}/review`, { content_hash: doc.content_hash ?? "", note: "" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-documents"] }),
  });
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={REVIEW_TONE[review.status] ?? review.status} label={label(review.status)} />
        {review.reviewed_at && <span className="text-xs text-ink-3">{relativeTime(review.reviewed_at)}</span>}
        {isAdmin && doc.kind === "DOC" && doc.content_hash && review.status !== "REVIEWED" && (
          <Button className="text-xs" busy={m.isPending} onClick={() => m.mutate()}>
            Tandai sudah direview (hash ini)
          </Button>
        )}
      </div>
      {review.status === "REVIEW_STALE" && (
        <p className="text-xs text-warn">Review tercatat untuk konten lama ({shortHash(review.content_hash)}); konten sekarang belum direview.</p>
      )}
      <p className="text-xs text-ink-3">Review hanya metadata baca-ulang; bukan persetujuan rencana atau publikasi.</p>
      <ErrorBox error={m.error} />
    </div>
  );
}

function DocDetail({ doc }: { doc: DocItem }) {
  const [showDiff, setShowDiff] = useState(false);
  const q = useQuery({
    queryKey: ["document-render", doc.id, doc.content_hash],
    queryFn: () => get<RenderResponse>(`/api/documents/${doc.id}/render`),
    enabled: doc.kind === "DOC",
  });
  const issues = (doc.links.issues as { issue_id: string; url: string | null }[] | undefined) ?? [];
  return (
    <div className="space-y-3">
      <KeyValue
        items={[
          ["Logical doc ID", <Mono key="l">{doc.logical_doc_id}</Mono>],
          ["Status", <span key="s"><StatusBadge status={doc.status} label={label(doc.status)} /> {doc.status_reason && <span className="text-xs text-ink-3">{doc.status_reason}</span>}</span>],
          ["Owner", doc.owner ?? "UNASSIGNED"],
          ["Tipe konten", CONTENT_TYPE_LABELS[doc.content_type] ?? doc.content_type],
          ["Waktu (§28)", doc.timing],
          ["Sumber plan/versi", doc.source_plan_version_id ? <Mono key="v">{shortHash(doc.source_plan_version_id, 8)}</Mono> : "—"],
          ["Hash konten", <Mono key="h">{shortHash(doc.content_hash)}</Mono>],
          ["Revisi eksternal", doc.external_revision ?? "—"],
          ["Dirender", relativeTime(doc.rendered_at)],
          ["Sinkron terakhir", <Freshness key="f" at={doc.last_synced_at} label="Sinkron" staleAfterSeconds={86400 * 7} />],
          ["Direview manusia", doc.kind === "DOC" ? <ReviewBox key="r" doc={doc} /> : "—"],
        ]}
      />
      {doc.pending_sections.length > 0 && (
        <Notice tone="warn">
          Documentation pending: {doc.pending_sections.join(", ")}. Isi bagian ini lewat PlanVersion baru yang disetujui —
          AEOS tidak mengarang isinya.
        </Notice>
      )}
      {doc.open_revision_inputs > 0 && (
        <Notice tone="warn">
          <AlertTriangle className="mr-1 inline h-4 w-4" aria-hidden />
          Dokumen ini diedit manusia di provider. Edit dibaca sebagai input revisi dan tidak ditimpa.
        </Notice>
      )}
      {(issues.length > 0 || (doc.evidence ?? []).length > 0) && (
        <div className="space-y-1 text-xs">
          {issues.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-ink-3">Issue:</span>
              {issues.map((i) =>
                safeHref(i.url) ? (
                  <a key={i.issue_id} href={safeHref(i.url)} target="_blank" rel="noreferrer noopener" className="text-accent">#{i.issue_id}</a>
                ) : (
                  <span key={i.issue_id}>#{i.issue_id}</span>
                ),
              )}
            </div>
          )}
          {(doc.evidence ?? []).length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-ink-3">Evidence (live):</span>
              {/* An evidence artefact existing (even from a trusted collector) is not a PASS: the verdict lives in Evidence. */}
              {(doc.evidence ?? []).map((e) => (
                <StatusBadge key={e.id} status={e.trusted ? "CONNECTED" : "UNVERIFIED"}
                  label={`${e.kind} ${e.evidence_level} — ${e.trusted ? "kolektor tepercaya; hasil lihat Evidence" : "tidak tepercaya"}`} />
              ))}
            </div>
          )}
        </div>
      )}
      <HumanEdits doc={doc} />
      {doc.kind === "DOC" && (
        <>
          <div className="flex gap-2">
            <Button variant={showDiff ? "secondary" : "primary"} className="text-xs" onClick={() => setShowDiff(false)}>Preview</Button>
            <Button variant={showDiff ? "primary" : "secondary"} className="text-xs" onClick={() => setShowDiff(true)}>Diff sebelum → sesudah</Button>
          </div>
          {showDiff ? (
            <DiffView docId={doc.id} />
          ) : (
            <div className="max-h-[32rem] overflow-y-auto rounded border border-line p-3">
              {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : <MarkdownPreview markdown={q.data?.markdown ?? ""} />}
            </div>
          )}
        </>
      )}
      {doc.sections.length > 0 && (
        <details>
          <summary className="cursor-pointer text-sm text-ink-2">Section ({doc.sections.length})</summary>
          <ul className="mt-2 space-y-1 text-xs">
            {doc.sections.map((s) => (
              <li key={s.key} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.title}</span>
                <span className="text-ink-3">{CONTENT_TYPE_LABELS[s.content_type] ?? s.content_type}</span>
                <StatusBadge status={s.status === "PENDING" ? "STALE" : s.status === "FILLED" ? "CONFIGURED" : s.status === "HUMAN_REQUIRED" ? "PENDING" : s.status === "LIVE_REFERENCE" ? "CONNECTED" : s.status}
                  label={{ PENDING: "documentation pending", FILLED: "terisi dari rencana", HUMAN_REQUIRED: "menunggu manusia", LIVE_REFERENCE: "rujukan sumber live", NOT_STARTED: "belum dimulai", NOT_APPLICABLE: "tidak berlaku" }[s.status] ?? s.status} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function DocumentsPanel({ projectId }: { projectId: string }) {
  // Keep only the id: the detail always shows the freshest record (SSE/invalidation refetches the list).
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["project-documents", projectId],
    queryFn: () => get<DocumentsResponse>(`/api/projects/${projectId}/documents`),
  });
  const structure = useQuery({
    queryKey: ["product1-structure"],
    queryFn: () => get<{ title_template: string; nodes: { key: string; title: string; kind: string; collection: string | null }[] }>("/api/docs/product1-structure"),
    enabled: !!q.data && q.data.items.length === 0,
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const data = q.data!;
  const selected = selectedId ? data.items.find((i) => i.id === selectedId) ?? null : null;
  if (data.items.length === 0) {
    return (
      <div className="space-y-4">
        <EmptyState title="Belum ada dokumen Product 1 untuk proyek ini">
          {data.remedy ?? "Buat preview publikasi dari PlanVersion yang disetujui."}
          <div className="mt-2">
            Preview lokal: <Mono>aeos docs render &lt;plan_version_id&gt;</Mono>
          </div>
        </EmptyState>
        {structure.data && (
          <Card title={<span className="flex items-center gap-2">Struktur yang akan dibuat <StatusBadge status="DESIGN" /></span>}>
            <p className="mb-2 text-sm text-ink-2">{structure.data.title_template}</p>
            <ul className="grid gap-1 text-sm sm:grid-cols-2">
              {structure.data.nodes.map((n) => (
                <li key={n.key} className="flex items-center gap-2">
                  {n.kind === "FOLDER" ? <Folder className="h-4 w-4 text-ink-3" aria-hidden /> : <FileText className="h-4 w-4 text-ink-3" aria-hidden />}
                  {n.title}
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    );
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <Card
        title={data.product_title}
        actions={<Freshness at={data.checked_at} label="Dirender" staleAfterSeconds={86400 * 30} />}
      >
        <div className="mb-3 flex flex-wrap gap-2 text-xs">
          <StatusBadge status="CONFIGURED" label={`${data.summary.documents} dokumen`} />
          <StatusBadge status="IDLE" label={`${data.summary.folders} folder`} />
          <StatusBadge status={data.summary.published > 0 ? "PUBLISHED" : "PENDING"} label={`${data.summary.published} terpublikasi`} />
          {data.summary.pending > 0 && <StatusBadge status="STALE" label={`${data.summary.pending} documentation pending`} />}
          {data.summary.conflicts > 0 && <StatusBadge status="CONFLICT" label={`${data.summary.conflicts} edit manusia terbuka`} />}
        </div>
        <DocumentTreeView items={data.items} selected={selected?.id ?? null} onSelect={(d) => setSelectedId(d.id)} />
      </Card>
      <Card title={selected ? selected.title : "Detail dokumen"}>
        {selected ? <DocDetail doc={selected} /> : <EmptyState title="Pilih dokumen atau folder">Klik judul di pohon untuk melihat metadata, section dan preview Markdown.</EmptyState>}
      </Card>
    </div>
  );
}
