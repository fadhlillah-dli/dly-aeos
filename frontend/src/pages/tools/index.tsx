import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { CoverageBanner } from "../../components/inventory/CoverageBanner";
import { DomainRequired } from "../../components/inventory/DomainRequired";
import { Badge, DIMENSION_LABELS, REQUIREMENT_LABELS } from "../../components/inventory/labels";
import type { ComponentList, ComponentRow, DimensionValue } from "../../components/inventory/types";
import { Button, ErrorBox, inputClass, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { relativeTime, shortHash } from "../../lib/format";

const DIMS = ["installation", "configuration", "connectivity", "functional", "enablement", "usage"] as const;

function Dim({ d }: { d: DimensionValue }) {
  return (
    <div title={[d.source, d.reason, d.at ? `pada ${d.at}` : null].filter(Boolean).join(" · ")}>
      <Badge status={d.effective} />
      {d.at && <div className="mt-0.5 text-[11px] text-ink-3">{relativeTime(d.at)}</div>}
    </div>
  );
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
  useEventStream((e) => {
    if (e.kind === "inventory.reconcile.completed" || e.kind.startsWith("inventory.drift")) qc.invalidateQueries({ queryKey: ["inventory"] });
  });

  const data = list.data;
  const facets = data?.facets;
  const columns = [
    {
      header: "Komponen",
      cell: (r: ComponentRow) => (
        <div className="min-w-48">
          <Link to={`/tools/${r.component_id}`} className="font-medium text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
            {r.friendly_name || r.name}
          </Link>
          <div className="mono text-xs text-ink-3">{r.component_id}</div>
          <div className="text-xs text-ink-3">
            {r.kind} · {r.category_label}
            {r.domains.includes("PLATFORM") ? "" : ` · ${r.domains.join("/")}`}
          </div>
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
      mode={data?.mode}
      actions={
        isAdmin && domain ? (
          <Button busy={refresh.isPending} onClick={() => refresh.mutate()} title="Probe read-only; tidak memasang apa pun">
            <RefreshCw className="h-4 w-4" aria-hidden /> Segarkan (rekonsiliasi)
          </Button>
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
        </>
      )}
    </div>
  );
}
