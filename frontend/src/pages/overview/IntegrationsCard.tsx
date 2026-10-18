import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { Provenance } from "../../components/control_center/Provenance";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, EmptyState, ErrorBox, Loading, Mono } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import type { IntegrationList } from "./types";

/**
 * Integration settings / health summary (§35 "integration settings/health", "Integrations & Access":
 * credential status/ref/expiry, never secret values; CONNECTED is not VERIFIED).
 */
export function IntegrationsView({ data }: { data: IntegrationList }) {
  return (
    <>
      {data.items.length === 0 ? (
        <EmptyState title="Belum ada integrasi tercatat untuk domain/proyek ini">
          Hubungkan target resmi lebih dulu, mis. <Mono>aeos publish google-connect --client-secret &lt;file&gt;</Mono> atau token
          GitHub lewat <Mono>aeos secrets set personal/github --stdin</Mono>, lalu daftarkan di halaman Integrasi & Akses.
        </EmptyState>
      ) : (
        <ul className="space-y-2 text-sm" aria-label="Integrasi">
          {data.items.map((i) => (
            <li key={i.id} className="rounded-md border border-line p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{i.provider}</span>
                <span className="text-ink-3">{i.purpose}</span>
                {i.project_name ? <span className="text-xs text-ink-3">· {i.project_name}</span> : null}
                <span className="ml-auto inline-flex flex-wrap items-center gap-1">
                  <StatusBadge status={i.credential.status} label={`Credential: ${i.credential.status}`} />
                  <StatusBadge
                    status={i.verification === "NOT_TESTED" ? "NOT_TESTED" : "CONFIGURED"}
                    label={i.verification === "NOT_TESTED" ? "Belum diuji fungsi" : `Diverifikasi ${relativeTime(i.last_verified_at)}`}
                  />
                </span>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-ink-3">
                {i.root_id ? (
                  <span>
                    Root/target: <Mono>{i.root_id}</Mono>
                  </span>
                ) : null}
                {i.credential_ref ? (
                  <span>
                    Ref: <Mono>{i.credential_ref}</Mono> (nilai tidak pernah ditampilkan)
                  </span>
                ) : null}
                {i.credential.expires_at ? <span>Kedaluwarsa {relativeTime(i.credential.expires_at)}</span> : null}
                {i.credential.reason ? <span className="text-warn">{i.credential.reason}</span> : null}
              </div>
              {i.checks.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {i.checks.map((c) => (
                    <StatusBadge key={c.check_id} status={c.status} label={`${c.check_id}: ${c.status}`} />
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <Provenance source={data.source} checkedAt={data.checked_at} evidenceLimit={data.evidence_limit} />
    </>
  );
}

export function IntegrationsCard({ domain, projectId }: { domain: string; projectId: string | null }) {
  const q = useQuery({
    queryKey: ["cc-integrations", domain, projectId],
    queryFn: () =>
      get<IntegrationList>(`/api/control-center/integrations${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  });
  return (
    <Card
      title="Integrasi & kesehatan akses"
      actions={
        <Link to="/integrations" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">
          Integrasi & Akses <ArrowRight className="h-4 w-4" aria-hidden />
        </Link>
      }
    >
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : q.data ? <IntegrationsView data={q.data} /> : null}
    </Card>
  );
}
