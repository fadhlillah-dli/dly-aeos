import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, Loading, Mono, Table } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { ClassificationBadge, ModuleBadge } from "./badges";
import type { ContextPackView, ListResponse, ProjectView } from "./types";

const ROLES = ["default", "planner", "feature-implementer", "reviewer", "security-reviewer", "docs-writer", "researcher"];
const TRUST_LABEL: Record<string, [string, string]> = {
  POLICY_READ_ONLY: ["CONFIGURED", "Kebijakan (read-only)"],
  AEOS_RECORD: ["LIVE", "Record AEOS"],
  REPO_DATA: ["NOT_CHECKED", "Data repo"],
  MEMORY_DATA: ["NOT_CHECKED", "Memori (bukan kebijakan)"],
  UNTRUSTED_DATA: ["UNVERIFIED", "Data tak tepercaya"],
  DATA: ["UNVERIFIED", "Riset (DATA)"],
};

const splitList = (s: string) =>
  s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

function PackDetail({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["context-pack", id], queryFn: () => get<{ context_pack: ContextPackView }>(`/api/context-packs/${id}`) });
  const [openItem, setOpenItem] = useState<string | null>(null);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const pack = q.data!.context_pack;
  return (
    <div className="space-y-3 rounded-md border border-line bg-surface-2 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ModuleBadge kind="freshness" value={pack.freshness} />
        <span>
          base <Mono>{pack.base_commit ? shortHash(pack.base_commit, 12) : "—"}</Mono>
        </span>
        <span>
          hash <Mono>{shortHash(pack.pack_hash, 12)}</Mono>
        </span>
        <span>
          {pack.token_estimate}/{pack.token_budget} token (estimasi chars/4)
        </span>
        <ClassificationBadge value={pack.classification} />
        {pack.checked_at && <Freshness at={pack.checked_at} />}
      </div>
      {pack.stale_reason && (
        <div className="flex items-start gap-1 text-warn">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden /> {pack.stale_reason} — bangun pack baru sebelum dipakai.
        </div>
      )}
      {pack.warnings.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-warn">
          {pack.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <ul className="divide-y divide-line rounded border border-line bg-surface">
        {pack.items.map((it) => {
          const [st, label] = TRUST_LABEL[it.trust] ?? ["UNKNOWN", it.trust];
          const prov = it.provenance as Record<string, unknown>;
          return (
            <li key={it.id} className="p-2">
              <button className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpenItem(openItem === it.id ? null : it.id)}>
                <span className="w-20 shrink-0 text-xs uppercase text-ink-3">{it.kind}</span>
                <span className="min-w-0 flex-1 truncate font-medium">{it.title}</span>
                <StatusBadge status={st} label={label} />
                <span className="text-xs text-ink-3">{it.tokens} tok</span>
                {it.redacted && <StatusBadge status="DEGRADED" label="diredaksi" />}
                {it.truncated && <StatusBadge status="PARTIAL" label="dipotong" />}
              </button>
              {openItem === it.id && (
                <div className="mt-2 space-y-1 text-xs">
                  <div className="text-ink-2">
                    ref <Mono>{it.ref}</Mono> · hash <Mono>{shortHash(it.hash, 12)}</Mono> · {it.classification}
                  </div>
                  <div className="text-ink-3">
                    sumber: {String(prov.source ?? "—")}
                    {prov.commit ? ` · commit ${shortHash(String(prov.commit), 12)}` : ""}
                    {prov.path ? ` · ${String(prov.path)}` : ""}
                    {Array.isArray(prov.lines) ? ` · baris ${(prov.lines as number[]).join("–")}` : ""}
                  </div>
                  {it.content && <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap rounded border border-line bg-surface-2 p-2">{it.content}</pre>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {pack.omitted.length > 0 && (
        <p className="text-xs text-ink-3">
          {pack.omitted.length} item tidak dimasukkan karena anggaran token: {pack.omitted.slice(0, 5).map((o) => o.ref).join(", ")}
          {pack.omitted.length > 5 ? " …" : ""}
        </p>
      )}
      <p className="text-xs text-ink-3">Forbidden scope: {pack.forbidden_paths.slice(0, 12).join(", ")}</p>
    </div>
  );
}

function BuildForm({ project, onBuilt }: { project: ProjectView; onBuilt: (id: string) => void }) {
  const [role, setRole] = useState("default");
  const [query, setQuery] = useState("");
  const [paths, setPaths] = useState("");
  const [forbidden, setForbidden] = useState("");
  const [budget, setBudget] = useState(8000);
  const [repositoryId, setRepositoryId] = useState(project.repositories[0]?.id ?? "");
  const [baseCommit, setBaseCommit] = useState("");
  const build = useMutation({
    mutationFn: () =>
      post<{ context_pack: ContextPackView }>(`/api/projects/${project.id}/context-packs`, {
        role_key: role,
        query: splitList(query),
        paths: splitList(paths),
        forbidden_paths: splitList(forbidden),
        token_budget: budget,
        repository_id: repositoryId || null,
        base_commit: baseCommit.trim() || null,
      }),
    onSuccess: (res) => onBuilt(res.context_pack.id),
  });
  return (
    <Card title="Bangun ContextPack">
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          build.mutate();
        }}
      >
        <Field label="Peran">
          <select className={inputClass} value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </Field>
        <Field label="Repository">
          <select className={inputClass} value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)}>
            <option value="">(otomatis)</option>
            {project.repositories.map((r) => (
              <option key={r.id} value={r.id}>
                {r.full_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Kata kunci / simbol" hint="Pisahkan dengan koma; dipakai untuk git grep dan pencarian simbol.">
          <input className={inputClass} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="compute_total, InvoiceBuilder" />
        </Field>
        <Field label="Commit dasar (opsional)" hint="Default HEAD checkout; selalu disimpan sebagai SHA persis.">
          <input className={inputClass} value={baseCommit} onChange={(e) => setBaseCommit(e.target.value)} />
        </Field>
        <Field label="Berkas yang disertakan (glob)" hint="mis. src/*.py, docs/*.md">
          <input className={inputClass} value={paths} onChange={(e) => setPaths(e.target.value)} />
        </Field>
        <Field label="Forbidden scope tambahan (glob)" hint="Default selalu: .env*, kunci, credential, file DB">
          <input className={inputClass} value={forbidden} onChange={(e) => setForbidden(e.target.value)} />
        </Field>
        <Field label="Anggaran token">
          <input className={inputClass} type="number" min={1024} max={200000} value={budget} onChange={(e) => setBudget(Number(e.target.value))} />
        </Field>
        {build.error ? (
          <div className="sm:col-span-2">
            <ErrorBox error={build.error} />
          </div>
        ) : null}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={build.isPending}>
            Bangun pack
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function ContextPanel({ project, isAdmin }: { project: ProjectView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["context-packs", project.id],
    queryFn: () => get<ListResponse<ContextPackView>>(`/api/projects/${project.id}/context-packs`),
  });
  const [open, setOpen] = useState<string | null>(null);
  const packs = list.data?.items ?? [];
  return (
    <div className="space-y-4">
      {list.isLoading && <Loading />}
      {list.error ? <ErrorBox error={list.error} /> : null}
      {list.data &&
        (packs.length === 0 ? (
          <EmptyState title="Belum ada ContextPack">
            {isAdmin ? "Bangun pack untuk task/peran di bawah; konteks diambil dari commit yang diketahui." : "Belum ada pack yang dibangun."}
          </EmptyState>
        ) : (
          <Table
            rows={packs}
            rowKey={(p) => p.id}
            onRowClick={(p) => setOpen(open === p.id ? null : p.id)}
            columns={[
              { header: "Dibuat", cell: (p) => absoluteTime(p.created_at) },
              { header: "Peran", cell: (p) => p.role_key ?? "—" },
              { header: "Base", cell: (p) => <Mono>{p.base_commit ? shortHash(p.base_commit, 10) : "—"}</Mono> },
              { header: "Item", cell: (p) => `${p.item_count} (${p.kinds.join(", ")})` },
              { header: "Token", cell: (p) => `${p.token_estimate}/${p.token_budget}` },
              { header: "Kesegaran", cell: (p) => (p.stale ? <ModuleBadge kind="freshness" value="STALE" /> : <StatusBadge status="NOT_CHECKED" label="Buka untuk cek" />) },
            ]}
          />
        ))}
      {open && <PackDetail id={open} />}
      {isAdmin && (
        <BuildForm
          project={project}
          onBuilt={(id) => {
            qc.invalidateQueries({ queryKey: ["context-packs", project.id] });
            setOpen(id);
          }}
        />
      )}
    </div>
  );
}
