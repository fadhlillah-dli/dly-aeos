import { HelpCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "./ui";

/**
 * §35 "Apa fungsinya? / Mengapa dipilih? / Dipakai oleh siapa? / Lihat bukti / Apa dampaknya jika
 * diganti? / Usulkan perubahan" — each opens the SAME source record (no separate AI-invented text).
 * Callers pass the content derived from the record; badges/costs/permissions stay computed by code.
 */
export interface ExplainContent {
  what?: ReactNode;
  why?: ReactNode;
  usedBy?: ReactNode;
  evidence?: ReactNode;
  impact?: ReactNode;
  propose?: ReactNode;
}

const LABELS: [keyof ExplainContent, string][] = [
  ["what", "Apa fungsinya?"],
  ["why", "Mengapa dipilih?"],
  ["usedBy", "Dipakai oleh siapa?"],
  ["evidence", "Lihat bukti"],
  ["impact", "Apa dampaknya jika diganti?"],
  ["propose", "Usulkan perubahan"],
];

export function ExplainButtons({ content }: { content: ExplainContent }) {
  const [open, setOpen] = useState<keyof ExplainContent | null>(null);
  return (
    <div>
      <div className="flex flex-wrap gap-1.5">
        {LABELS.filter(([k]) => content[k] !== undefined).map(([k, label]) => (
          <Button key={k} variant={open === k ? "primary" : "secondary"} className="text-xs" onClick={() => setOpen(open === k ? null : k)}>
            <HelpCircle className="h-3.5 w-3.5" aria-hidden /> {label}
          </Button>
        ))}
      </div>
      {open && <div className="mt-3 rounded-md border border-line bg-surface-2 p-3 text-sm">{content[open]}</div>}
    </div>
  );
}
