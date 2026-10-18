import { RelatedLinks } from "../../components/control_center/Provenance";
import { PageHeader, Tabs } from "../../components/ui";
import { AuditTab } from "./AuditTab";
import { BackupsTab } from "./BackupsTab";
import { EmergencyTab } from "./EmergencyTab";
import { ExportTab } from "./ExportTab";
import { OpsTab } from "./OpsTab";
import { ReleasesTab } from "./ReleasesTab";
import { DecisionsTab, PublicationsTab, SnapshotsTab } from "./TimelineTabs";

// Riwayat, Audit & Pemulihan (§35 History / Audit / Recovery, §33, §41).
export default function Page() {
  return (
    <div>
      <PageHeader
        title="Riwayat, Audit & Pemulihan"
        question="Apa yang berubah, siapa menyetujui dan bagaimana memulihkannya?"
        source="audit_events, decisions, architecture_snapshots, publications, backup manifests, release provenance (waktu periksa per tab)"
        evidenceLimit="Riwayat lokal append-only (bukan WORM); pemulihan terbukti hanya setelah restore test berhasil."
      />
      <Tabs
        tabs={[
          { id: "audit", label: "Audit", content: <AuditTab /> },
          { id: "decisions", label: "Keputusan", content: <DecisionsTab /> },
          { id: "snapshots", label: "Snapshot arsitektur", content: <SnapshotsTab /> },
          { id: "publications", label: "Publikasi", content: <PublicationsTab /> },
          { id: "backups", label: "Backup & pemulihan", content: <BackupsTab /> },
          { id: "releases", label: "Rilis controller", content: <ReleasesTab /> },
          { id: "ops", label: "SLI, alert & flag", content: <OpsTab /> },
          { id: "export", label: "Ekspor", content: <ExportTab /> },
          { id: "emergency", label: "Darurat", content: <EmergencyTab /> },
        ]}
      />
      <RelatedLinks />
    </div>
  );
}
