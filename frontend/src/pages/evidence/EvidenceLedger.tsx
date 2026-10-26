import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { RetentionBadge } from "../../components/control_center/RetentionBadge";
import { EmptyState, ErrorBox, Freshness, Loading, Mono, Table, inputClass } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";
import { evidenceApi, type EvidenceItem } from "./api";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { LEVEL_SHORT, LevelBadge, Status, TrustBadge } from "./labels";

const KINDS = ["VERIFICATION_REPORT", "TEST_REPORT", "SCOPE_CHECK", "DIFF", "LINT_REPORT", "SECRET_SCAN", "CI_STATUS",
  "PROVIDER_CAPABILITY", "REVIEW_PACKAGE", "PR_BUNDLE", "GOVERNANCE_HANDOFF", "PROBE", "LOG", "SCREENSHOT",
  "RUNTIME_OBSERVATION", "SMOKE_TEST", "ARTIFACT_DIGEST", "PROVIDER_ENFORCEMENT_TEST"];

export function EvidenceLedger({ runId }: { runId?: string }) {
  const { domain } = useAuth();
  const [level, setLevel] = useState("");
  const [trusted, setTrusted] = useState("");
  const [kind, setKind] = useState("");
  // Deep link (e.g. from Live Work "Bukti"): /evidence?id=<evidence id> opens that record's drawer.
  const [params] = useSearchParams();
  const [open, setOpen] = useState<string | null>(runId ? null : params.get("id"));
  const filters = { level: level || undefined, trusted: trusted || undefined, kind: kind || undefined, run_id: runId };
  const q = useQuery({ queryKey: ["evidence", domain, filters], queryFn: () => evidenceApi.list(filters) });
  const env = (e: EvidenceItem, k: string) => (e.environment?.[k] as string | undefined) ?? null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs">
          <span className="mb-1 block text-ink-3">Level</span>
          <select aria-label="Filter level" className={inputClass} value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">Semua level</option>
            {Object.entries(LEVEL_SHORT).map(([k, v]) => (
              <option key={k} value={k}>
                {k} · {v}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs">
          <span className="mb-1 block text-ink-3">Asal</span>
          <select aria-label="Filter asal" className={inputClass} value={trusted} onChange={(e) => setTrusted(e.target.value)}>
            <option value="">Semua asal</option>
            <option value="true">Kolektor tepercaya</option>
            <option value="false">Tidak tepercaya (klaim)</option>
          </select>
        </label>
        <label className="text-xs">
          <span className="mb-1 block text-ink-3">Jenis</span>
          <select aria-label="Filter jenis" className={inputClass} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">Semua jenis</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        {q.data && <Freshness at={q.data.checked_at} label="Dimuat" />}
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && q.data.items.length === 0 && (
        <EmptyState title="Belum ada bukti untuk filter ini">
          Bukti tepercaya dibuat oleh verifier/collector, bukan oleh worker. Jalankan{" "}
          <Mono>aeos verify run &lt;run_id&gt;</Mono> untuk run berstatus REVIEW, atau{" "}
          <Mono>aeos delivery capabilities --record</Mono> untuk observasi provider.
        </EmptyState>
      )}
      {q.data && q.data.items.length > 0 && (
        <Table
          rows={q.data.items}
          rowKey={(r) => r.id}
          onRowClick={(r) => setOpen(r.id)}
          columns={[
            { header: "Klaim", cell: (r) => <span className="line-clamp-2 max-w-xs break-words">{r.claim}</span> },
            { header: "Jenis", cell: (r) => <Mono>{r.kind}</Mono> },
            { header: "Level", cell: (r) => <LevelBadge level={r.evidence_level} explanation={r.level_explanation} /> },
            { header: "Asal", cell: (r) => <div className="space-y-1"><TrustBadge trusted={r.trusted} /><div className="text-xs text-ink-3">{r.origin}</div></div> },
            { header: "Kolektor", cell: (r) => <span className="mono text-xs break-all">{r.collector_identity}</span> },
            { header: "Hash", cell: (r) => <Mono>{shortHash(r.content_hash)}</Mono> },
            {
              header: "Commit / config",
              cell: (r) => (
                <span className="mono text-xs">
                  {shortHash(env(r, "commit"), 8)} / {shortHash(env(r, "config_hash"), 8)}
                </span>
              ),
            },
            { header: "Mode", cell: (r) => (r.mode === "LIVE" ? <span className="text-xs text-ink-3">LIVE</span> : <Status status={r.mode} />) },
            { header: "Waktu", cell: (r) => <span className="text-xs" title={r.created_at}>{relativeTime(r.created_at)}</span> },
            { header: "Retensi", cell: (r) => <RetentionBadge marker={r.retention} /> },
          ]}
        />
      )}
      {open && <EvidenceDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
