import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import type { AdrDetail, AdrSummary } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";

interface MciView {
  name: string;
  declared_status: string;
  status: string;
  compliance: string;
  note: string;
  verification_note: string;
  packs: string[];
  risk_classes: { key: string; label: string; threshold_status: string }[];
  checked_at: string;
}

interface SnapshotView {
  active: {
    id: string;
    version: string;
    content_hash: string;
    created_at: string | null;
    source: string;
    files: Record<string, { path: string; sha256: string; status: string }>;
    decision_count: number;
  } | null;
  status: string;
  current_content_hash: string;
  drift: boolean;
  remedy: string | null;
  mci_status: string;
  checked_at: string;
}

export function MciBanner() {
  const q = useQuery({ queryKey: ["policy", "mci"], queryFn: () => get<MciView>("/api/policy/mci") });
  if (!q.data) return null;
  return (
    <Notice tone="warn">
      <div className="flex flex-wrap items-center gap-2">
        <strong>{q.data.name}</strong> <StatusBadge status={q.data.status} /> · kepatuhan <StatusBadge status={q.data.compliance} />
      </div>
      <div className="mt-1">{q.data.verification_note}</div>
      <div className="mt-1 text-xs">
        Kelas risiko {q.data.risk_classes.map((r) => `${r.key} (${r.label})`).join(", ")} — ambang {q.data.risk_classes[0]?.threshold_status ?? "UNVERIFIED"}; tidak ada
        ambang yang dikarang.
      </div>
    </Notice>
  );
}

export function SnapshotCard({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["policy", "snapshot"], queryFn: () => get<SnapshotView>("/api/policy/snapshot") });
  const refresh = useMutation({
    mutationFn: () => post("/api/policy/snapshot/refresh"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["policy"] }),
  });
  const s = q.data;
  return (
    <Card
      title="Policy snapshot aktif"
      actions={
        isAdmin && s && (s.drift || !s.active) ? (
          <Button busy={refresh.isPending} onClick={() => refresh.mutate()}>
            Aktifkan snapshot terbaru
          </Button>
        ) : undefined
      }
    >
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? refresh.error} />
      {s && !s.active && (
        <EmptyState title="Belum ada policy snapshot aktif">
          Jalankan <Mono>{s.remedy ?? "aeos seed"}</Mono>
        </EmptyState>
      )}
      {s?.active && (
        <div className="grid gap-2">
          <KeyValue
            items={[
              ["Hash", <Mono key="h">{s.active.content_hash}</Mono>],
              ["Versi", `v${s.active.version} · ${relativeTime(s.active.created_at)} · ${s.active.source}`],
              ["Keputusan terikat", String(s.active.decision_count)],
              ["MCI", <StatusBadge key="m" status={s.mci_status} />],
              ["Drift", s.drift ? <StatusBadge key="d" status="DRIFTED" label="Berubah sejak snapshot" /> : <StatusBadge key="d" status="CONFIGURED" label="Sesuai" />],
            ]}
          />
          <ul className="text-xs text-ink-3">
            {Object.entries(s.active.files).map(([name, f]) => (
              <li key={name}>
                {f.path} <Mono>{shortHash(f.sha256)}</Mono> [{f.status}]
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

interface WriteEnablement {
  items: { provider: string; flag: boolean; decision_id: string | null; decision_expires_at: string | null; effective: boolean }[];
  domain: string;
}

export function WriteEnablementCard() {
  const q = useQuery({ queryKey: ["policy", "write-enablement"], queryFn: () => get<WriteEnablement>("/api/policy/write-enablement") });
  return (
    <Card title="Izin tulis eksternal per provider">
      <ErrorBox error={q.error} />
      {q.data && (
        <Table
          rows={q.data.items}
          rowKey={(i) => i.provider}
          columns={[
            { header: "Provider", cell: (i) => <Mono>{i.provider}</Mono> },
            { header: "Flag", cell: (i) => (i.flag ? "aktif" : "nonaktif") },
            { header: "Keputusan manusia", cell: (i) => (i.decision_id ? <Mono>{i.decision_id.slice(0, 8)}</Mono> : "—") },
            { header: "Efektif", cell: (i) => (i.effective ? <StatusBadge status="ENABLED" label="Diizinkan (tetap lewat interlock)" /> : <StatusBadge status="DISABLED" />) },
          ]}
        />
      )}
      <p className="mt-2 text-xs text-ink-3">
        Izin tulis hanya salah satu syarat interlock: bootstrap, AUTH aktor, domain/provider/target, versi + hash yang disetujui, dan capability yang belum
        kedaluwarsa tetap diperiksa setiap penulisan.
      </p>
    </Card>
  );
}

interface OfficePolicy {
  external_worker_allowed: boolean;
  approved_worker_routes: string[];
  claude_max_allowed_for_office_data: boolean;
  atlassian_deployment: string;
  office_authorization: boolean;
  authorized_providers: string[];
  source: string;
}

export function OfficePolicyCard() {
  const q = useQuery({ queryKey: ["policy", "office"], queryFn: () => get<OfficePolicy>("/api/policy/office") });
  const p = q.data;
  return (
    <Card title="Kebijakan OFFICE (default deny)">
      <ErrorBox error={q.error} />
      {p && (
        <KeyValue
          items={[
            ["Otorisasi perusahaan", p.office_authorization ? <StatusBadge key="a" status="CONFIGURED" label="Tercatat" /> : <StatusBadge key="a" status="MISSING" label="Belum ada" />],
            ["Worker eksternal", p.external_worker_allowed ? "diizinkan" : <StatusBadge key="w" status="DENIED" label="Ditolak" />],
            ["Route worker disetujui", p.approved_worker_routes.join(", ") || "—"],
            ["Claude Max untuk data kantor", p.claude_max_allowed_for_office_data ? "diizinkan" : <StatusBadge key="c" status="DENIED" label="Belum disetujui" />],
            ["Provider diotorisasi", p.authorized_providers.join(", ") || "—"],
            ["Atlassian", <StatusBadge key="d" status={p.atlassian_deployment === "UNKNOWN" ? "UNKNOWN" : "CONFIGURED"} label={p.atlassian_deployment} />],
            ["Sumber", p.source],
          ]}
        />
      )}
    </Card>
  );
}

export function AdrList() {
  const q = useQuery({ queryKey: ["adrs"], queryFn: () => get<{ items: AdrSummary[]; remedy: string | null }>("/api/adrs") });
  const [open, setOpen] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["adrs", open], queryFn: () => get<AdrDetail>(`/api/adrs/${open}`), enabled: !!open });
  return (
    <Card title="Register ADR">
      <ErrorBox error={q.error} />
      {q.data && !q.data.items.length && (
        <EmptyState title="ADR belum dimuat">
          Jalankan <Mono>{q.data.remedy ?? "aeos policy adrs --load"}</Mono>
        </EmptyState>
      )}
      {q.data && q.data.items.length > 0 && (
        <ul className="grid gap-2">
          {q.data.items.map((a) => (
            <li key={a.key} className="rounded border border-line p-2">
              <button type="button" className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === a.key ? null : a.key)} aria-expanded={open === a.key}>
                <Mono>{a.key}</Mono>
                <span className="font-medium">{a.title}</span>
                <StatusBadge status={a.status === "ACCEPTED" ? "ACCEPTED" : a.status === "SUPERSEDED" ? "STALE" : "PENDING"} label={a.status} />
                <span className="text-xs text-ink-3">
                  {a.source_path} · <Mono>{shortHash(a.content_hash)}</Mono>
                </span>
              </button>
              {open === a.key && detail.data && (
                <div className="mt-2 grid gap-2 text-sm">
                  {Object.entries(detail.data.sections).map(([h, body]) => (
                    <div key={h}>
                      <div className="font-medium">{h}</div>
                      <p className="whitespace-pre-wrap text-ink-2">{body}</p>
                    </div>
                  ))}
                  {detail.data.linked_decisions.length > 0 && (
                    <div className="text-xs">Keputusan terkait: {detail.data.linked_decisions.map((d) => `${d.kind} ${d.status} (${d.id.slice(0, 8)})`).join(", ")}</div>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
