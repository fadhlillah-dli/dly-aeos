import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Gavel } from "lucide-react";
import { useId, useState } from "react";
import { get, post } from "../../api/client";
import { DecisionStatusBadge } from "../../components/planning/DecisionStatusBadge";
import type { DecisionView } from "../../components/planning/types";
import { Button, ErrorBox, Field, inputClass, Mono, Notice } from "../../components/ui";
import { absoluteTime } from "../../lib/format";

export type EnvironmentName = "staging" | "production";

/** The exact target the delivery gates read (backend delivery.gates.ENVIRONMENT_DECISION_TARGETS). */
export const ENVIRONMENT_DECISION_TARGET: Record<EnvironmentName, string> = {
  staging: "staging_environment",
  production: "production_environment",
};

type Choice = "AUTHORISED" | "NONE";

/** auth_scope of the owner's environment decision: {staging: AUTHORISED, target: <exact url>} or {<env>: NONE}. */
export function environmentScope(env: EnvironmentName, choice: Choice, targetUrl: string | null): Record<string, string> {
  if (env === "staging" && choice === "AUTHORISED") return { staging: "AUTHORISED", target: (targetUrl ?? "").replace(/\/+$/, "") };
  return { [env]: "NONE" };
}

function scopeText(scope: Record<string, unknown>): string {
  return Object.entries(scope)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}

/**
 * §31 G6/G8 owner decision about one environment of a project, recorded through the generic Decision flow
 * (POST /api/decisions, kind POLICY, target_type <env>_environment, target_id = project id) after an explicit
 * confirmation that shows the exact binding. Nothing here is implied by a click: the decision is the owner's AUTH.
 */
export function EnvironmentDecisionForm({
  projectId,
  environment,
  targetUrl,
  isAdmin,
}: {
  projectId: string;
  environment: EnvironmentName;
  /** the registered deployment-target base URL of this environment (null = none registered) */
  targetUrl: string | null;
  isAdmin: boolean;
}) {
  const qc = useQueryClient();
  const targetType = ENVIRONMENT_DECISION_TARGET[environment];
  const q = useQuery({
    queryKey: ["env-decision", projectId, environment],
    queryFn: () =>
      get<{ items: DecisionView[] }>(
        `/api/decisions?kind=POLICY&target_type=${encodeURIComponent(targetType)}&target_id=${encodeURIComponent(projectId)}`,
      ),
  });
  const decisions = (q.data?.items ?? []).filter((d) => d && d.target_type === targetType && d.target_id === projectId);
  const active = decisions.find((d) => d.status === "ACTIVE") ?? null;
  const [choice, setChoice] = useState<Choice>(environment === "staging" && targetUrl ? "AUTHORISED" : "NONE");
  const [rationale, setRationale] = useState("");
  const [expires, setExpires] = useState("");
  const [confirming, setConfirming] = useState(false);
  const groupId = useId();
  const effectiveChoice: Choice = environment === "staging" && choice === "AUTHORISED" && targetUrl ? "AUTHORISED" : "NONE";
  const scope = environmentScope(environment, effectiveChoice, targetUrl);
  const body: Record<string, unknown> = {
    kind: "POLICY",
    target_type: targetType,
    target_id: projectId,
    project_id: projectId,
    auth_scope: scope,
    allowed_operations: [],
    rationale: rationale.trim(),
    ...(expires ? { expires_at: new Date(expires).toISOString() } : {}),
  };
  const save = useMutation({
    mutationFn: () => post<DecisionView>("/api/decisions", body),
    onSuccess: () => {
      setConfirming(false);
      setRationale("");
      qc.invalidateQueries({ queryKey: ["env-decision", projectId, environment] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["gates"] });
    },
  });
  const label = environment === "staging" ? "staging" : "produksi";
  const activeTarget = active ? String(active.auth_scope?.target ?? "").replace(/\/+$/, "") : "";
  const mismatch =
    environment === "staging" &&
    !!active &&
    String(active.auth_scope?.staging ?? "").toUpperCase() === "AUTHORISED" &&
    !!targetUrl &&
    activeTarget !== targetUrl.replace(/\/+$/, "");
  return (
    <div className="space-y-2 rounded-md border border-line p-3" data-testid={`env-decision-${environment}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Gavel className="h-4 w-4 text-ink-3" aria-hidden />
        <span className="text-sm font-medium">Keputusan pemilik: lingkungan {label}</span>
        <Mono>{targetType}</Mono>
      </div>
      {q.isLoading ? (
        <p className="text-xs text-ink-3">Memuat keputusan…</p>
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : active ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <DecisionStatusBadge status={active.status} />
          <Mono>{scopeText(active.auth_scope ?? {})}</Mono>
          <span className="text-ink-3">
            oleh {active.actor} · {absoluteTime(active.created_at)}
            {active.expires_at ? ` · berlaku s.d. ${absoluteTime(active.expires_at)}` : ""}
          </span>
        </div>
      ) : (
        <p className="text-xs text-warn">
          Belum ada keputusan aktif. {environment === "staging" ? "G6" : "G8"} tidak pernah menjadi NOT_APPLICABLE tanpa keputusan pemilik.
        </p>
      )}
      {mismatch && (
        <Notice tone="warn">
          Keputusan aktif mengotorisasi target lain (<Mono>{activeTarget}</Mono>), bukan target terdaftar <Mono>{targetUrl}</Mono>: G6 tidak menganggapnya.
        </Notice>
      )}
      {!isAdmin ? (
        <p className="text-xs text-ink-3">Hanya SUPER_ADMIN yang mencatat keputusan ini; peran VIEWER hanya-baca.</p>
      ) : (
        <div className="space-y-2">
          <fieldset aria-labelledby={groupId} className="space-y-1 text-sm">
            <legend id={groupId} className="text-xs font-medium text-ink-2">
              Keputusan baru (keputusan aktif terbaru yang berlaku)
            </legend>
            {environment === "staging" && (
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name={`${groupId}-choice`}
                  className="mt-1"
                  checked={effectiveChoice === "AUTHORISED"}
                  disabled={!targetUrl}
                  onChange={() => setChoice("AUTHORISED")}
                />
                <span>
                  Deploy staging (oleh manusia) ke target persis ini diotorisasi:{" "}
                  {targetUrl ? <Mono>{targetUrl}</Mono> : <span className="text-warn">daftarkan target staging dulu</span>}
                </span>
              </label>
            )}
            <label className="flex items-start gap-2">
              <input type="radio" name={`${groupId}-choice`} className="mt-1" checked={effectiveChoice === "NONE"} onChange={() => setChoice("NONE")} />
              <span>
                Proyek ini tanpa lingkungan {label} ({environment}=NONE
                {environment === "staging" ? "; G6 tidak berlaku" : "; G8 tidak berlaku"})
                {targetUrl && <span className="block text-xs text-warn">Diabaikan selama target {label} terdaftar.</span>}
              </span>
            </label>
          </fieldset>
          <Field label="Rationale (wajib, min. 10 karakter)">
            <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} maxLength={5000} />
          </Field>
          <Field label="Berlaku sampai (opsional)">
            <input type="datetime-local" className={inputClass} value={expires} onChange={(e) => setExpires(e.target.value)} />
          </Field>
          {!confirming ? (
            <Button disabled={rationale.trim().length < 10} onClick={() => setConfirming(true)}>
              Tinjau keputusan
            </Button>
          ) : (
            <div role="group" aria-label={`Konfirmasi keputusan ${label}`} className="space-y-2 rounded-md bg-surface-2 p-2 text-xs">
              <p>Catat keputusan manusia ini (diaudit, terikat target persis):</p>
              <ul className="space-y-0.5">
                <li>
                  Jenis <Mono>POLICY</Mono> · target <Mono>{`${targetType}/${projectId}`}</Mono>
                </li>
                <li>
                  Scope <Mono>{scopeText(scope)}</Mono>
                </li>
              </ul>
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" busy={save.isPending} onClick={() => save.mutate()}>
                  Konfirmasi &amp; catat keputusan
                </Button>
                <Button variant="ghost" onClick={() => setConfirming(false)}>
                  Batal
                </Button>
              </div>
            </div>
          )}
          <ErrorBox error={save.error} />
          <p aria-live="polite" className="text-xs text-ink-2">
            {save.isSuccess ? "Keputusan tercatat. Gate delivery membacanya pada perhitungan berikutnya." : ""}
          </p>
        </div>
      )}
    </div>
  );
}
