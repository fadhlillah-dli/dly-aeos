import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, ErrorBox, Freshness, inputClass, Loading } from "../../components/ui";
import { type BadgeKind, ModuleBadge } from "./badges";
import type { SearchResponse } from "./types";

const KIND_LABEL: Record<string, string> = {
  project: "Proyek",
  plan: "Rencana",
  task: "Task",
  component: "Komponen",
  decision: "Keputusan",
  adr: "ADR",
  document: "Dokumen",
  attachment: "Lampiran",
  repository: "Repository",
  memory: "Memori",
};
// Module statuses use the same honest mapping as the rest of the projects pages (e.g. a CLEAN scan
// or an ACTIVE lifecycle is never rendered as a green pass); other kinds keep the canonical badge.
const KIND_BADGE: Partial<Record<string, BadgeKind>> = { project: "lifecycle", attachment: "scan", repository: "verify", memory: "memory" };

/** §35 metadata search, scoped by the backend to the active domain (counts included); with
 * `projectId` it is project-scoped (that project's records plus platform-global ones). */
export function SearchPanel({ projectId }: { projectId?: string } = {}) {
  const { domain } = useAuth();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(text.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [text]);
  const enabled = q.length >= 2;
  const res = useQuery({
    queryKey: ["search", domain, projectId ?? null, q, offset],
    queryFn: () =>
      get<SearchResponse>(
        `/api/search?q=${encodeURIComponent(q)}&limit=20&offset=${offset}${projectId ? `&project=${encodeURIComponent(projectId)}` : ""}`,
      ),
    enabled,
  });
  return (
    <Card title={projectId ? "Cari metadata proyek ini" : `Cari metadata di domain ${domain ?? "?"}`}>
      <label className="relative block">
        <span className="sr-only">Kata kunci</span>
        <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-ink-3" aria-hidden />
        <input
          className={inputClass + " pl-8"}
          placeholder="Cari proyek, rencana, task, keputusan, dokumen, komponen… (min. 2 huruf)"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <p className="mt-1 text-xs text-ink-3">
        {projectId
          ? "Record proyek ini ditambah record global platform (ADR, komponen); proyek dan domain lain tidak ditampilkan."
          : "Hanya metadata domain aktif; jumlah dan hasil domain lain tidak pernah ditampilkan."}
      </p>
      {enabled && res.isLoading && <Loading label="Mencari…" />}
      {res.error && <ErrorBox error={res.error} />}
      {enabled && res.data && (
        <div className="mt-3">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-2">
            <span>{res.data.total} hasil</span>
            {Object.entries(res.data.counts)
              .filter(([, n]) => n > 0)
              .map(([k, n]) => (
                <span key={k} className="rounded bg-surface-3 px-1.5 py-0.5">
                  {KIND_LABEL[k] ?? k}: {n}
                </span>
              ))}
            <Freshness at={res.data.checked_at} label="Dicari" />
          </div>
          {res.data.items.length === 0 ? (
            <p className="text-sm text-ink-3">Tidak ada hasil untuk “{q}”.</p>
          ) : (
            <ul className="divide-y divide-line rounded-md border border-line">
              {res.data.items.map((it) => (
                <li key={`${it.kind}:${it.id}`} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  <span className="w-24 shrink-0 text-xs uppercase tracking-wide text-ink-3">{KIND_LABEL[it.kind] ?? it.kind}</span>
                  <Link to={it.url} className="min-w-0 flex-1 truncate font-medium text-accent hover:underline">
                    {it.title}
                  </Link>
                  {it.subtitle && <span className="text-xs text-ink-3">{it.subtitle}</span>}
                  {it.status &&
                    (KIND_BADGE[it.kind] ? <ModuleBadge kind={KIND_BADGE[it.kind]!} value={it.status} /> : <StatusBadge status={it.status} />)}
                </li>
              ))}
            </ul>
          )}
          <div className="mt-2 flex gap-2">
            <button className="text-xs text-accent disabled:text-ink-3" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>
              ← Sebelumnya
            </button>
            <button className="text-xs text-accent disabled:text-ink-3" disabled={!res.data.has_more} onClick={() => setOffset(offset + 20)}>
              Berikutnya →
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}
