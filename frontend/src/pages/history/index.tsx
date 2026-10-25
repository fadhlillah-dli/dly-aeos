import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Notice, PageHeader, Tabs } from "../../components/ui";
import { AttemptsTab } from "./AttemptsTab";
import { AuditTab } from "./AuditTab";
import { BackupsTab } from "./BackupsTab";
import { EmergencyTab } from "./EmergencyTab";
import { ExportTab } from "./ExportTab";
import { IncidentsTab } from "./IncidentsTab";
import { OpsTab } from "./OpsTab";
import { ReleasesTab } from "./ReleasesTab";
import { RetentionTab } from "./RetentionTab";
import { RetirementTab } from "./RetirementTab";
import { ServicesTab } from "./ServicesTab";
import { DecisionsTab, PublicationsTab, SnapshotsTab } from "./TimelineTabs";

// Riwayat, Audit & Pemulihan (§35 History / Audit / Recovery, §33, §41).
export default function Page() {
  const { domain } = useAuth();
  // "Diperiksa pada" of the page = the audit timeline read (same query/cache as the default Audit tab).
  const audit = useQuery({
    queryKey: ["history-audit", "limit=50"],
    queryFn: () => get<{ checked_at?: string | null }>("/api/control-center/history/audit?limit=50"),
  });
  return (
    <div>
      <PageHeader
        title="Riwayat, Audit & Pemulihan"
        question="Apa yang berubah, siapa menyetujui dan bagaimana memulihkannya?"
        source="audit_events, decisions, architecture_snapshots, publications, backup manifests, release provenance (waktu periksa per tab)"
        checkedAt={audit.data?.checked_at ?? null}
        evidenceLimit="Riwayat lokal append-only (bukan WORM); pemulihan terbukti hanya setelah restore test berhasil."
      />
      {!domain && (
        <div className="mb-4">
          <Notice tone="warn">
            Pilih domain PERSONAL atau OFFICE di header: audit, keputusan, publikasi, backup, SLI dan ekspor dibatasi pada domain yang
            Anda pilih sendiri.
          </Notice>
        </div>
      )}
      <Tabs
        tabs={[
          { id: "audit", label: "Audit", content: <AuditTab /> },
          { id: "attempts", label: "Percobaan", content: <AttemptsTab /> },
          { id: "decisions", label: "Keputusan", content: <DecisionsTab /> },
          { id: "snapshots", label: "Snapshot arsitektur", content: <SnapshotsTab /> },
          { id: "publications", label: "Publikasi", content: <PublicationsTab /> },
          { id: "backups", label: "Backup & pemulihan", content: <BackupsTab /> },
          { id: "retention", label: "Retensi", content: <RetentionTab /> },
          { id: "releases", label: "Rilis controller", content: <ReleasesTab /> },
          { id: "ops", label: "SLI, alert & flag", content: <OpsTab /> },
          { id: "incidents", label: "Insiden", content: <IncidentsTab /> },
          { id: "services", label: "Layanan & runbook", content: <ServicesTab /> },
          { id: "export", label: "Ekspor", content: <ExportTab /> },
          { id: "retirement", label: "Retirement", content: <RetirementTab /> },
          { id: "emergency", label: "Darurat", content: <EmergencyTab /> },
        ]}
      />
    </div>
  );
}
