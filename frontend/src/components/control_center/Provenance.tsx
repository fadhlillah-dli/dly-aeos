import { Database, Lightbulb, Scale } from "lucide-react";
import { Link } from "react-router-dom";
import { Freshness } from "../ui";

/**
 * Per-card provenance line (§41): "Sumber data", "Diperiksa pada", "Batas bukti". The optional
 * footer adds "Keputusan terkait" and "Usulkan koreksi" links that open the same records.
 */
export function Provenance({
  source,
  checkedAt,
  evidenceLimit,
  staleAfterSeconds = 900,
}: {
  source?: string;
  checkedAt?: string | null;
  evidenceLimit?: string;
  staleAfterSeconds?: number;
}) {
  return (
    <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-2 text-xs text-ink-3">
      {source && (
        <div className="flex items-center gap-1">
          <Database className="h-3 w-3" aria-hidden />
          <dt>Sumber data:</dt>
          <dd className="text-ink-2">{source}</dd>
        </div>
      )}
      <div className="flex items-center gap-1">
        <dt>Diperiksa pada:</dt>
        <dd>
          <Freshness at={checkedAt ?? null} staleAfterSeconds={staleAfterSeconds} label="" />
        </dd>
      </div>
      {evidenceLimit && (
        <div className="flex items-center gap-1">
          <dt>Batas bukti:</dt>
          <dd className="text-ink-2">{evidenceLimit}</dd>
        </div>
      )}
    </dl>
  );
}

export function RelatedLinks() {
  return (
    <div className="mt-6 flex flex-wrap gap-4 text-sm text-ink-2">
      <Link to="/decisions" className="inline-flex items-center gap-1 hover:underline">
        <Scale className="h-4 w-4" aria-hidden /> Keputusan terkait
      </Link>
      <Link to="/proposals" className="inline-flex items-center gap-1 hover:underline">
        <Lightbulb className="h-4 w-4" aria-hidden /> Usulkan koreksi
      </Link>
    </div>
  );
}
