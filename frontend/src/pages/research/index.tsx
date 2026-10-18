import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Lightbulb, RefreshCw } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { IDENTITY_STATUS, MappedBadge, RESEARCH_LIFECYCLE, UPSTREAM_STATUS } from "../../components/proposals/MappedBadge";
import { RelatedLinks } from "../../components/proposals/PageLinks";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Table, Tabs } from "../../components/ui";

interface Entry {
  id: string;
  entry_key: string | null;
  name: string;
  identity: string | null;
  category: string;
  urls: string[];
  source_refs: string[];
  evidence_level: string;
  lifecycle: string;
  placement: string;
  identity_status: string;
  upstream_status: string;
  checked_at: string | null;
  freshness: "NOT_CHECKED" | "STALE" | "FRESH";
  research_date: string | null;
  release_date: string | null;
  component_id: string | null;
  source_kind: string;
  mode: string;
  section_refs: string[];
  is_reference: boolean;
  latest_tag: string | null;
  update_available: boolean | null;
  checkable: boolean;
}

interface EntryDetail extends Entry {
  relevance: string;
  claims: string[];
  limitations: string;
  license: string | null;
  source_version: string | null;
  upstream: Record<string, unknown>;
  history: { at: string; event: string; from?: string; to?: string; status?: string; reason?: string }[];
  check_status?: string;
}

interface ListResponse {
  items: Entry[];
  source: string;
  checked_at: string | null;
  mode: string;
  lifecycles: string[];
  evidence_limit: string;
}

const STALE_AFTER = 30 * 86400;

function FreshnessCell({ e }: { e: Entry }) {
  if (!e.checked_at) return <StatusBadge status="NOT_CHECKED" label={`Belum dicek live (riset ${e.research_date ?? "?"})`} />;
  return <Freshness at={e.checked_at} staleAfterSeconds={STALE_AFTER} label="Dicek" />;
}

function EntryDetailPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["research", "entry", id], queryFn: () => get<EntryDetail>(`/api/research/${id}`) });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const e = q.data;
  if (!e) return null;
  const up = e.upstream as Record<string, unknown>;
  return (
    <Card title={`Detail: ${e.name}`} actions={<Button variant="ghost" onClick={onClose}>Tutup</Button>}>
      <KeyValue
        items={[
          ["Identitas", <Mono key="i">{e.identity ?? "—"}</Mono>],
          ["Penempatan", e.placement],
          ["Relevansi", e.relevance || "—"],
          ["Klaim (bukan bukti DLY AEOS)", e.claims.length ? <ul className="list-disc pl-5">{e.claims.map((c) => <li key={c}>{c}</li>)}</ul> : "—"],
          ["Batasan", e.limitations || "—"],
          ["Referensi", e.source_refs.join(", ") || "—"],
          ["Bagian master", e.section_refs.join(", ") || "—"],
          ["Lisensi", e.license ?? "UNKNOWN"],
          ["Versi dipin", e.source_version ?? "Tidak ada (tag terbaru hanya informasi)"],
          ["Upstream", up.observed_full_name ? `${String(up.observed_full_name)} · tag ${String(up.latest_tag ?? "?")} · arsip ${String(up.archived)}` : "Belum dicek"],
          ["Kompatibilitas", String(up.compatibility ?? "UNKNOWN")],
          ["Komponen inventaris", e.component_id ? <Link to={`/tools/${e.component_id}`}>{e.component_id}</Link> : "Belum tertaut"],
        ]}
      />
      {e.history.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-sm font-medium">Riwayat</div>
          <ul className="list-disc pl-5 text-xs">
            {e.history.map((h, i) => (
              <li key={i}>
                {h.at} — {h.event} {h.from ? `${h.from} → ${h.to}` : ""} {h.status ?? ""} {h.reason ?? ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function AddUrlForm() {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const m = useMutation({
    mutationFn: () => post<{ entry: Entry; duplicate: boolean; note: string }>("/api/research", { url, name: name || null, notes: notes || null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["research"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Tambah lewat URL">
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="URL repo/docs" hint="Tidak ada instalasi, aktivasi atau pengiriman data proyek">
          <input className={inputClass} value={url} onChange={(e) => setUrl(e.target.value)} required placeholder="https://github.com/owner/repo" />
        </Field>
        <Field label="Nama (opsional)">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Catatan (opsional)">
          <input className={inputClass} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </div>
      <ErrorBox error={m.error} />
      {m.data && (
        <Notice tone={m.data.duplicate ? "warn" : "info"}>
          {m.data.duplicate ? `Sudah ada di registry: ${m.data.entry.name} (${m.data.entry.identity})` : `${m.data.entry.name} ditambahkan sebagai DISCOVERED. ${m.data.note}`}
        </Notice>
      )}
      <Button variant="primary" type="submit" busy={m.isPending}>
        Tambah ke registry
      </Button>
    </form>
  );
}

function CheckButton({ e }: { e: Entry }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => post<EntryDetail>(`/api/research/${e.id}/check-upstream`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["research"] }),
  });
  return (
    <div className="space-y-1">
      <Button className="text-xs" onClick={(ev) => { ev.stopPropagation(); m.mutate(); }} busy={m.isPending} disabled={!e.checkable} title={e.checkable ? "Ambil metadata repo + rilis terbaru (GitHub API publik)" : "Hanya entri GitHub dengan identitas tercatat"}>
        <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Check upstream
      </Button>
      {m.data?.check_status && <MappedBadge value={m.data.check_status} map={UPSTREAM_STATUS} />}
      <ErrorBox error={m.error} />
    </div>
  );
}

function RegistryTable({ rows, isAdmin, onOpen }: { rows: Entry[]; isAdmin: boolean; onOpen: (id: string) => void }) {
  return (
    <Table
      rows={rows}
      rowKey={(r) => r.id}
      onRowClick={(r) => onOpen(r.id)}
      empty="Tidak ada entri dengan filter ini"
      columns={[
        {
          header: "Nama / identitas",
          cell: (r) => (
            <div className="min-w-0">
              <div className="font-medium">{r.name}</div>
              <div className="truncate text-xs text-ink-3">{r.identity}</div>
              {r.urls[0] ? (
                <a href={r.urls[0]} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs" onClick={(ev) => ev.stopPropagation()}>
                  sumber <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              ) : (
                <span className="text-xs text-warn">Tanpa URL — identitas belum diverifikasi (tidak ditebak)</span>
              )}
            </div>
          ),
        },
        { header: "Kategori", cell: (r) => r.category },
        { header: "Lifecycle riset", cell: (r) => <MappedBadge value={r.lifecycle} map={RESEARCH_LIFECYCLE} /> },
        { header: "Bukti", cell: (r) => <StatusBadge status={r.evidence_level === "E0" ? "UNVERIFIED" : "NOT_TESTED"} label={`${r.evidence_level}${r.evidence_level === "E0" ? " klaim" : " terdokumentasi"}`} /> },
        { header: "Identitas", cell: (r) => <MappedBadge value={r.identity_status} map={IDENTITY_STATUS} /> },
        { header: "Diperiksa", cell: (r) => <FreshnessCell e={r} /> },
        {
          header: "Upstream",
          cell: (r) => (
            <div className="text-xs">
              <MappedBadge value={r.upstream_status} map={UPSTREAM_STATUS} />
              {r.latest_tag && <div>tag {r.latest_tag}</div>}
              {r.update_available && <div className="text-warn">Ada versi baru — belum tentu kompatibel</div>}
            </div>
          ),
        },
        { header: "Inventaris", cell: (r) => (r.component_id ? <Link to={`/tools/${r.component_id}`} onClick={(ev) => ev.stopPropagation()}>{r.component_id}</Link> : "—") },
        {
          header: "Aksi",
          cell: (r) => (
            <div className="flex flex-col gap-1">
              {isAdmin && !r.is_reference && <CheckButton e={r} />}
              {isAdmin && !r.is_reference && (
                <Link className="inline-flex items-center gap-1 text-xs" to={`/proposals?source=${encodeURIComponent(r.urls[0] ?? r.name)}${r.component_id ? `&component=${r.component_id}` : ""}`} onClick={(ev) => ev.stopPropagation()}>
                  <Lightbulb className="h-3 w-3" aria-hidden /> Usulkan
                </Link>
              )}
            </div>
          ),
        },
      ]}
    />
  );
}

export default function ResearchPage() {
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();
  const [lifecycle, setLifecycle] = useState("");
  const [evidence, setEvidence] = useState("");
  const [q, setQ] = useState("");
  const openId = params.get("entry");
  const qs = new URLSearchParams({ kind: "candidate" });
  if (lifecycle) qs.set("lifecycle", lifecycle);
  if (evidence) qs.set("evidence", evidence);
  if (q) qs.set("q", q);
  const cands = useQuery({ queryKey: ["research", "candidates", lifecycle, evidence, q], queryFn: () => get<ListResponse>(`/api/research?${qs.toString()}`) });
  const refs = useQuery({ queryKey: ["research", "references"], queryFn: () => get<ListResponse>("/api/research?kind=reference") });
  const open = (id: string) => setParams({ entry: id });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Riset & Registry Teknologi"
        question="Kandidat apa yang ditemukan dan sejauh mana terbukti?"
        source={cands.data?.source ?? "research_entries"}
        checkedAt={cands.data?.checked_at ?? null}
        evidenceLimit={cands.data?.evidence_limit ?? "Tahap riset bukan bukti instalasi/koneksi/penggunaan"}
        mode="DESIGN"
        actions={<RelatedLinks subject="DLY AEOS Research & Technology Registry" />}
      />
      <Notice tone="mock">
        Katalog bertanggal dari master (§45, §47, §52; riset 17–18 Oktober 2026) — watchlist desain, bukan daftar komponen terpasang. Check Upstream hanya
        membaca metadata publik; tidak memasang, memperbarui atau mengaktifkan apa pun. Data proyek Office tidak pernah dikirim ke riset publik.
      </Notice>
      {openId && <EntryDetailPanel id={openId} onClose={() => setParams({})} />}
      <Tabs
        tabs={[
          {
            id: "candidates",
            label: "Kandidat, tools & metode",
            content: (
              <Card
                actions={
                  <div className="flex flex-wrap gap-2">
                    <input aria-label="Cari" className={inputClass + " w-48"} placeholder="Cari…" value={q} onChange={(e) => setQ(e.target.value)} />
                    <select aria-label="Filter lifecycle" className={inputClass + " w-52"} value={lifecycle} onChange={(e) => setLifecycle(e.target.value)}>
                      <option value="">Semua lifecycle</option>
                      {(cands.data?.lifecycles ?? Object.keys(RESEARCH_LIFECYCLE)).map((l) => (
                        <option key={l} value={l}>
                          {RESEARCH_LIFECYCLE[l]?.[1] ?? l}
                        </option>
                      ))}
                    </select>
                    <select aria-label="Filter bukti" className={inputClass + " w-36"} value={evidence} onChange={(e) => setEvidence(e.target.value)}>
                      <option value="">Semua bukti</option>
                      <option value="E0">E0 — klaim</option>
                      <option value="E1">E1 — terdokumentasi</option>
                    </select>
                  </div>
                }
                title="Registry"
              >
                {cands.isLoading && <Loading />}
                <ErrorBox error={cands.error} />
                {cands.data && cands.data.items.length === 0 && !lifecycle && !evidence && !q ? (
                  <EmptyState title="Registry riset kosong">
                    Muat katalog desain: <Mono>aeos seed --only aeos.research.seed</Mono>
                  </EmptyState>
                ) : (
                  cands.data && <RegistryTable rows={cands.data.items} isAdmin={isAdmin} onOpen={open} />
                )}
              </Card>
            ),
          },
          {
            id: "references",
            label: "Referensi S01–S64",
            content: (
              <Card title="Referensi dan batas bukti (§52)">
                <ErrorBox error={refs.error} />
                {refs.data && refs.data.items.length === 0 ? (
                  <EmptyState title="Referensi belum dimuat">
                    Jalankan <Mono>aeos seed --only aeos.research.seed</Mono>
                  </EmptyState>
                ) : (
                  refs.data && (
                    <Table
                      rows={refs.data.items}
                      rowKey={(r) => r.id}
                      columns={[
                        { header: "ID", cell: (r) => <Mono>{r.name}</Mono> },
                        { header: "Kegunaan", cell: (r) => r.placement },
                        {
                          header: "Sumber",
                          cell: (r) => (
                            <div className="space-y-0.5">
                              {r.urls.map((u) => (
                                <a key={u} href={u} target="_blank" rel="noreferrer noopener" className="block break-all text-xs">
                                  {u}
                                </a>
                              ))}
                            </div>
                          ),
                        },
                        { header: "Bukti", cell: (r) => <StatusBadge status={r.evidence_level === "E0" ? "UNVERIFIED" : "NOT_TESTED"} label={r.evidence_level === "E0" ? "E0 komunitas" : "E1 resmi"} /> },
                        { header: "Tanggal", cell: (r) => r.release_date ?? r.research_date ?? "—" },
                      ]}
                    />
                  )
                )}
              </Card>
            ),
          },
          ...(isAdmin
            ? [
                {
                  id: "add",
                  label: "Tambah lewat URL",
                  content: (
                    <Card title="Tambah kandidat lewat URL">
                      <AddUrlForm />
                    </Card>
                  ),
                },
              ]
            : []),
        ]}
      />
    </div>
  );
}
