import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useAuth } from "../../auth/AuthContext";
import { Card, EmptyState, ErrorBox, KeyValue, Loading, Mono, Notice } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";
import { evidenceApi, type ReviewItem } from "./api";
import { Status } from "./labels";

const NR_LABELS: Record<string, string> = {
  existing_behavior: "Perilaku yang sudah ada",
  callers: "Pemanggil langsung/berdekatan",
  contract_consumers: "Konsumen kontrak",
  authn_authz: "Autentikasi/otorisasi",
  fallback_order: "Fallback/urutan",
  state_data: "State/data",
  recovery: "Pemulihan operasional",
  integration: "Integrasi E2E",
};

function val(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join("; ") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function Section({ title, data }: { title: string; data: Record<string, unknown> }) {
  const entries = Object.entries(data ?? {});
  return (
    <section>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">{title}</h4>
      {entries.length ? <KeyValue items={entries.map(([k, v]) => [k, val(v)])} /> : <p className="text-sm text-ink-3">—</p>}
    </section>
  );
}

function ReviewCard({ r }: { r: ReviewItem }) {
  const [open, setOpen] = useState(false);
  return (
    <Card
      title={
        <button className="flex items-center gap-1.5 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
          Review {shortHash(r.id, 8)} · run {shortHash(r.run_id, 8)}
        </button>
      }
      actions={
        <>
          <Status status={r.status} />
          <Status status={r.verdict} />
          {r.mode !== "LIVE" && <Status status={r.mode === "MANUAL_FALLBACK" ? "PARTIAL" : r.mode} label={r.mode === "MANUAL_FALLBACK" ? "Paket manual" : undefined} />}
        </>
      }
    >
      <div className="space-y-2 text-sm">
        <KeyValue
          items={[
            ["Reviewer", <span key="r">{r.reviewer_identity} · model {r.model_resolved ?? "UNKNOWN"}</span>],
            ["Verdict diminta → dicatat", `${r.verdict_requested ?? "—"} → ${r.verdict}`],
            ["Alasan / status", r.status_reason || "—"],
            ["Hash paket", <Mono key="p">{shortHash(r.package_hash, 16)}</Mono>],
            ["Repair", r.repair_status ?? "—"],
            ["Waktu", relativeTime(r.created_at)],
          ]}
        />
        {r.status === "WAITING_QUOTA" && <Notice tone="warn">Kuota Claude Max habis: review menunggu dan gate tidak dibuka.</Notice>}
        {r.status === "NEEDS_DECISION" && (
          <Notice tone="warn">
            Bridge belum siap: jalankan paket di Claude Code resmi lalu impor hasilnya (<Mono>aeos verify collect-review {r.id}</Mono>).
          </Notice>
        )}
        {open && (
          <div className="grid gap-4 border-t border-line pt-3 lg:grid-cols-2">
            <Section title="REVIEW" data={r.review} />
            <Section title="VERIFY" data={r.verify} />
            <section className="lg:col-span-2">
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Non-regression (§34)</h4>
              <ul className="grid gap-1 sm:grid-cols-2">
                {Object.entries(NR_LABELS).map(([k, label]) => (
                  <li key={k} className="flex flex-wrap items-center gap-2">
                    <Status status={r.non_regression?.[k]?.status === "COVERED" ? "VERIFIED" : r.non_regression?.[k]?.status === "NOT_APPLICABLE" ? "NOT_APPLICABLE" : r.non_regression?.[k] ? "NOT-PROVEN" : "UNKNOWN"} label={r.non_regression?.[k]?.status ?? "UNKNOWN"} />
                    <span>{label}</span>
                  </li>
                ))}
              </ul>
            </section>
            <section className="lg:col-span-2">
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">VERDICT &amp; temuan</h4>
              {r.findings.length === 0 ? (
                <p className="text-ink-3">Tidak ada temuan.</p>
              ) : (
                <ul className="space-y-1">
                  {r.findings.map((f, i) => (
                    <li key={i}>
                      <span className="mono text-xs">[{f.severity}]</span> {f.title} — <span className="text-ink-2">{f.required_action}</span>
                    </li>
                  ))}
                </ul>
              )}
              {r.unknowns.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-warn">
                  {r.unknowns.map((u, i) => (
                    <li key={i}>{u}</li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </div>
    </Card>
  );
}

export function ReviewsPanel() {
  const { domain } = useAuth();
  const q = useQuery({ queryKey: ["reviews", domain], queryFn: () => evidenceApi.reviews() });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  return (
    <div className="space-y-3">
      <Notice>Verdict reviewer saja tidak pernah sama dengan penerimaan (U-14); penerimaan juga butuh verifier PASS.</Notice>
      {items.length === 0 ? (
        <EmptyState title="Belum ada review checkpoint">
          Review diminta setelah verifier PASS: <Mono>aeos verify review &lt;run_id&gt;</Mono>.
        </EmptyState>
      ) : (
        items.map((r) => <ReviewCard key={r.id} r={r} />)
      )}
    </div>
  );
}
