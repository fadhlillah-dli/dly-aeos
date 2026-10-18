import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { Card, EmptyState, ErrorBox, inputClass, Loading, Mono } from "../../components/ui";
import type { SearchHit } from "./types";

const TYPE_LABEL: Record<string, string> = {
  project: "Proyek",
  plan: "Rencana",
  task: "Task",
  document: "Dokumen",
  run: "Run",
  component: "Komponen",
  adr: "ADR",
  proposal: "Usulan",
};

export function SearchBox({ domain }: { domain: string }) {
  const [text, setText] = useState("");
  const [term, setTerm] = useState("");
  const q = useQuery({
    queryKey: ["cc-search", domain, term],
    queryFn: () => get<{ items: SearchHit[]; truncated: boolean }>(`/api/control-center/search?q=${encodeURIComponent(term)}`),
    enabled: term.length >= 2,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTerm(text.trim());
  };
  return (
    <Card title="Cari metadata (domain aktif saja)">
      <form onSubmit={submit} className="flex gap-2" role="search">
        <input
          className={inputClass}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Nama proyek, rencana, task, komponen, ADR…"
          aria-label="Kata kunci pencarian"
          minLength={2}
          maxLength={100}
        />
        <button type="submit" className="inline-flex items-center gap-1 rounded-md border border-line px-3 text-sm hover:bg-surface-3">
          <Search className="h-4 w-4" aria-hidden /> Cari
        </button>
      </form>
      <div className="mt-3">
        {term.length > 0 && term.length < 2 && <p className="text-sm text-ink-3">Minimal 2 karakter.</p>}
        {q.isFetching && <Loading label="Mencari…" />}
        {q.error ? <ErrorBox error={q.error} /> : null}
        {q.data && q.data.items.length === 0 && <EmptyState title="Tidak ada hasil di domain ini" />}
        {q.data && q.data.items.length > 0 && (
          <ul className="space-y-1 text-sm">
            {q.data.items.map((h) => (
              <li key={`${h.type}:${h.id}`} className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-surface-3 px-1.5 text-xs">{TYPE_LABEL[h.type] ?? h.type}</span>
                <Link to={h.link} className="font-medium hover:underline">
                  {h.title}
                </Link>
                {h.subtitle ? <Mono>{h.subtitle}</Mono> : null}
              </li>
            ))}
          </ul>
        )}
        {q.data?.truncated && <p className="mt-1 text-xs text-ink-3">Hasil dipotong; persempit kata kunci.</p>}
      </div>
    </Card>
  );
}
