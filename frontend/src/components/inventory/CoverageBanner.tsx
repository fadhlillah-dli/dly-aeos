import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Notice } from "../ui";
import type { Coverage } from "./types";

/** §37: inventory that has not read an environment/lockfile shows a coverage gap, never "all mapped". */
export function CoverageBanner({ coverage }: { coverage: Coverage | undefined }) {
  const [open, setOpen] = useState(false);
  if (!coverage) return null;
  const important = coverage.gaps.filter((g) => g.severity === "HIGH" || g.severity === "MEDIUM");
  const info = coverage.gaps.filter((g) => g.severity !== "HIGH" && g.severity !== "MEDIUM");
  const t = coverage.totals;
  return (
    <div className="mb-4 space-y-2" data-testid="coverage-banner">
      {important.length > 0 && (
        <Notice tone="warn">
          <div className="font-medium">Cakupan inventaris belum lengkap</div>
          <ul className="mt-1 list-disc pl-5">
            {important.map((g) => (
              <li key={g.code}>{g.message}</li>
            ))}
          </ul>
        </Notice>
      )}
      <div className="rounded-md border border-line bg-surface p-3 text-xs text-ink-2">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>Komponen: {t.components ?? "?"}</span>
          <span>Dapat diprobe: {t.probeable ?? "?"}</span>
          <span>Teramati (segar): {t.observed_fresh ?? 0}</span>
          <span>Hanya observasi usang: {t.stale_only ?? 0}</span>
          <span>Belum pernah diprobe: {t.never_probed ?? 0}</span>
          <span>Kandidat tidak diprobe: {t.not_probed_candidates ?? 0}</span>
          <span>Layanan eksternal: {t.external_services ?? 0}</span>
        </div>
        {info.length > 0 && (
          <button className="mt-2 inline-flex items-center gap-1 text-ink-3 hover:text-ink" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
            Batas cakupan lain ({info.length})
          </button>
        )}
        {open && (
          <ul className="mt-1 list-disc pl-5">
            {info.map((g) => (
              <li key={g.code}>{g.message}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
