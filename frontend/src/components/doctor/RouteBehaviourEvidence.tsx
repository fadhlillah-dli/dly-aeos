import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Badge } from "../inventory/labels";
import { StatusBadge } from "../StatusBadge";
import { Button, ErrorBox, Field, inputClass, Mono, Notice, Table } from "../ui";

/** Backend aeos.doctor.checks.worker: the owner's threshold POLICY target (details.max_failure_rate 0..1). */
export const ROUTE_BEHAVIOUR_TARGET = { type: "worker_route_behaviour", id: "tool_schema_failures" } as const;

export interface RouteBehaviourRoute {
  route_key: string;
  route_status?: string;
  verdict: string;
  why: string;
  tool_schema_failures?: number;
  failure_rate?: number;
  sample_size?: number | null;
  verified_runs?: number;
  tool_errors?: number;
  eval_run_id?: string;
}

export interface RouteBehaviourThreshold {
  status: "DECIDED" | "UNDECIDED" | string;
  max_failure_rate: number | null;
  decision_id: string | null;
  target?: string;
}

function pct(v: number | null | undefined): string {
  return typeof v === "number" ? `${Math.round(v * 10000) / 100}%` : "UNKNOWN";
}

/** The owner's threshold through the existing Decision API (POST /api/decisions, POLICY); explicit confirmation. */
function ThresholdForm({ current, domain }: { current: RouteBehaviourThreshold; domain: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [rate, setRate] = useState("");
  const [rationale, setRationale] = useState("");
  const parsed = rate.trim() === "" ? NaN : Number(rate.replace(",", "."));
  const inRange = Number.isFinite(parsed) && parsed >= 0 && parsed <= 100;
  // the exact value recorded (0..1, 4 decimals) - shown before the owner confirms
  const recordedRate = inRange ? Math.round((parsed / 100) * 10000) / 10000 : null;
  const valid = recordedRate !== null && rationale.trim().length >= 10;
  const save = useMutation({
    mutationFn: () =>
      post<{ id: string }>("/api/decisions", {
        kind: "POLICY",
        target_type: ROUTE_BEHAVIOUR_TARGET.type,
        target_id: ROUTE_BEHAVIOUR_TARGET.id,
        rationale: rationale.trim(),
        auth_scope: { threshold: "worker.route_behaviour tool_schema_failures", recorded_via: "Control Center" },
        allowed_operations: [],
        details: { max_failure_rate: recordedRate },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["doctor"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
    },
  });
  // focus returns to the opener when the owner cancels (the rate field takes focus when the form opens)
  const opener = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && !open) opener.current?.focus();
    wasOpen.current = open;
  }, [open]);
  return (
    <div className="space-y-2">
      {/* always mounted, so a screen reader announces the recorded decision when it appears */}
      <div aria-live="polite">
        {save.data && (
          <Notice tone="info">
            Ambang tercatat sebagai keputusan <Mono>{save.data.id}</Mono>. Hasil Doctor menjadi STALE dan dinilai ulang pada pemeriksaan
            berikutnya; ambang tidak membuat route lulus tanpa evaluasi tepercaya.
          </Notice>
        )}
      </div>
      {!save.data && !open && (
        <Button ref={opener} onClick={() => setOpen(true)}>
          {current.status === "DECIDED" ? "Ubah ambang pemilik…" : "Tetapkan ambang pemilik…"}
        </Button>
      )}
      {!save.data && open && (
        <div className="space-y-2 rounded-md border border-line bg-surface-2 p-3 text-sm" role="group" aria-label="Ambang kegagalan tool/schema">
          <p>
            Keputusan yang akan dicatat: <b>POLICY</b> oleh Anda (manusia), target{" "}
            <Mono>
              {ROUTE_BEHAVIOUR_TARGET.type}:{ROUTE_BEHAVIOUR_TARGET.id}
            </Mono>
            , <Mono>details.max_failure_rate</Mono>, domain sesi <b>{domain}</b>. Tidak ada nilai bawaan: tanpa keputusan ini kegagalan
            hanya dilaporkan (UNKNOWN).
            {current.status === "DECIDED" && current.max_failure_rate !== null
              ? ` Keputusan baru menggantikan ambang yang berlaku sekarang (${pct(current.max_failure_rate)}) sebagai yang terbaru.`
              : ""}
          </p>
          <Field label="Laju kegagalan maksimum (%)" hint="0-100; mis. 5 = maks 5% sampel evaluasi dengan kegagalan tool/schema.">
            {/* autoFocus: focus moves into the form the owner just opened */}
            <input aria-label="Laju kegagalan maksimum (%)" inputMode="decimal" autoFocus className={inputClass} value={rate} onChange={(e) => setRate(e.target.value)} />
          </Field>
          <Field label="Alasan (wajib)" hint="Minimal 10 karakter; tersimpan pada keputusan dan audit.">
            <textarea aria-label="Alasan ambang" className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </Field>
          {rate.trim() !== "" && !inRange && <p className="text-xs text-warn">Isi angka 0 sampai 100.</p>}
          {recordedRate !== null && (
            <p className="text-xs">
              Nilai yang dicatat: <Mono>max_failure_rate = {recordedRate}</Mono> ({pct(recordedRate)})
            </p>
          )}
          <ErrorBox error={save.error} />
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={!valid} busy={save.isPending} onClick={() => save.mutate()}>
              Konfirmasi keputusan POLICY
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Batal
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Doctor ``worker.route_behaviour`` (W6-021): per worker route the trusted evaluation / verified-run evidence of
 * tool/schema behaviour, and the owner's failure-rate threshold (UNDECIDED = reported, never judged).
 */
export function RouteBehaviourEvidence({ evidence }: { evidence: Record<string, unknown> }) {
  const { isAdmin, domain } = useAuth();
  const routes = (Array.isArray(evidence.routes) ? evidence.routes : []) as RouteBehaviourRoute[];
  const t = (evidence.threshold ?? { status: "UNDECIDED", max_failure_rate: null, decision_id: null }) as RouteBehaviourThreshold;
  const installed = typeof evidence.installed_engine_version === "string" ? evidence.installed_engine_version : null;
  return (
    <div className="space-y-3" data-testid="route-behaviour-evidence">
      <div className="text-xs font-semibold uppercase tracking-wide text-ink-3">Perilaku tool/schema per route worker</div>
      <div className="space-y-1 text-sm">
        {t.status === "DECIDED" && t.max_failure_rate !== null ? (
          <StatusBadge status="CONFIGURED" label={`Ambang pemilik: maks ${pct(t.max_failure_rate)} kegagalan`} />
        ) : (
          <StatusBadge status="NEEDS_DECISION" label="Ambang pemilik UNDECIDED: kegagalan dilaporkan, tidak dinilai" />
        )}
        {t.decision_id && (
          <div className="text-xs">
            Keputusan:{" "}
            <Link className="text-accent underline" to={`/decisions?decision=${encodeURIComponent(t.decision_id)}`}>
              <Mono>{t.decision_id}</Mono>
            </Link>
            {t.status !== "DECIDED" && " (nilai tidak terbaca: tetap UNDECIDED)"}
          </div>
        )}
        <div className="text-xs text-ink-3">OpenCode terpasang: {installed ?? "UNKNOWN"}</div>
      </div>
      <Table
        rows={routes}
        rowKey={(r) => r.route_key}
        empty="Belum ada route worker APPROVED/TRIAL: perilaku tool/schema NOT-PROVEN"
        columns={[
          { header: "Route", cell: (r) => <Mono>{r.route_key}</Mono> },
          { header: "Verdict", cell: (r) => <Badge status={r.verdict} /> },
          {
            header: "Kegagalan tool/schema",
            cell: (r) =>
              typeof r.tool_schema_failures === "number" ? (
                <span className="text-xs">
                  {r.tool_schema_failures}/{r.sample_size ?? "?"} sampel · {pct(r.failure_rate)}
                </span>
              ) : (
                <StatusBadge status="UNKNOWN" label="Tidak tercatat" />
              ),
          },
          {
            header: "Run terverifikasi",
            cell: (r) => (
              <span className="text-xs">
                {r.verified_runs ?? "UNKNOWN"} run lulus verifier · {r.tool_errors ?? "UNKNOWN"} error alat teramati runner
              </span>
            ),
          },
          { header: "Alasan", cell: (r) => <span className="text-xs">{r.why}</span> },
        ]}
      />
      {isAdmin && domain === "PERSONAL" ? (
        <ThresholdForm current={t} domain={domain} />
      ) : isAdmin ? (
        <p className="text-xs text-ink-3">Ambang platform dicatat di sesi PERSONAL (domain platform).</p>
      ) : (
        <p className="text-xs text-ink-3">Hanya SUPER_ADMIN yang dapat menetapkan ambang; VIEWER hanya-baca.</p>
      )}
    </div>
  );
}
