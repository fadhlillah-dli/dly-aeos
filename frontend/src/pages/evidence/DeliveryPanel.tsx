import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, RefreshCw } from "lucide-react";
import { useAuth } from "../../auth/AuthContext";
import { ApiError } from "../../api/client";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Table } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { evidenceApi, type PRItem, type ProviderCaps } from "./api";
import { Status } from "./labels";

/** The code host of the capability probe: Bitbucket (OFFICE) or GitHub (PERSONAL); never a fixed "GitHub" title. */
export function providerLabel(provider: string | undefined, domain: string | null | undefined): string {
  const p = (provider ?? "").toLowerCase();
  return p === "bitbucket" || (!p && domain === "OFFICE") ? "Bitbucket" : "GitHub";
}

/** Residual-risk banner (§31): enforcement that the provider plan cannot provide is never claimed. */
export function ProviderRiskBanner({ caps }: { caps: ProviderCaps }) {
  const enforceable = caps.merge_gate === "CONFIGURED";
  const label = providerLabel(caps.provider, null);
  return (
    <div role="status" className="rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn">
      <div className="flex items-center gap-2 font-medium">
        <AlertTriangle className="h-4 w-4" aria-hidden />
        {enforceable
          ? "Branch protection dikonfigurasi, tetapi penegakannya belum diuji (NOT-PROVEN) — merge tetap tindakan manusia"
          : caps.merge_gate === "NOT_ENFORCEABLE"
            ? `Branch protection tidak dapat ditegakkan pada paket ${label} ini — merge tetap tindakan manusia`
            : caps.merge_gate === "NOT_CONFIGURED"
              ? "Branch protection belum dikonfigurasi — merge tetap tindakan manusia"
              : "Kemampuan penegakan merge tidak diketahui — merge tetap tindakan manusia"}
      </div>
      {caps.residual_risks.length > 0 && (
        <ul className="mt-1 list-disc pl-6">
          {caps.residual_risks.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function DeliveryPanel() {
  const { domain, isAdmin } = useAuth();
  const qc = useQueryClient();
  const caps = useQuery({ queryKey: ["provider-caps", domain], queryFn: evidenceApi.capabilities, retry: false, staleTime: 300_000 });
  const prs = useQuery({ queryKey: ["prs", domain], queryFn: evidenceApi.prs });
  const sync = useMutation({
    mutationFn: (id: string) => evidenceApi.syncPr(id),
    onSettled: () => qc.invalidateQueries({ predicate: (x) => ["prs", "gates", "acceptance"].includes(String(x.queryKey[0])) }),
  });
  const record = useMutation({
    mutationFn: evidenceApi.recordCapabilities,
    onSettled: () => qc.invalidateQueries({ queryKey: ["provider-caps"] }),
  });
  const noRepo = caps.error instanceof ApiError && caps.error.status === 404;
  const host = providerLabel(caps.data?.provider, domain);

  return (
    <div className="space-y-4">
      <Card
        title={`Kemampuan provider (${host}, hanya baca)`}
        actions={
          <>
            {caps.data && <Freshness at={caps.data.checked_at} />}
            {isAdmin && !noRepo && (
              <Button className="text-xs" busy={record.isPending} onClick={() => record.mutate()}>
                Catat sebagai bukti
              </Button>
            )}
          </>
        }
      >
        {caps.isLoading && <Loading label="Memeriksa provider…" />}
        {noRepo && (
          <EmptyState title={`Belum ada repository ${host} terdaftar di domain ini`}>
            Daftarkan repository proyek (portofolio), lalu jalankan <Mono>aeos delivery capabilities</Mono>.
          </EmptyState>
        )}
        {caps.error && !noRepo && <ErrorBox error={caps.error} />}
        {caps.data && (
          <div className="space-y-3">
            <ProviderRiskBanner caps={caps.data} />
            <KeyValue
              items={[
                ["Repository", <Mono key="r">{caps.data.repo}@{caps.data.branch ?? "?"}</Mono>],
                ["Visibilitas / paket", `${caps.data.visibility ?? "UNKNOWN"} / ${caps.data.plan ?? "UNKNOWN"}`],
                ["Credential", caps.data.credential],
                ["Gate merge", <Status key="g" status={caps.data.merge_gate} />],
                ["Penegakan diuji", <Status key="e" status={caps.data.enforcement_tested} />],
                ["Required checks", caps.data.required_status_checks.join(", ") || "—"],
              ]}
            />
            <ul className="space-y-1 text-sm">
              {caps.data.checks.map((c) => (
                <li key={c.key} className="flex flex-wrap items-center gap-2">
                  <Status status={c.status} /> <Mono>{c.key}</Mono> <span className="text-ink-2">{c.summary}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <ErrorBox error={record.error} />
      </Card>

      <Card title="Pull request yang disiapkan broker">
        {prs.isLoading && <Loading />}
        <ErrorBox error={prs.error ?? sync.error} />
        {prs.data && prs.data.items.length === 0 && (
          <EmptyState title="Belum ada PR">
            PR disiapkan dari tab “Gate” setelah penerimaan valid (broker hanya mendorong branch <Mono>aeos/*</Mono>).
          </EmptyState>
        )}
        {prs.data && prs.data.items.length > 0 && (
          <Table<PRItem>
            rows={prs.data.items}
            rowKey={(p) => p.id}
            columns={[
              {
                header: "PR",
                cell: (p) => (
                  <div>
                    {p.url ? (
                      <a className="inline-flex items-center gap-1 text-accent hover:underline" href={p.url} target="_blank" rel="noreferrer noopener">
                        #{p.number} <ExternalLink className="h-3 w-3" aria-hidden />
                      </a>
                    ) : (
                      <span>#{p.number ?? "?"}</span>
                    )}
                    <div className="mono text-xs text-ink-3">{p.head_ref} → {p.base_ref}</div>
                  </div>
                ),
              },
              { header: "Status provider", cell: (p) => <Status status={p.state} /> },
              {
                header: "Merge oleh",
                cell: (p) =>
                  p.merged ? (
                    <span className="text-sm">
                      {p.merged_by_login} <span className="text-xs text-ink-3">({p.merged_by_type})</span>
                    </span>
                  ) : (
                    <span className="text-xs text-ink-3">belum</span>
                  ),
              },
              {
                header: "Head diterima / teramati",
                cell: (p) => (
                  <span className="mono text-xs">
                    {shortHash(p.accepted_head_sha, 8)} / {shortHash(p.observed_head_sha, 8)}
                    {p.observed_head_sha && p.accepted_head_sha && p.observed_head_sha !== p.accepted_head_sha && (
                      <Status status="STALE" label="Head berubah" />
                    )}
                  </span>
                ),
              },
              {
                header: "Sinkron",
                cell: (p) => (
                  <span className="text-xs">
                    {/* an observation of the provider, not a verification */}
                    <Status
                      status={p.sync_status === "SYNCED" ? "CONFIGURED" : p.sync_status === "FAILED" ? "FAILED" : "NOT_CHECKED"}
                      label={p.sync_status === "SYNCED" ? "Teramati" : p.sync_status === "FAILED" ? "Gagal sinkron" : "Belum disinkronkan"}
                    />{" "}
                    {p.last_synced_at && <Freshness at={p.last_synced_at} label="Sinkron" />} {p.sync_error}
                  </span>
                ),
              },
              {
                header: "",
                cell: (p) =>
                  isAdmin ? (
                    <Button className="text-xs" busy={sync.isPending && sync.variables === p.id} onClick={() => sync.mutate(p.id)}>
                      <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Sinkronkan
                    </Button>
                  ) : null,
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
