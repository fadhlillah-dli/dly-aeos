import { ListChecks, Power, PowerOff } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, ErrorBox, Mono } from "../ui";
import { ConfirmAction } from "./ConfirmAction";
import type { AgentDef, AgentPrerequisites } from "./types";

/**
 * Owner enable/disable of an implementation agent (§17, §19). Enabling is refused by the backend with the exact
 * list of unmet prerequisites (AGENT_PREREQUISITES) - shown in the dialog - and "aktif" is never "sedang dipakai":
 * runs only start for an executed approved plan, through admission.
 */
export function AgentControls({ agent, isAdmin, onChanged }: { agent: AgentDef; isAdmin: boolean; onChanged: () => void }) {
  const [pre, setPre] = useState<AgentPrerequisites | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (agent.harness !== "opencode") {
    return (
      <p className="text-xs text-ink-3">
        Status agent ini mengikuti gate modul pemiliknya ({agent.harness}); tidak diaktifkan atau dinonaktifkan di sini.
      </p>
    );
  }
  const check = async () => {
    setChecking(true);
    setError(null);
    try {
      setPre(await get<AgentPrerequisites>(`/api/workforce/agents/${encodeURIComponent(agent.agent_key)}/prerequisites`));
    } catch (e) {
      setError(e);
    } finally {
      setChecking(false);
    }
  };
  return (
    <div className="space-y-2" data-testid={`agent-controls-${agent.agent_key}`}>
      <div className="flex flex-wrap items-start gap-2">
        {agent.enabled ? (
          <ConfirmAction
            label="Nonaktifkan"
            variant="danger"
            requireReason
            isAdmin={isAdmin}
            icon={<PowerOff className="h-4 w-4" aria-hidden />}
            confirmText="Scheduler tidak lagi meng-admit run baru untuk agent ini. Run yang sedang berjalan tidak dihentikan (gunakan Batalkan di Pekerjaan Live)."
            onConfirm={async (reason) => {
              await post(`/api/workforce/agents/${encodeURIComponent(agent.agent_key)}/disable`, { reason });
              setPre(null);
              onChanged();
            }}
          />
        ) : (
          <ConfirmAction
            label="Aktifkan"
            requireReason
            isAdmin={isAdmin}
            icon={<Power className="h-4 w-4" aria-hidden />}
            confirmText="Agent hanya diaktifkan bila semua prasyarat READY: engine untuk sandbox yang dipakai (OpenCode di host atau image worker), sandbox, route worker yang disetujui, dan pemeriksaan CAN_RUN_WORKER lainnya. Bila belum, daftar prasyarat ditampilkan dan tidak ada yang berubah. Aktif bukan berarti sedang dipakai: run hanya dimulai untuk rencana disetujui yang dijalankan."
            onConfirm={async (reason) => {
              await post(`/api/workforce/agents/${encodeURIComponent(agent.agent_key)}/enable`, { reason });
              setPre(null);
              onChanged();
            }}
          />
        )}
        <Button variant="ghost" busy={checking} onClick={check}>
          <ListChecks className="h-4 w-4" aria-hidden /> Periksa prasyarat
        </Button>
      </div>
      <ErrorBox error={error} />
      {pre && (
        <div className="rounded-md border border-line bg-surface-2 p-2 text-xs" aria-label="Prasyarat agent">
          <StatusBadge status={pre.ready ? "READY" : "BLOCKED"} label={pre.ready ? "Prasyarat siap" : "Prasyarat belum siap"} />
          {pre.reasons.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {pre.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
          <p className="mt-1 text-ink-3">
            Diperiksa dari {pre.source}; pemeriksaan <Mono>{pre.excluded_check}</Mono> dikecualikan karena itulah yang diaktifkan.
            {pre.sandbox?.kind ? ` Sandbox: ${pre.sandbox.kind}.` : ""}
          </p>
        </div>
      )}
    </div>
  );
}
