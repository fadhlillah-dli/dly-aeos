import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, inputClass, Loading, Mono } from "../../components/ui";
import { SEARCH_KIND_LABEL, searchUrl, type SearchResult } from "../../lib/search";

const PAGE = 20;

/**
 * Project-scoped or domain-wide metadata search on the Ringkasan page. Same contract as the header
 * search (GET /api/search, ADR-0024): rows, counts and pagination are filtered by the backend.
 */
export function SearchBox({ domain }: { domain: string }) {
  const { projectId } = useAuth();
  const [text, setText] = useState("");
  const [term, setTerm] = useState("");
  const [onlyProject, setOnlyProject] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  const scoped = onlyProject && !!projectId;
  const q = useQuery({
    queryKey: ["search", domain, term, scoped ? projectId : null, limit],
    queryFn: () => get<SearchResult>(searchUrl(term, { limit, project: scoped ? projectId : null })),
    enabled: term.length >= 2,
    placeholderData: (prev) => prev,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setLimit(PAGE);
    setTerm(text.trim());
  };
  const counts = Object.entries(q.data?.counts ?? {}).filter(([, n]) => n > 0);
  return (
    <Card title={`Cari metadata (domain ${domain} saja)`}>
      <form onSubmit={submit} className="flex gap-2" role="search" aria-label="Pencarian metadata ringkasan">
        <input
          className={inputClass}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Nama proyek, rencana, task, komponen, ADR…"
          aria-label="Kata kunci pencarian"
          minLength={2}
          maxLength={200}
        />
        <button type="submit" className="inline-flex shrink-0 items-center gap-1 rounded-md border border-line px-3 text-sm hover:bg-surface-3">
          <Search className="h-4 w-4" aria-hidden /> Cari
        </button>
      </form>
      <label className="mt-2 flex items-center gap-1.5 text-xs text-ink-2">
        <input type="checkbox" checked={scoped} disabled={!projectId} onChange={(e) => setOnlyProject(e.target.checked)} />
        Hanya proyek aktif {projectId ? "" : "(pilih proyek di header)"}
      </label>
      <div className="mt-3">
        {term.length > 0 && term.length < 2 && <p className="text-sm text-ink-3">Minimal 2 karakter.</p>}
        {q.isFetching && !q.data && <Loading label="Mencari…" />}
        {q.error ? <ErrorBox error={q.error} /> : null}
        {q.data && q.data.items.length === 0 && <EmptyState title={`Tidak ada hasil di domain ${q.data.domain}`} />}
        {counts.length > 0 && (
          <p className="mb-2 text-xs text-ink-3">
            {q.data?.total} hasil: {counts.map(([k, n]) => `${SEARCH_KIND_LABEL[k] ?? k} ${n}`).join(" · ")}
          </p>
        )}
        {q.data && q.data.items.length > 0 && (
          <ul className="space-y-1 text-sm">
            {q.data.items.map((h) => (
              <li key={`${h.kind}:${h.id}`} className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="rounded bg-surface-3 px-1.5 text-xs">{SEARCH_KIND_LABEL[h.kind] ?? h.kind}</span>
                <Link to={h.url} className="min-w-0 font-medium hover:underline">
                  {h.title}
                </Link>
                {h.subtitle ? <Mono>{h.subtitle}</Mono> : null}
                {h.status ? <StatusBadge status={h.status} /> : null}
              </li>
            ))}
          </ul>
        )}
        {q.data?.has_more && (
          <Button className="mt-2 text-xs" busy={q.isFetching} onClick={() => setLimit((l) => Math.min(l + PAGE, 100))} disabled={limit >= 100}>
            Tampilkan lebih banyak
          </Button>
        )}
        {q.data && <p className="mt-2 text-[11px] text-ink-3">Sumber: {q.data.source} · hanya metadata, tanpa isi dokumen/secret.</p>}
      </div>
    </Card>
  );
}
