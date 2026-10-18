import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { formatDuration, STAGE_ORDER } from "../../components/planning/planState";
import { RunStatusBadge } from "../../components/planning/PlanStateBadge";
import type { ChallengeFindings, FinalReview, StageView } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Mono } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";

function findingCount(f: ChallengeFindings | null): number {
  if (!f) return 0;
  return f.contradictions.length + f.missing_dependencies.length + f.risks.length + f.scope_issues.length + f.other_findings.length;
}

/** §15 four-stage planning timeline: role, model requested vs resolved, status, duration, outputs. */
export function StageTimeline({
  stages,
  findings,
  verdict,
  verdictEffective,
}: {
  stages: StageView[];
  findings: ChallengeFindings | null;
  verdict: FinalReview | null;
  verdictEffective: string | null;
}) {
  const ordered = STAGE_ORDER.map((s) => stages.find((x) => x.stage === s)).filter((s): s is StageView => !!s);
  return (
    <ol className="grid gap-3 lg:grid-cols-4">
      {ordered.map((s, idx) => {
        const r = s.latest;
        const mismatch = r && r.model_resolved && r.model_matches_policy === false;
        return (
          <li key={s.stage} className="rounded-lg border border-line bg-surface p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-ink-3">Tahap {idx + 1}</div>
              <RunStatusBadge status={r?.status ?? null} />
            </div>
            <div className="mt-1 font-medium">{s.role}</div>
            <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-2 gap-y-1 text-xs">
              <dt className="text-ink-3">Model diminta</dt>
              <dd>
                <Mono>{s.model}</Mono> <span className="text-ink-3">(keluarga {s.family})</span>
              </dd>
              <dt className="text-ink-3">Model teresolusi</dt>
              <dd className="flex items-center gap-1">
                {r?.model_resolved ? <Mono>{r.model_resolved}</Mono> : <StatusBadge status="UNKNOWN" />}
                {r?.model_resolved && r.model_matches_policy && <CheckCircle2 className="h-3.5 w-3.5 text-ok" aria-label="sesuai policy" />}
                {mismatch && <AlertTriangle className="h-3.5 w-3.5 text-bad" aria-label="tidak sesuai policy" />}
              </dd>
              <dt className="text-ink-3">Mode</dt>
              <dd>{r ? <StatusBadge status={r.mode === "NATIVE" ? "LIVE" : r.mode === "MOCK" ? "MOCK" : "PENDING"} label={r.mode} /> : "—"}</dd>
              <dt className="text-ink-3">Durasi</dt>
              <dd>{formatDuration(r?.duration_ms)}</dd>
              <dt className="text-ink-3">Engine</dt>
              <dd>{r?.engine_version ? <Mono>{r.engine_version}</Mono> : "—"}</dd>
              <dt className="text-ink-3">Hash in/out</dt>
              <dd>
                <Mono>{shortHash(r?.input_hash, 8)}</Mono> → <Mono>{shortHash(r?.output_hash, 8)}</Mono>
              </dd>
              <dt className="text-ink-3">Selesai</dt>
              <dd>{relativeTime(r?.finished_at)}</dd>
            </dl>
            {s.stage === "CHALLENGE_REVIEW" && findings && (
              <div className="mt-2 text-xs">
                {findingCount(findings)} temuan · keparahan <StatusBadge status={findings.overall_severity === "LOW" || findings.overall_severity === "INFO" ? "PENDING" : "DEGRADED"} label={findings.overall_severity} />
              </div>
            )}
            {s.stage === "FINAL_REVIEW" && verdict && (
              <div className="mt-2 text-xs">
                Verdict: <StatusBadge status={verdictEffective ?? verdict.verdict} /> <span className="text-ink-3">(bukan persetujuan)</span>
              </div>
            )}
            {r?.error && (
              <div className="mt-2 rounded bg-bad-bg p-2 text-xs text-bad">
                <Mono>{r.error_code}</Mono> {r.error}
              </div>
            )}
            {s.history.length > 1 && <div className="mt-2 text-xs text-ink-3">{s.history.length} percobaan tercatat (riwayat dipertahankan)</div>}
          </li>
        );
      })}
    </ol>
  );
}
