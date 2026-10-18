import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ExplainButtons } from "../../components/ExplainButtons";
import { DomainRequired } from "../../components/inventory/DomainRequired";
import { Badge, DIMENSION_LABELS, RELATION_LABELS, REQUIREMENT_LABELS, SeverityBadge } from "../../components/inventory/labels";
import type { BlastRadius, ComponentDetail, RelationView } from "../../components/inventory/types";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";

function Unknown({ v }: { v: { value?: string | null; status: string; reason?: string | null } }) {
  if (v.value) return <>{v.value}</>;
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
      ]}
    />
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
        mode={d.mode}
        actions={
          isAdmin ? (
            <Button busy={refresh.isPending} onClick={() => refresh.mutate()}>
              <RefreshCw className="h-4 w-4" aria-hidden /> Segarkan komponen ini
            </Button>
          ) : undefined
        }
      />
      {refresh.error && <ErrorBox error={refresh.error} />}
      {d.tombstoned_at && <Notice tone="warn">Komponen dihapus dari manifest pada {absoluteTime(d.tombstoned_at)}; histori dipertahankan.</Notice>}
      {d.source.unresolved_identity && (
        <Notice tone="warn">Identitas sumber belum terverifikasi; tidak ada tautan tebakan dan tidak ada tombol instalasi.</Notice>
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
                <li className="text-ink-3">Task/run: {use.consuming_tasks.reason}</li>
              </ul>
            ) : (
              <p>Belum ada komponen yang tercatat memakainya. {use.consuming_tasks.reason}</p>
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
                <Link className="text-accent hover:underline" to={d.changes.propose_url}>Buka formulir usulan untuk komponen ini</Link>.
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
              ["Scope otorisasi", d.scope.authorization_scope.value ?? "—"],
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
                {(d.operations.capability_tests ?? []).map((t, i) => (
                  <li key={i}>
                    {String(t.description)} {t.requires_authorization ? <span className="text-xs text-warn">(butuh otorisasi)</span> : null}
                  </li>
                ))}
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
          <div className="mt-3">
            {d.changes.proposals.length === 0 ? (
              <EmptyState title="Belum ada usulan untuk komponen ini">
                <Link className="text-accent hover:underline" to={d.changes.propose_url}>Usulkan perubahan</Link>
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
