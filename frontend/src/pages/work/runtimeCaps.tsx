import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, KeyValue, Mono } from "../../components/ui";
import type { RunRuntimeCaps } from "../../components/workforce/types";
import { shortHash } from "../../lib/format";
import { neutralisationSpec } from "./IntegrityHold";

// Labels for what the worker runtime records on a run (backend aeos.worker_runtime.executor). Every value keeps
// text + icon + colour; an absent or UNKNOWN value is never shown as a pass.

/** Quarantine reason rules (WorkerRun.quarantine_reasons[].rule) with an Indonesian explanation. */
const QUARANTINE_RULES: Record<string, string> = {
  "db-trust-table-modified": "Baris tabel DB tepercaya diubah proses lain saat worker berjalan (HIGH, memicu integrity hold)",
  "db-trust-check-unknown": "Pemeriksaan tabel DB tepercaya tidak dapat dievaluasi (UNKNOWN, bukan lulus): perlu review khusus",
  "controller-modified": "File controller/config/bytecode AEOS berubah saat worker berjalan (HIGH, memicu integrity hold)",
  "git-control-modified": "File kontrol git kandidat diubah (hook/config); git runtime ditolak",
  "outside-worktree-attempt": "Panggilan alat di luar worktree",
  "candidate-loaded-into-controller": "Kode kandidat termuat ke proses controller",
  "diff-unavailable": "Diff tidak dapat dihitung",
  "protected-path": "Menyentuh path terlindungi (review khusus)",
};

export function quarantineRuleLabel(rule: string): string {
  return QUARANTINE_RULES[rule] ?? rule;
}

/** controller-modified detail is "<change>: <what the runtime did>"; "incomplete" = the snapshot cap was reached. */
export function QuarantineReason({ q }: { q: { path: string; rule: string; detail: string } }) {
  const incomplete = q.rule === "controller-modified" && q.detail.startsWith("incomplete");
  const n = q.rule === "controller-modified" ? neutralisationSpec(q.detail) : null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Mono>{q.path}</Mono> — {quarantineRuleLabel(q.rule)} (<Mono>{q.rule}</Mono>)
      {incomplete && <StatusBadge status="UNKNOWN" label="Perbandingan tidak lengkap (batas snapshot)" />}
      {n && <StatusBadge status={n.status} label={n.label} />}
      {q.detail ? <span className="wrap-anywhere">({q.detail})</span> : null}
    </span>
  );
}

/** BrokerRequest.reasoning_effort: what the broker sent upstream (null = recorded before the column existed). */
export function reasoningSentLabel(v: string | null | undefined): { status: string; label: string } {
  if (!v) return { status: "UNKNOWN", label: "Tidak tercatat (UNKNOWN)" };
  if (v === "NOT_SENT") return { status: "NOT_APPLICABLE", label: "Tidak dikirim (default provider)" };
  return { status: "CONFIGURED", label: `${v} (konfigurasi rute)` };
}

/** BrokerRequest.reasoning_requested: what the worker asked for - always dropped, never forwarded. */
export function reasoningRequestedLabel(effort: string | null | undefined, requested: string | null | undefined): { status: string; label: string } {
  if (!requested) return effort ? { status: "NOT_APPLICABLE", label: "Pekerja tidak meminta" } : { status: "UNKNOWN", label: "Tidak tercatat (UNKNOWN)" };
  if (requested === "UNRECOGNISED") return { status: "DENIED", label: "Nilai tak dikenal - dibuang (teks pekerja tidak disimpan)" };
  return { status: "DENIED", label: `${requested} - dibuang, tidak diteruskan` };
}

/** worker_runtime.alert codes (severity HIGH) in the live feed and the run timeline. */
const ALERT_CODES: Record<string, string> = {
  DB_TRUST_TAMPER: "Tabel DB tepercaya diubah proses lain saat worker berjalan",
  INTEGRITY_HOLD: "Integrity hold: semua worker di semua domain diblokir sampai pemilik merekonsiliasi",
  CONTROLLER_MODIFIED: "File controller AEOS berubah saat worker berjalan",
};

export function alertText(payload: Record<string, unknown>): string {
  const code = typeof payload.code === "string" ? payload.code : "UNKNOWN";
  const what = ALERT_CODES[code] ?? `Peringatan runtime ${code}`;
  const n = typeof payload.rows === "number" ? ` (${payload.rows} baris)` : typeof payload.findings === "number" ? ` (${payload.findings} temuan)` : typeof payload.files === "number" ? ` (${payload.files} file)` : "";
  return `${what}${n}`;
}

export function isHighAlert(kind: string, payload: Record<string, unknown>): boolean {
  return kind === "worker_runtime.alert" && payload.severity === "HIGH";
}

const DB_WINDOW: Record<string, { status: string; label: string }> = {
  PASS: { status: "PASS", label: "Tidak ada perubahan asing pada tabel tepercaya" },
  FAIL: { status: "FAILED", label: "Perubahan asing pada tabel tepercaya" },
  UNKNOWN: { status: "UNKNOWN", label: "Tidak dapat dibandingkan (bukan lulus)" },
  NOT_APPLICABLE: { status: "NOT_APPLICABLE", label: "Tidak berlaku (DB tidak di-mount ke container)" },
};

function dbWindow(w: NonNullable<RunRuntimeCaps["db_trust_window"]>) {
  const spec = DB_WINDOW[w.status ?? ""] ?? { status: "UNKNOWN", label: `Status ${w.status ?? "UNKNOWN"}` };
  const counts = [
    w.foreign_count !== undefined ? `${w.foreign_count} baris asing` : null,
    w.tolerated_count !== undefined ? `${w.tolerated_count} ditoleransi (pembukuan broker/sesi)` : null,
    w.tables !== undefined ? `${w.tables} tabel` : null,
    w.rows !== undefined ? `${w.rows} baris dibandingkan` : null,
    w.own_writes !== undefined ? `${w.own_writes} tulis controller sendiri` : null,
  ].filter(Boolean);
  return (
    <span key="db" className="space-y-1">
      <StatusBadge status={spec.status} label={spec.label} title={w.status ?? "UNKNOWN"} />
      {counts.length > 0 && <span className="block text-xs">{counts.join(" · ")}</span>}
      {w.error && <span className="block text-xs text-warn">{w.error}</span>}
      {w.note && <span className="block text-xs text-ink-3">{w.note}</span>}
    </span>
  );
}

function cpuCap(c: NonNullable<RunRuntimeCaps["cpu_cap"]>) {
  return (
    <span key="cpu" className="space-y-1">
      {c.enforced ? (
        <StatusBadge status="CONFIGURED" label={`Ditegakkan: ${c.by ?? "UNKNOWN"}`} />
      ) : (
        <StatusBadge status="DEGRADED" label={`Tidak ditegakkan: ${c.by ?? "UNKNOWN"}`} />
      )}
      <span className="block text-xs">
        maks {c.max_cpus ?? "UNKNOWN"} CPU{c.job_cpu_rate !== undefined && c.job_cpu_rate !== null ? ` · CPU rate job ${c.job_cpu_rate}` : ""}
      </span>
    </span>
  );
}

/** A runtime evidence ref: ``evidence:<id>`` (ledger record) or ``local-unverified:<file>`` (collector unavailable). */
export function evidenceRefLink(ref: string): { id: string | null; text: string } {
  if (ref.startsWith("evidence:")) return { id: ref.slice("evidence:".length), text: ref.slice("evidence:".length) };
  return { id: null, text: ref };
}

function commandLogs(c: NonNullable<RunRuntimeCaps["command_logs"]>) {
  if (c.rejected || c.ignored)
    return (
      <span key="cl" className="space-y-1">
        <StatusBadge status="BLOCKED" label="Log perintah tidak diambil" />
        <span className="block text-xs">{c.rejected ?? c.ignored}</span>
      </span>
    );
  if (c.files === undefined) return <StatusBadge key="cl" status="NOT_APPLICABLE" label="Tidak ada log perintah runner" />;
  return (
    <span key="cl" className="space-y-1">
      <StatusBadge status="UNVERIFIED" label="Dari sisi worker (E0, tidak tepercaya)" />
      <span className="block text-xs">
        {c.files} perintah · {c.bytes ?? 0} byte{c.truncated ? " · terpotong" : ""}
        {c.skipped ? ` · ${c.skipped} dilewati` : ""} · {c.removed_from_candidate ? "dikeluarkan dari kandidat" : "TIDAK dikeluarkan dari kandidat"}
      </span>
      {c.evidence_ref &&
        (() => {
          const ref = evidenceRefLink(c.evidence_ref);
          return ref.id ? (
            <Link className="text-xs text-accent underline" to={`/evidence?id=${encodeURIComponent(ref.id)}`}>
              Bukti log perintah <Mono>{shortHash(ref.id, 12)}</Mono>
            </Link>
          ) : (
            <span className="block text-xs text-warn">
              Bukti log perintah hanya tersimpan lokal (collector tidak tersedia, UNVERIFIED): <Mono>{ref.text}</Mono>
            </span>
          );
        })()}
    </span>
  );
}

function contextPack(p: NonNullable<RunRuntimeCaps["context_pack"]>) {
  const b = p.build ?? null;
  return (
    <span key="cp" className="space-y-1">
      {p.used ? (
        <StatusBadge status="CURRENT" label={`ContextPack dipakai (${p.items ?? "?"} item, budget ${p.token_budget ?? "UNKNOWN"} token)`} />
      ) : p.pack_id ? (
        // a pack existed but was refused (stale/expired, or built for another base commit): never given to the worker
        <StatusBadge status="STALE" label={`ContextPack ditolak (${p.freshness && p.freshness !== "CURRENT" ? p.freshness : "base commit lain"}); hanya konteks TaskSpec`} />
      ) : (
        <StatusBadge status="NOT_APPLICABLE" label="Tanpa ContextPack (hanya konteks TaskSpec)" />
      )}
      {p.reason && <span className="block text-xs text-ink-3">{p.reason}</span>}
      {p.pack_hash && (
        <span className="block text-xs">
          pack <Mono>{shortHash(p.pack_hash, 16)}</Mono>
          {p.freshness ? ` · ${p.freshness}` : ""}
        </span>
      )}
      {p.artifacts_in_pack !== undefined && (
        <span className="block text-xs">
          Artefak input {p.artifacts_in_pack ? "di dalam pack" : "tidak di pack"}
          {p.artifacts_omitted ? ` · ${p.artifacts_omitted} artefak dihilangkan (budget)` : ""}
        </span>
      )}
      {b && (
        <span className="block text-xs">
          Dibangun runtime: {b.built ? `ya (${b.items ?? "?"} item, estimasi ${b.token_estimate ?? "?"}/${b.token_budget ?? "?"} token, ${b.omitted ?? 0} dihilangkan)` : `tidak - ${b.reason ?? "UNKNOWN"}`}
          {b.warnings && b.warnings.length > 0 ? ` · peringatan: ${b.warnings.join("; ")}` : ""}
        </span>
      )}
    </span>
  );
}

/** WorkerRun.caps recorded by the runtime: tamper windows, the integrity hold, the CPU cap, runner logs, context. */
export function RuntimeIntegrity({ caps }: { caps: Record<string, unknown> }) {
  const c = caps as RunRuntimeCaps;
  const items: [string, ReactNode][] = [];
  if (c.integrity_hold !== undefined)
    items.push([
      "Integrity hold",
      c.integrity_hold ? (
        <StatusBadge key="ih" status="BLOCKED" label="Run ini memicu integrity hold (semua domain diblokir)" />
      ) : (
        <StatusBadge key="ih" status="NOT_APPLICABLE" label="Tidak memicu integrity hold" />
      ),
    ]);
  if (c.controller_integrity)
    items.push([
      "Integritas controller",
      c.controller_integrity.changed ? (
        <StatusBadge key="ci" status="FAILED" label={`${c.controller_integrity.changed} temuan dari ${c.controller_integrity.checked_files ?? "?"} file diperiksa`} />
      ) : !c.controller_integrity.checked_files ? (
        // no file compared (no snapshot recorded): nothing was checked, so this is never a pass
        <StatusBadge key="ci" status="UNKNOWN" label="Tidak ada file yang dibandingkan (bukan lulus)" />
      ) : (
        <StatusBadge key="ci" status="PASS" label={`Tidak ada perubahan (${c.controller_integrity.checked_files} file diperiksa)`} />
      ),
    ]);
  if (c.db_trust_window) items.push(["Jendela DB tepercaya", dbWindow(c.db_trust_window)]);
  if (c.cpu_cap) items.push(["Batas CPU", cpuCap(c.cpu_cap)]);
  if (c.command_logs) items.push(["Log perintah shell (runner AEOS)", commandLogs(c.command_logs)]);
  if (c.context_pack) items.push(["ContextPack", contextPack(c.context_pack)]);
  if (!items.length) return <EmptyState title="Belum dicatat runtime">Run belum selesai, atau dibuat sebelum pemeriksaan ini ada (UNKNOWN, bukan lulus).</EmptyState>;
  return <KeyValue items={items} />;
}
