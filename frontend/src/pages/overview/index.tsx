import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { RelatedLinks } from "../../components/control_center/Provenance";
import { Button, ErrorBox, Loading, Notice, PageHeader } from "../../components/ui";
import { ActivityFeed } from "./ActivityFeed";
import { IntegrationsCard } from "./IntegrationsCard";
import { OverviewView } from "./OverviewView";
import { SearchBox } from "./SearchBox";
import type { Overview } from "./types";

/** Domain-first (U-05): nothing domain-scoped is requested before the human picks a domain. */
export function NoDomainNotice() {
  return (
    <Notice tone="warn">
      <strong>Pilih domain PERSONAL atau OFFICE di header terlebih dahulu.</strong> Ringkasan, aktivitas, pencarian dan
      integrasi selalu dibatasi pada satu domain yang Anda pilih sendiri; sistem tidak pernah menebak atau mengganti domain.
    </Notice>
  );
}

// Ringkasan (§35 Overview / Readiness): "Apakah proyek ini dapat berjalan dan apa yang belum siap?"
export default function Page() {
  const { domain, projectId } = useAuth();
  const q = useQuery({
    queryKey: ["cc-overview", domain, projectId],
    queryFn: () => get<Overview>("/api/control-center/overview"),
    refetchInterval: 30_000,
    enabled: !!domain,
  });
  return (
    <div>
      <PageHeader
        title="Ringkasan"
        question="Apakah proyek ini dapat berjalan dan apa yang belum siap?"
        source={q.data?.source ?? "Control Center projection"}
        checkedAt={q.data?.generated_at ?? null}
        evidenceLimit="Status dihitung kode dari record; UNKNOWN/NOT-PROVEN tidak pernah dianggap lulus."
        mode={q.data?.mode}
        actions={
          <Button onClick={() => q.refetch()} busy={q.isFetching} disabled={!domain}>
            <RefreshCw className="h-4 w-4" aria-hidden /> Periksa ulang
          </Button>
        }
      />
      {!domain ? (
        <NoDomainNotice />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : q.data ? (
        <OverviewView data={q.data} />
      ) : null}
      {domain && (
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <ActivityFeed domain={domain} />
          <div className="space-y-4">
            <IntegrationsCard domain={domain} projectId={q.data?.project?.id ?? projectId} />
            <SearchBox domain={domain} />
          </div>
        </div>
      )}
      <RelatedLinks />
    </div>
  );
}
