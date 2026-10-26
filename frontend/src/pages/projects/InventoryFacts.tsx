import { AlertTriangle } from "lucide-react";
import { StatusBadge } from "../../components/StatusBadge";
import { Freshness, KeyValue, Mono } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { formatBytes } from "./badges";
import { BaselineFacts } from "./TestBaselinePanel";
import type { InventoryView } from "./types";

function Group({ title, data }: { title: string; data: Record<string, string[]> }) {
  const entries = Object.entries(data);
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">{title}</div>
      {entries.length === 0 ? (
        <div className="text-xs text-ink-3">Tidak ditemukan pada commit ini</div>
      ) : (
        <ul className="space-y-0.5 text-sm">
          {entries.map(([k, paths]) => (
            <li key={k}>
              <span className="font-medium">{k}</span>: <span className="text-ink-2">{paths.slice(0, 4).join(", ")}</span>
              {paths.length > 4 && <span className="text-ink-3"> +{paths.length - 4}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** §05 step 6 facts — read-only, exact commit, coverage gaps shown (§37). */
export function InventoryFacts({ inv }: { inv: InventoryView }) {
  return (
    <div className="space-y-4 rounded-md border border-line bg-surface-2 p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
        <span>Commit</span>
        <Mono>{shortHash(inv.commit, 12)}</Mono>
        {inv.branch && (
          <span>
            branch <Mono>{inv.branch}</Mono>
          </span>
        )}
        {inv.commit_is_head === false && (
          <StatusBadge status="DRIFTED" label={`bukan HEAD (HEAD ${inv.head_commit ? shortHash(inv.head_commit, 12) : "tidak diketahui"})`} />
        )}
        <span>
          {inv.files} berkas · {formatBytes(inv.total_bytes)}
        </span>
        <span>bukti {inv.evidence_level} (statis)</span>
        <Freshness at={inv.checked_at} staleAfterSeconds={86400} />
      </div>
      <KeyValue
        items={[
          ["Bahasa", inv.languages.length ? inv.languages.slice(0, 8).map((l) => `${l.language} (${l.files})`).join(", ") : "—"],
          [
            "Manifest dependensi",
            inv.dependency_manifests.length ? (
              <ul className="space-y-0.5">
                {inv.dependency_manifests.slice(0, 12).map((m) => (
                  <li key={m.path} className="flex flex-wrap items-center gap-2">
                    <Mono>{m.path}</Mono> <span className="text-xs text-ink-3">{m.ecosystem}</span>
                    <StatusBadge
                      status={m.lockfile_status === "PRESENT" ? "CONFIGURED" : m.lockfile_status === "MISSING" ? "MISSING" : "NOT_APPLICABLE"}
                      label={m.lockfile_status === "PRESENT" ? "lockfile ada" : m.lockfile_status === "MISSING" ? "lockfile tidak ada" : "tanpa lockfile"}
                    />
                  </li>
                ))}
              </ul>
            ) : (
              "—"
            ),
          ],
          [
            "Tes",
            <span key="t" className="flex flex-wrap items-center gap-2">
              {inv.tests.frameworks.length ? inv.tests.frameworks.join(", ") : "framework tidak terdeteksi"}
              {inv.tests.directories.length > 0 && <span className="text-ink-3">· direktori {inv.tests.directories.slice(0, 4).join(", ")}</span>}
            </span>,
          ],
          ["Baseline tes", inv.tests.baseline ? <BaselineFacts key="b" b={inv.tests.baseline} /> : <StatusBadge key="b" status="NOT_TESTED" label="Baseline belum dijalankan" />],
          ["Kondisi working tree", inv.working_tree.dirty === null ? "UNKNOWN" : inv.working_tree.dirty ? `dirty (${inv.working_tree.entries} entri)` : "bersih"],
        ]}
      />
      <div className="grid gap-4 md:grid-cols-2">
        <Group title="CI" data={inv.ci} />
        <Group title="Deployment / IaC" data={inv.deployment} />
        <Group title="Kontrak / skema" data={inv.contracts} />
        <Group title="Dokumentasi" data={inv.docs} />
      </div>
      {(inv.sensitive_paths.length > 0 || inv.symlinks.length > 0) && (
        <div className="text-xs text-warn">
          {inv.sensitive_paths.length > 0 && <div>Path sensitif terdeteksi (tidak dibaca ke konteks): {inv.sensitive_paths.slice(0, 6).join(", ")}</div>}
          {inv.symlinks.length > 0 && <div>Symlink dicatat, tidak diikuti: {inv.symlinks.slice(0, 6).join(", ")}</div>}
        </div>
      )}
      <div>
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Batas cakupan</div>
        <ul className="space-y-0.5 text-xs text-ink-2">
          {inv.coverage_gaps.map((g) => (
            <li key={g} className="flex items-start gap-1">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warn" aria-hidden /> {g}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
