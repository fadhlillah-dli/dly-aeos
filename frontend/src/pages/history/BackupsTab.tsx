import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Button, EmptyState, ErrorBox, Loading, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import type { BackupList, BackupRow, HashVerification, RestoreTest } from "./types";

function size(n?: number) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function partitionCounts(result: RestoreTest | HashVerification): string {
  const counts = "stats" in result ? result.stats.files_verified_by_partition : result.files_checked_by_partition;
  if (!counts) return "";
  return Object.entries(counts)
    .map(([part, n]) => `${part}: ${n}`)
    .join(", ");
}

/** Last restore test per backup: status + time + evidence, so "lulus tanpa bukti" never looks ready (§33). */
export function LastRestoreTest({ row }: { row: BackupRow }) {
  const t = row.last_restore_test;
  if (!t) return <span className="text-xs text-warn">Belum pernah diuji</span>;
  return (
    <span className="flex flex-col gap-0.5 text-xs">
      <span className="inline-flex items-center gap-1">
        <OpsBadge status={t.status} />
        <span className="text-ink-3" title={absoluteTime(t.finished_at)}>
          {relativeTime(t.finished_at)}
        </span>
      </span>
      {t.evidence_id ? (
        <span className="text-ink-3">
          Bukti <Mono>{t.evidence_id.slice(0, 8)}</Mono>
        </span>
      ) : (
        <span className="text-warn">{t.evidence_note || "Tidak ada bukti tercatat"}</span>
      )}
    </span>
  );
}

export function RecoveryNotice({ data }: { data: BackupList }) {
  if (data.recovery_ready) {
    return (
      <Notice tone="info">
        Backup teruji-restore terbaru: <Mono>{data.latest_restore_tested}</Mono>. Restore diuji di direktori sementara terisolasi
        (bukan pemulihan produksi).
      </Notice>
    );
  }
  return (
    <Notice tone="warn">
      <strong>Restore belum diuji</strong> — pemulihan belum terbukti. Buat backup lalu jalankan restore test; backup tidak dianggap
      siap sebelum restore test berhasil dan tercatat sebagai bukti.
    </Notice>
  );
}

function ResultBox({ title, result }: { title: string; result: RestoreTest | HashVerification }) {
  const isRestore = "status" in result;
  const status = isRestore ? result.status : result.ok ? "OK" : "MISMATCH";
  return (
    <div className="rounded-md border border-line bg-surface-2 p-3 text-sm" role="status">
      <div className="flex items-center gap-2 font-medium">
        {title} <OpsBadge status={status} />
      </div>
      {!isRestore && <div className="mt-1 text-xs text-ink-3">Verifikasi hash bukan restore test; backup belum dianggap siap karenanya.</div>}
      {"evidence_id" in result && result.evidence_id && (
        <div className="mt-1">
          Bukti: <Mono>{result.evidence_id}</Mono>
        </div>
      )}
      {"evidence_note" in result && result.evidence_note && <div className="mt-1 text-warn">{result.evidence_note}</div>}
      {partitionCounts(result) && <div className="mt-1 text-xs text-ink-3">Berkas terverifikasi: {partitionCounts(result)}</div>}
      {result.scope && result.scope !== "ALL" && (
        <div className="mt-1 text-xs text-ink-3">
          Rincian per partisi hanya untuk domain aktif ({result.scope}); status keseluruhan tetap mencakup semua partisi.
        </div>
      )}
      {result.problems.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-bad">
          {result.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function BackupsTab() {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ops-backups"], queryFn: () => get<BackupList>("/api/ops/backups") });
  const [result, setResult] = useState<{ title: string; data: RestoreTest | HashVerification } | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["ops-backups"] });
  const create = useMutation({ mutationFn: () => post<BackupRow>("/api/ops/backups", { label: "dashboard" }), onSuccess: refresh });
  const verify = useMutation({
    mutationFn: (id: string) => post<HashVerification>(`/api/ops/backups/${id}/verify`),
    onSuccess: (data) => {
      setResult({ title: "Verifikasi hash", data });
      refresh();
    },
  });
  const restoreTest = useMutation({
    mutationFn: (id: string) => post<RestoreTest>(`/api/ops/backups/${id}/restore-test`),
    onSuccess: (data) => {
      setResult({ title: "Restore test", data });
      refresh();
    },
  });
  const error = create.error ?? verify.error ?? restoreTest.error;

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const data = q.data!;
  return (
    <div className="space-y-3">
      <RecoveryNotice data={data} />
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => create.mutate()} busy={create.isPending} disabled={!isAdmin} title={isAdmin ? "" : "Hanya SUPER_ADMIN"}>
          Buat backup
        </Button>
        {!isAdmin && <span className="text-xs text-ink-3">VIEWER hanya dapat melihat.</span>}
        <span className="text-xs text-ink-3">
          Restore ke data live hanya lewat CLI lokal dengan server berhenti: <Mono>aeos ops backup restore &lt;id&gt;</Mono>
        </span>
      </div>
      {error ? <ErrorBox error={error} /> : null}
      {result && <ResultBox title={result.title} result={result.data} />}
      {data.items.length === 0 ? (
        <EmptyState title="Belum ada backup">
          Buat dari tombol di atas atau jalankan <Mono>aeos ops backup create</Mono>, lalu <Mono>aeos ops backup restore-test &lt;id&gt;</Mono>.
        </EmptyState>
      ) : (
        <Table
          rows={data.items}
          rowKey={(b) => b.backup_id}
          columns={[
            { header: "Backup", cell: (b) => <Mono>{b.backup_id}</Mono> },
            { header: "Dibuat", cell: (b) => (b.created_at ? <span title={absoluteTime(b.created_at)}>{relativeTime(b.created_at)}</span> : "—") },
            { header: "Kesiapan", cell: (b) => <OpsBadge status={b.readiness} /> },
            { header: "Uji restore terakhir", cell: (b) => <LastRestoreTest row={b} /> },
            { header: "Ukuran", cell: (b) => size(b.size_bytes) },
            { header: "Hash DB", cell: (b) => <Mono>{shortHash(b.db_sha256)}</Mono> },
            { header: "Retensi", cell: (b) => <OpsBadge status={b.retention} /> },
            {
              header: "Aksi",
              cell: (b) =>
                b.readiness === "INCOMPLETE" ? (
                  <span className="text-xs text-ink-3">{b.note}</span>
                ) : (
                  <span className="flex flex-wrap gap-1">
                    <Button className="text-xs" disabled={!isAdmin} busy={verify.isPending && verify.variables === b.backup_id} onClick={() => verify.mutate(b.backup_id)}>
                      Verifikasi hash
                    </Button>
                    <Button className="text-xs" disabled={!isAdmin} busy={restoreTest.isPending && restoreTest.variables === b.backup_id} onClick={() => restoreTest.mutate(b.backup_id)}>
                      Restore test
                    </Button>
                  </span>
                ),
            },
          ]}
        />
      )}
      <Provenance source={data.source} checkedAt={data.checked_at} evidenceLimit={data.evidence_limit} />
    </div>
  );
}
