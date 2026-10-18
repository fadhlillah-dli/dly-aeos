import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RotateCcw, Send, Trash2, XCircle } from "lucide-react";
import { useState } from "react";
import { ApiError, post } from "../../api/client";
import { operationsFor } from "../../components/planning/planState";
import type { OperationCatalog, PlanDetail, VersionSummary } from "../../components/planning/types";
import { Button, Card, ErrorBox, Field, inputClass, KeyValue, Mono, Notice } from "../../components/ui";

export interface TargetRow {
  provider: string;
  identifiers: string;
}

const PLACEHOLDER: Record<string, string> = {
  "google-drive": "root_id=<ID folder Drive proyek>",
  "google-docs": "root_id=<ID folder Drive proyek>",
  "github-issues": "repo=<owner>/<nama>",
  "github-repo": "repo=<owner>/<nama>",
  confluence: "space=<KEY>; parent_id=<ID parent>",
  jira: "project_key=<KEY>",
  bitbucket: "repo=<workspace>/<nama>",
};

/** "a=1; b=2" -> {provider, a: "1", b: "2"}; rows without identifiers are dropped (backend rejects them too). */
export type Target = { provider: string } & Record<string, string>;

export function parseTargets(rows: TargetRow[]): Target[] {
  const out: Target[] = [];
  for (const r of rows) {
    const ids: Record<string, string> = {};
    for (const part of r.identifiers.split(/[;\n]/)) {
      const [k, ...rest] = part.split("=");
      const key = k?.trim();
      const value = rest.join("=").trim();
      if (key && value) ids[key] = value;
    }
    if (r.provider && Object.keys(ids).length) out.push({ provider: r.provider, ...ids });
  }
  return out;
}

type Action = "APPROVE_AND_PUBLISH" | "APPROVE" | "REQUEST_REVISION" | "REJECT";

export function DecisionPanel({ plan, version, catalog }: { plan: PlanDetail; version: VersionSummary; catalog: OperationCatalog }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState<TargetRow[]>([{ provider: catalog.providers[0] ?? "", identifiers: "" }]);
  const [rationale, setRationale] = useState("");
  const [rerun, setRerun] = useState(true);
  const targets = parseTargets(rows);
  const publishTargets = targets.filter((t) => (catalog.publish[t.provider] ?? []).length);
  const decide = useMutation({
    mutationFn: (action: Action) =>
      post<{ plan: { state: string } }>(`/api/plans/${plan.id}/decision`, {
        action,
        version_id: version.id,
        version_hash: version.manifest_hash,
        targets: action === "APPROVE" ? targets.filter((t) => !(catalog.publish[t.provider] ?? []).length) : targets,
        rationale,
        rerun: action === "REQUEST_REVISION" ? rerun : false,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plan", plan.id] }),
  });
  const stale = decide.error instanceof ApiError && decide.error.code === "STALE_PLAN_VERSION" ? decide.error.details : null;
  const canApprove = plan.allowed.approve;
  const canRevise = plan.allowed.revise_or_reject;
  const opsPublish = operationsFor("APPROVE_AND_PUBLISH", targets, catalog);
  const opsApprove = operationsFor("APPROVE", targets.filter((t) => !(catalog.publish[t.provider] ?? []).length), catalog);

  return (
    <Card title="Keputusan pemilik">
      <div className="grid gap-4">
        <Notice tone="info">
          Keputusan Anda dicatat sebagai Decision yang terikat pada versi, hash, target dan operasi berikut. Verdict reviewer bukan persetujuan. Edit atau
          revisi baru membatalkan persetujuan versi lama.
        </Notice>
        <KeyValue
          items={[
            ["Versi yang diputuskan", <span key="v">v{version.version} ({version.source})</span>],
            ["Manifest hash", <Mono key="h">{version.manifest_hash}</Mono>],
            ["Domain", plan.domain],
          ]}
        />
        <div>
          <div className="mb-1 text-sm font-medium">Target eksternal (scope persis)</div>
          <div className="grid gap-2">
            {rows.map((r, i) => (
              <div key={i} className="grid gap-2 sm:grid-cols-[180px_1fr_auto]">
                <select
                  aria-label="Provider"
                  className={inputClass}
                  value={r.provider}
                  onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, provider: e.target.value } : x)))}
                >
                  {catalog.providers.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
                <input
                  aria-label="Identifier target"
                  className={inputClass}
                  placeholder={PLACEHOLDER[r.provider] ?? "key=value"}
                  value={r.identifiers}
                  onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, identifiers: e.target.value } : x)))}
                />
                <Button type="button" variant="ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Hapus target">
                  <Trash2 className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            ))}
            <div>
              <Button type="button" onClick={() => setRows([...rows, { provider: catalog.providers[0] ?? "", identifiers: "" }])}>
                Tambah target
              </Button>
            </div>
          </div>
        </div>
        <div className="grid gap-2 text-sm sm:grid-cols-2">
          <div className="rounded-md border border-line p-2">
            <div className="font-medium">Approve &amp; Publish akan mengikat operasi:</div>
            <div className="mt-1 flex flex-wrap gap-1">{opsPublish.map((o) => <Mono key={o}>{o}</Mono>)}</div>
            {!publishTargets.length && <div className="mt-1 text-xs text-warn">Butuh minimal satu target publikasi.</div>}
          </div>
          <div className="rounded-md border border-line p-2">
            <div className="font-medium">Approve (tanpa publikasi) mengikat:</div>
            <div className="mt-1 flex flex-wrap gap-1">{opsApprove.map((o) => <Mono key={o}>{o}</Mono>)}</div>
            <div className="mt-1 text-xs text-ink-3">Merge ke main dan produksi tetap tindakan manusia (tidak pernah diikat).</div>
          </div>
        </div>
        <Field label="Rationale" hint="Wajib untuk Request Revision dan Reject (min. 10 karakter).">
          <textarea className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={rerun} onChange={(e) => setRerun(e.target.checked)} /> Setelah Request Revision, jalankan ulang empat tahap
          perencanaan (membuat versi baru, tidak mengubah versi lama)
        </label>
        {stale && (
          <div className="rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn" role="alert">
            Versi yang Anda tinjau sudah usang. Versi terbaru: v{String(stale.latest_version)} <Mono>{String(stale.latest_hash)}</Mono>. Muat ulang dan tinjau
            revisi terbaru sebelum memutuskan.
          </div>
        )}
        {!stale && <ErrorBox error={decide.error} />}
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" disabled={!canApprove || !publishTargets.length} busy={decide.isPending} onClick={() => decide.mutate("APPROVE_AND_PUBLISH")}>
            <Send className="h-4 w-4" aria-hidden /> Approve &amp; Publish
          </Button>
          <Button disabled={!canApprove} busy={decide.isPending} onClick={() => decide.mutate("APPROVE")}>
            <CheckCircle2 className="h-4 w-4" aria-hidden /> Approve
          </Button>
          <Button disabled={!canRevise || rationale.trim().length < 10} busy={decide.isPending} onClick={() => decide.mutate("REQUEST_REVISION")}>
            <RotateCcw className="h-4 w-4" aria-hidden /> Request Revision
          </Button>
          <Button
            variant="danger"
            disabled={!canRevise || rationale.trim().length < 10}
            busy={decide.isPending}
            onClick={() => {
              if (window.confirm("Tolak rencana ini? Status REJECTED bersifat final.")) decide.mutate("REJECT");
            }}
          >
            <XCircle className="h-4 w-4" aria-hidden /> Reject
          </Button>
        </div>
      </div>
    </Card>
  );
}
