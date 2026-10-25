import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, inputClass, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { ClassificationBadge, ModuleBadge } from "./badges";
import { CLASSIFICATIONS, type ListResponse, type MemoryView, type ProjectView } from "./types";

const SOURCES: [string, string][] = [
  ["human_statement", "Pernyataan pemilik"],
  ["decision", "Keputusan (Decision ACTIVE)"],
  ["plan_version", "PlanVersion yang disetujui"],
  ["acceptance", "AcceptanceRecord ACCEPTED"],
  ["adr", "ADR ACCEPTED (proyek platform)"],
];

/** "a, b c" -> ["a", "b", "c"] (EvalRun ids). */
export function parseIds(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function MemoryRow({ m, isAdmin, onChanged }: { m: MemoryView; isAdmin: boolean; onChanged: () => void }) {
  // §41/§43: accepting memory is a system change; the owner may cite the evaluation runs that are evidence of
  // exactly this record (backend: COMPLETED, every gate PASS, context_params naming the record) - else NOT_EVALUATED
  const [evalRuns, setEvalRuns] = useState("");
  const act = useMutation({
    mutationFn: ({ action, rationale }: { action: "accept" | "reject" | "expire"; rationale: string }) => {
      const ids = action === "accept" ? parseIds(evalRuns) : [];
      return post(`/api/memory/${m.id}/${action}`, ids.length ? { rationale, eval_run_ids: ids } : { rationale });
    },
    onSuccess: onChanged,
  });
  const ask = (action: "accept" | "reject" | "expire") => {
    const label = action === "accept" ? "menerima" : action === "reject" ? "menolak" : "mengakhiri";
    const rationale = window.prompt(`Alasan ${label} memori ini (dicatat sebagai keputusan manusia):`, "");
    if (rationale === null) return;
    act.mutate({ action, rationale });
  };
  const prov = m.provenance as Record<string, unknown>;
  return (
    <li className="p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ModuleBadge kind="memory" value={m.status} />
        <span className="text-xs text-ink-3">{m.scope === "DOMAIN" ? "lintas proyek (domain sama)" : "proyek ini"}</span>
        <ClassificationBadge value={m.classification} />
        <span className="text-xs text-ink-3">
          sumber {String(prov.source_type ?? "?")}
          {prov.source_id ? ` · ${String(prov.source_id)}` : ""}
        </span>
        {m.expires_at && <span className="text-xs text-ink-3">berlaku s.d. {absoluteTime(m.expires_at)}</span>}
      </div>
      <p className="mt-1">{m.content}</p>
      <div className="mt-1 text-xs text-ink-3">
        {m.auth_decision_id ? (
          <>
            Keputusan <Mono>{m.auth_decision_id}</Mono>
          </>
        ) : (
          "Belum ada keputusan"
        )}
        {m.superseded_by && (
          <>
            {" "}
            · digantikan oleh <Mono>{m.superseded_by}</Mono>
          </>
        )}
      </div>
      {m.system_change && (
        // §41/§43: accepted memory reaches model prompts, so it is a system change - its evaluation state and
        // rollback target are stated, never implied
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-3" aria-label="System change memori">
          <StatusBadge
            status={m.system_change.evaluation === "EVALUATED" ? "PASS" : "NOT_TESTED"}
            label={m.system_change.evaluation === "EVALUATED" ? "System change dievaluasi" : "System change belum dievaluasi"}
          />
          {m.system_change.rollback_target && (
            <span>
              rollback: <Mono>{m.system_change.rollback_target}</Mono>
            </span>
          )}
        </div>
      )}
      {act.error ? (
        <div className="mt-2">
          <ErrorBox error={act.error} />
        </div>
      ) : null}
      {isAdmin && m.status === "PROPOSED" && (
        <div className="mt-2 max-w-xl">
          <Field
            label="ID EvalRun system-change (opsional)"
            hint="Kosongkan bila belum dievaluasi: penerimaan tercatat 'belum dievaluasi'. EvalRun harus COMPLETED dengan semua gate PASS dan context_params-nya memuat ID atau hash memori ini."
          >
            <input className={inputClass} value={evalRuns} onChange={(e) => setEvalRuns(e.target.value)} placeholder="uuid EvalRun, pisahkan koma" />
          </Field>
        </div>
      )}
      {isAdmin && (m.status === "PROPOSED" || m.status === "ACCEPTED") && (
        <div className="mt-2 flex flex-wrap gap-2">
          {m.status === "PROPOSED" && (
            <>
              <Button variant="primary" onClick={() => ask("accept")} busy={act.isPending}>
                Terima
              </Button>
              <Button onClick={() => ask("reject")} busy={act.isPending}>
                Tolak
              </Button>
            </>
          )}
          <Button variant="ghost" onClick={() => ask("expire")} busy={act.isPending}>
            Akhiri masa berlaku
          </Button>
        </div>
      )}
    </li>
  );
}

function ProposeForm({ project, onSaved, accepted }: { project: ProjectView; onSaved: () => void; accepted: MemoryView[] }) {
  const [content, setContent] = useState("");
  const [sourceType, setSourceType] = useState("human_statement");
  const [sourceId, setSourceId] = useState("");
  const [scope, setScope] = useState("PROJECT");
  const [classification, setClassification] = useState(project.data_classification);
  const [expires, setExpires] = useState("");
  const [supersedes, setSupersedes] = useState("");
  const sensitive = classification === "CONFIDENTIAL" || classification === "RESTRICTED";
  const save = useMutation({
    mutationFn: () =>
      post(`/api/projects/${project.id}/memory`, {
        content,
        source_type: sourceType,
        source_id: sourceType === "human_statement" ? null : sourceId.trim() || null,
        scope,
        classification,
        expires_at: expires ? new Date(expires).toISOString() : null,
        supersedes_id: supersedes || null,
      }),
    onSuccess: () => {
      setContent("");
      setSourceId("");
      setSupersedes("");
      onSaved();
    },
  });
  return (
    <Card title="Usulkan memori">
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="sm:col-span-2">
          <Field label="Fakta / keputusan" hint="Tanpa secret dan tanpa instruksi; isi seperti itu ditolak backend.">
            <textarea className={inputClass + " min-h-20"} value={content} onChange={(e) => setContent(e.target.value)} maxLength={4000} required />
          </Field>
        </div>
        <Field label="Sumber yang sudah diterima">
          <select className={inputClass} value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
            {SOURCES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        {sourceType !== "human_statement" && (
          <Field label="ID sumber">
            <input className={inputClass} value={sourceId} onChange={(e) => setSourceId(e.target.value)} required />
          </Field>
        )}
        <Field label="Cakupan" hint={sensitive ? "Data sensitif tidak boleh lintas proyek." : "DOMAIN = boleh dipakai proyek lain di domain yang sama."}>
          <select className={inputClass} value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="PROJECT">Proyek ini</option>
            <option value="DOMAIN" disabled={sensitive}>
              Lintas proyek (domain sama)
            </option>
          </select>
        </Field>
        <Field label="Klasifikasi">
          <select className={inputClass} value={classification} onChange={(e) => setClassification(e.target.value)}>
            {CLASSIFICATIONS.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </Field>
        <Field label="Berlaku sampai (opsional)">
          <input className={inputClass} type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
        <Field label="Menggantikan memori (opsional)">
          <select className={inputClass} value={supersedes} onChange={(e) => setSupersedes(e.target.value)}>
            <option value="">—</option>
            {accepted.map((m) => (
              <option key={m.id} value={m.id}>
                {m.content.slice(0, 60)}
              </option>
            ))}
          </select>
        </Field>
        {save.error ? (
          <div className="sm:col-span-2">
            <ErrorBox error={save.error} />
          </div>
        ) : null}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!content.trim()}>
            Usulkan
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function MemoryPanel({ project, isAdmin }: { project: ProjectView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["memory", project.id], queryFn: () => get<ListResponse<MemoryView>>(`/api/projects/${project.id}/memory`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["memory", project.id] });
  const items = q.data?.items ?? [];
  return (
    <div className="space-y-4">
      <Notice>
        Memori hanya berasal dari fakta/keputusan yang sudah diterima, diterima lewat keputusan manusia, dipartisi per domain, dan tidak pernah menjadi
        kebijakan. Data CONFIDENTIAL/RESTRICTED tidak dipakai lintas proyek.
      </Notice>
      {q.isLoading && <Loading />}
      {q.error ? <ErrorBox error={q.error} /> : null}
      {q.data &&
        (items.length === 0 ? (
          <EmptyState title="Belum ada memori proyek">{isAdmin ? "Usulkan fakta yang sudah diterima di bawah." : "Belum ada memori."}</EmptyState>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
            {items.map((m) => (
              <MemoryRow key={m.id} m={m} isAdmin={isAdmin} onChanged={refresh} />
            ))}
          </ul>
        ))}
      {isAdmin && <ProposeForm project={project} onSaved={refresh} accepted={items.filter((m) => m.status === "ACCEPTED")} />}
    </div>
  );
}
