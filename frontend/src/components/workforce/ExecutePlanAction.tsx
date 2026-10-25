import { Rocket } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Notice } from "../ui";
import { ConfirmAction } from "./ConfirmAction";

interface ExecuteResult {
  plan_version_id: string;
  tasks: number;
  ready?: number;
  blocked?: number;
  unchanged?: number;
  scheduler_enabled: boolean;
  paused: boolean;
  note: string;
}

/**
 * Owner action on an APPROVED plan (§07 step 9, §14): queue the approved version's TaskSpecs and make them READY.
 * The scheduler still admits nothing without CAN_RUN_WORKER, route, sandbox, budget and verifier; refusals are shown
 * with their reasons (CAPABILITY_NOT_READY / PLAN_NOT_APPROVED).
 */
export function ExecutePlanAction({ planId, approved, isAdmin }: { planId: string; approved: boolean; isAdmin: boolean }) {
  const [result, setResult] = useState<ExecuteResult | null>(null);
  if (!approved) return null;
  return (
    <div className="space-y-2">
      <ConfirmAction
        label="Jalankan rencana"
        variant="primary"
        isAdmin={isAdmin}
        icon={<Rocket className="h-4 w-4" aria-hidden />}
        confirmText="TaskSpec dari versi yang DISETUJUI diantrikan dan dibuat READY. Admission tetap memeriksa CAN_RUN_WORKER, agent pelaksana aktif, route, sandbox, budget dan verifier; tidak ada run yang dimulai tanpa itu, dan hasil worker tetap REVIEW sampai verifikasi + review + acceptance."
        onConfirm={async (reason) => {
          setResult(await post<ExecuteResult>(`/api/plans/${encodeURIComponent(planId)}/execute`, { reason }));
        }}
      />
      {result && (
        <Notice tone="info">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge status="QUEUED" label={`${result.tasks} task diantrikan`} />
            <StatusBadge status="READY" label={`READY ${result.ready ?? 0}`} />
            {(result.blocked ?? 0) > 0 && <StatusBadge status="BLOCKED" label={`terblokir ${result.blocked}`} />}
            <StatusBadge status={result.scheduler_enabled ? "ENABLED" : "DISABLED"} label={result.scheduler_enabled ? "Loop scheduler aktif" : "Loop scheduler nonaktif"} />
            {result.paused && <StatusBadge status="BLOCKED" label="Penjadwalan dijeda" />}
          </div>
          <p className="mt-1">
            Task READY di-admit oleh loop scheduler (aktifkan di <Link to="/work" className="underline">Pekerjaan Live</Link>) atau sekali jalan
            dengan <code>aeos work run-once</code>.
          </p>
        </Notice>
      )}
    </div>
  );
}
