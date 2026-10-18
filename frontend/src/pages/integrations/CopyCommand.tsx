import { Check, Copy } from "lucide-react";
import { useState } from "react";

/** A remedy command the owner runs locally; copying never executes anything (§08 remedy rule). */
export function CopyCommand({ command, why }: { command: string; why?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <code className="mono min-w-0 break-all rounded bg-surface-3 px-2 py-1">{command}</code>
      <button
        type="button"
        onClick={copy}
        className="inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 text-xs hover:bg-surface-3"
        aria-label={`Salin perintah ${command}`}
      >
        {copied ? <Check className="h-3.5 w-3.5" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
        {copied ? "Tersalin" : "Salin"}
      </button>
      {why && <span className="text-xs text-ink-3">{why}</span>}
    </div>
  );
}
