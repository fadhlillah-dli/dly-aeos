import { Gavel, MessageSquarePlus } from "lucide-react";
import { Link } from "react-router-dom";

const linkClass =
  "inline-flex items-center gap-1 rounded-md border border-line bg-surface px-2.5 py-1.5 text-sm text-ink-2 hover:bg-surface-2";

/** Link to the Decisions page, optionally highlighting one recorded decision. */
export function decisionHref(decisionId?: string | null): string {
  return decisionId ? `/decisions?decision=${encodeURIComponent(decisionId)}` : "/decisions";
}

/** Link that opens the "Usulkan perubahan" form prefilled as a COMPARE/RESEARCH review of `subject`. */
export function correctionHref(subject: string): string {
  return `/proposals?kind=COMPARE&source=${encodeURIComponent(subject)}`;
}

/**
 * §41: every page offers "Keputusan terkait" and "Usulkan koreksi" next to "Sumber data",
 * "Diperiksa pada" and "Batas bukti". A correction is a COMPARE proposal: reviewing it never
 * installs, activates, raises a budget or sends project data; only SUPER_ADMIN can submit it.
 */
export function RelatedLinks({ subject, decisionId }: { subject: string; decisionId?: string | null }) {
  return (
    <>
      <Link to={decisionHref(decisionId)} className={linkClass}>
        <Gavel className="h-4 w-4" aria-hidden /> Keputusan terkait
      </Link>
      <Link to={correctionHref(subject)} className={linkClass}>
        <MessageSquarePlus className="h-4 w-4" aria-hidden /> Usulkan koreksi
      </Link>
    </>
  );
}
