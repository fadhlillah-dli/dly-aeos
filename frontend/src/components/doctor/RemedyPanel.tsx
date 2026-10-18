import { Check, ClipboardCopy, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Button, KeyValue, Mono, Notice } from "../ui";
import type { Remedy } from "./types";

export function CopyButton({ text, label = "Salin" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      variant="secondary"
      className="text-xs"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          setDone(false);
        }
      }}
      aria-label={`${label}: ${text}`}
    >
      {done ? <Check className="h-3.5 w-3.5" aria-hidden /> : <ClipboardCopy className="h-3.5 w-3.5" aria-hidden />}
      {done ? "Disalin" : label}
    </Button>
  );
}

/**
 * §08 remedy: operation, source/version, target, privilege, cost, rollback and evidence to collect.
 * Copy only — the dashboard never runs remedies (no auto-install, IAM change or hidden fix).
 */
export function RemedyPanel({
  remedy,
  requiresDestructive,
  requiresPaidWrite,
}: {
  remedy: Partial<Remedy> | null | undefined;
  requiresDestructive?: boolean;
  requiresPaidWrite?: boolean;
}) {
  if (!remedy || !remedy.operation) {
    return <p className="text-sm text-ink-3">Tidak ada usulan perbaikan untuk hasil ini.</p>;
  }
  return (
    <div className="space-y-3" data-testid="remedy-panel">
      <div className="font-medium">{remedy.operation}</div>
      {remedy.command ? (
        <div className="flex flex-wrap items-start gap-2">
          <pre className="mono min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-surface-3 p-2">{remedy.command}</pre>
          <CopyButton text={remedy.command} label="Salin perintah" />
        </div>
      ) : (
        <p className="text-sm text-ink-3">Tidak ada perintah otomatis; ikuti sumber resmi di bawah.</p>
      )}
      <KeyValue
        items={[
          ["Sumber / versi", remedy.source ?? "—"],
          ["Target", remedy.target ? <Mono>{remedy.target}</Mono> : "—"],
          ["Hak akses", remedy.privilege ?? "—"],
          ["Biaya", remedy.cost ?? "—"],
          ["Rollback", remedy.rollback ?? "—"],
          ["Bukti yang dikumpulkan", remedy.evidence_to_collect ?? "—"],
          ["Dijalankan oleh", remedy.executed_by ?? "MANUSIA"],
        ]}
      />
      {(requiresDestructive || requiresPaidWrite) && (
        <div className="flex items-center gap-2 rounded-md bg-bad-bg p-2 text-sm text-bad">
          <ShieldAlert className="h-4 w-4" aria-hidden />
          {requiresDestructive && "Tindakan ini destruktif/tidak dapat dibatalkan. "}
          {requiresPaidWrite && "Tindakan ini menimbulkan biaya. "}
          Butuh keputusan manusia.
        </div>
      )}
      <Notice tone="info">Perintah hanya disalin. DLY AEOS tidak menjalankan perbaikan, instalasi, perubahan IAM atau top-up secara otomatis.</Notice>
    </div>
  );
}
