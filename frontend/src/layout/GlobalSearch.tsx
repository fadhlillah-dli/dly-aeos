import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Search, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { get } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorBox } from "../components/ui";
import { SEARCH_KIND_LABEL as KIND_LABEL, searchUrl, type SearchResult } from "../lib/search";

/**
 * Global metadata search (§35 "project-scoped/global metadata search yang diotorisasi") over
 * GET /api/search. The backend scopes every row and count to the active domain and the user's
 * projects; this box only renders what came back (hiding names on screen alone is not enough).
 */
export function GlobalSearch({ className }: { className?: string }) {
  const { domain } = useAuth();
  const [text, setText] = useState("");
  const [term, setTerm] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  // Debounced type-ahead; Enter searches immediately.
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 300);
    return () => clearTimeout(t);
  }, [text]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const q = useQuery({
    queryKey: ["global-search", domain, term],
    queryFn: () => get<SearchResult>(searchUrl(term, { limit: 8 })),
    enabled: !!domain && term.length >= 2,
    staleTime: 30_000,
    retry: false,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTerm(text.trim());
    setOpen(true);
  };
  const reset = () => {
    setOpen(false);
    setText("");
    setTerm("");
  };
  const showPanel = open && text.trim().length > 0;

  return (
    <div ref={box} className={clsx("relative", className)}>
      <form role="search" onSubmit={submit} aria-label="Pencarian metadata">
        <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden />
        <input
          type="search"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          placeholder={domain ? `Cari di ${domain}: proyek, rencana, komponen…` : "Pilih domain dahulu"}
          aria-label="Cari metadata"
          maxLength={200}
          disabled={!domain}
          className="w-full rounded-md border border-line bg-surface-2 py-1.5 pl-8 pr-7 text-sm placeholder:text-ink-3 focus:border-accent focus:bg-surface focus:outline-none"
        />
        {text && (
          <button type="button" onClick={reset} aria-label="Hapus pencarian" className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-ink-3 hover:bg-surface-3">
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        )}
      </form>
      {showPanel && (
        <div
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[70vh] overflow-y-auto rounded-lg border border-line bg-surface p-2 shadow-xl md:left-auto md:w-[28rem]"
          aria-live="polite"
        >
          {text.trim().length < 2 ? (
            <p className="px-2 py-1 text-sm text-ink-3">Minimal 2 karakter.</p>
          ) : q.isFetching && !q.data ? (
            <p className="px-2 py-1 text-sm text-ink-3" role="status">
              Mencari…
            </p>
          ) : q.error ? (
            <ErrorBox error={q.error} />
          ) : q.data && q.data.items.length === 0 ? (
            <p className="px-2 py-1 text-sm text-ink-3">Tidak ada hasil di domain {q.data.domain}.</p>
          ) : q.data ? (
            <>
              <ul className="space-y-0.5">
                {q.data.items.map((h) => (
                  <li key={`${h.kind}:${h.id}`}>
                    <Link to={h.url} onClick={reset} className="flex min-w-0 items-start gap-2 rounded-md px-2 py-1.5 hover:bg-surface-2">
                      <span className="mt-0.5 shrink-0 rounded bg-surface-3 px-1.5 text-[11px] text-ink-2">{KIND_LABEL[h.kind] ?? h.kind}</span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{h.title}</span>
                        {h.subtitle && <span className="mono block truncate text-[11px] text-ink-3">{h.subtitle}</span>}
                      </span>
                      {h.status && <StatusBadge status={h.status} className="shrink-0" />}
                    </Link>
                  </li>
                ))}
              </ul>
              <p className="mt-1 border-t border-line px-2 pt-1.5 text-[11px] text-ink-3">
                {q.data.total} hasil metadata di domain {q.data.domain}
                {q.data.has_more ? " — menampilkan 8 teratas; persempit kata kunci" : ""}. Sumber: {q.data.source}.
              </p>
            </>
          ) : null}
        </div>
      )}
    </div>
  );
}
