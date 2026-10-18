import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, Mono, PageHeader, Tabs } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { DocumentsPanel } from "./DocumentTree";
import { PublicationPanel } from "./PublicationPanel";
import type { DocumentsResponse } from "./types";
import { WorkItemsPanel } from "./WorkItemsPanel";

/**
 * Dokumen & Publikasi (master §27-§30, §41): Product 1 tree per project, publication jobs with preview →
 * human Approve & Publish (one approval per run) → outbox operations, and the work-item projection.
 */
export default function PublicationsPage() {
  const { projectId: sessionProject, domain } = useAuth();
  const [params] = useSearchParams();
  const projectId = params.get("project") ?? sessionProject;
  const qc = useQueryClient();
  const { status: streamStatus, lastSeenAt } = useEventStream((e) => {
    if (e.project_id && e.project_id !== projectId) return;
    if (e.kind.startsWith("publication")) {
      qc.invalidateQueries({ queryKey: ["publications"] });
      qc.invalidateQueries({ queryKey: ["publication"] });
    }
    if (e.kind.startsWith("publication.operation") || e.kind.startsWith("documents") || e.kind.startsWith("document")) {
      qc.invalidateQueries({ queryKey: ["project-documents"] });
    }
    if (e.kind.startsWith("work_items")) qc.invalidateQueries({ queryKey: ["work-items"] });
  }, !!projectId);
  const docs = useQuery({
    queryKey: ["project-documents", projectId],
    queryFn: () => get<DocumentsResponse>(`/api/projects/${projectId}/documents`),
    enabled: !!projectId,
  });
  const header = (
    <PageHeader
      title="Dokumen & Publikasi"
      question="Dokumen/tiket apa yang dipublikasikan dan bagaimana statusnya?"
      source={domain === "OFFICE" ? "AEOS DocumentRecord + outbox → Confluence / Jira" : "AEOS DocumentRecord + outbox → Google Drive/Docs / GitHub Issues"}
      checkedAt={docs.data?.checked_at ?? null}
      evidenceLimit="Terpublikasi ≠ diimplementasikan/terverifikasi; status issue adalah proyeksi; CONFIGURED bukan VERIFIED"
      mode={docs.data?.mode}
      actions={
        <span className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
          <StatusBadge status={streamStatus === "live" ? "LIVE" : "STALE"} label={streamStatus === "live" ? "Stream langsung" : "Stream terputus"} />
          {lastSeenAt && <span>event terakhir {new Date(lastSeenAt).toLocaleTimeString("id-ID")}</span>}
          <Link to="/decisions" className="text-accent underline">Keputusan terkait</Link>
          <Link to="/proposals" className="text-accent underline">Usulkan koreksi</Link>
        </span>
      }
    />
  );
  if (!projectId) {
    return (
      <div>
        {header}
        <EmptyState title="Belum ada proyek yang dipilih">
          Pilih proyek di pemilih proyek pada header atau dari <Link to="/projects" className="text-accent underline">Portofolio</Link>.
          Domain aktif: <Mono>{domain ?? "—"}</Mono>.
        </EmptyState>
      </div>
    );
  }
  return (
    <div>
      {header}
      <Tabs
        tabs={[
          { id: "docs", label: "Struktur Product 1", content: <DocumentsPanel projectId={projectId} /> },
          { id: "publications", label: "Publikasi", content: <PublicationPanel projectId={projectId} /> },
          { id: "work", label: "Work items", content: <WorkItemsPanel projectId={projectId} /> },
          { id: "connections", label: "Koneksi & remedy", content: <ConnectionsPanel projectId={projectId} /> },
        ]}
      />
    </div>
  );
}
