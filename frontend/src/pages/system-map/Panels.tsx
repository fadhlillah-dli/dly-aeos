import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, Lightbulb, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Badge, DIMENSION_LABELS, RELATION_LABELS } from "../../components/inventory/labels";
import type { ComponentDetail, MapNode, RelationView, SnapshotDiff, SnapshotMeta } from "../../components/inventory/types";
import { proposeComponentHref } from "../../components/proposals/PageLinks";
import { Button, Card, EmptyState, ErrorBox, inputClass, KeyValue, LinkButton, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";

export function NodeCard({ node, lensLabel, onClose }: { node: MapNode; lensLabel: string; onClose: () => void }) {
  const unregistered = node.kind === "unregistered";
  const { domain, isAdmin } = useAuth();
  const detail = useQuery({
    queryKey: ["inventory", "component", "card", domain, node.id],
    queryFn: () => get<ComponentDetail>(`/api/inventory/components/${encodeURIComponent(node.id)}`),
    enabled: !unregistered,
    retry: false,
  });
  const d = detail.data;
  return (
    <Card
      title={node.label}
      actions={
        <Button variant="ghost" onClick={onClose} aria-label="Tutup kartu">
          <X className="h-4 w-4" aria-hidden />
        </Button>
      }
    >
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge status={node.status} />
          <span className="text-xs text-ink-3">
            {lensLabel}
            {node.at ? ` · ${relativeTime(node.at)}` : ""}
          </span>
        </div>
        <p>{node.function}</p>
        {node.basis.length > 0 && <p className="text-xs text-ink-3">Dasar: {node.basis.join("; ")}</p>}
        {node.drift.length > 0 && (
          <Notice tone="warn">
            Drift: {node.drift.map((x) => x.label).join(", ")}
          </Notice>
        )}
        {unregistered ? (
          <Notice tone="warn">
            Ditemukan di managed scope tanpa registrasi. Tidak diadopsi, tidak diuninstall dan tidak diberi akses.{" "}
            {isAdmin ? <Link className="underline" to={proposeComponentHref(node.id)}>Minta review</Link> : "Review dapat diminta oleh SUPER_ADMIN."}
          </Notice>
        ) : detail.isLoading ? (
          <Loading />
        ) : detail.error ? (
          <ErrorBox error={detail.error} />
        ) : d ? (
          <>
            <KeyValue
              items={[
                ["Alasan dipilih", d.usage_role.why_selected],
                ["Dipakai oleh", d.usage_role.used_by.length ? d.usage_role.used_by.map((u) => u.name).join(", ") : "belum tercatat"],
                ["Domain", d.scope.domains.join(", ")],
                ["Proyek / tingkat", d.scope.level === "project" ? `proyek (${d.scope.owner || "pemilik belum ditetapkan"})` : `${d.scope.level} · ${d.scope.environment}`],
                ["Sumber", d.source.source_url ?? "UNKNOWN (tidak ditebak)"],
                ["Pin / teramati", `${d.versions.approved_pin ?? "UNKNOWN"} / ${d.versions.observed_version ?? "—"}`],
                ["Biaya", String(d.cost.design.model ?? "UNKNOWN")],
                ["Data / akses", Object.keys(d.access ?? {}).length
                  ? Object.entries(d.access).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : String(v)}`).join(" · ")
                  : "tidak tercatat"],
                ["Bergantung pada", d.relations.outgoing.length
                  ? d.relations.outgoing.map((r) => `${r.relation} → ${r.target_name || r.target}`).join(", ")
                  : "tidak ada relasi keluar tercatat"],
              ]}
            />
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Status per dimensi</div>
              <div className="grid grid-cols-1 gap-1">
                {Object.entries(d.lifecycle).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between gap-2 text-xs">
                    <span className="text-ink-3">{DIMENSION_LABELS[k] ?? k}</span>
                    <Badge status={v.effective} />
                  </div>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Bukti</div>
              <ul className="space-y-0.5 text-xs">
                {d.evidence.claims.slice(0, 6).map((c, i) => (
                  <li key={i}>
                    <Badge status={c.status} /> {c.claim} <span className="text-ink-3">{c.level ?? "tanpa bukti"}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <LinkButton to={`/tools/${encodeURIComponent(node.id)}`}>Buka detail komponen</LinkButton>
              {isAdmin && (
                <LinkButton to={proposeComponentHref(node.id)} variant="primary" title="Usulan tidak memasang atau mengaktifkan apa pun">
                  <Lightbulb className="h-4 w-4" aria-hidden /> Usulkan perubahan
                </LinkButton>
              )}
            </div>
            {d.changes.proposals.length > 0 && (
              <p className="text-xs text-ink-3">
                {d.changes.proposals.length} usulan terkait:{" "}
                {d.changes.proposals.slice(0, 3).map((p, i) => (
                  <span key={p.id}>
                    {i > 0 && ", "}
                    <Link className="text-accent hover:underline" to={`/proposals/${p.id}`}>{p.title}</Link>
                  </span>
                ))}
              </p>
            )}
          </>
        ) : null}
      </div>
    </Card>
  );
}

export function EdgeCard({ rels, onClose }: { rels: RelationView[]; onClose: () => void }) {
  return (
    <Card
      title={rels.length > 1 ? `${rels.length} relasi teragregasi` : "Relasi"}
      actions={
        <Button variant="ghost" onClick={onClose} aria-label="Tutup relasi">
          <X className="h-4 w-4" aria-hidden />
        </Button>
      }
    >
      <div className="space-y-4 text-sm">
        {rels.map((r) => (
          <div key={r.id} className="space-y-1">
            <div className="font-medium">
              {r.source_name} <span className="text-ink-3">{RELATION_LABELS[r.relation] ?? r.relation}</span> {r.target_name}
            </div>
            <KeyValue
              items={[
                ["Data yang lewat", r.data_description || "UNKNOWN"],
                ["Arah", <Mono key="d">{r.direction}</Mono>],
                ["Operasi", r.operation],
                ["Broker / boundary", r.boundary ?? "—"],
                ["Referensi identitas", r.identity_ref ?? "—"],
                ["Lensa / status", <span key="s" className="inline-flex gap-1"><Badge status={r.status === "CONFIGURED" ? "CONFIGURED" : r.status} /> {r.lens}</span>],
                ["Provenance", r.provenance || "—"],
                ["Kesegaran", r.last_observed_at ? `${relativeTime(r.last_observed_at)}${r.freshness.stale ? " (usang)" : ""}` : "tidak ada observasi traffic"],
                ["Hasil uji / bukti", r.evidence],
              ]}
            />
          </div>
        ))}
        <p className="text-xs text-ink-3">Nilai credential dan payload sensitif tidak pernah ditampilkan.</p>
      </div>
    </Card>
  );
}

function DiffView({ diff }: { diff: SnapshotDiff }) {
  if (diff.same_hash) return <Notice tone="info">Kedua snapshot identik (hash sama: {shortHash(diff.a.snapshot_hash)}).</Notice>;
  return (
    <div className="space-y-3 text-sm">
      <div className="grid gap-3 md:grid-cols-3">
        <div>
          <div className="text-xs font-semibold uppercase text-ok">Node ditambah ({diff.nodes.added.length})</div>
          <ul className="text-xs">{diff.nodes.added.map((n) => <li key={n.id}>{n.id}</li>)}</ul>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase text-bad">Node dihapus ({diff.nodes.removed.length})</div>
          <ul className="text-xs">{diff.nodes.removed.map((n) => <li key={n.id}>{n.id}</li>)}</ul>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase text-warn">Node berubah ({diff.nodes.changed.length})</div>
          <ul className="text-xs">
            {diff.nodes.changed.map((c) => (
              <li key={c.key}>
                {c.key}:{" "}
                {Object.entries(c.fields)
                  .map(([f, [a, b]]) => `${f} ${String(a)} → ${String(b)}`)
                  .join("; ")}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="text-xs font-semibold uppercase text-ok">Relasi ditambah ({diff.edges.added.length})</div>
          <ul className="text-xs">{diff.edges.added.map((e, i) => <li key={i}>{e.source} {e.relation} {e.target} ({e.lens})</li>)}</ul>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase text-bad">Relasi dihapus ({diff.edges.removed.length})</div>
          <ul className="text-xs">{diff.edges.removed.map((e, i) => <li key={i}>{e.source} {e.relation} {e.target} ({e.lens})</li>)}</ul>
        </div>
      </div>
      {Object.keys(diff.refs_changed).length > 0 && <p className="text-xs text-ink-3">Referensi berubah: {Object.keys(diff.refs_changed).join(", ")}</p>}
      <p className="text-xs text-ink-3">{diff.note}</p>
    </div>
  );
}

export function SnapshotPanel() {
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const list = useQuery({ queryKey: ["inventory", "snapshots", domain], queryFn: () => get<{ items: SnapshotMeta[] }>("/api/inventory/snapshots") });
  const create = useMutation({
    mutationFn: () => post<SnapshotMeta>("/api/inventory/snapshots", { label }),
    onSuccess: () => {
      setLabel("");
      qc.invalidateQueries({ queryKey: ["inventory", "snapshots"] });
    },
  });
  const diff = useQuery({
    queryKey: ["inventory", "snapshot-diff", domain, a, b],
    queryFn: () => get<SnapshotDiff>(`/api/inventory/snapshots/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
    enabled: !!a && !!b && a !== b,
  });
  const items = list.data?.items ?? [];
  return (
    <Card title="Snapshot arsitektur (immutable)" className="mt-5">
      <p className="mb-3 text-xs text-ink-3">Snapshot menyimpan referensi/hash manifest, master, ADR, kontrak, keputusan serta node/relasi Rancangan & Konfigurasi pada domain ini. Snapshot bukan live inventory.</p>
      {isAdmin && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input className={`${inputClass} max-w-xs`} placeholder="Label snapshot" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Label snapshot" />
          <Button busy={create.isPending} onClick={() => create.mutate()}>
            <Camera className="h-4 w-4" aria-hidden /> Buat snapshot
          </Button>
          {create.error && <ErrorBox error={create.error} />}
        </div>
      )}
      {list.isLoading ? (
        <Loading />
      ) : items.length === 0 ? (
        <EmptyState title="Belum ada snapshot">{isAdmin ? "Buat snapshot pertama dengan tombol di atas." : "SUPER_ADMIN dapat membuat snapshot."}</EmptyState>
      ) : (
        <>
          <ul className="mb-3 space-y-1 text-sm">
            {items.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.label}</span>
                <span className="text-xs text-ink-3" title={absoluteTime(s.created_at)}>{relativeTime(s.created_at)}</span>
                <Mono>{shortHash(s.snapshot_hash)}</Mono>
                <span className="text-xs text-ink-3">
                  {s.counts.nodes} node · {s.counts.edges} relasi
                </span>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>Bandingkan</span>
            {(
              [
                { v: a, set: setA, l: "A" },
                { v: b, set: setB, l: "B" },
              ] as { v: string; set: (x: string) => void; l: string }[]
            ).map(({ v, set, l }) => (
              <select key={l} aria-label={`Snapshot ${l}`} className="rounded-md border border-line bg-surface px-2 py-1" value={v} onChange={(e) => set(e.target.value)}>
                <option value="">— snapshot {l} —</option>
                {items.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label} ({shortHash(s.snapshot_hash, 6)})
                  </option>
                ))}
              </select>
            ))}
          </div>
          <div className="mt-3">
            {diff.isLoading && <Loading />}
            {diff.error && <ErrorBox error={diff.error} />}
            {diff.data && <DiffView diff={diff.data} />}
          </div>
        </>
      )}
    </Card>
  );
}
