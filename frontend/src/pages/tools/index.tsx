import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Lightbulb, RefreshCw, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { CoverageBanner } from "../../components/inventory/CoverageBanner";
import { DomainRequired } from "../../components/inventory/DomainRequired";
import { Badge, DIMENSION_LABELS, REQUIREMENT_LABELS } from "../../components/inventory/labels";
import type { ComponentList, ComponentRow, DimensionValue, ProjectRepository, ProjectService, RoleRecord, RouteRecord } from "../../components/inventory/types";
import { proposeComponentHref } from "../../components/proposals/PageLinks";
import { Button, Card, ErrorBox, inputClass, LinkButton, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";

const DIMS = ["installation", "configuration", "connectivity", "functional", "enablement", "usage"] as const;

export function Dim({ d }: { d: DimensionValue }) {
  // §38: "Sedang digunakan oleh task ..." needs the run/operation reference; shown with the badge, never inferred
  const usedBy = d.refs && ["IN_USE", "LAST_USED", "STALE"].includes(d.effective) ? d.reason : null;
  return (
    <div title={[d.source, d.reason, d.at ? `pada ${d.at}` : null].filter(Boolean).join(" · ")}>
      <Badge status={d.effective} />
      {usedBy && <div className="mt-0.5 max-w-48 text-[11px] text-ink-2">{usedBy}</div>}
      {d.at && <div className="mt-0.5 text-[11px] text-ink-3">{relativeTime(d.at)}</div>}
    </div>
  );
}

/** Default-view admission (the advanced "Admission" column stays): a trialled tool must not look merely DISABLED.
 * TRIAL_APPROVED -> "TRIAL", APPROVED -> "Disetujui", anything else (DISCOVERED, ...) -> nothing. A change-proposal
 * TRIAL decision carries no expires_at, so no date is shown for tools. */
export function AdmissionBadge({ admission }: { admission: string | null | undefined }) {
  if (admission === "TRIAL_APPROVED") return <span data-testid="admission-badge"><Badge status="TRIAL_APPROVED" label="TRIAL" /></span>;
  if (admission === "APPROVED") return <span data-testid="admission-badge"><Badge status="APPROVED" label="Disetujui" /></span>;
  return null;
}

function VersionCell({ row }: { row: ComponentRow }) {
  const v = row.version;
  return (
    <div className="text-xs">
      <div>
        pin: <Mono>{v.approved_pin ?? "UNKNOWN"}</Mono>
      </div>
      <div>
        teramati: <Mono>{v.observed_version ?? "—"}</Mono>
      </div>
      <Badge status={v.status} />
    </div>
  );
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== "") sp.set(k, String(v));
  });
  return sp.toString();
}

export default function ToolsPage() {
  const { isAdmin, domain } = useAuth();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [requirement, setRequirement] = useState("");
  const [category, setCategory] = useState("");
  const [designLabel, setDesignLabel] = useState("");
  const [view, setView] = useState<"default" | "advanced">("default");
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const qs = buildQuery({ q, kind, requirement, category, design_label: designLabel, view, page, page_size: pageSize });
  const list = useQuery({
    queryKey: ["inventory", "components", domain, qs],
    queryFn: () => get<ComponentList>(`/api/inventory/components?${qs}`),
    enabled: !!domain,
  });
  const refresh = useMutation({
    mutationFn: () => post("/api/inventory/refresh", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["inventory"] }),
  });
  // Live refresh: one shared event -> query map in AppShell (lib/eventInvalidation.ts), not a per-page filter.

  const data = list.data;
  const facets = data?.facets;
  const columns = [
    {
      header: "Komponen",
      cell: (r: ComponentRow) => (
        <div className="min-w-48">
          <div className="flex flex-wrap items-center gap-1.5">
            <Link to={`/tools/${r.component_id}`} className="font-medium text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
              {r.friendly_name || r.name}
            </Link>
            <AdmissionBadge admission={r.dimensions.admission?.effective} />
          </div>
          {/* §35: friendly name, reason used and next action come before the technical ID */}
          {r.why_selected && (
            <div className="line-clamp-2 max-w-80 text-xs text-ink-2" data-testid="why-selected">
              {r.why_selected}
            </div>
          )}
          {r.next_action && (
            <div className="mt-0.5 text-xs" data-testid="next-action" title={r.next_action.command ?? undefined}>
              <span className="text-ink-3">Berikutnya: </span>
              {r.next_action.link ? (
                <Link to={r.next_action.link} className="text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
                  {r.next_action.label}
                </Link>
              ) : (
                <span>{r.next_action.label}</span>
              )}
              {r.next_action.command && <Mono>{` ${r.next_action.command}`}</Mono>}
            </div>
          )}
          <div className="mono text-xs text-ink-3" data-testid="component-id">{r.component_id}</div>
          <div className="text-xs text-ink-3">
            {r.kind} · {r.category_label}
            {r.domains.includes("PLATFORM") ? "" : ` · ${r.domains.join("/")}`}
          </div>
          {isAdmin && (
            // §39: "Usulkan perubahan" per component (SUPER_ADMIN only; a VIEWER cannot submit proposals).
            <Link
              to={proposeComponentHref(r.component_id)}
              onClick={(e) => e.stopPropagation()}
              className="mt-0.5 inline-flex items-center gap-1 whitespace-nowrap text-xs text-accent hover:underline"
              title={`Usulkan perubahan (ADD/REPLACE/UPGRADE/REMOVE/COMPARE) untuk ${r.friendly_name || r.name}; tidak memasang apa pun`}
            >
              <Lightbulb className="h-3.5 w-3.5" aria-hidden /> Usulkan perubahan
            </Link>
          )}
          {r.drift.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {r.drift.map((d) => (
                <span key={d.drift_class} className="rounded bg-warn-bg px-1 text-[11px] text-warn" title={d.drift_class}>
                  drift: {d.label}
                </span>
              ))}
            </div>
          )}
        </div>
      ),
    },
    {
      header: "Rancangan",
      cell: (r: ComponentRow) => (
        <div className="space-y-1 text-xs">
          <div>{REQUIREMENT_LABELS[r.requirement] ?? r.requirement}</div>
          <Badge status={r.design_label} />
          {r.identity_verified !== "VERIFIED" && <Badge status="UNVERIFIED" />}
        </div>
      ),
    },
    ...DIMS.map((k) => ({ header: DIMENSION_LABELS[k], cell: (r: ComponentRow) => <Dim d={r.dimensions[k]} /> })),
    { header: "Versi", cell: (r: ComponentRow) => <VersionCell row={r} /> },
    ...(view === "advanced"
      ? [
          { header: "Admission", cell: (r: ComponentRow) => <Dim d={r.dimensions.admission} /> },
          { header: "Lifecycle", cell: (r: ComponentRow) => <Dim d={r.dimensions.lifecycle_final} /> },
          {
            header: "Sumber & provenance",
            cell: (r: ComponentRow) => (
              <div className="max-w-64 space-y-0.5 text-xs">
                {r.source_url ? (
                  <a href={r.source_url} target="_blank" rel="noreferrer noopener" className="break-all text-accent hover:underline">
                    {r.source_url}
                  </a>
                ) : (
                  <span className="text-ink-3">URL kanonik: UNKNOWN (tidak ditebak)</span>
                )}
                <div>refs: {(r.source_refs ?? []).join(", ") || "—"}</div>
                <div>identitas: {r.upstream_identity ?? "UNKNOWN"}</div>
                <div>lisensi: {r.license ?? "UNKNOWN"}</div>
                <div>manifest: {shortHash(r.manifest_hash)}</div>
              </div>
            ),
          },
          { header: "Relasi", cell: (r: ComponentRow) => <span className="text-xs">{r.relations ?? 0}</span> },
          { header: "Urutan bootstrap", cell: (r: ComponentRow) => <span className="text-xs">{r.bootstrap_order ?? "—"}</span> },
        ]
      : []),
  ];

  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;
  const header = (
    <PageHeader
      title="Tools & Repos"
      question="Tool/repository apa yang dipakai, dari mana asalnya dan untuk apa?"
      source={data?.source ?? "InstallManifest + observasi"}
      checkedAt={data?.checked_at ?? null}
      evidenceLimit="Katalog desain bukan bukti instalasi; tiap dimensi berasal dari observasi bertanggal"
      relatedDecisionsHref="/decisions?target_type=component"
      mode={data?.mode}
      actions={
        domain ? (
          <>
            {/* §37: SBOM (CycloneDX) of the platform lockfiles: declarations of what builds link, not running processes */}
            <a className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-sm hover:bg-surface-3" href="/api/inventory/sbom" download="dly-aeos.cdx.json">
              <Download className="h-4 w-4" aria-hidden /> Unduh SBOM
            </a>
            {isAdmin && (
              <>
                <Button busy={refresh.isPending} onClick={() => refresh.mutate()} title="Probe read-only; tidak memasang apa pun">
                  <RefreshCw className="h-4 w-4" aria-hidden /> Segarkan (rekonsiliasi)
                </Button>
                <LinkButton to="/proposals?new=1&kind=ADD" title="Usulkan komponen baru; mengajukan URL tidak memasang apa pun">
                  <Lightbulb className="h-4 w-4" aria-hidden /> Usulkan komponen baru
                </LinkButton>
              </>
            )}
          </>
        ) : undefined
      }
    />
  );
  if (!domain)
    return (
      <div>
        {header}
        <DomainRequired />
      </div>
    );

  return (
    <div>
      {header}
      {refresh.error && <ErrorBox error={refresh.error} />}
      {data?.mode === "DESIGN" && (
        <Notice tone="mock">
          Semua baris masih berstatus DESAIN (dari InstallManifest). Belum ada observasi host; jalankan <Mono>aeos inventory reconcile</Mono>
          {isAdmin ? " atau tombol Segarkan." : "."}
        </Notice>
      )}
      <div className="mt-3">
        <CoverageBanner coverage={data?.coverage} />
      </div>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-ink-3" aria-hidden />
          <input
            className={`${inputClass} pl-8`}
            placeholder="Cari nama, fungsi, identitas…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
            aria-label="Cari komponen"
          />
        </div>
        {(
          [
            ["Jenis", kind, setKind, facets?.kinds ?? []],
            ["Kebutuhan", requirement, setRequirement, facets?.requirements ?? []],
            ["Kategori", category, setCategory, facets?.categories ?? []],
            ["Label desain", designLabel, setDesignLabel, facets?.design_labels ?? []],
          ] as [string, string, (v: string) => void, string[]][]
        ).map(([label, value, set, options]) => (
          <label key={label} className="text-xs">
            <span className="mb-0.5 block text-ink-3">{label}</span>
            <select
              className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm"
              value={value}
              onChange={(e) => {
                set(e.target.value);
                setPage(1);
              }}
            >
              <option value="">Semua</option>
              {options.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>
        ))}
        <div className="ml-auto flex gap-1" role="group" aria-label="Tampilan">
          <Button variant={view === "default" ? "primary" : "secondary"} onClick={() => setView("default")}>
            Default
          </Button>
          <Button variant={view === "advanced" ? "primary" : "secondary"} onClick={() => setView("advanced")}>
            Advanced
          </Button>
        </div>
      </div>
      {list.isLoading ? (
        <Loading />
      ) : list.error ? (
        <ErrorBox error={list.error} />
      ) : (
        <>
          <Table
            rows={data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.component_id}
            empty="Tidak ada komponen untuk filter ini. Muat katalog dengan: aeos seed"
            onRowClick={(r) => nav(`/tools/${r.component_id}`)}
          />
          <div className="mt-3 flex items-center justify-between text-xs text-ink-3">
            <span>
              {data?.total ?? 0} komponen · manifest {shortHash(data?.manifest?.content_hash)}
            </span>
            <div className="flex items-center gap-2">
              <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>
                Sebelumnya
              </Button>
              <span>
                Hal {page}/{totalPages}
              </span>
              <Button disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
                Berikutnya
              </Button>
            </div>
          </div>
          <ProjectRecords repos={data?.project_repositories ?? []} services={data?.project_services ?? []} routes={data?.routes ?? []} roles={data?.roles ?? []} />
        </>
      )}
    </div>
  );
}

/** §37 kinds that live in other registries: project repositories (exact provider repo id) and the services a
 *  project declares (approved PlanVersion / repository markers). Shown with their source, never as installed. */
export function ProjectRecords({
  repos,
  services,
  routes = [],
  roles = [],
}: {
  repos: ProjectRepository[];
  services: ProjectService[];
  routes?: RouteRecord[];
  roles?: RoleRecord[];
}) {
  return (
    <div className="mt-6 grid gap-4 lg:grid-cols-2">
      <Card title="Route model & role (registry lain)">
        {routes.length === 0 && roles.length === 0 ? (
          <p className="text-sm text-ink-3">Belum ada route APPROVED/TRIAL atau agent aktif untuk domain ini.</p>
        ) : (
          <ul className="space-y-1 text-sm" data-testid="registry-records">
            {routes.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2">
                <Link to="/models" className="font-medium text-accent hover:underline">route {r.route_key}</Link>
                <Badge status={r.status} />
                <span className="text-xs text-ink-3">{r.pool} · provider {r.provider_component ?? r.provider}</span>
              </li>
            ))}
            {roles.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2">
                <Link to="/organization" className="font-medium text-accent hover:underline">role {r.name}</Link>
                <span className="text-xs text-ink-3">agent aktif: {r.agents.map((a) => a.agent_key).join(", ")} · harness {r.harness_components.join(", ") || "—"}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Repository proyek (domain ini)">
        {repos.length === 0 ? (
          <p className="text-sm text-ink-3">Belum ada repository proyek terdaftar di domain ini.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {repos.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2">
                <Link to={r.link} className="font-medium text-accent hover:underline">{r.full_name}</Link>
                <span className="text-xs text-ink-3">{r.project_name}</span>
                <Badge status={r.identity_status} />
                <Mono>{r.identity ?? "ID repo UNKNOWN"}</Mono>
                <span className="text-xs text-ink-3">
                  {r.default_branch}
                  {r.head_commit ? ` @ ${shortHash(r.head_commit, 8)}` : ""}
                  {r.dirty ? " · dirty" : ""} · diverifikasi {r.last_verified_at ? relativeTime(r.last_verified_at) : "belum"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Layanan hasil build proyek (deklarasi)">
        {services.length === 0 ? (
          <p className="text-sm text-ink-3">Belum ada layanan yang dideklarasikan PlanVersion yang disetujui atau penanda repository.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {services.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.name}</span>
                <span className="text-xs text-ink-3">{s.project_name}</span>
                <Badge status={s.status} />
                <span className="text-xs text-ink-3" title={s.observed_reason}>
                  teramati: {s.observed} · {s.provenance}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
