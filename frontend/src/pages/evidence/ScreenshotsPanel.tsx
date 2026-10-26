import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Freshness, Loading, Mono, Notice, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";
import { evidenceApi, type EvidenceItem } from "./api";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { LevelBadge, TrustBadge } from "./labels";
import { Outcome } from "./outcomes";
import { isObj } from "./ReportViews";

/** The owner's POLICY decision on authenticated, read-only capture (backend docs_engine.screenshots). */
export interface AuthenticatedCapture {
  status?: string; // ALLOWED | DENIED | UNDECIDED
  note?: string;
  decision_id?: string | null;
  scope?: string | null;
  expires_at?: string | null;
  preset?: string;
  capture_mode?: string; // what the collector does: UNAUTHENTICATED in this build
  authenticated_capture_built?: boolean;
  record_hint?: string | null;
}

/** GET /api/docs/visuals/status (only the part this page states). */
export interface VisualsStatus {
  screenshot?: string;
  screenshot_collector?: {
    available?: boolean;
    command?: string;
    remedy?: string | null;
    note?: string;
    authenticated_capture?: AuthenticatedCapture;
  };
  screenshot_note?: string;
  checked_at?: string | null;
}

const env = (e: EvidenceItem, k: string): unknown => e.environment?.[k];

/**
 * The owner's decision on authenticated capture (ALLOWED / DENIED / UNDECIDED, W6-047) and what the collector does: it
 * never logs in in this build, so pages behind a login are not evidence whatever the decision says.
 */
export function AuthenticatedCaptureNotice() {
  const { isAdmin, domain } = useAuth();
  const q = useQuery({ queryKey: ["docs-visuals-status", domain], queryFn: () => get<VisualsStatus>("/api/docs/visuals/status"), staleTime: 300_000 });
  const cap = q.data?.screenshot_collector?.authenticated_capture;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!cap) return null;
  const preset = cap.preset ?? "screenshot_authenticated_capture";
  return (
    <div role="status" className="rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn" data-testid="authenticated-capture">
      <div className="flex flex-wrap items-center gap-2 font-medium">
        Capture terautentikasi (halaman di balik login):{" "}
        {cap.status === "ALLOWED" ? (
          <StatusBadge status="CONFIGURED" label="ALLOWED oleh pemilik (jalur capture terautentikasi belum dibangun)" />
        ) : cap.status === "DENIED" ? (
          <StatusBadge status="DISABLED" label="DENIED oleh pemilik" />
        ) : (
          <Outcome status={cap.status ?? "UNKNOWN"} />
        )}
        {cap.capture_mode && <span className="text-xs font-normal">· mode kolektor: {cap.capture_mode === "UNAUTHENTICATED" ? "tanpa login" : cap.capture_mode}</span>}
      </div>
      {cap.note && <p className="mt-1">{cap.note}</p>}
      {cap.decision_id && (
        <p className="mt-1 text-xs">
          Keputusan pemilik{" "}
          <Link className="underline" to={`/decisions?decision=${encodeURIComponent(cap.decision_id)}`}>
            <Mono>{cap.decision_id}</Mono>
          </Link>
          {cap.scope ? ` · scope: ${cap.scope}` : ""}
          {cap.expires_at ? ` · berlaku sampai ${cap.expires_at}` : ""}
        </p>
      )}
      {domain === "PERSONAL" && (
        <p className="mt-1 text-xs">
          {isAdmin ? (
            <Link className="underline" to={`/decisions?preset=${encodeURIComponent(preset)}`}>
              {cap.status === "UNDECIDED" ? "Catat keputusan pemilik (ALLOWED/DENIED)…" : "Ubah keputusan pemilik…"}
            </Link>
          ) : (
            "Hanya SUPER_ADMIN yang dapat mencatat keputusan ini; peran VIEWER hanya-baca."
          )}
        </p>
      )}
      {q.data?.screenshot_collector?.available === false && q.data.screenshot_collector.remedy && (
        <p className="mt-1 text-xs">
          Kolektor belum tersedia di host ini: <Mono>{q.data.screenshot_collector.remedy}</Mono>
        </p>
      )}
    </div>
  );
}

/**
 * The ui-a11y report of a run: the run's newest VERIFICATION_REPORT names it exactly (checks.ui_a11y.evidence_id), so
 * the report is found by that link, never guessed from claim text.
 */
function A11yReportButton({ runId, onOpen }: { runId: string; onOpen: (id: string) => void }) {
  const qc = useQueryClient();
  const { domain } = useAuth();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState<unknown>(null);
  const find = async () => {
    setBusy(true);
    setMsg("");
    setError(null);
    try {
      // only the verifier's own (trusted) reports: a claim never names the report
      const filters = { kind: "VERIFICATION_REPORT", run_id: runId, trusted: true };
      const list = await qc.fetchQuery({ queryKey: ["evidence", domain, filters], queryFn: () => evidenceApi.list(filters) });
      const newest = list.items[0];
      if (!newest) {
        setMsg("Run ini belum punya laporan verifikasi.");
        return;
      }
      const detail = await qc.fetchQuery({ queryKey: ["evidence-detail", newest.id], queryFn: () => evidenceApi.detail(newest.id) });
      const checks = isObj(detail.content_json) && isObj(detail.content_json.checks) ? detail.content_json.checks : {};
      const ui = isObj(checks.ui_a11y) ? checks.ui_a11y : null;
      const id = ui && typeof ui.evidence_id === "string" ? ui.evidence_id : null;
      if (!id) {
        setMsg(`Laporan verifikasi terbaru tidak menautkan laporan ui-a11y (status cek: ${ui ? String(ui.status ?? "UNKNOWN") : "tidak ada"}).`);
        return;
      }
      onOpen(id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button className="text-xs" busy={busy} onClick={find}>
        Laporan ui-a11y
      </Button>
      <span aria-live="polite" className="text-xs text-ink-3">
        {msg}
      </span>
      <ErrorBox error={error} />
    </div>
  );
}

/** The first cell opens the record with the keyboard too (the row click is a pointer convenience only). */
function OpenCell({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      className="mono rounded text-left text-accent underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      aria-label={`Buka detail tangkapan layar ${label}`}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {label}
    </button>
  );
}

function UiStateTable({ rows, onOpen }: { rows: EvidenceItem[]; onOpen: (id: string) => void }) {
  return (
    <Table<EvidenceItem>
      rows={rows}
      rowKey={(r) => r.id}
      onRowClick={(r) => onOpen(r.id)}
      columns={[
        { header: "State", cell: (r) => <OpenCell label={String(env(r, "ui_state") ?? "?")} onOpen={() => onOpen(r.id)} /> },
        { header: "Viewport", cell: (r) => <span className="text-xs">{String(env(r, "viewport") ?? "—")}</span> },
        { header: "API dijawab", cell: (r) => <span className="text-xs">{String(env(r, "answered_by") ?? "verifier")}</span> },
        {
          header: "Render",
          cell: (r) =>
            !r.trusted ? (
              <StatusBadge status="UNVERIFIED" label="Klaim — tidak dihitung" />
            ) : env(r, "render_error") === true ? (
              <StatusBadge status="FAILED" label="Error skrip — bukan bukti state" />
            ) : (
              <StatusBadge status="CONFIGURED" label="Tanpa error skrip" />
            ),
        },
        { header: "Asal", cell: (r) => <TrustBadge trusted={r.trusted} /> },
        { header: "Level", cell: (r) => <LevelBadge level={r.evidence_level} explanation={r.level_explanation} /> },
        { header: "Hash", cell: (r) => <Mono>{shortHash(r.content_hash, 10)}</Mono> },
      ]}
    />
  );
}

/** §29/§31 screenshots: verifier UI-state renders of a candidate bundle and target pages of the screenshot collector. */
export function ScreenshotsPanel() {
  const { domain } = useAuth();
  const filters = { kind: "SCREENSHOT" };
  const q = useQuery({ queryKey: ["evidence", domain, filters], queryFn: () => evidenceApi.list(filters) });
  const [open, setOpen] = useState<string | null>(null);
  const items = q.data?.items ?? [];
  const uiStates = items.filter((e) => env(e, "ui_state") !== undefined);
  const pages = items.filter((e) => env(e, "ui_state") === undefined);
  const byRun = new Map<string, EvidenceItem[]>();
  for (const e of uiStates) {
    const k = e.run_id ?? "—";
    byRun.set(k, [...(byRun.get(k) ?? []), e]);
  }
  return (
    <div className="space-y-4">
      <AuthenticatedCaptureNotice />
      <Notice tone="info">
        Tangkapan layar adalah bukti tampilan pada commit tercatat, bukan kelulusan. Render yang memunculkan error skrip ditandai{" "}
        <strong>bukan bukti state</strong>. Teks di dalam gambar tidak diperiksa (tanpa OCR).
      </Notice>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && items.length === 0 && (
        <EmptyState title="Belum ada bukti SCREENSHOT">
          State UI direkam verifier saat frontend berubah (cek <Mono>ui_a11y</Mono>); halaman target direkam dengan{" "}
          <Mono>aeos docs screenshot &lt;project_id&gt; --environment staging --commit &lt;sha&gt; --route /</Mono>.
        </EmptyState>
      )}
      {[...byRun.entries()].map(([runId, rows]) => (
        <Card
          key={runId}
          title={
            <span className="inline-flex items-center gap-2">
              <Camera className="h-4 w-4" aria-hidden /> State UI kandidat · run <Mono>{shortHash(runId, 8)}</Mono>
            </span>
          }
          actions={
            <>
              {runId !== "—" && <A11yReportButton runId={runId} onOpen={setOpen} />}
              <span className="text-xs text-ink-3">{relativeTime(rows[0]?.created_at)}</span>
            </>
          }
        >
          {rows.some((r) => r.trusted && env(r, "render_error") === true) && (
            <div className="mb-2">
              <Notice tone="bad">
                {rows.filter((r) => r.trusted && env(r, "render_error") === true).length} render memunculkan error skrip: tangkapan layar itu bukan
                bukti state yang dimaksud.
              </Notice>
            </div>
          )}
          <UiStateTable rows={rows} onOpen={setOpen} />
        </Card>
      ))}
      {pages.length > 0 && (
        <Card title="Halaman target (kolektor screenshot)" actions={q.data && <Freshness at={q.data.checked_at} label="Dimuat" />}>
          <Table<EvidenceItem>
            rows={pages}
            rowKey={(r) => r.id}
            onRowClick={(r) => setOpen(r.id)}
            columns={[
              { header: "Lingkungan", cell: (r) => <span className="text-xs">{String(env(r, "name") ?? "—")}</span> },
              { header: "Rute", cell: (r) => <OpenCell label={String(env(r, "route") ?? "—")} onOpen={() => setOpen(r.id)} /> },
              { header: "Commit", cell: (r) => <Mono>{shortHash(String(env(r, "commit") ?? "") || null, 10)}</Mono> },
              { header: "Viewport", cell: (r) => <span className="text-xs">{String(env(r, "viewport") ?? "—")}</span> },
              { header: "Asal", cell: (r) => <TrustBadge trusted={r.trusted} /> },
              { header: "Waktu", cell: (r) => <span className="text-xs">{relativeTime(r.created_at)}</span> },
            ]}
          />
        </Card>
      )}
      {open && <EvidenceDrawer key={open} id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
