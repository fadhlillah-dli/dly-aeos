import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Lightbulb } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { MappedBadge, PROPOSAL_STATUS } from "../../components/proposals/MappedBadge";
import { Button, Card, EmptyState, ErrorBox, inputClass, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import { ProposalForm } from "./ProposalForm";
import type { Meta, ProposalRow } from "./types";

const STATUSES = Object.keys(PROPOSAL_STATUS);
const KINDS = ["ADD", "REPLACE", "UPGRADE", "REMOVE", "COMPARE"];

export default function ProposalsPage() {
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();
  const component = params.get("component");
  const source = params.get("source");
  const [open, setOpen] = useState(Boolean(component || source));
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const nav = useNavigate();
  const qc = useQueryClient();
  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (kind) qs.set("kind", kind);
  const list = useQuery({
    queryKey: ["proposals", status, kind],
    queryFn: () => get<{ items: ProposalRow[]; checked_at: string; source: string }>(`/api/proposals?${qs.toString()}`),
  });
  const meta = useQuery({ queryKey: ["proposals", "meta"], queryFn: () => get<Meta>("/api/proposals/meta") });
  const rows = list.data?.items ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Usulan Upgrade"
        question="Bagaimana mengusulkan dan menilai komponen baru?"
        source={list.data?.source ?? "change_proposals + decisions"}
        checkedAt={list.data?.checked_at ?? null}
        evidenceLimit="Usulan dan review bukan instalasi/aktivasi; keputusan manusia terikat baseline/candidate hash"
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={() => setOpen(true)} disabled={open}>
              <Lightbulb className="h-4 w-4" aria-hidden /> Usulkan perubahan
            </Button>
          ) : undefined
        }
      />
      {!isAdmin && <Notice>Peran VIEWER hanya dapat melihat usulan; pengajuan dan keputusan memerlukan SUPER_ADMIN.</Notice>}
      {open && isAdmin && (
        <Card title={component ? `Usulkan perubahan untuk komponen ${component}` : "Usulkan perubahan"}>
          <ProposalForm
            meta={meta.data}
            initialComponent={component}
            initialSource={source}
            onCancel={() => {
              setOpen(false);
              setParams({});
            }}
            onCreated={(p) => {
              qc.invalidateQueries({ queryKey: ["proposals"] });
              nav(`/proposals/${p.id}`);
            }}
          />
        </Card>
      )}
      <Card
        title="Daftar usulan"
        actions={
          <div className="flex flex-wrap gap-2">
            <select aria-label="Filter status" className={inputClass + " w-56"} value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Semua status</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {PROPOSAL_STATUS[s][1]}
                </option>
              ))}
            </select>
            <select aria-label="Filter jenis" className={inputClass + " w-36"} value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">Semua jenis</option>
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {list.isLoading && <Loading />}
        <ErrorBox error={list.error} />
        {list.data && rows.length === 0 && (
          <EmptyState title="Belum ada usulan pada domain ini">
            Gunakan tombol “Usulkan perubahan” di sini, di Tools & Repos atau di detail komponen. Mengajukan URL tidak memasang apa pun.
          </EmptyState>
        )}
        {rows.length > 0 && (
          <Table
            rows={rows}
            rowKey={(r) => r.id}
            onRowClick={(r) => nav(`/proposals/${r.id}`)}
            columns={[
              {
                header: "Usulan",
                cell: (r) => (
                  <div className="min-w-0">
                    <Link to={`/proposals/${r.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
                      {r.title}
                    </Link>
                    <div className="truncate text-xs text-ink-3">{r.canonical_identities.join(", ") || r.existing_component_ids.join(", ")}</div>
                    {r.duplicate_of && (
                      <div className="text-xs text-warn">
                        Duplikat dari <Link to={`/proposals/${r.duplicate_of}`}>usulan {r.duplicate_of.slice(0, 8)}</Link>
                      </div>
                    )}
                    {r.parent_id && <div className="text-xs text-ink-3">Bagian dari usulan {r.parent_id.slice(0, 8)}</div>}
                  </div>
                ),
              },
              { header: "Jenis", cell: (r) => <Mono>{r.kind}</Mono> },
              { header: "Status", cell: (r) => <MappedBadge value={r.status} map={PROPOSAL_STATUS} /> },
              { header: "Cakupan", cell: (r) => (r.scope_type === "PROJECT" ? `Proyek ${r.project_id?.slice(0, 8)}` : "Platform") },
              { header: "Bagian", cell: (r) => (r.parts_count ? `${r.parts_count} bagian` : "—") },
              { header: "Diajukan", cell: (r) => relativeTime(r.created_at) },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
