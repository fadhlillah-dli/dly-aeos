import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Lightbulb, RefreshCw } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ExplainButtons } from "../../components/ExplainButtons";
import { DomainRequired } from "../../components/inventory/DomainRequired";
import { Badge, DIMENSION_LABELS, RELATION_LABELS, REQUIREMENT_LABELS, SeverityBadge } from "../../components/inventory/labels";
import type { BlastRadius, ComponentDetail, DependencyPage, RelationView } from "../../components/inventory/types";
import { proposeComponentHref } from "../../components/proposals/PageLinks";
import { Button, Card, EmptyState, ErrorBox, Freshness, inputClass, KeyValue, LinkButton, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";

function Unknown({ v }: { v: { value?: unknown; status: string; reason?: string | null } }) {
  if (v.value !== null && v.value !== undefined && v.value !== "") return <>{asText(v.value)}</>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge status={v.status} />
      {v.reason && <span className="text-xs text-ink-3">{v.reason}</span>}
    </span>
  );
}

function asText(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map(String).join(", ") : "—";
  if (typeof v === "object") return <Mono>{JSON.stringify(v)}</Mono>;
  return String(v);
}

function RelationsTable({ rows, dir }: { rows: RelationView[]; dir: "out" | "in" }) {
  return (
    <Table
      rows={rows}
      rowKey={(r) => r.id}
      empty={dir === "out" ? "Tidak ada relasi keluar" : "Tidak ada relasi masuk"}
      columns={[
        {
          header: dir === "out" ? "Ke" : "Dari",
          cell: (r) => {
            const other = dir === "out" ? r.target : r.source;
            return (
              <Link className="text-accent hover:underline" to={`/tools/${other}`}>
                {dir === "out" ? r.target_name : r.source_name}
              </Link>
            );
          },
        },
        { header: "Relasi", cell: (r) => <span>{RELATION_LABELS[r.relation] ?? r.relation}</span> },
        { header: "Lensa", cell: (r) => <Badge status={r.lens === "DESIGN" ? "DESIGN" : r.lens === "OBSERVED" ? "OBSERVED" : "CONFIGURED"} /> },
        { header: "Data / arah", cell: (r) => <div className="max-w-80 text-xs">{r.data_description}<div className="mono text-ink-3">{r.direction}</div></div> },
        { header: "Operasi", cell: (r) => <span className="text-xs">{r.operation}</span> },
        { header: "Boundary / identitas", cell: (r) => <div className="text-xs">{r.boundary ?? "—"}<div className="text-ink-3">{r.identity_ref ?? ""}</div></div> },
        { header: "Provenance", cell: (r) => <span className="text-xs text-ink-3">{r.evidence}</span> },
        {
          header: "Hasil uji",
          cell: (r) =>
            r.tests && r.tests.length ? (
              <ul className="text-xs">
                {r.tests.map((t) => (
                  <li key={`${t.about}-${t.check_id}`}>
                    <Badge status={t.status} /> <Mono>{t.check_id}</Mono>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-xs text-ink-3">belum ada pemeriksaan</span>
            ),
        },
      ]}
    />
  );
}

/** §37 "paket transitif dapat dieksplorasi": the paginated lockfile view of a component that owns a lockfile. */
export function DependencyExplorer({ componentId }: { componentId: string }) {
  const [page, setPage] = useState(1);
  const [relation, setRelation] = useState<"" | "direct" | "transitive">("");
  const [q, setQ] = useState("");
  const qs = new URLSearchParams({ page: String(page), page_size: "50" });
  if (relation) qs.set("relation", relation);
  if (q.trim()) qs.set("q", q.trim());
  const deps = useQuery({
    queryKey: ["inventory", "dependencies", componentId, qs.toString()],
    queryFn: () => get<DependencyPage>(`/api/inventory/components/${encodeURIComponent(componentId)}/dependencies?${qs.toString()}`),
  });
  const data = deps.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  return (
    <div className="mt-3" data-testid="dependency-explorer">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <input className={`${inputClass} max-w-56`} placeholder="Cari paket…" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} aria-label="Cari paket" />
        <select aria-label="Relasi dependensi" className="rounded-md border border-line bg-surface px-2 py-1" value={relation} onChange={(e) => { setRelation(e.target.value as "" | "direct" | "transitive"); setPage(1); }}>
          <option value="">Semua</option>
          <option value="direct">Langsung</option>
          <option value="transitive">Transitif</option>
        </select>
        {data?.counts && (
          <span className="text-xs text-ink-3">
            {data.counts.direct} langsung · {data.counts.transitive} transitif · lockfile {data.lockfile?.path}
            {data.lockfile?.matches_last_reconcile === false ? " (berubah sejak rekonsiliasi)" : ""}
          </span>
        )}
      </div>
      {deps.isLoading ? (
        <Loading />
      ) : deps.error ? (
        <ErrorBox error={deps.error} />
      ) : !data || data.items.length === 0 ? (
        <p className="text-xs text-ink-3">{data?.reason ?? "Tidak ada paket untuk filter ini."}</p>
      ) : (
        <>
          <Table
            rows={data.items}
            rowKey={(p) => `${p.ecosystem}:${p.name}:${p.version}:${p.parent_path.join("/")}`}
            columns={[
              { header: "Paket", cell: (p) => <div><Mono>{p.name}</Mono> <span className="text-xs">{p.version ?? "?"}</span>{p.dev ? <span className="text-xs text-ink-3"> (dev)</span> : null}</div> },
              { header: "Relasi", cell: (p) => <span className="text-xs">{p.relation === "direct" ? "langsung" : `transitif via ${p.parent_path.join(" > ") || p.required_by.slice(0, 3).join(", ") || "?"}`}</span> },
              { header: "Dipakai oleh", cell: (p) => <span className="text-xs">{p.used_by_build?.label ?? "—"}</span> },
            ]}
          />
          <div className="mt-2 flex items-center gap-2 text-xs text-ink-3">
            <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>Sebelumnya</Button>
            <span>Hal {page}/{pages} · {data.total} paket</span>
            <Button disabled={page >= pages} onClick={() => setPage(page + 1)}>Berikutnya</Button>
          </div>
          <p className="mt-1 text-xs text-ink-3">{data.coverage.images}</p>
        </>
      )}
    </div>
  );
}

function ConsumingTasks({ c }: { c: ComponentDetail["usage_role"]["consuming_tasks"] }) {
  return (
    <>
      {(c.routes ?? []).map((r) => (
        <li key={r.route_key}>route {r.route_key} ({r.status}, {r.pool})</li>
      ))}
      {(c.agents ?? []).map((a) => (
        <li key={a.agent_key}>agent {a.agent_key} ({a.role_key}){a.enabled ? "" : " — nonaktif"}</li>
      ))}
      {c.items.map((r) => (
        <li key={r.run_id}>
          run <Mono>{r.run_id.slice(0, 8)}</Mono> task {r.task_key ?? "—"} <Badge status={r.status} />
        </li>
      ))}
      {c.reason && <li className="text-ink-3">{c.reason}</li>}
    </>
  );
}

export default function ComponentDetailPage() {
  const { componentId = "" } = useParams();
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ["inventory", "component", domain, componentId],
    queryFn: () => get<ComponentDetail>(`/api/inventory/components/${encodeURIComponent(componentId)}`),
    retry: false,
    enabled: !!domain,
  });
  const blast = useQuery({
    queryKey: ["inventory", "blast", domain, componentId],
    queryFn: () => get<BlastRadius>(`/api/inventory/blast-radius/${encodeURIComponent(componentId)}`),
    enabled: detail.isSuccess,
  });
  const refresh = useMutation({
    mutationFn: () => post("/api/inventory/refresh", { component_id: componentId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["inventory"] }),
  });

  if (!domain)
    return (
      <div>
        <PageHeader title="Detail komponen" question="Fungsi, sumber, versi, akses, bukti dan perubahan komponen" />
        <DomainRequired />
      </div>
    );
  if (detail.isLoading) return <Loading />;
  if (detail.error)
    return (
      <div>
        <PageHeader title="Detail komponen" question="Fungsi, sumber, versi, akses, bukti dan perubahan komponen" />
        <ErrorBox error={detail.error} />
        <p className="mt-2 text-sm text-ink-3">Komponen tidak ada di domain aktif atau belum dimuat (jalankan aeos seed).</p>
      </div>
    );
  const d = detail.data!;
  const id = d.identity;
  const ver = d.versions;
  const use = d.usage_role;
  const ev = d.evidence;

  return (
    <div>
      <Link to="/tools" className="mb-2 inline-flex items-center gap-1 text-sm text-accent hover:underline">
        <ArrowLeft className="h-4 w-4" aria-hidden /> Tools & Repos
      </Link>
      <PageHeader
        title={id.friendly_name || id.name}
        question={id.function}
        source="InstallManifest + observasi + keputusan"
        checkedAt={ev.last_observed_at ?? ev.last_checked_at}
        evidenceLimit="Rancangan ≠ terpasang ≠ aktif ≠ dipakai; tiap dimensi punya waktu dan sumber sendiri"
        relatedDecisionsHref={`/decisions?target_type=component&target_id=${encodeURIComponent(componentId ?? "")}`}
        mode={d.mode}
        actions={
          isAdmin ? (
            <>
              <Button busy={refresh.isPending} onClick={() => refresh.mutate()}>
                <RefreshCw className="h-4 w-4" aria-hidden /> Segarkan komponen ini
              </Button>
              <LinkButton to={proposeComponentHref(componentId)} variant="primary" title="Add/Replace/Upgrade/Remove/Compare; review tidak memasang apa pun">
                <Lightbulb className="h-4 w-4" aria-hidden /> Usulkan perubahan
              </LinkButton>
            </>
          ) : undefined
        }
      />
      {refresh.error && <ErrorBox error={refresh.error} />}
      {d.tombstoned_at && <Notice tone="warn">Komponen dihapus dari manifest pada {absoluteTime(d.tombstoned_at)}; histori dipertahankan.</Notice>}
      {d.source.unresolved_identity && (
        <Notice tone="warn">Identitas sumber belum terverifikasi; tidak ada tautan tebakan dan tidak ada tombol instalasi.</Notice>
      )}
      {d.next_action && (
        <Notice tone="info">
          <span data-testid="detail-next-action">
            Tindakan berikutnya: {d.next_action.link ? <Link className="underline" to={d.next_action.link}>{d.next_action.label}</Link> : d.next_action.label}
            {d.next_action.command && <> — <Mono>{d.next_action.command}</Mono></>}
          </span>
        </Notice>
      )}

      <div className="my-4">
        <ExplainButtons
          content={{
            what: <p>{id.function}</p>,
            why: (
              <div className="space-y-1">
                <p>{use.why_selected}</p>
                <p className="text-ink-3">Penempatan: {use.placement}</p>
              </div>
            ),
            usedBy: use.used_by.length ? (
              <ul className="list-disc pl-5">
                {use.used_by.map((u) => (
                  <li key={`${u.component_id}-${u.relation}`}>
                    <Link className="text-accent hover:underline" to={`/tools/${u.component_id}`}>{u.name}</Link> ({RELATION_LABELS[u.relation] ?? u.relation}, {u.lens})
                  </li>
                ))}
                <ConsumingTasks c={use.consuming_tasks} />
              </ul>
            ) : (
              <div>
                <p>Belum ada komponen yang tercatat memakainya.</p>
                <ul className="list-disc pl-5">
                  <ConsumingTasks c={use.consuming_tasks} />
                </ul>
              </div>
            ),
            evidence: (
              <ul className="space-y-1">
                {ev.claims.map((c, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2">
                    <Badge status={c.status} /> {c.claim} <span className="text-xs text-ink-3">{c.level ?? "tanpa bukti"} · {c.source}{c.at ? ` · ${relativeTime(c.at)}` : ""}</span>
                  </li>
                ))}
              </ul>
            ),
            impact: blast.data ? (
              <div>
                <p>
                  {blast.data.summary.affected} komponen terdampak bila komponen ini berubah atau gagal (dependensi transitif, bukan koneksi jaringan langsung).
                </p>
                <ul className="mt-1 list-disc pl-5">
                  {blast.data.items.slice(0, 12).map((i) => (
                    <li key={i.component_id}>
                      <Link className="text-accent hover:underline" to={`/tools/${i.component_id}`}>{i.name}</Link> <span className="text-xs text-ink-3">(jarak {i.depth})</span>
                    </li>
                  ))}
                </ul>
                <Link className="text-xs text-accent hover:underline" to={`/system-map?blast=${encodeURIComponent(d.component_id)}`}>Lihat di Peta Sistem</Link>
              </div>
            ) : (
              <Loading />
            ),
            propose: (
              <p>
                Usulan Add/Replace/Upgrade/Remove/Compare tidak memasang apa pun.{" "}
                {isAdmin ? (
                  <Link className="text-accent hover:underline" to={proposeComponentHref(componentId)}>Buka formulir usulan untuk komponen ini</Link>
                ) : (
                  "Pengajuan usulan memerlukan SUPER_ADMIN"
                )}
                .
              </p>
            ),
          }}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Identitas">
          <KeyValue
            items={[
              ["ID", <Mono key="id">{id.component_id}</Mono>],
              ["Nama", id.name],
              ["Jenis", id.kind],
              ["Kategori", id.category_label],
              ["Jenis sumber", id.source_kind],
              ["Identitas upstream", id.upstream_identity ?? <Badge status="UNKNOWN" />],
              ["Instance teramati", id.instances?.length ? id.instances.join(", ") : id.instances_note],
            ]}
          />
        </Card>
        <Card title="Scope">
          <KeyValue
            items={[
              ["Level", d.scope.level],
              ["Domain", d.scope.domains.join(", ")],
              ["Environment", d.scope.environment],
              ["Owner", d.scope.owner],
              ["Maintainer operasional", <Unknown key="m" v={d.scope.maintainer} />],
              [
                "Scope otorisasi",
                <span key="a">
                  <Badge status={d.scope.authorization_scope.status} /> {d.scope.authorization_scope.value ?? "—"}
                  {(d.scope.authorization_scope.integrations ?? []).map((i) => (
                    <div key={`${i.project_id}-${i.provider}-${i.purpose}`} className="text-xs text-ink-3">
                      integrasi {i.provider}/{i.purpose}: {i.root_id ?? "root UNKNOWN"}
                      {i.tenant_id ? ` @ ${i.tenant_id}` : ""}
                    </div>
                  ))}
                  {(d.scope.authorization_scope.routes ?? []).map((r) => (
                    <div key={r.route_key} className="text-xs text-ink-3">route {r.route_key} ({r.status}): {r.eligible_domains.join(", ") || "semua domain"}</div>
                  ))}
                </span>,
              ],
            ]}
          />
        </Card>
        <Card title="Sumber">
          <KeyValue
            items={[
              [
                "URL kanonik",
                d.source.source_url ? (
                  <a key="u" href={d.source.source_url} target="_blank" rel="noreferrer noopener" className="break-all text-accent hover:underline">
                    {d.source.source_url}
                  </a>
                ) : (
                  <span key="u" className="text-ink-3">{d.source.source_url_note}</span>
                ),
              ],
              ["Referensi master", d.source.source_refs.join(", ") || "—"],
              ["Identitas sumber (menurut master)", <Badge key="i" status={d.source.identity_verified} />],
              ["Publisher", <Unknown key="p" v={d.source.publisher} />],
              ["Lisensi", <Unknown key="l" v={d.source.license} />],
              ["Diperiksa (riset)", d.source.retrieval_time ?? "—"],
            ]}
          />
        </Card>
        <Card title="Versi">
          <KeyValue
            items={[
              ["Pin disetujui", ver.approved_pin ?? <Badge key="p" status="NO_PIN" />],
              ["Terpasang/teresolusi", ver.observed_version ?? "—"],
              ["Status versi", <Badge key="s" status={ver.status} />],
              [
                "Digest disetujui / teramati",
                <span key="dg" className="inline-flex flex-wrap items-center gap-1">
                  <Mono>{shortHash(ver.approved_digest)}</Mono> / <Mono>{shortHash(ver.observed_digest)}</Mono> {ver.digest_status && <Badge status={ver.digest_status} />}
                  {ver.approved_digest_platform && (
                    <span className="text-xs text-ink-3" data-testid="digest-platform">
                      platform ini: <Mono>{ver.approved_digest_platform}</Mono>
                      {ver.approved_digest_platforms && ver.approved_digest_platforms.length > 0 && (
                        <> · digest tercatat untuk: <Mono>{ver.approved_digest_platforms.join(", ")}</Mono></>
                      )}
                    </span>
                  )}
                </span>,
              ],
              ["Artefak distribusi", asText(ver.distribution)],
              ["Tanda tangan / provenance", asText(ver.signature)],
              [
                "Schema tool/MCP (saat admission)",
                ver.tool_schema_hash ? (
                  <Mono key="ts">{shortHash(ver.tool_schema_hash)}</Mono>
                ) : (
                  <span key="ts" className="inline-flex items-center gap-1">
                    <Badge status="UNKNOWN" /> <span className="text-xs text-ink-3">belum direkam saat admission; perubahan schema belum dapat dideteksi</span>
                  </span>
                ),
              ],
              ["Revisi konfigurasi", <Unknown key="c" v={ver.config_revision} />],
              ["Lockfile", ver.lockfile_refs.length ? ver.lockfile_refs.join(", ") : "—"],
              ["Catatan", <span key="n" className="text-xs text-ink-3">{ver.provenance_note}</span>],
            ]}
          />
        </Card>
        <Card title="Peran penggunaan">
          <KeyValue
            items={[
              ["Peran", use.usage_role],
              ["Kebutuhan", REQUIREMENT_LABELS[use.requirement] ?? use.requirement],
              ["Label desain", <Badge key="d" status={use.design_label} />],
              ["Urutan bootstrap", use.bootstrap_order ?? "—"],
              ["Mengapa dipilih", use.why_selected],
              ["Penempatan (§45)", use.placement],
              ["Konfigurasi berlaku", d.config_lens.present ? d.config_lens.basis.join("; ") : <span key="c" className="text-ink-3">belum di-enable ({d.config_lens.note})</span>],
            ]}
          />
        </Card>
        <Card title="Lifecycle (dimensi terpisah)">
          <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <tbody className="divide-y divide-line">
              {Object.entries(d.lifecycle).map(([k, v]) => (
                <tr key={k} className="align-top">
                  <td className="py-1.5 pr-3 text-ink-3">{DIMENSION_LABELS[k] ?? k}</td>
                  <td className="py-1.5 pr-3">
                    <Badge status={v.effective} />
                  </td>
                  <td className="py-1.5 text-xs text-ink-3">
                    {v.source}
                    {v.at ? ` · ${relativeTime(v.at)}` : ""}
                    {v.reason ? ` · ${v.reason}` : ""}
                    {v.stale && " · STALE"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Card>
        <Card title="Akses dan data">
          <KeyValue items={Object.entries(d.access).map(([k, v]) => [k, asText(v)])} />
        </Card>
        <Card title="Biaya">
          <KeyValue
            items={[
              ["Model biaya (desain)", asText(d.cost.design.model)],
              ["Catatan", asText(d.cost.design.note)],
              ["Sumber", asText(d.cost.design.source)],
              ["Aktual", <span key="a">{asText(d.cost.actual.status)} {d.cost.actual.total_usd != null ? `$${d.cost.actual.total_usd}` : ""}</span>],
              ["Alasan", asText(d.cost.actual.reason)],
              ["Masa gratis berakhir", d.cost.free_expiry ? <Unknown key="f" v={d.cost.free_expiry} /> : "—"],
              [
                "Sumber budget",
                d.cost.budget_source?.items.length
                  ? d.cost.budget_source.items.map((b) => `${b.scope_type}:${b.scope_id} ${b.period} ${b.limit_usd ?? "?"} USD`).join("; ")
                  : <span key="b" className="text-xs text-ink-3">{d.cost.budget_source?.reason ?? "—"}</span>,
              ],
              [
                "Sumber kuota",
                d.cost.quota_source?.items.length
                  ? d.cost.quota_source.items.map((q) => `${q.provider} ${q.state} (${q.source})`).join("; ")
                  : <span key="q" className="text-xs text-ink-3">{d.cost.quota_source?.reason ?? "—"}</span>,
              ],
            ]}
          />
        </Card>
      </div>

      <Card title="Relasi" className="mt-4">
        <div className="space-y-4">
          <div>
            <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Keluar</div>
            <RelationsTable rows={d.relations.outgoing} dir="out" />
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Masuk (dipakai oleh)</div>
            <RelationsTable rows={d.relations.incoming} dir="in" />
          </div>
        </div>
      </Card>

      <Card title="Bukti" className="mt-4">
        {ev.drift.length > 0 && (
          <div className="mb-3 space-y-1">
            {ev.drift.map((x) => (
              <div key={x.id} className="flex flex-wrap items-center gap-2 text-sm">
                <SeverityBadge severity={x.severity} /> <span className="font-medium">{x.label}</span> <span className="text-ink-3">{x.summary}</span>
              </div>
            ))}
          </div>
        )}
        <div className="mb-3 text-xs text-ink-3">
          Terakhir diperiksa: <Freshness at={ev.last_checked_at} /> · teramati: {relativeTime(ev.last_observed_at)} · dipakai: {ev.last_used_at ? relativeTime(ev.last_used_at) : "belum pernah teramati"}
        </div>
        <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Pemeriksaan Doctor</div>
        <Table
          rows={ev.checks}
          rowKey={(c) => c.check_id}
          empty="Belum ada pemeriksaan Doctor untuk komponen ini (aeos doctor)"
          columns={[
            { header: "Check", cell: (c) => <Link className="text-accent hover:underline" to="/readiness">{c.title}<div className="mono text-xs text-ink-3">{c.check_id}</div></Link> },
            { header: "Status", cell: (c) => <Badge status={c.status} /> },
            { header: "Ringkasan", cell: (c) => <span className="text-xs">{c.summary}</span> },
            { header: "Waktu", cell: (c) => <Freshness at={c.last_checked_at} /> },
          ]}
        />
        <div className="mb-1 mt-4 text-xs font-semibold uppercase text-ink-3">Observasi</div>
        <Table
          rows={ev.observations}
          rowKey={(o) => o.id}
          empty="Belum ada observasi; jalankan rekonsiliasi"
          columns={[
            { header: "Dimensi", cell: (o) => DIMENSION_LABELS[o.dimension] ?? o.dimension },
            { header: "Status", cell: (o) => <Badge status={o.stale ? "STALE" : o.status} /> },
            { header: "Versi / identitas", cell: (o) => <span className="text-xs">{o.observed_version ?? "—"} {o.observed_identity ?? ""}</span> },
            { header: "Lokasi (logis)", cell: (o) => <span className="mono text-xs">{o.location ?? "—"}</span> },
            { header: "Operasi", cell: (o) => <span className="mono text-xs">{o.operation_ref ?? "—"}</span> },
            { header: "Observer / waktu", cell: (o) => <span className="text-xs">{o.observer} · {relativeTime(o.observed_at)}</span> },
          ]}
        />
      </Card>

      {d.dependencies.length > 0 && (
        <Card title="Dependensi dari managed scope (deklarasi, bukan proses aktif)" className="mt-4">
          {d.dependencies.map((dep, i) => (
            <div key={i} className="mb-3">
              <div className="text-xs text-ink-3">
                {dep.source} · {relativeTime(dep.observed_at)} {dep.stale ? "· STALE" : ""}
                {dep.transitive_packages != null && ` · ${dep.transitive_packages} paket transitif`}
              </div>
              {dep.packages && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {dep.packages.map((p) => (
                    <span key={p.name} className="rounded bg-surface-3 px-1.5 py-0.5 text-xs" title={`${p.group} ${p.spec}`}>
                      {p.name} {p.resolved ?? p.installed ?? p.spec}
                    </span>
                  ))}
                </div>
              )}
              {dep.actions && (
                <ul className="mt-1 text-xs">
                  {dep.actions.map((a, j) => (
                    <li key={j}>
                      <Mono>{a.uses}@{a.ref}</Mono> {a.pinned_sha ? <Badge status="MATCH" /> : <Badge status="MISMATCH" />}
                    </li>
                  ))}
                </ul>
              )}
              {dep.recommendations && <div className="mt-1 text-xs">{dep.recommendations.join(", ")}</div>}
              {dep.package && <div className="mt-1 text-xs">{dep.package} {dep.spec} → {dep.version ?? "?"}</div>}
              {dep.used_by_build && <div className="mt-1 text-xs text-ink-2" data-testid="used-by-build">{dep.used_by_build.label}</div>}
              {dep.dependencies_url && <DependencyExplorer componentId={d.component_id} />}
            </div>
          ))}
        </Card>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Operasi">
          <KeyValue items={Object.entries(d.operations).filter(([k]) => k !== "capability_tests").map(([k, v]) => [k, asText(v)])} />
          {(d.operations.capability_tests ?? []).length > 0 && (
            <div className="mt-3">
              <div className="mb-1 text-xs font-semibold uppercase text-ink-3">Capability test</div>
              <ul className="list-disc pl-5 text-sm">
                {(d.operations.capability_tests ?? []).map((t, i) => {
                  const res = d.admission?.capability_tests.find((x) => x.id === t.id);
                  return (
                    <li key={i}>
                      {String(t.description)} {t.requires_authorization ? <span className="text-xs text-warn">(butuh otorisasi)</span> : null}{" "}
                      {res && (
                        <span className="text-xs">
                          <Badge status={res.stale ? "STALE" : res.result} /> {res.evidence_id ? <Mono>bukti {shortHash(res.evidence_id, 8)}</Mono> : "tanpa bukti"}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </Card>
        <Card title="Perubahan">
          <KeyValue
            items={[
              ["Keputusan", d.changes.decision_refs.length ? d.changes.decision_refs.join(", ") : "belum ada keputusan tercatat"],
              ["Manifest", `${shortHash(d.changes.manifest_hash)}${d.changes.manifest_record?.loaded_at ? ` · dimuat ${relativeTime(d.changes.manifest_record.loaded_at)}` : ""}`],
              ["Konfigurasi diterima sebelumnya", <Unknown key="p" v={d.changes.previous_accepted_configuration} />],
            ]}
          />
          {(d.changes.decisions ?? []).length > 0 && (
            <ul className="mt-2 space-y-1 text-xs" data-testid="decision-bindings">
              {(d.changes.decisions ?? []).map((x) => (
                <li key={`${x.id}-${x.via}`}>
                  <Badge status={x.status} /> {x.kind} ({x.via}) · target hash <Mono>{shortHash(x.binding_hash)}</Mono>
                  {x.candidate_hash ? <> · kandidat <Mono>{shortHash(x.candidate_hash)}</Mono></> : null}
                  {x.created_at ? ` · ${relativeTime(x.created_at)}` : ""}
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3">
            {d.changes.proposals.length === 0 ? (
              <EmptyState title="Belum ada usulan untuk komponen ini">
                {isAdmin ? (
                  <Link className="text-accent hover:underline" to={proposeComponentHref(componentId)}>Usulkan perubahan</Link>
                ) : (
                  "Pengajuan usulan memerlukan SUPER_ADMIN."
                )}
              </EmptyState>
            ) : (
              <ul className="space-y-1 text-sm">
                {d.changes.proposals.map((p) => (
                  <li key={p.id}>
                    <Link className="text-accent hover:underline" to={`/proposals/${p.id}`}>{p.title}</Link> <Badge status={p.status} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
