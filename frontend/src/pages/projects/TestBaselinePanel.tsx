import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { Button, Card, ErrorBox, Freshness, KeyValue, Mono, Notice } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { Status } from "../evidence/labels";
import { Outcome } from "../evidence/outcomes";

/** inventory.tests.baseline / GET /api/projects/{id}/test-baseline (backend verification.project_baseline). */
export interface TestBaseline {
  status?: string; // PASS | FAIL | TIMED_OUT | TAMPERED | BLOCKED | NOT_RUN | UNKNOWN
  commit?: string | null;
  reason?: string;
  evidence_id?: string;
  evidence_level?: string;
  checked_at?: string | null;
  suite_identity?: { kind?: string; selection?: string[]; files?: number; files_hash?: string; mark_expr?: string; config?: string } | null;
  passed?: number | null;
  failed?: number | null;
  skipped?: number | null;
  failing_sample?: string[];
  javascript?: { status?: string | null; passed?: number | null; failed?: number | null };
  runner?: string | null;
  isolation?: string | null;
  source?: string;
}

const count = (v: number | null | undefined) => (v === null || v === undefined ? "?" : String(v));

/** The facts of one baseline (shared by the project card and the inventory facts). */
export function BaselineFacts({ b }: { b: TestBaseline }) {
  const ran = !!b.evidence_id;
  return (
    <div className="space-y-2 text-sm" data-testid="test-baseline">
      <div className="flex flex-wrap items-center gap-2">
        <Outcome status={b.status ?? "UNKNOWN"} label={b.status === "NOT_RUN" ? "Baseline belum dijalankan" : undefined} />
        {b.commit && (
          <span className="text-xs">
            commit <Mono>{shortHash(b.commit, 12)}</Mono>
          </span>
        )}
        {b.checked_at && <Freshness at={b.checked_at} label="Dijalankan" staleAfterSeconds={7 * 86400} />}
      </div>
      {b.reason && <p className="text-xs text-ink-2 wrap-anywhere">{b.reason}</p>}
      {ran && (
        <KeyValue
          items={[
            ["Tes Python", `${count(b.passed)} lulus · ${count(b.failed)} gagal · ${count(b.skipped)} dilewati`],
            [
              "Tes JavaScript",
              b.javascript?.status ? (
                <span key="js" className="flex flex-wrap items-center gap-2">
                  <Status status={b.javascript.status} /> {b.javascript.passed !== undefined && b.javascript.passed !== null && `${b.javascript.passed} lulus · ${count(b.javascript.failed)} gagal`}
                </span>
              ) : (
                "—"
              ),
            ],
            [
              "Identitas suite",
              b.suite_identity ? (
                <span key="si" className="text-xs">
                  {b.suite_identity.selection?.join(", ") || "—"} · {b.suite_identity.files ?? "?"} berkas · <Mono>{shortHash(b.suite_identity.files_hash ?? null, 12)}</Mono>
                </span>
              ) : (
                "—"
              ),
            ],
            [
              "Runner",
              <span key="rn" className="flex flex-wrap items-center gap-2">
                <Mono>{b.runner ?? "?"}</Mono>
                {b.isolation && <Status status={b.isolation} />}
              </span>,
            ],
            [
              "Bukti",
              <Link key="ev" to={`/evidence?id=${encodeURIComponent(b.evidence_id!)}`} className="text-accent hover:underline">
                {b.evidence_level ? `${b.evidence_level} · ` : ""}
                <Mono>{shortHash(b.evidence_id!, 8)}</Mono>
              </Link>,
            ],
          ]}
        />
      )}
      {(b.failing_sample ?? []).length > 0 && (
        <div>
          <div className="text-xs font-medium">Contoh tes gagal (pengukuran, bukan putusan)</div>
          <ul className="mt-0.5 space-y-0.5 text-xs">
            {(b.failing_sample ?? []).map((t) => (
              <li key={t}>
                <Mono>{t}</Mono>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * §05 step 6 "baseline tes": the project's own existing tests at its inventoried commit, run by the verifier runner
 * inside the tamper window and recorded as a trusted TEST_REPORT. A failing test in the baseline is a measurement.
 */
export function TestBaselinePanel({ projectId, isAdmin }: { projectId: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["test-baseline", projectId], queryFn: () => get<TestBaseline>(`/api/projects/${encodeURIComponent(projectId)}/test-baseline`) });
  const [confirming, setConfirming] = useState(false);
  const run = useMutation({
    mutationFn: () => post<TestBaseline>(`/api/projects/${encodeURIComponent(projectId)}/test-baseline`, {}),
    onSuccess: (data) => {
      setConfirming(false);
      qc.setQueryData(["test-baseline", projectId], data);
      qc.invalidateQueries({ queryKey: ["test-baseline", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: () => setConfirming(false),
  });
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-2">
          <FlaskConical className="h-4 w-4" aria-hidden /> Baseline tes proyek
        </span>
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-ink-3">
          Menjalankan tes yang sudah ada di repository pada commit inventaris (runner verifier + jendela tamper). Hasilnya dikutip inventaris dan
          perencanaan; tes yang gagal dicatat apa adanya.
        </p>
        <ErrorBox error={q.error} />
        {q.data && <BaselineFacts b={q.data} />}
        {isAdmin && (
          <div className="space-y-2">
            {!confirming ? (
              <Button onClick={() => setConfirming(true)} disabled={run.isPending}>
                <FlaskConical className="h-4 w-4" aria-hidden /> Jalankan baseline tes
              </Button>
            ) : (
              <div role="group" aria-label="Konfirmasi baseline tes" className="space-y-2 rounded-md bg-surface-2 p-2 text-xs">
                <p>
                  Kode proyek akan dijalankan oleh runner verifier pada commit <Mono>{shortHash(q.data?.commit ?? null, 12)}</Mono>. Proses ini sinkron dan
                  bisa lama (batas bawaan 30 menit; melewati batas dicatat TIMED_OUT).
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" busy={run.isPending} onClick={() => run.mutate()}>
                    Konfirmasi &amp; jalankan
                  </Button>
                  <Button variant="ghost" disabled={run.isPending} onClick={() => setConfirming(false)}>
                    Batal
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
        <ErrorBox error={run.error} />
        <div aria-live="polite" className="text-xs text-ink-2">
          {run.isPending ? "Baseline tes sedang berjalan…" : run.data ? `Baseline tercatat: ${run.data.status ?? "UNKNOWN"}.` : ""}
        </div>
        {q.data?.status === "NOT_RUN" && !isAdmin && (
          <Notice tone="info">Baseline dijalankan SUPER_ADMIN (atau CLI: aeos verify project-baseline &lt;project id&gt;).</Notice>
        )}
      </div>
    </Card>
  );
}
