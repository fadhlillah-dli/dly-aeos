import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, RefreshCw, SearchCheck, ShieldCheck, UserCog } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, EmptyState, ErrorBox, Field, Freshness, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { VerifierPolicyFacts } from "../../components/planning/VerifierPolicy";
import { ConfirmAction } from "../../components/workforce/ConfirmAction";
import { InvestigationPanel } from "../../components/workforce/InvestigationPanel";
import { ExecState, ModeBadge, SeparateStatuses, costText, shortId } from "../../components/workforce/status";
import type { ActionEvent, AgentDef, RunDetail } from "../../components/workforce/types";
import { useLiveInvalidation } from "../../components/workforce/useLive";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import { alertText, isHighAlert, QuarantineReason, reasoningRequestedLabel, reasoningSentLabel, RuntimeIntegrity } from "./runtimeCaps";

function List({ items }: { items: unknown[] | undefined }) {
  if (!items || items.length === 0) return <span className="text-ink-3">—</span>;
  return (
    <ul className="list-disc pl-5">
      {items.map((x, i) => (
        <li key={i}>{typeof x === "string" ? x : JSON.stringify(x)}</li>
      ))}
    </ul>
  );
}

function Paths({ items }: { items: string[] | undefined }) {
  if (!items || items.length === 0) return <span className="text-ink-3">(seluruh repository)</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((p) => (
        <Mono key={p}>{p}</Mono>
      ))}
    </div>
  );
}

function actionText(a: ActionEvent): string {
  const p = a.payload as Record<string, unknown>;
  if (typeof p.label === "string") return p.label + (typeof p.detail === "string" && p.detail ? ` — ${p.detail}` : "");
  if (a.kind === "run.state_changed") return `State run: ${String(p.from ?? "—")} → ${String(p.to ?? "—")} ${p.reason ? `(${String(p.reason)})` : ""}`;
  if (a.kind === "run.model_call") {
    const base = `Panggilan model via broker: ${String(p.outcome)} · ${String(p.prompt_tokens ?? "?")}+${String(p.completion_tokens ?? "?")} token · biaya ${String(p.cost_usd)}`;
    // ADR-0041 amendment 2026-10-25: the level the broker sent, and the worker's requested level it dropped
    if (!("reasoning_effort" in p)) return base;
    const sent = reasoningSentLabel(p.reasoning_effort as string | null).label;
    const dropped = p.reasoning_requested_dropped ? ` · permintaan reasoning pekerja dibuang: ${String(p.reasoning_requested_dropped)}` : "";
    return `${base} · reasoning: ${sent}${dropped}`;
  }
  if (a.kind === "worker_runtime.alert") return `Peringatan ${String(p.severity ?? "")}: ${alertText(p)}`;
  if (a.kind === "run.heartbeat") return "Heartbeat (liveness, bukan kemajuan)";
  if (a.kind === "run.stale_event") return `Event lama ditolak: ${String(p.rejected)} (${String(p.why)})`;
  if (a.kind === "run.side_effect") return `Efek eksternal: ${String(p.kind)} ${String(p.status)}`;
  return a.kind;
}

function Timeline({ actions }: { actions: ActionEvent[] }) {
  const [showHeartbeats, setShowHeartbeats] = useState(false);
  const shown = actions.filter((a) => showHeartbeats || a.kind !== "run.heartbeat");
  return (
    <div>
      <label className="mb-2 flex items-center gap-2 text-xs text-ink-3">
        <input type="checkbox" checked={showHeartbeats} onChange={(e) => setShowHeartbeats(e.target.checked)} /> tampilkan heartbeat
      </label>
      {shown.length === 0 ? (
        <EmptyState title="Belum ada aksi teramati" />
      ) : (
        <ol className="space-y-1.5 text-sm">
          {shown.map((a) => {
            const narration = a.label.startsWith("summary");
            const alert = isHighAlert(a.kind, a.payload);
            return (
              <li key={a.seq} className="flex flex-wrap items-start gap-2 border-b border-line pb-1.5 last:border-0">
                <span className="w-24 shrink-0 text-xs text-ink-3" title={absoluteTime(a.at)}>
                  {relativeTime(a.at)}
                </span>
                {alert ? (
                  <StatusBadge status="BLOCKED" label={`Peringatan HIGH: ${String(a.payload.code ?? "UNKNOWN")}`} />
                ) : (
                  <StatusBadge status={narration ? "ESTIMATE" : "CONFIGURED"} label={narration ? "Ringkasan AI (bukan bukti)" : "Aksi alat teramati"} />
                )}
                {a.mode !== "LIVE" && <StatusBadge status={a.mode} />}
                <span className={narration ? "italic text-ink-2" : ""}>{actionText(a)}</span>
                <span className="text-xs text-ink-3">· {a.source}</span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function OperatorActions({ run, isAdmin, onDone }: { run: RunDetail; isAdmin: boolean; onDone: () => void }) {
  const te = run.task_execution;
  const [extra, setExtra] = useState(0);
  const [agent, setAgent] = useState("");
  const agents = useQuery({ queryKey: ["workforce-agents"], queryFn: () => get<{ items: AgentDef[] }>("/api/workforce/agents"), enabled: isAdmin });
  const implementers = (agents.data?.items ?? []).filter((a) => a.harness === "opencode");
  const needsDecision = te?.reason_code === "NEEDS_DECISION";
  return (
    <Card title="Kontrol operator">
      <div className="flex flex-wrap items-start gap-3">
        {["LEASED", "RUNNING", "REVIEW", "QUEUED"].includes(run.execution_state) && (
          <ConfirmAction
            label="Batalkan run"
            variant="danger"
            icon={<Ban className="h-4 w-4" aria-hidden />}
            isAdmin={isAdmin}
            confirmText="Capability dicabut, process tree/sandbox dihentikan, reservasi dilepas. Bukti disimpan; efek eksternal yang sudah terjadi tidak dibatalkan otomatis."
            onConfirm={async (reason) => {
              await post(`/api/runs/${run.run_id}/cancel`, { reason });
              onDone();
            }}
          />
        )}
        {te && run.task.id && ["REVIEW", "BLOCKED"].includes(te.state) && (
          <ConfirmAction
            label={te.state === "REVIEW" ? "Minta repair (attempt baru)" : "Coba lagi"}
            icon={<RefreshCw className="h-4 w-4" aria-hidden />}
            isAdmin={isAdmin}
            requireReason={needsDecision}
            confirmText={
              needsDecision
                ? "Batas attempt tercapai. Menambah attempt adalah keputusan pemilik (EXCEPTION) yang terikat ke hash TaskSpec."
                : "Repair dikerjakan attempt baru oleh implementer (bukan reviewer), tetap dalam batas attempt."
            }
            extra={
              needsDecision ? (
                <Field label="Tambahan attempt (keputusan pemilik)">
                  <input type="number" min={1} max={3} className={inputClass} value={extra} onChange={(e) => setExtra(Number(e.target.value))} />
                </Field>
              ) : undefined
            }
            onConfirm={async (reason) => {
              await post(`/api/tasks/${run.task.id}/retry`, { extra_attempts: needsDecision ? extra : 0, rationale: reason });
              onDone();
            }}
          />
        )}
        {te && run.task.id && ["READY", "BLOCKED", "LEASED", "RUNNING"].includes(te.state) && (
          <ConfirmAction
            label="Alihkan ke agent lain"
            icon={<UserCog className="h-4 w-4" aria-hidden />}
            isAdmin={isAdmin}
            confirmText="Lease lama dihentikan dan capability dicabut sebelum lease baru diizinkan."
            extra={
              <Field label="Agent pelaksana">
                <select className={inputClass} value={agent} onChange={(e) => setAgent(e.target.value)}>
                  <option value="">— pilih —</option>
                  {implementers.map((a) => (
                    <option key={a.agent_key} value={a.agent_key} disabled={!a.enabled}>
                      {a.agent_key} {a.enabled ? "" : "(nonaktif)"}
                    </option>
                  ))}
                </select>
              </Field>
            }
            onConfirm={async (reason) => {
              await post(`/api/tasks/${run.task.id}/reassign`, { agent_key: agent, reason });
              onDone();
            }}
          />
        )}
      </div>
      <p className="mt-2 text-xs text-ink-3">Tidak tersedia: force-green, bypass review, fallback berbayar, merge otomatis.</p>
    </Card>
  );
}

function QuarantineNotice({ run, isAdmin, onDone }: { run: RunDetail; isAdmin: boolean; onDone: () => void }) {
  const release = run.decisions.find(
    (d) => d.target_type === "run_quarantine" && d.status === "ACTIVE" && d.target_hash && d.target_hash === run.head_commit,
  );
  const releasable = ["REVIEW", "DONE"].includes(run.execution_state) && Boolean(run.head_commit);
  return (
    <Notice tone="warn">
      <strong>Patch dikarantina</strong> — memerlukan review khusus sebelum diterima atau dipublikasikan (push/PR).
      <ul className="mt-1 list-disc pl-5">
        {run.quarantine_reasons.map((q2, i) => (
          <li key={i}>
            <QuarantineReason q={q2} />
          </li>
        ))}
      </ul>
      {release ? (
        <p className="mt-2 inline-flex items-center gap-1">
          <ShieldCheck className="h-4 w-4" aria-hidden />
          Hasil review khusus tercatat sebagai keputusan pemilik untuk head <Mono>{shortHash(run.head_commit)}</Mono>. Acceptance tetap
          memerlukan verifier dan review.
        </p>
      ) : releasable ? (
        <div className="mt-2">
          <ConfirmAction
            label="Catat hasil review khusus"
            icon={<ShieldCheck className="h-4 w-4" aria-hidden />}
            isAdmin={isAdmin}
            requireReason
            confirmText={`Keputusan pemilik (EXCEPTION) terikat ke run ini dan head ${shortHash(run.head_commit)}. Ini bukan acceptance: verifier + review tetap wajib. Tulis alasan/temuan review (min. 10 karakter).`}
            onConfirm={async (reason) => {
              await post(`/api/runs/${run.run_id}/quarantine-release`, { rationale: reason });
              onDone();
            }}
          />
        </div>
      ) : (
        <p className="mt-2 text-xs">Belum dapat dirilis: run harus REVIEW dengan head commit dari runtime.</p>
      )}
    </Notice>
  );
}

export default function RunDetailPage() {
  const { runId = "" } = useParams();
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const key = ["run", domain, runId];
  const q = useQuery({ queryKey: key, queryFn: () => get<RunDetail>(`/api/runs/${runId}`), refetchInterval: 10_000 });
  const stream = useLiveInvalidation([key], (e) => e.run_id === runId || e.entity_id === runId || e.entity_id === q.data?.task.id);
  const r = q.data;
  return (
    <div className="space-y-5">
      <Link to="/work" className="inline-flex items-center gap-1 text-sm text-accent">
        <ArrowLeft className="h-4 w-4" aria-hidden /> Pekerjaan Live
      </Link>
      <PageHeader
        title={r ? `Run ${shortId(r.run_id)} · ${r.task.key ?? r.run_type}` : "Detail run"}
        question="Apa yang dilakukan, berubah dan terbukti pada pekerjaan ini?"
        source="WorkerRun, TaskSpec, Lease, event teramati, EvidenceArtifact, ReviewRecord, AcceptanceRecord, Decision"
        checkedAt={r?.checked_at ?? null}
        evidenceLimit="Aksi = event alat yang teramati runtime; narasi AI = ringkasan, bukan bukti. Exit 0 ≠ diterima."
        relatedDecisionsHref="/decisions?target_type=workforce"
        mode={r?.mode}
      />
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : !r ? null : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <ExecState state={r.execution_state} uiState={r.ui_state} />
            <ModeBadge mode={r.mode} />
            <Freshness at={r.checked_at} staleAfterSeconds={60} />
            <span className="text-xs text-ink-3">stream: {stream.status}</span>
          </div>
          <Card title="Status terpisah (tidak pernah satu tanda centang)">
            <SeparateStatuses statuses={r.statuses} />
            {r.task_execution && (
              <p className="mt-2 text-sm">
                State task: <StatusBadge status={r.task_execution.state} /> <Mono>{r.task_execution.reason_code || "—"}</Mono>{" "}
                <span className="text-ink-2">{r.task_execution.reason}</span>
              </p>
            )}
          </Card>
          {r.task_execution?.reason_code === "ACCEPTANCE_STALE" && (
            <Notice tone="warn">
              Acceptance usang (head/base/konfigurasi berubah): tugas dibuka ulang untuk verifikasi dan review ulang. Hasil lama tidak lagi dihitung
              diterima.
            </Notice>
          )}
          {r.ui_state === "WAITING_QUOTA" && r.execution_state === "REVIEW" && (
            <Notice tone="warn">Kuota Claude Max habis: review checkpoint menunggu (BLOCKED); tidak ada gate yang dibuka dan tidak ada repair otomatis.</Notice>
          )}
          {r.quarantined && <QuarantineNotice run={r} isAdmin={isAdmin} onDone={() => qc.invalidateQueries({ queryKey: key })} />}
          {r.mode !== "LIVE" && <Notice tone="mock">Run ini adalah fixture {r.mode}; bukan pekerjaan nyata dan tidak dihitung.</Notice>}

          <OperatorActions run={r} isAdmin={isAdmin} onDone={() => qc.invalidateQueries({ queryKey: key })} />
          {r.run_type === "INVESTIGATION" && r.task.id && <InvestigationPanel taskId={r.task.id} isAdmin={isAdmin} />}

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Ringkasan run">
              <KeyValue
                items={[
                  ["Proyek / domain", `${r.project.name ?? r.project.key ?? "—"} · ${r.domain}`],
                  ["Agent / peran", `${r.agent_key ?? "—"} · ${r.role_name ?? r.role_key ?? "—"}`],
                  ["Attempt", r.task_execution ? `${r.attempt} dari maks ${r.task_execution.max_attempts ?? "?"}` : r.attempt],
                  ["Route / model", <span key="m"><Mono>{r.route.key ?? "—"}</Mono> · {r.route.model_resolved}</span>],
                  ["Engine", `${r.route.engine ?? "—"} ${r.route.engine_version}`],
                  ["Sandbox", `${r.workspace.sandbox_kind ?? "—"}${r.workspace.sandbox_kind === "process" ? " (LEMAH, tanpa egress enforcement)" : ""}`],
                  ["Workspace", r.workspace.strategy ?? (r.workspace.present ? "ada" : "—")],
                  ["Branch", r.branch ? <Mono>{r.branch}</Mono> : "—"],
                  ["Base → head", <span key="bh"><Mono>{shortHash(r.base_commit)}</Mono> → <Mono>{shortHash(r.head_commit)}</Mono></span>],
                  ["Mulai / selesai", `${absoluteTime(r.started_at)} / ${absoluteTime(r.finished_at)}`],
                  ["Heartbeat terakhir", <span key="hb">{relativeTime(r.last_heartbeat_at)} {r.heartbeat_stale && <StatusBadge status="STALE" label="usang" />}</span>],
                  ["Lease", r.lease ? `${r.lease.state} · berakhir ${absoluteTime(r.lease.expires_at)} ${r.lease.release_reason ? `· ${r.lease.release_reason}` : ""}` : "—"],
                  ["Capability", r.capability ? `${r.capability.active ? "aktif" : "dicabut"} · s/d ${absoluteTime(r.capability.expires_at)} · terpakai ${JSON.stringify(r.capability.used)}` : "—"],
                  ["Biaya / usage", `${costText(r.usage.cost_usd, r.usage.requests)} · ${r.usage.requests ?? 0} req · kuota ${r.usage.quota_status}`],
                  ["Exit code (worker)", r.exit_code ?? "—"],
                ]}
              />
            </Card>
            <Card title="TaskSpec">
              {r.task_spec ? (
                <KeyValue
                  items={[
                    ["Tujuan", r.task_spec.goal],
                    ["Acceptance criteria", <List key="ac" items={r.task_spec.acceptance_criteria} />],
                    ["Keluaran", <List key="eo" items={r.task_spec.expected_outputs} />],
                    ["Path diizinkan", <Paths key="ap" items={r.task_spec.allowed_paths} />],
                    ["Path dilarang", <Paths key="fp" items={r.task_spec.forbidden_paths.length ? r.task_spec.forbidden_paths : ["(tidak ada tambahan)"]} />],
                    ["Tools / egress", `${r.task_spec.tools_allowed.join(", ") || "default (bash/webfetch ditolak)"} · ${r.task_spec.egress_allowed.join(", ") || "tidak ada"}`],
                    ["Batas", <Mono key="l">{JSON.stringify(r.task_spec.limits)}</Mono>],
                    ["Stop conditions", <List key="sc" items={r.task_spec.stop_conditions} />],
                    ["Risiko / klasifikasi", `${r.task_spec.risk_class} · ${r.task_spec.data_classification}`],
                    ["Dependency", r.task_spec.depends_on.join(", ") || "—"],
                    ["Kebijakan verifier", <VerifierPolicyFacts key="vp" policy={r.task_spec.verifier_policy} />],
                    ["Hash spec / versi rencana", <span key="h"><Mono>{shortHash(r.task_spec.spec_hash)}</Mono> · <Mono>{shortHash(r.task_spec.plan_version_hash)}</Mono></span>],
                  ]}
                />
              ) : (
                <EmptyState title="Tidak ada TaskSpec (fixture/run tanpa task)" />
              )}
            </Card>
          </div>

          <Card title="Integritas runtime & batas (dicatat controller, bukan klaim worker)">
            <RuntimeIntegrity caps={r.caps ?? {}} />
          </Card>

          <Card title="Linimasa aksi teramati">
            <Timeline actions={r.actions} />
          </Card>

          <Card title="Ringkasan diff (dihitung runtime, bukan klaim worker)">
            {r.diff_summary.paths ? (
              <>
                <p className="mb-2 text-sm">
                  {r.diff_summary.files} file · +{r.diff_summary.added} / −{r.diff_summary.deleted}
                  {r.diff_summary.patch_truncated && " · patch terpotong"}
                </p>
                <Table
                  rows={r.diff_summary.paths}
                  rowKey={(p) => p.path}
                  columns={[
                    { header: "Status", cell: (p) => <Mono>{p.status}</Mono> },
                    { header: "Path", cell: (p) => <Mono>{p.path}</Mono> },
                    { header: "Mode", cell: (p) => p.mode ?? "—" },
                  ]}
                />
              </>
            ) : (
              <EmptyState title="Belum ada diff" />
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Attempt">
              <Table
                rows={r.attempts}
                rowKey={(a) => a.run_id}
                empty="Tidak ada attempt lain"
                columns={[
                  { header: "#", cell: (a) => a.attempt },
                  { header: "Run", cell: (a) => (a.current ? <Mono>{shortId(a.run_id)} (ini)</Mono> : <Link className="text-accent underline" to={`/runs/${a.run_id}`}><Mono>{shortId(a.run_id)}</Mono></Link>) },
                  { header: "Status", cell: (a) => <StatusBadge status={a.status} /> },
                  { header: "Hasil", cell: (a) => <Mono>{a.outcome_code || "—"}</Mono> },
                  { header: "Exit", cell: (a) => a.exit_code ?? "—" },
                ]}
              />
            </Card>
            <Card title="Bukti">
              <Table
                rows={r.evidence}
                rowKey={(e) => e.id}
                empty="Belum ada evidence artifact"
                columns={[
                  { header: "Jenis", cell: (e) => <Mono>{e.kind}</Mono> },
                  { header: "Asal", cell: (e) => e.origin },
                  { header: "Kepercayaan", cell: (e) => <StatusBadge status={e.trusted ? "VERIFIED" : "UNVERIFIED"} label={e.trusted ? "Collector tepercaya" : "Dari worker (tidak tepercaya)"} /> },
                  { header: "Level", cell: (e) => e.level },
                  { header: "Klaim", cell: (e) => <span className="text-xs">{e.claim}</span> },
                ]}
              />
              {r.evidence_refs.filter((x) => x.startsWith("local-unverified:")).length > 0 && (
                <p className="mt-2 text-xs text-warn">Sebagian bukti hanya tersimpan lokal (collector tidak tersedia) — UNVERIFIED.</p>
              )}
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Review & acceptance">
              {r.reviews.length === 0 && r.acceptance.length === 0 ? (
                <EmptyState title="Belum ada review atau acceptance">Hasil exit 0 tetap REVIEW sampai verifier dan reviewer memberi bukti.</EmptyState>
              ) : (
                <ul className="space-y-2 text-sm">
                  {r.reviews.map((rv) => (
                    <li key={rv.id}>
                      <StatusBadge status={rv.verdict} /> {rv.reviewer_kind} · {absoluteTime(rv.created_at)} · {rv.findings.length} temuan
                    </li>
                  ))}
                  {r.acceptance.map((a) => (
                    <li key={a.id}>
                      <StatusBadge status={a.status} label={`Acceptance ${a.status}`} /> base <Mono>{shortHash(a.base_commit)}</Mono> head{" "}
                      <Mono>{shortHash(a.head_commit)}</Mono> {a.invalidated_reason && `· ${a.invalidated_reason}`}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Keputusan terkait">
              <Table
                rows={r.decisions}
                rowKey={(d) => d.id}
                empty="Tidak ada keputusan terkait"
                columns={[
                  { header: "Jenis", cell: (d) => <Mono>{d.kind}</Mono> },
                  { header: "Target", cell: (d) => `${d.target_type}/${shortId(d.target_id, 10)}` },
                  { header: "Status", cell: (d) => <StatusBadge status={d.status === "ACTIVE" ? "APPROVED" : d.status} label={d.status} /> },
                  { header: "Alasan", cell: (d) => <span className="text-xs">{d.rationale}</span> },
                ]}
              />
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Efek eksternal (tidak dibatalkan otomatis oleh cancel)">
              <Table
                rows={r.side_effects}
                rowKey={(s) => s.id}
                empty="Tidak ada push/PR untuk run ini"
                columns={[
                  { header: "Jenis", cell: (s) => <Mono>{s.kind}</Mono> },
                  { header: "Status", cell: (s) => <StatusBadge status={s.status === "SUCCEEDED" ? "SUCCEEDED" : s.status} /> },
                  { header: "Target", cell: (s) => <span className="text-xs">{JSON.stringify(s.target)}</span> },
                  { header: "URL", cell: (s) => (s.external_url ? <a className="text-accent underline" href={s.external_url} target="_blank" rel="noreferrer">buka</a> : "—") },
                  {
                    header: "Rekonsiliasi",
                    cell: (s) =>
                      ["PENDING", "UNKNOWN"].includes(s.status) ? (
                        <ConfirmAction
                          label="Periksa status eksternal"
                          icon={<SearchCheck className="h-4 w-4" aria-hidden />}
                          isAdmin={isAdmin}
                          confirmText="Hanya membaca status di remote/GitHub (tidak push, tidak membuat PR, tidak merge). Hasil dicatat; retry tetap menunggu sampai status jelas."
                          onConfirm={async () => {
                            await post(`/api/broker/side-effects/${s.id}/reconcile`, {});
                            qc.invalidateQueries({ queryKey: key });
                          }}
                        />
                      ) : (
                        <span className="text-xs text-ink-3">—</span>
                      ),
                  },
                ]}
              />
              {r.side_effects.some((s) => ["PENDING", "UNKNOWN"].includes(s.status)) && (
                <p className="mt-2 text-xs text-warn">
                  Ada efek eksternal yang hasilnya belum diketahui: attempt baru menunggu (SIDE_EFFECT_UNRECONCILED) sampai direkonsiliasi.
                </p>
              )}
            </Card>
            <Card title="Panggilan model (dicatat broker)">
              <Table
                rows={r.model_calls}
                rowKey={(c) => `${c.at}-${c.route_key}-${c.status}-${c.prompt_tokens}`}
                empty="Belum ada panggilan model"
                columns={[
                  { header: "Waktu", cell: (c) => <span className="text-xs">{relativeTime(c.at)}</span> },
                  { header: "Hasil", cell: (c) => <StatusBadge status={c.outcome === "OK" ? "SUCCEEDED" : c.outcome === "RATE_LIMITED" ? "EXPIRED" : "FAILED"} label={c.outcome} /> },
                  { header: "Token", cell: (c) => `${c.prompt_tokens ?? "?"}+${c.completion_tokens ?? "?"}` },
                  { header: "Biaya", cell: (c) => costText(c.cost_usd) },
                  { header: "Model teramati", cell: (c) => <span className="text-xs">{c.model_resolved ?? "UNKNOWN"}</span> },
                  {
                    header: "Reasoning dikirim",
                    cell: (c) => {
                      const s = reasoningSentLabel(c.reasoning_effort);
                      return <StatusBadge status={s.status} label={s.label} />;
                    },
                  },
                  {
                    header: "Diminta pekerja",
                    cell: (c) => {
                      const s = reasoningRequestedLabel(c.reasoning_effort, c.reasoning_requested);
                      return <StatusBadge status={s.status} label={s.label} />;
                    },
                  },
                ]}
              />
            </Card>
          </div>
          {(r.error || r.block_reason) && (
            <Card title="Catatan runtime">
              <p className="whitespace-pre-wrap text-sm text-ink-2">{r.block_reason || r.error}</p>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
