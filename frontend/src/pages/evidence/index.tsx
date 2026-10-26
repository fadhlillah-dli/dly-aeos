import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import { PageHeader, Tabs } from "../../components/ui";
import { useEventStream, type AeosEvent } from "../../lib/events";
import { AcceptancePanel } from "./AcceptancePanel";
import { AsvsPanel } from "./AsvsPanel";
import { evidenceApi } from "./api";
import { DeliveryPanel } from "./DeliveryPanel";
import { EvidenceLedger } from "./EvidenceLedger";
import { GatesPanel } from "./GatesPanel";
import { ReviewsPanel } from "./ReviewsPanel";
import { RunStatusPanel } from "./RunStatusPanel";

const LIVE_KINDS = /^(evidence\.|verification\.|review\.|acceptance\.|delivery\.|governance\.)/;

/**
 * Evidence & Decisions — evidence part (§35): claims/evidence/levels, review findings, acceptance
 * validity, delivery gates G0-G8 and provider residual risk. Everything is a projection of records
 * computed by code; nothing here grants approval or performs a merge.
 */
export default function EvidencePage() {
  const { domain } = useAuth();
  const qc = useQueryClient();
  const [taskId, setTaskId] = useState<string | null>(null);
  // Tabs keeps its own active state; bumping `jump` remounts it on the requested tab.
  const [nav, setNav] = useState({ tab: "status", jump: 0 });
  const head = useQuery({ queryKey: ["evidence", domain, { header: true }], queryFn: () => evidenceApi.list({}) });
  const onEvent = useCallback(
    (e: AeosEvent) => {
      if (LIVE_KINDS.test(e.kind)) {
        qc.invalidateQueries({
          predicate: (x) => ["evidence", "verification-summary", "reviews", "acceptance", "gates", "prs"].includes(String(x.queryKey[0])),
        });
      }
    },
    [qc],
  );
  useEventStream(onEvent);

  const selectTask = (id: string) => {
    setTaskId(id);
    setNav((n) => ({ tab: "gates", jump: n.jump + 1 }));
  };

  return (
    <div>
      <PageHeader
        title="Bukti & Penerimaan"
        question="Apa bukti kelulusan setiap klaim, siapa yang memverifikasinya, dan apa yang belum terbukti?"
        source="evidence_artifacts · review_records · acceptance_records · delivery_pull_requests"
        checkedAt={head.data?.checked_at ?? null}
        evidenceLimit="Ledger lokal append-only (bukan WORM); klaim worker = E0; verdict reviewer ≠ penerimaan; merge tetap tindakan manusia"
        relatedDecisionsHref="/decisions?target_type=trusted_suite"
        mode={head.data?.mode}
      />
      <Tabs
        key={nav.jump}
        initial={nav.tab}
        tabs={[
          { id: "status", label: "Status run", content: <RunStatusPanel onSelectTask={selectTask} /> },
          { id: "ledger", label: "Ledger bukti", content: <EvidenceLedger /> },
          { id: "reviews", label: "Review", content: <ReviewsPanel /> },
          { id: "acceptance", label: "Penerimaan", content: <AcceptancePanel /> },
          { id: "gates", label: "Gate delivery", content: <GatesPanel taskId={taskId} /> },
          { id: "delivery", label: "PR & provider", content: <DeliveryPanel /> },
          { id: "asvs", label: "Kontrol keamanan (ASVS)", content: <AsvsPanel /> },
        ]}
      />
    </div>
  );
}
