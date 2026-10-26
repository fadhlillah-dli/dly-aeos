import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Settings2 } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { Button, Card, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, inputClass } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { EnvironmentDecisionForm, type EnvironmentName } from "../decisions/EnvironmentDecisionForm";
import { Outcome, ROLLBACK_LABELS } from "../evidence/outcomes";
import type { ArtifactState, CheckResult, ObserveResult, TargetList, TargetView } from "./deliveryTypes";
import { TargetChecksEditor } from "./TargetChecksEditor";
import type { IntegrationView, ProjectView } from "./types";

const ENVS: EnvironmentName[] = ["staging", "production"];
const ENV_LABEL: Record<EnvironmentName, string> = { staging: "Staging", production: "Produksi" };
/** POST /api/delivery/targets/observe accepts a 7-64 hex commit. */
export const OBSERVE_SHA = /^[0-9a-fA-F]{7,64}$/;

function EvidenceLink({ id, children }: { id: string | null | undefined; children?: ReactNode }) {
  if (!id) return <span className="text-ink-3">—</span>;
  return (
    <Link to={`/evidence?id=${encodeURIComponent(id)}`} className="text-accent hover:underline" title={id}>
      {children ?? <Mono>{shortHash(id, 8)}</Mono>}
    </Link>
  );
}

function ArtifactFacts({ a }: { a: ArtifactState | undefined }) {
  if (!a || !a.status) return <span className="text-ink-3">—</span>;
  return (
    <span className="flex flex-col gap-0.5">
      <Outcome status={a.status} />
      {a.reported_digest && (
        <span className="text-xs">
          dilaporkan <Mono>{a.reported_digest}</Mono>
        </span>
      )}
      {(a.recorded_digests ?? []).length > 0 && (
        <span className="text-xs">
          CI: {(a.recorded_digests ?? []).map((d) => (
            <Mono key={d}>{d}</Mono>
          ))}
        </span>
      )}
    </span>
  );
}

function Conditions({ items }: { items: string[] | undefined }) {
  if (!items?.length) return <span className="text-ink-3">tidak ada</span>;
  return <span className="text-bad">{items.map((c) => ROLLBACK_LABELS[c] ?? c).join(", ")}</span>;
}

function CheckResults({ title, rows, sli }: { title: string; rows: CheckResult[]; sli?: boolean }) {
  if (!rows.length) return null;
  return (
    <div>
      <div className="mb-1 text-xs font-semibold">{title}</div>
      <ul className="space-y-1 text-xs">
        {rows.map((c) => (
          <li key={c.name} className="rounded border border-line p-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{c.name}</span>
              {c.kind && !sli && <Mono>{c.kind}</Mono>}
              {c.path && <Mono>{c.path}</Mono>}
              <Outcome status={c.status} />
              {sli && c.value !== null && c.value !== undefined && <span>nilai {c.value}</span>}
              {c.http_status !== null && c.http_status !== undefined && <span className="text-ink-3">HTTP {c.http_status}</span>}
              {c.evidence_id && <EvidenceLink id={c.evidence_id} />}
            </div>
            {c.reason && <div className="mt-0.5 text-ink-2 wrap-anywhere">{c.reason}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Result of one read-only observation (POST /api/delivery/targets/observe). */
export function ObserveResultView({ r }: { r: ObserveResult }) {
  const pr = r.post_release;
  const rec = pr?.recommendation;
  const recStatus = typeof rec === "string" ? rec : rec?.status;
  return (
    <div className="space-y-2 rounded-md border border-line bg-surface-2 p-3 text-sm" data-testid="observe-result">
      <div className="flex flex-wrap items-center gap-2">
        <Outcome status={r.status} />
        <span className="text-xs text-ink-2 wrap-anywhere">{r.reason}</span>
      </div>
      <KeyValue
        items={[
          ["Target", <Mono key="t">{r.target}</Mono>],
          ["Bukti observasi", <EvidenceLink key="e" id={r.evidence_id} />],
          ["Kondisi gagal", <Conditions key="c" items={r.failure_conditions} />],
          ["Digest artefak", <ArtifactFacts key="a" a={r.artifact} />],
        ]}
      />
      <CheckResults title="Smoke check & alur kritis" rows={r.checks ?? []} />
      <CheckResults title="SLI" rows={r.slis ?? []} sli />
      {pr && (
        <div role={r.status === "FAIL" ? "alert" : undefined} className="space-y-1 rounded-md border border-line bg-surface p-2 text-xs">
          <div className="font-semibold">Respons pasca-rilis (G8)</div>
          <KeyValue
            items={[
              ["Alert", pr.alert_id ? <Mono key="al">{shortHash(pr.alert_id, 8)}</Mono> : "—"],
              ...(pr.alert_status ? ([["Status alert", pr.alert_status]] as [string, string][]) : []),
              ["Insiden", pr.incident_id ? <Mono key="in">{shortHash(pr.incident_id, 8)}</Mono> : "—"],
              ["Permintaan keputusan", pr.decision_request_id ? <Link key="dr" to="/decisions" className="text-accent hover:underline">Antrean keputusan</Link> : "—"],
              ["Rekomendasi", recStatus ? <Outcome key="r" status={recStatus} /> : "—"],
              ["Runbook", pr.runbook ? <Mono key="rb">{pr.runbook}</Mono> : "—"],
            ]}
          />
          {typeof rec === "object" && rec?.summary && <p className="text-ink-2">{rec.summary}</p>}
          <p className="text-ink-3">AEOS tidak pernah deploy atau rollback: rollback manual, forward-fix atau penerimaan risiko diputuskan manusia.</p>
        </div>
      )}
    </div>
  );
}

function ObserveForm({ target }: { target: TargetView }) {
  const qc = useQueryClient();
  const [sha, setSha] = useState("");
  const inputId = useId();
  const m = useMutation({
    mutationFn: () => post<ObserveResult>("/api/delivery/targets/observe", { project_id: target.project_id, environment: target.environment, sha: sha.trim() }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["delivery-targets", target.project_id] });
      qc.invalidateQueries({ predicate: (x) => ["gates", "evidence"].includes(String(x.queryKey[0])) });
    },
  });
  const valid = OBSERVE_SHA.test(sha.trim());
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        <label htmlFor={inputId} className="min-w-0 flex-1 text-xs">
          <span className="mb-1 block text-ink-3">Commit yang seharusnya berjalan di target</span>
          <input id={inputId} className={`${inputClass} mono`} value={sha} onChange={(e) => setSha(e.target.value)} placeholder="sha commit" aria-invalid={(!!sha.trim() && !valid) || undefined} />
        </label>
        <Button busy={m.isPending} disabled={!valid} onClick={() => m.mutate()}>
          <Eye className="h-4 w-4" aria-hidden /> Observasi target (GET)
        </Button>
      </div>
      {target.environment === "staging" && (
        <p className="text-xs text-ink-3">Observasi staging hanya berjalan saat kapabilitas CAN_VALIDATE_STAGING READY untuk proyek ini (selain itu ditolak).</p>
      )}
      {target.environment === "production" && (
        <p className="text-xs text-ink-3">
          Observasi produksi yang FAIL membuka alert pasca-rilis, insiden dan satu permintaan keputusan di antrean (rollback/forward-fix/terima risiko
          diputuskan manusia).
        </p>
      )}
      <ErrorBox error={m.error} />
      <div aria-live="polite">{m.data && <ObserveResultView r={m.data} />}</div>
    </div>
  );
}

function TargetCard({ target, isAdmin }: { target: TargetView; isAdmin: boolean }) {
  const [editing, setEditing] = useState(false);
  const s = target.settings ?? { integration_id: target.integration_id, base_url: "" };
  const obs = target.latest_observation ?? { status: "NO_EVIDENCE", evidence_id: null };
  const smoke = s.smoke_checks ?? [];
  const slis = s.sli_checks ?? [];
  return (
    <div className="space-y-3">
      {target.config_problem && (
        <Notice tone="bad">
          Konfigurasi target tersimpan tidak valid ({target.config_problem}); observasi ditolak sampai diperbaiki.
        </Notice>
      )}
      <KeyValue
        items={[
          ["URL dasar", <Mono key="u">{s.base_url || "—"}</Mono>],
          ["Path health", <Mono key="h">{s.health_path ?? "—"}</Mono>],
          [
            "Smoke check",
            smoke.length ? (
              <ul key="sm" className="space-y-0.5 text-xs">
                {smoke.map((c) => (
                  <li key={c.name}>
                    <span className="font-medium">{c.name}</span> <Mono>{c.path}</Mono> → HTTP {c.expect_status}
                    {c.kind === "critical_flow" && <span className="text-ink-3"> · alur kritis</span>}
                    {c.json_keys.length > 0 && <span className="text-ink-3"> · kunci {c.json_keys.join(", ")}</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <span key="sm" className="text-ink-3">belum ada (hanya health)</span>
            ),
          ],
          [
            "SLI",
            slis.length ? (
              <ul key="sl" className="space-y-0.5 text-xs">
                {slis.map((c) => (
                  <li key={c.name} className="flex flex-wrap items-center gap-1">
                    <span className="font-medium">{c.name}</span> <Mono>{`${c.path} → ${c.json_key}`}</Mono>
                    {c.min === null && c.max === null ? (
                      <Outcome status="NO_TARGET" />
                    ) : (
                      <span>
                        ambang pemilik {c.min !== null ? `min ${c.min}` : ""} {c.max !== null ? `max ${c.max}` : ""}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <span key="sl" className="text-ink-3">belum ada</span>
            ),
          ],
          [
            "Kriteria rollback",
            <span key="rb" className="flex flex-wrap items-center gap-2">
              <Outcome status={s.rollback_criteria ?? "UNDECIDED"} />
              {(s.rollback_on ?? []).length > 0 && <span className="text-xs">{(s.rollback_on ?? []).map((c) => ROLLBACK_LABELS[c] ?? c).join(", ")}</span>}
            </span>,
          ],
          ["Wajib digest artefak", s.require_artifact_digest ? "ya" : "tidak"],
          ["Hash konfigurasi", target.target_config_hash ? <Mono key="ch">{shortHash(target.target_config_hash, 12)}</Mono> : "—"],
        ]}
      />
      <div className="rounded-md border border-line p-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold">Observasi terbaru (konfigurasi saat ini)</span>
          <Outcome status={obs.status} />
          {obs.observed_at && <Freshness at={obs.observed_at} label="Diamati" staleAfterSeconds={86400} />}
        </div>
        {obs.status !== "NO_EVIDENCE" && (
          <div className="mt-1 space-y-0.5 text-xs text-ink-2">
            {obs.reason && <p className="wrap-anywhere">{obs.reason}</p>}
            <p>
              Commit <Mono>{shortHash(obs.commit ?? null, 12)}</Mono> · bukti <EvidenceLink id={obs.evidence_id} /> · kondisi gagal{" "}
              <Conditions items={obs.failure_conditions} />
            </p>
            {obs.artifact?.status && (
              <div className="flex flex-wrap items-center gap-1">
                Digest artefak: <ArtifactFacts a={obs.artifact} />
              </div>
            )}
          </div>
        )}
      </div>
      {isAdmin && (
        <div className="space-y-3">
          <ObserveForm target={target} />
          <Button variant="ghost" aria-expanded={editing} onClick={() => setEditing(!editing)}>
            <Settings2 className="h-4 w-4" aria-hidden /> {editing ? "Tutup editor pemeriksaan" : "Ubah pemeriksaan target"}
          </Button>
          {editing && <TargetChecksEditor key={target.target_config_hash ?? target.integration_id} target={target} />}
        </div>
      )}
    </div>
  );
}

/**
 * §31 G6/G8 for a PERSONAL project: the owner-registered observe-only deployment targets with their checks, the newest
 * observation, a read-only observation action and the owner's environment decisions. AEOS never deploys.
 */
export function DeploymentTargetsPanel({ project, integrations, isAdmin }: { project: ProjectView; integrations: IntegrationView[]; isAdmin: boolean }) {
  const registered = integrations.filter((i) => i.provider === "deployment-target");
  const q = useQuery({
    queryKey: ["delivery-targets", project.id],
    queryFn: () => get<TargetList>(`/api/delivery/targets?project_id=${encodeURIComponent(project.id)}`),
    enabled: registered.length > 0,
  });
  const targets = (q.data?.items ?? []).filter((t) => t && typeof t.integration_id === "string" && t.settings && typeof t.environment === "string");
  return (
    <Card title="Target deployment: pemeriksaan, observasi & keputusan lingkungan (G6/G8)">
      <div className="space-y-4">
        <Notice tone="warn">
          AEOS hanya mengamati target dengan GET read-only. Deploy, rilis, promosi dan rollback tetap tindakan manusia di platform deploy proyek.
        </Notice>
        {ENVS.map((env) => {
          const target = targets.find((t) => t.environment === env);
          const integ = registered.find((i) => i.purpose === env);
          const url = target?.settings.base_url || integ?.root_id || null;
          return (
            <section key={env} aria-label={`Lingkungan ${ENV_LABEL[env]}`} className="space-y-3 rounded-lg border border-line p-3">
              <h3 className="text-sm font-semibold">{ENV_LABEL[env]}</h3>
              {target ? (
                <TargetCard target={target} isAdmin={isAdmin} />
              ) : integ ? (
                q.isLoading ? (
                  <Loading label="Memuat target…" />
                ) : q.error ? (
                  <ErrorBox error={q.error} />
                ) : (
                  <p className="text-xs text-ink-3">Target {env} terdaftar tetapi pengaturannya belum terbaca.</p>
                )
              ) : (
                <p className="text-xs text-ink-3">
                  Belum ada target {env} terdaftar: daftarkan URL dasarnya di “{ENV_LABEL[env]} — Deployment target” pada pengaturan integrasi di bawah, atau catat
                  keputusan bahwa proyek ini tanpa {env}.
                </p>
              )}
              <EnvironmentDecisionForm projectId={project.id} environment={env} targetUrl={url} isAdmin={isAdmin} domain={project.domain} />
            </section>
          );
        })}
      </div>
    </Card>
  );
}
