import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Button, Card, ErrorBox, Field, inputClass, Loading, Mono, Notice, Table } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import type { ListMeta } from "./types";

export interface IncidentView {
  id: string;
  title: string;
  status: string;
  source: string;
  source_ref: string | null;
  owner: string;
  domain: string | null;
  impact: string;
  root_cause: string;
  root_cause_evidence_refs: string[];
  corrective_actions: { description: string; ref?: string | null }[];
  regression_test_ids: string[];
  eval_case_refs: string[];
  follow_ups: { kind: string; id: string; status?: string }[];
  timeline: { at: string; kind: string; note: string; evidence_ids?: string[] }[];
  resolution_requirements: string[];
  created_at: string | null;
  resolved_at: string | null;
}

const REQUIREMENT_LABEL: Record<string, string> = {
  root_cause_with_evidence: "root cause dengan bukti tepercaya",
  corrective_action: "tindakan korektif",
  regression_test_or_eval_case: "regression test atau eval case",
};

const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

/** One incident: what is recorded and what the lawful follow-up steps are (§05 step 15, §16, §28, §33). */
export function IncidentDetail({ inc, isAdmin }: { inc: IncidentView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [tests, setTests] = useState("");
  const [caseTitle, setCaseTitle] = useState("");
  const [expected, setExpected] = useState("");
  const [components, setComponents] = useState("");
  const done = () => qc.invalidateQueries({ queryKey: ["ops-incidents"] });
  const regression = useMutation({
    mutationFn: () => post(`/api/ops/incidents/${inc.id}/regression`, { test_ids: csv(tests).filter((t) => !t.includes("#")), eval_case_refs: csv(tests).filter((t) => t.includes("#")) }),
    onSuccess: done,
  });
  const evalCase = useMutation({
    mutationFn: () => post<{ candidate_id: string }>(`/api/ops/incidents/${inc.id}/eval-case`, { title: caseTitle, expected }),
    onSuccess: done,
  });
  const followUp = useMutation({
    mutationFn: () => post<{ proposal_id: string }>(`/api/ops/incidents/${inc.id}/follow-up`, { component_ids: csv(components) }),
    onSuccess: done,
  });
  const resolve = useMutation({ mutationFn: () => post(`/api/ops/incidents/${inc.id}/resolve`), onSuccess: done });
  const resolved = inc.status === "RESOLVED";
  return (
    <div className="space-y-3 text-sm">
      <div>
        <strong>Root cause:</strong>{" "}
        {inc.root_cause ? (
          <>
            {inc.root_cause} <span className="text-xs text-ink-3">(bukti: {inc.root_cause_evidence_refs.join(", ")})</span>
          </>
        ) : (
          <span className="text-ink-3">UNKNOWN — belum didukung bukti</span>
        )}
      </div>
      <div>
        <strong>Regression / eval case:</strong>{" "}
        {[...inc.regression_test_ids, ...inc.eval_case_refs].map((t) => <Mono key={t}>{t}</Mono>)}
        {!inc.regression_test_ids.length && !inc.eval_case_refs.length && <span className="text-ink-3">belum ada</span>}
      </div>
      <div>
        <strong>Tindak lanjut:</strong>{" "}
        {inc.follow_ups.length ? inc.follow_ups.map((f) => <span key={f.id} className="mr-2">{f.kind} <Mono>{f.id.slice(0, 8)}</Mono></span>) : <span className="text-ink-3">belum ada</span>}
      </div>
      {inc.resolution_requirements.length > 0 && (
        <Notice tone="warn">Belum dapat ditutup: {inc.resolution_requirements.map((r) => REQUIREMENT_LABEL[r] ?? r).join(", ")}.</Notice>
      )}
      {!resolved && (
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="Tautkan regression test / eval case (tests/...py::test_x, suite#case)">
            <input className={inputClass} value={tests} disabled={!isAdmin} onChange={(e) => setTests(e.target.value)} />
            <Button className="mt-1 text-xs" disabled={!isAdmin || !csv(tests).length} busy={regression.isPending} onClick={() => regression.mutate()}>
              Tautkan
            </Button>
          </Field>
          <Field label="Ajukan kandidat eval case (direview manusia)">
            <input className={inputClass} placeholder="Judul" value={caseTitle} disabled={!isAdmin} onChange={(e) => setCaseTitle(e.target.value)} />
            <input className={`${inputClass} mt-1`} placeholder="Perilaku yang diharapkan" value={expected} disabled={!isAdmin} onChange={(e) => setExpected(e.target.value)} />
            <Button className="mt-1 text-xs" disabled={!isAdmin || caseTitle.trim().length < 5 || expected.trim().length < 10} busy={evalCase.isPending} onClick={() => evalCase.mutate()}>
              Ajukan eval case
            </Button>
          </Field>
          <Field label="Usulan perubahan untuk komponen (id, dipisah koma)">
            <input className={inputClass} value={components} disabled={!isAdmin} onChange={(e) => setComponents(e.target.value)} />
            <Button className="mt-1 text-xs" disabled={!isAdmin || !csv(components).length} busy={followUp.isPending} onClick={() => followUp.mutate()}>
              Ajukan usulan
            </Button>
          </Field>
        </div>
      )}
      {!resolved && (
        <Button disabled={!isAdmin || inc.resolution_requirements.length > 0} busy={resolve.isPending} onClick={() => resolve.mutate()}>
          Tutup insiden
        </Button>
      )}
      {[regression.error, evalCase.error, followUp.error, resolve.error].map((e, i) => (e ? <ErrorBox key={i} error={e} /> : null))}
    </div>
  );
}

export function IncidentsTab() {
  const { isAdmin } = useAuth();
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["ops-incidents"], queryFn: () => get<ListMeta & { items: IncidentView[] }>("/api/ops/incidents") });
  const selected = q.data?.items.find((i) => i.id === open) ?? null;
  return (
    <Card title="Insiden">
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : (
        <Table
          rows={q.data?.items ?? []}
          rowKey={(r) => r.id}
          onRowClick={(r) => setOpen(open === r.id ? null : r.id)}
          empty="Tidak ada insiden tercatat untuk domain ini"
          columns={[
            { header: "Insiden", cell: (r) => r.title },
            { header: "Status", cell: (r) => <OpsBadge status={r.status === "CONTAINED" ? "PAUSED" : r.status} label={r.status === "CONTAINED" ? "Terkendali" : undefined} /> },
            { header: "Sumber", cell: (r) => `${r.source}${r.source_ref ? ` · ${r.source_ref.slice(0, 8)}` : ""}` },
            { header: "Owner", cell: (r) => r.owner },
            { header: "Dibuka", cell: (r) => (r.created_at ? relativeTime(r.created_at) : "—") },
          ]}
        />
      )}
      {selected && (
        <div className="mt-3 rounded-md border border-line p-3">
          <h3 className="mb-2 font-semibold">{selected.title}</h3>
          <IncidentDetail inc={selected} isAdmin={isAdmin} />
        </div>
      )}
      <Provenance
        source={q.data?.source}
        checkedAt={q.data?.checked_at}
        evidenceLimit="Root cause hanya dengan bukti tepercaya; usulan dan eval case adalah draft untuk review manusia; dokumen 09-incidents dirender dari record ini."
      />
    </Card>
  );
}
