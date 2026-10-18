import { useQuery } from "@tanstack/react-query";
import { FolderPlus, GitBranch } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, EmptyState, ErrorBox, Freshness, Loading, Mono, PageHeader } from "../../components/ui";
import { ClassificationBadge, DomainBadge, ModuleBadge, PlatformBadge } from "./badges";
import { SearchPanel } from "./SearchPanel";
import type { ListResponse, ProjectView } from "./types";

const LIFECYCLES = ["ALL", "ACTIVE", "ARCHIVED", "RETIRING", "RETIRED"] as const;
const LIFECYCLE_LABEL: Record<(typeof LIFECYCLES)[number], string> = {
  ALL: "Semua",
  ACTIVE: "Aktif",
  ARCHIVED: "Diarsipkan",
  RETIRING: "Dipensiunkan (proses)",
  RETIRED: "Dipensiunkan",
};

function ProjectCard({ p }: { p: ProjectView }) {
  return (
    <Link to={`/projects/${p.id}`} className="block rounded-lg border border-line bg-surface p-4 transition hover:border-accent">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-base font-semibold">{p.name}</h3>
        {p.is_platform && <PlatformBadge />}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-3">
        <Mono>{p.project_key}</Mono>
        <DomainBadge domain={p.domain} />
        <ModuleBadge kind="lifecycle" value={p.lifecycle} />
        <ClassificationBadge value={p.data_classification} />
        <span>{p.project_kind === "EXISTING" ? "Proyek existing" : "Proyek baru"}</span>
      </div>
      <div className="mt-3 space-y-1.5 text-sm">
        {p.repositories.length === 0 ? (
          <div className="text-xs text-ink-3">Belum ada repository terdaftar.</div>
        ) : (
          p.repositories.map((r) => (
            <div key={r.id} className="flex flex-wrap items-center gap-2">
              <GitBranch className="h-3.5 w-3.5 text-ink-3" aria-hidden />
              <span className="font-medium">{r.full_name}</span>
              <span className="text-xs text-ink-3">{r.provider}</span>
              <ModuleBadge kind="verify" value={r.verification_status} />
              {r.role === "platform" && <span className="text-xs text-ink-3">(platform)</span>}
            </div>
          ))
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <span className="flex items-center gap-1">
          Dokumen: <ModuleBadge kind="config" value={p.integration_state.docs} />
        </span>
        <span className="flex items-center gap-1">
          Issue: <ModuleBadge kind="config" value={p.integration_state.issues} />
        </span>
      </div>
    </Link>
  );
}

export default function ProjectsPage() {
  const { domain, isAdmin } = useAuth();
  const nav = useNavigate();
  const [lifecycle, setLifecycle] = useState<(typeof LIFECYCLES)[number]>("ALL");
  const q = useQuery({ queryKey: ["projects", domain], queryFn: () => get<ListResponse<ProjectView>>("/api/projects") });
  const items = useMemo(() => (q.data?.items ?? []).filter((p) => lifecycle === "ALL" || p.lifecycle === lifecycle), [q.data, lifecycle]);
  return (
    <div className="space-y-5">
      <PageHeader
        title="Portofolio"
        question="Proyek apa saja di domain ini?"
        source="AEOS control plane — proyek, referensi repository, registri integrasi"
        checkedAt={q.data?.checked_at ?? null}
        evidenceLimit="Status repository = lookup provider terakhir; integrasi = konfigurasi saja (koneksi dibuktikan Doctor)"
        mode={q.data?.mode}
        actions={
          <Button variant="primary" onClick={() => nav("/projects/new")} disabled={!isAdmin} title={isAdmin ? undefined : "Hanya SUPER_ADMIN"}>
            <FolderPlus className="h-4 w-4" aria-hidden /> Proyek baru
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-ink-3">Domain aktif:</span>
        {domain && <DomainBadge domain={domain} />}
        <span className="ml-2 text-ink-3">Siklus hidup:</span>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter siklus hidup">
          {LIFECYCLES.map((l) => (
            <button
              key={l}
              onClick={() => setLifecycle(l)}
              aria-pressed={lifecycle === l}
              className={"rounded-md px-2 py-1 text-xs " + (lifecycle === l ? "bg-accent text-accent-ink" : "border border-line bg-surface hover:bg-surface-3")}
            >
              {LIFECYCLE_LABEL[l]}
            </button>
          ))}
        </div>
        {q.data?.checked_at && (
          <span className="ml-auto">
            <Freshness at={q.data.checked_at} />
          </span>
        )}
      </div>
      {q.isLoading && <Loading />}
      {q.error && <ErrorBox error={q.error} />}
      {q.data && items.length === 0 && (
        <EmptyState title={q.data.items.length ? "Tidak ada proyek dengan filter ini" : `Belum ada proyek di domain ${domain}`}>
          {q.data.items.length ? (
            "Ubah filter siklus hidup."
          ) : (
            <>
              Jalankan <Mono>aeos seed</Mono> untuk mendaftarkan proyek DLY AEOS (PERSONAL), atau klik <strong>Proyek baru</strong>
              {isAdmin ? "." : " (perlu SUPER_ADMIN)."}
            </>
          )}
        </EmptyState>
      )}
      {items.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((p) => (
            <ProjectCard key={p.id} p={p} />
          ))}
        </div>
      )}
      <SearchPanel />
    </div>
  );
}
