import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, Pause, Play, Power, PowerOff, Radio, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, EmptyState, ErrorBox, Freshness, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { ConcurrencyPanel } from "../../components/workforce/ConcurrencyPanel";
import { ConfirmAction } from "../../components/workforce/ConfirmAction";
import { ExecState, ModeBadge, SeparateStatuses, costText, shortId } from "../../components/workforce/status";
import type { RunList, RunRow, WorkforceSummary } from "../../components/workforce/types";
import { useLiveInvalidation } from "../../components/workforce/useLive";
import { absoluteTime, relativeTime } from "../../lib/format";

const STATE_ORDER = ["QUEUED", "READY", "LEASED", "RUNNING", "REVIEW", "BLOCKED", "DONE", "FAILED", "CANCELLED"];

function capsText(row: RunRow): string {
  const c = row.caps ?? {};
  const parts: string[] = [];
  if (c.max_requests !== undefined) parts.push(`req ≤ ${String(c.max_requests)}`);
  if (c.max_tokens !== undefined) parts.push(`token ≤ ${String(c.max_tokens)}`);
  if (c.max_cost_usd !== undefined) parts.push(`biaya ≤ $${String(c.max_cost_usd)}`);
  if (c.timeout_seconds !== undefined) parts.push(`waktu ≤ ${String(c.timeout_seconds)}s`);
  return parts.join(" · ") || "—";
}

function StreamFlag({ status, lastSeenAt }: { status: string; lastSeenAt: string | null }) {
  const live = status === "live";
  return (
    <span className="inline-flex items-center gap-1 text-xs" title={absoluteTime(lastSeenAt)}>
      <Radio className={live ? "h-3.5 w-3.5 text-ok" : "h-3.5 w-3.5 text-warn"} aria-hidden />
      {live ? "Stream terhubung" : status === "connecting" ? "Menghubungkan stream…" : "Stream terputus"}
      {lastSeenAt && <span className="text-ink-3">· terakhir {relativeTime(lastSeenAt)}</span>}
    </span>
  );
}

function SummaryCards({ s }: { s: WorkforceSummary }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Card title="Scheduler">
        <div className="flex flex-wrap gap-1.5">
          <StatusBadge status={s.scheduler.enabled ? "ENABLED" : "DISABLED"} label={s.scheduler.enabled ? "Loop aktif" : "Loop nonaktif"} />
          {s.scheduler.paused && <StatusBadge status="BLOCKED" label="Dijeda" />}
        </div>
        <p className="mt-2 text-xs text-ink-3">
          Batas worker aktif: <strong>{s.scheduler.concurrency_cap}</strong>
          {s.scheduler.cap_decision_id
            ? " (dengan keputusan pemilik)"
            : s.scheduler.cap_source && s.scheduler.cap_source !== "PILOT_DEFAULT"
              ? " (keputusan pemilik dibatasi budget resource)"
              : " (default pilot)"}
          {s.scheduler.paused && s.scheduler.paused_reason ? ` · alasan jeda: ${s.scheduler.paused_reason}` : ""}
        </p>
        {s.scheduler.cap_reason && <p className="mt-1 text-xs text-ink-3">{s.scheduler.cap_reason}</p>}
        {!s.scheduler.enabled && (
          <p className="mt-1 text-xs text-ink-3">
            Loop nonaktif: task READY tidak di-admit otomatis (sekali jalan: <Mono>aeos work run-once</Mono>).
          </p>
        )}
      </Card>
      <Card title="Task per state (LIVE)">
        <ul className="flex flex-wrap gap-1.5">
          {STATE_ORDER.map((st) => (
            <li key={st}>
              <StatusBadge status={st} label={`${st} ${s.task_counts[st] ?? 0}`} />
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-ink-3">
          Run aktif: {s.active_runs}
          {s.demo_runs > 0 && ` · baris DEMO: ${s.demo_runs} (tidak dihitung)`}
        </p>
      </Card>
      <Card title="Sandbox & engine">
        <div className="flex flex-wrap gap-1.5">
          <StatusBadge status={s.sandbox.status} label={`Sandbox: ${s.sandbox.status === "READY" ? `siap (${s.sandbox.kind})` : "terblokir"}`} />
          <StatusBadge status={s.engine.status === "INSTALLED" ? "INSTALLED" : s.engine.status} label={`OpenCode: ${s.engine.status}${s.engine.version ? ` ${s.engine.version}` : ""}`} />
          <StatusBadge status="NOT-PROVEN" label="Fungsi engine: belum terbukti" />
        </div>
        {s.sandbox.kind === "process" && <p className="mt-2 text-xs text-warn">Process sandbox LEMAH: tanpa penegakan egress (pengecualian pemilik).</p>}
      </Card>
      <Card title="Biaya & kuota worker">
        <p className="text-sm">
          Biaya tercatat: <strong>{s.cost.cost_known_usd === null ? "UNKNOWN" : `$${s.cost.cost_known_usd.toFixed(4)}`}</strong>
        </p>
        {s.cost.cost_status === "UNKNOWN" && (
          <div className="mt-1">
            <StatusBadge status="UNKNOWN" label="Total biaya belum diketahui (sebagian tanpa biaya dari provider)" />
          </div>
        )}
        <p className="text-xs text-ink-3">
          {s.cost.requests} request · {s.cost.input_tokens + s.cost.output_tokens} token · {s.cost.cost_unknown_entries} entri tanpa biaya (UNKNOWN)
        </p>
        <ul className="mt-1 flex flex-wrap gap-1">
          {s.cost.quota.slice(0, 4).map((q) => (
            <li key={`${q.provider}-${q.scope}`}>
              <StatusBadge status={q.state === "EXHAUSTED" ? "EXPIRED" : q.state} label={`${q.scope}: ${q.state}`} />
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export default function WorkPage() {
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const runs = useQuery({ queryKey: ["runs", domain], queryFn: () => get<RunList>("/api/runs"), refetchInterval: 15_000 });
  const summary = useQuery({
    queryKey: ["workforce-summary", domain],
    queryFn: () => get<WorkforceSummary>("/api/workforce/summary"),
    refetchInterval: 15_000,
  });
  const stream = useLiveInvalidation([["runs", domain], ["workforce-summary", domain]]);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["runs", domain] });
    qc.invalidateQueries({ queryKey: ["workforce-summary", domain] });
  };
  const rows = runs.data?.items ?? [];
  const hasDemo = rows.some((r) => r.mode !== "LIVE");
  const paused = summary.data?.scheduler.paused ?? false;
  const loopEnabled = summary.data?.scheduler.enabled ?? false;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Pekerjaan Live"
        question="Apa yang sedang dikerjakan setiap agent?"
        source="WorkerRun / TaskExecution / Lease (scheduler deterministik) + usage dari broker"
        checkedAt={runs.data?.checked_at ?? null}
        evidenceLimit="Heartbeat = liveness, bukan kemajuan/kualitas. Status worker, verifier, review, acceptance dan merge terpisah."
        relatedDecisionsHref="/decisions?target_type=workforce"
        mode={runs.data?.mode}
        actions={
          <div className="flex flex-wrap items-start gap-2">
            {summary.data &&
              (loopEnabled ? (
                <ConfirmAction
                  label="Nonaktifkan loop scheduler"
                  confirmText="Loop latar tidak lagi meng-admit task READY. Run yang berjalan tidak dihentikan (gunakan Batalkan)."
                  isAdmin={isAdmin}
                  icon={<PowerOff className="h-4 w-4" aria-hidden />}
                  onConfirm={async (reason) => {
                    await post("/api/workforce/scheduler", { enabled: false, reason });
                    refresh();
                  }}
                />
              ) : (
                <ConfirmAction
                  label="Aktifkan loop scheduler"
                  confirmText="Loop latar akan meng-admit task READY melalui admission (rencana disetujui, CAN_RUN_WORKER, route, sandbox, budget, verifier). Mengaktifkan loop tidak memulai run apa pun dengan sendirinya."
                  isAdmin={isAdmin}
                  icon={<Power className="h-4 w-4" aria-hidden />}
                  onConfirm={async (reason) => {
                    await post("/api/workforce/scheduler", { enabled: true, reason });
                    refresh();
                  }}
                />
              ))}
            {paused ? (
              <ConfirmAction
                label="Lanjutkan penjadwalan"
                confirmText="Scheduler akan kembali mengizinkan task READY (tetap melalui admission)."
                isAdmin={isAdmin}
                icon={<Play className="h-4 w-4" aria-hidden />}
                onConfirm={async (reason) => {
                  await post("/api/workforce/resume", { reason });
                  refresh();
                }}
              />
            ) : (
              <ConfirmAction
                label="Jeda penjadwalan"
                confirmText="Tidak ada run baru yang dimulai. Run yang berjalan tidak dihentikan (gunakan Batalkan)."
                isAdmin={isAdmin}
                icon={<Pause className="h-4 w-4" aria-hidden />}
                onConfirm={async (reason) => {
                  await post("/api/workforce/pause", { reason });
                  refresh();
                }}
              />
            )}
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <StreamFlag status={stream.status} lastSeenAt={stream.lastSeenAt} />
        <Freshness at={runs.data?.checked_at} staleAfterSeconds={60} label="Data run diperiksa" />
      </div>
      {stream.status !== "live" && stream.status !== "connecting" && (
        <Notice tone="warn">
          Stream event terputus. Status di bawah adalah observasi terakhir dan dapat usang — perlakukan sebagai UNKNOWN sampai stream
          tersambung kembali (halaman tetap memuat ulang tiap 15 detik).
        </Notice>
      )}
      {hasDemo && (
        <Notice tone="mock">
          Baris berlabel <strong>DEMO</strong> adalah fixture untuk pratinjau UI, bukan pekerjaan nyata: tidak dihitung sebagai run aktif,
          biaya, atau hasil yang diterima. Hapus dengan <Mono>aeos work demo-fixtures --remove</Mono>.
        </Notice>
      )}

      {summary.isLoading ? <Loading /> : summary.error ? <ErrorBox error={summary.error} /> : summary.data && <SummaryCards s={summary.data} />}

      <ConcurrencyPanel domain={domain} isAdmin={isAdmin} />

      {summary.data && summary.data.blockers.length > 0 && (
        <Card title={`Task menunggu / terblokir (${summary.data.blockers.length})`}>
          <ul className="space-y-1.5 text-sm">
            {summary.data.blockers.map((b) => (
              <li key={b.task_id} className="flex flex-wrap items-start gap-2">
                <ExecState state={b.state} uiState={b.ui_state} />
                <span className="font-medium">{b.task_key}</span>
                <Mono>{b.reason_code || "—"}</Mono>
                <span className="text-ink-2">{b.reason}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {summary.data && summary.data.sandbox.status !== "READY" && (
        <Notice tone="warn">
          <span className="inline-flex items-center gap-1 font-medium">
            <ShieldAlert className="h-4 w-4" aria-hidden /> Worker tidak dapat berjalan: sandbox yang disyaratkan belum tersedia.
          </span>
          <ul className="mt-1 list-disc pl-5">
            {summary.data.sandbox.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <div className="mt-1">Langkah: {summary.data.sandbox.remedies.join(" · ")} (cek: <Mono>aeos worker status</Mono>)</div>
        </Notice>
      )}

      <Card title="Run">
        {runs.isLoading ? (
          <Loading />
        ) : runs.error ? (
          <ErrorBox error={runs.error} />
        ) : rows.length === 0 ? (
          <EmptyState title="Belum ada run pada domain ini">
            Run dibuat scheduler setelah rencana DISETUJUI dijalankan (halaman Rencana → <b>Jalankan rencana</b>, atau{" "}
            <Mono>aeos work execute &lt;plan_id&gt; --as &lt;admin&gt; --domain &lt;domain&gt;</Mono>), agent pelaksana diaktifkan (Organisasi &amp; Peran
            → <b>Aktifkan</b>), CAN_RUN_WORKER siap dan route worker disetujui. Untuk pratinjau tampilan: <Mono>aeos work demo-fixtures</Mono>.
          </EmptyState>
        ) : (
          <Table
            rows={rows}
            rowKey={(r) => r.run_id}
            columns={[
              {
                header: "Proyek / domain",
                cell: (r) => (
                  <div className="min-w-32">
                    <div className="font-medium">{r.project.name ?? r.project.key ?? shortId(r.project.id)}</div>
                    <div className="text-xs text-ink-3">{r.domain}</div>
                  </div>
                ),
              },
              {
                header: "Agent / peran",
                cell: (r) => (
                  <div className="min-w-36">
                    <Mono>{r.agent_key ?? "—"}</Mono>
                    <div className="text-xs text-ink-3">{r.role_name ?? r.role_key}</div>
                  </div>
                ),
              },
              {
                header: "Run",
                cell: (r) => (
                  <div className="min-w-28 space-y-1">
                    <Link to={r.links.run} className="text-accent underline">
                      <Mono>{shortId(r.run_id)}</Mono>
                    </Link>
                    <div className="text-xs text-ink-3">
                      {r.run_type} · attempt {r.attempt}
                    </div>
                    <ModeBadge mode={r.mode} />
                  </div>
                ),
              },
              {
                header: "Task / fitur",
                cell: (r) => (
                  <div className="min-w-36">
                    <div>{r.task.title ?? r.task.key ?? "—"}</div>
                    <div className="text-xs text-ink-3">
                      {r.task.key}
                      {r.task.feature_key ? ` · ${r.task.feature_key}` : ""}
                    </div>
                  </div>
                ),
              },
              {
                header: "Route model / engine",
                cell: (r) => (
                  <div className="min-w-40 text-xs">
                    <Mono>{r.route.key ?? "—"}</Mono>
                    <div>model: {r.route.model_resolved}</div>
                    <div>
                      {r.route.engine ?? "—"} {r.route.engine_version}
                    </div>
                  </div>
                ),
              },
              { header: "State", cell: (r) => <ExecState state={r.execution_state} uiState={r.ui_state} /> },
              {
                header: "Alasan tunggu / blokir",
                cell: (r) => <div className="max-w-56 text-xs text-ink-2">{r.wait_reason || (r.reason_code ? <Mono>{r.reason_code}</Mono> : "—")}</div>,
              },
              {
                header: "Aksi teramati",
                cell: (r) => (
                  <div className="max-w-56 text-xs" title={r.current_action_source}>
                    {r.current_action || "—"}
                    <div className="text-ink-3">sumber: {r.current_action_source || "UNKNOWN"}</div>
                  </div>
                ),
              },
              { header: "Mulai", cell: (r) => <span className="text-xs" title={absoluteTime(r.started_at)}>{relativeTime(r.started_at)}</span> },
              {
                header: "Heartbeat",
                cell: (r) => (
                  <div className="text-xs" title={absoluteTime(r.last_heartbeat_at)}>
                    {relativeTime(r.last_heartbeat_at)}
                    {r.heartbeat_stale && (
                      <div className="mt-0.5">
                        <StatusBadge status="STALE" label="Heartbeat usang" />
                      </div>
                    )}
                  </div>
                ),
              },
              {
                header: "File disentuh",
                cell: (r) => (
                  <div className="max-w-48 text-xs">
                    <div>{r.touched_count} file</div>
                    {r.touched_paths.slice(0, 3).map((p) => (
                      <div key={p} className="truncate text-ink-3" title={p}>
                        {p}
                      </div>
                    ))}
                    {(r.resources ?? []).slice(0, 3).map((x) => (
                      <div key={`${x.kind}:${x.target}`} className="truncate text-ink-3" title={`${x.kind} ${x.target ?? ""} (${x.status})`}>
                        {x.kind}: {x.target ?? "—"} ({x.status})
                      </div>
                    ))}
                    {r.quarantined && <StatusBadge status="BLOCKED" label="Dikarantina" />}
                  </div>
                ),
              },
              { header: "Status terpisah", cell: (r) => <SeparateStatuses statuses={r.statuses} compact /> },
              {
                header: "Biaya / usage / kuota",
                cell: (r) => (
                  <div className="min-w-32 text-xs">
                    <div>biaya: {costText(r.usage.cost_usd, r.usage.requests)}</div>
                    <div>
                      {r.usage.requests ?? 0} req · {(r.usage.prompt_tokens ?? 0) + (r.usage.completion_tokens ?? 0)} token
                    </div>
                    <StatusBadge status={r.usage.quota_status === "RATE_LIMITED" ? "EXPIRED" : "UNKNOWN"} label={`kuota: ${r.usage.quota_status}`} />
                  </div>
                ),
              },
              {
                header: "Cap aktif",
                cell: (r) => (
                  <div className="max-w-40 text-xs">
                    {capsText(r)}
                    {r.capability && (
                      <div className="mt-0.5">
                        <StatusBadge
                          status={r.capability.active ? "CONNECTED" : "DISABLED"}
                          label={r.capability.active ? `capability s/d ${relativeTime(r.capability.expires_at)}` : "capability dicabut"}
                        />
                      </div>
                    )}
                  </div>
                ),
              },
              {
                header: "Bukti",
                cell: (r) => (
                  <div className="min-w-28 space-y-0.5 text-xs">
                    {r.evidence_refs.slice(0, 3).map((ref) => (
                      <div key={ref}>
                        <Link to={`/evidence?id=${encodeURIComponent(ref)}`} className="text-accent underline" title={ref}>
                          <Mono>{shortId(ref)}</Mono>
                        </Link>
                      </div>
                    ))}
                    <Link to={r.links.run} className="text-ink-3 underline">
                      {r.evidence_refs.length} referensi
                    </Link>
                  </div>
                ),
              },
              {
                header: "Kebutuhan pemilik",
                cell: (r) => (
                  <ul className="max-w-56 list-disc pl-4 text-xs text-ink-2">
                    {r.owner_requirements.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                ),
              },
              {
                header: "Tindakan",
                cell: (r) =>
                  ["LEASED", "RUNNING", "REVIEW", "QUEUED"].includes(r.execution_state) ? (
                    <ConfirmAction
                      label="Batalkan"
                      variant="danger"
                      icon={<Ban className="h-4 w-4" aria-hidden />}
                      isAdmin={isAdmin}
                      confirmText="Capability dicabut, seluruh process tree dihentikan, reservasi dilepas. Efek eksternal yang sudah terjadi tidak dibatalkan otomatis."
                      onConfirm={async (reason) => {
                        await post(`/api/runs/${r.run_id}/cancel`, { reason });
                        refresh();
                      }}
                    />
                  ) : (
                    <span className="text-xs text-ink-3">—</span>
                  ),
              },
            ]}
          />
        )}
      </Card>
      <p className="flex items-center gap-1 text-xs text-ink-3">
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
        Tidak ada tombol force-green, bypass review, fallback berbayar, atau merge otomatis. Merge selalu dilakukan manusia.
      </p>
    </div>
  );
}
