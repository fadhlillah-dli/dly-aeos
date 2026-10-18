/** GET /api/search (ADR-0024): the one metadata search contract used by the Control Center UI. */
export interface SearchItem {
  kind: string;
  id: string;
  title: string;
  subtitle: string | null;
  snippet: string;
  project_id: string | null;
  status: string | null;
  url: string;
  updated_at?: string | null;
}

export interface SearchResult {
  items: SearchItem[];
  total: number;
  counts: Record<string, number>;
  limit: number;
  offset: number;
  has_more: boolean;
  query: string;
  domain: string;
  project_id: string | null;
  source: string;
  checked_at: string;
  mode: string;
}

export const SEARCH_KIND_LABEL: Record<string, string> = {
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

export function searchUrl(term: string, { limit, project }: { limit: number; project?: string | null }): string {
  const qs = new URLSearchParams({ q: term, limit: String(limit) });
  if (project) qs.set("project", project);
  return `/api/search?${qs.toString()}`;
}
