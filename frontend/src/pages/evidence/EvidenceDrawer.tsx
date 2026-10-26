import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, ErrorBox, KeyValue, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { evidenceApi } from "./api";
import { LevelBadge, Status, TrustBadge } from "./labels";
import { isObj, ScreenshotFacts, UiA11yReportView, VerificationReportView } from "./ReportViews";

const INTEGRITY_LABEL: Record<string, [string, string]> = {
  VERIFIED: ["VERIFIED", "Isi cocok dengan hash"],
  MISMATCH: ["FAILED", "Isi TIDAK cocok dengan hash"],
  MISSING: ["MISSING", "Berkas bukti tidak ditemukan"],
  NO_CONTENT: ["NOT_APPLICABLE", "Tanpa isi"],
};

export function EvidenceDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  // A report links other evidence (e.g. its ui-a11y TEST_REPORT): opened inside the drawer, with a way back.
  const [stack, setStack] = useState<string[]>([id]);
  const current = stack[stack.length - 1];
  const open = (next: string) => setStack((s) => (s[s.length - 1] === next ? s : [...s, next]));
  const back = () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s));
  const q = useQuery({ queryKey: ["evidence-detail", current], queryFn: () => evidenceApi.detail(current) });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    heading.current?.focus();
  }, [current]);
  const d = q.data;
  const integrity = d ? INTEGRITY_LABEL[d.integrity] : undefined;
  const json = d && isObj(d.content_json) ? d.content_json : null;
  const structured =
    d && json && d.kind === "VERIFICATION_REPORT" ? (
      <VerificationReportView report={json} onOpen={open} />
    ) : d && json && d.kind === "TEST_REPORT" && json.suite === "ui-a11y" ? (
      <UiA11yReportView report={json} />
    ) : null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Detail bukti"
        className="flex h-full w-full max-w-2xl flex-col overflow-y-auto border-l border-line bg-surface p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 ref={heading} tabIndex={-1} className="text-lg font-semibold focus:outline-none">
              Detail bukti
            </h2>
            <p className="break-words text-sm text-ink-2" aria-live="polite">
              {d?.claim ?? "…"}
            </p>
          </div>
          <div className="flex shrink-0 gap-1">
            {stack.length > 1 && (
              <Button variant="ghost" onClick={back}>
                <ArrowLeft className="h-4 w-4" aria-hidden /> Kembali
              </Button>
            )}
            <Button variant="ghost" aria-label="Tutup" onClick={onClose}>
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        </div>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        {d && (
          <div className="space-y-4">
            <KeyValue
              items={[
                ["Jenis", <Mono key="k">{d.kind}</Mono>],
                ["Level", <LevelBadge key="l" level={d.evidence_level} explanation={d.level_explanation} />],
                ["Arti level", d.level_explanation],
                [
                  "Level diminta",
                  d.level_requested && d.level_requested !== d.evidence_level
                    ? `${d.level_requested} → diturunkan: ${d.level_reason ?? ""}`
                    : (d.level_requested ?? "—"),
                ],
                ["Asal", <span key="o" className="flex flex-wrap items-center gap-2"><Mono>{d.origin}</Mono><TrustBadge trusted={d.trusted} /></span>],
                ["Identitas kolektor", <Mono key="c">{d.collector_identity}</Mono>],
                ["Hash isi", <Mono key="h">{d.content_hash}</Mono>],
                ["Integritas", integrity ? <Status key="i" status={integrity[0]} label={integrity[1]} /> : "—"],
                ["Klasifikasi", d.classification],
                ["Mode", <Status key="m" status={d.mode} />],
                ["Run / task", <span key="r" className="mono text-xs">{shortHash(d.run_id, 8)} / {shortHash(d.task_id, 8)}</span>],
                ["Waktu", absoluteTime(d.created_at)],
              ]}
            />
            {d.kind === "SCREENSHOT" && (
              <section>
                <h3 className="mb-1 text-sm font-semibold">Tangkapan layar</h3>
                <ScreenshotFacts env={d.environment ?? {}} />
              </section>
            )}
            {Object.keys(d.environment ?? {}).length > 0 && (
              <section>
                <h3 className="mb-1 text-sm font-semibold">Lingkungan (commit/config)</h3>
                <KeyValue
                  items={Object.entries(d.environment).map(([k, v]) => [
                    k,
                    <Mono key={k}>{v !== null && typeof v === "object" ? JSON.stringify(v) : String(v)}</Mono>,
                  ])}
                />
              </section>
            )}
            {d.integrity === "MISMATCH" && (
              <Notice tone="warn">
                <strong>Integritas gagal:</strong> isi yang tersimpan TIDAK cocok dengan hash yang tercatat. Jangan memakai
                isi ini sebagai bukti; periksa penyimpanan data dir dan kumpulkan ulang bukti dari kolektor tepercaya.
              </Notice>
            )}
            {d.integrity === "MISSING" && (
              <Notice tone="warn">Berkas bukti tidak ditemukan di data dir; klaim ini tidak dapat diperiksa ulang.</Notice>
            )}
            {!d.trusted && (
              <Notice tone="warn">
                Bukti ini berasal dari asal yang tidak tepercaya (mis. worker). Ia dicatat sebagai klaim E0 dan tidak
                pernah dihitung sebagai kelulusan.
              </Notice>
            )}
            {structured && d.integrity === "VERIFIED" && structured}
            {structured && d.integrity !== "VERIFIED" && (
              <Notice tone="warn">Tampilan terstruktur tidak ditampilkan: isi bukti ini tidak terverifikasi terhadap hash-nya.</Notice>
            )}
            <section>
              <h3 className="mb-1 text-sm font-semibold">Isi (sudah diredaksi)</h3>
              {d.content_note && <p className="text-sm text-ink-3">{d.content_note}</p>}
              {d.content !== null &&
                (structured ? (
                  <details>
                    <summary className="cursor-pointer text-xs text-ink-2">JSON mentah</summary>
                    <pre className="mono mt-1 max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface-2 p-3 text-xs">
                      {JSON.stringify(d.content_json, null, 2)}
                    </pre>
                  </details>
                ) : (
                  <pre className="mono max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface-2 p-3 text-xs">
                    {d.content_json !== undefined ? JSON.stringify(d.content_json, null, 2) : d.content}
                  </pre>
                ))}
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}
