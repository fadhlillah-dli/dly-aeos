import { AlertTriangle, ExternalLink, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, KeyValue, Mono, Notice } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { Status } from "./labels";
import { CHECK_LABELS, IMPACT_LABELS, Outcome } from "./outcomes";

// Read-only views of the JSON a verifier wrote (VERIFICATION_REPORT, ui-a11y TEST_REPORT, SCREENSHOT environment).
// Everything is DATA from the evidence record; missing keys render as "—", never as a pass.

type Obj = Record<string, unknown>;

export function isObj(v: unknown): v is Obj {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (v === null || v === undefined ? "" : String(v));
const strs = (v: unknown): string[] => arr(v).map(str).filter(Boolean);

function Section({ title, children, label }: { title: string; children: ReactNode; label?: string }) {
  return (
    <section aria-label={label ?? title} className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Lines({ items, tone = "text-ink-2" }: { items: string[]; tone?: string }) {
  if (!items.length) return <span className="text-xs text-ink-3">—</span>;
  return (
    <ul className={`list-disc space-y-0.5 pl-5 text-xs ${tone}`}>
      {items.map((t, i) => (
        <li key={i} className="wrap-anywhere">
          {t}
        </li>
      ))}
    </ul>
  );
}

function EvidenceButton({ id, onOpen, label = "Buka bukti" }: { id: string; onOpen?: (id: string) => void; label?: string }) {
  if (!onOpen) return <Mono>{shortHash(id, 8)}</Mono>;
  return (
    <Button variant="ghost" className="px-1.5 py-0.5 text-xs" onClick={() => onOpen(id)} title={id}>
      <ExternalLink className="h-3 w-3" aria-hidden /> {label}
    </Button>
  );
}

/** One verifier check: Indonesian label + raw name + status badge (text + icon + colour). */
export function CheckBadge({ name, status }: { name: string; status: unknown }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span className="text-xs font-medium">{CHECK_LABELS[name] ?? name}</span>
      <Mono>{name}</Mono>
      <Status status={str(status) || null} />
    </span>
  );
}

function ImpactChecks({ impact }: { impact: Obj }) {
  const cats = Object.entries(impact).filter(([, v]) => isObj(v)) as [string, Obj][];
  if (!cats.length) return <p className="text-xs text-ink-3">Perubahan ini tidak memiliki kategori dampak yang memilih cek tambahan.</p>;
  return (
    <ul className="space-y-2" aria-label="Cek per kategori dampak">
      {cats.map(([cat, e]) => {
        const checks = isObj(e.checks) ? Object.entries(e.checks) : [];
        const executed = strs(e.executed);
        return (
          <li key={cat} className="rounded-md border border-line p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">{IMPACT_LABELS[cat] ?? cat}</span>
              <Mono>{cat}</Mono>
              <Outcome status={str(e.status) || null} label={str(e.status) === "NOT_PROVEN" ? "Tidak ada cek terpilih yang dijalankan" : undefined} />
            </div>
            <div className="mt-1 text-xs text-ink-3">
              Path:{" "}
              {strs(e.paths).length ? (
                strs(e.paths).map((p) => (
                  <span key={p} className="mr-1">
                    <Mono>{p}</Mono>
                  </span>
                ))
              ) : (
                "—"
              )}
            </div>
            <div className="mt-1 flex flex-col gap-1">
              {checks.length ? checks.map(([n, s]) => <CheckBadge key={n} name={n} status={s} />) : <span className="text-xs text-ink-3">Belum ada cek verifier untuk kategori ini</span>}
            </div>
            <div className="mt-1 text-xs text-ink-2">Dijalankan (PASS/FAIL): {executed.length ? executed.join(", ") : "tidak ada"}</div>
          </li>
        );
      })}
    </ul>
  );
}

function RowList({ rows, cols }: { rows: Obj[]; cols: [string, string][] }) {
  if (!rows.length) return <span className="text-xs text-ink-3">—</span>;
  return (
    <ul className="space-y-1 text-xs">
      {rows.map((r, i) => (
        <li key={i} className="rounded border border-line p-1.5">
          {cols.map(([k, label]) =>
            r[k] !== undefined && r[k] !== null && r[k] !== "" ? (
              <div key={k} className="wrap-anywhere">
                <span className="text-ink-3">{label}: </span>
                {k === "remedy" || k === "detail" ? <span>{str(r[k])}</span> : <Mono>{typeof r[k] === "object" ? JSON.stringify(r[k]) : str(r[k])}</Mono>}
              </div>
            ) : null,
          )}
        </li>
      ))}
    </ul>
  );
}

/** E2 tamper.neutralized: what the verifier neutralised, what needs the owner, and the integrity hold. */
export function TamperView({ tamper }: { tamper: Obj }) {
  const n = isObj(tamper.neutralized) ? tamper.neutralized : {};
  const actions = arr(n.actions).filter(isObj);
  const manual = arr(n.manual).filter(isObj);
  const failed = arr(n.failed).filter(isObj);
  const hold = isObj(n.integrity_hold) ? n.integrity_hold : null;
  const findings = arr(tamper.findings).filter(isObj);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Status status={str(tamper.status) || null} />
        <span className="text-xs text-ink-2">{findings.length} temuan tamper</span>
        {Number(n.not_listed) > 0 && <span className="text-xs text-warn">+{str(n.not_listed)} baris tidak didaftar</span>}
      </div>
      {findings.length > 0 && <RowList rows={findings.slice(0, 20)} cols={[["kind", "Jenis"], ["path", "Path"], ["table", "Tabel"], ["change", "Perubahan"], ["detail", "Detail"]]} />}
      {hold && (
        <div role="alert" className="rounded-md border border-bad/40 bg-bad-bg p-3 text-sm text-bad">
          <div className="flex items-center gap-2 font-medium">
            <ShieldAlert className="h-4 w-4" aria-hidden /> Integrity hold: admisi run worker baru{" "}
            {hold.admission_paused === true ? "DIJEDA" : "TIDAK tercatat dijeda — jeda manual (aeos work pause)"}
          </div>
          {str(hold.reason) && <p className="mt-1 text-xs wrap-anywhere">{str(hold.reason)}</p>}
          {arr(hold.controller_changes).length > 0 && (
            <div className="mt-2">
              <div className="text-xs font-medium">Berkas controller yang berubah</div>
              <RowList rows={arr(hold.controller_changes).filter(isObj)} cols={[["path", "Path"], ["change", "Perubahan"]]} />
            </div>
          )}
          {arr(hold.remedies).length > 0 && (
            <div className="mt-2">
              <div className="text-xs font-medium">Remedi pemilik</div>
              <RowList rows={arr(hold.remedies).filter(isObj)} cols={[["path", "Path"], ["remedy", "Remedi"]]} />
            </div>
          )}
          {str(hold.resume) && <p className="mt-2 text-xs wrap-anywhere">Lanjutkan setelah rekonsiliasi: {str(hold.resume)}</p>}
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-3">
        <div>
          <div className="mb-1 text-xs font-semibold">Dinetralkan otomatis ({actions.length})</div>
          <RowList rows={actions} cols={[["table", "Tabel"], ["id", "ID"], ["change", "Perubahan"], ["action", "Tindakan"]]} />
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold">Perlu review pemilik ({manual.length})</div>
          <RowList rows={manual} cols={[["table", "Tabel"], ["id", "ID"], ["change", "Perubahan"], ["remedy", "Remedi"]]} />
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold">Gagal dinetralkan ({failed.length})</div>
          <RowList rows={failed} cols={[["table", "Tabel"], ["id", "ID"], ["change", "Perubahan"], ["error", "Error"]]} />
        </div>
      </div>
    </div>
  );
}

/** E2 result_seal per step: an authenticated end of each step's test session (MISSING fails closed). */
export function SealView({ seals }: { seals: Obj }) {
  const steps = Object.entries(seals).filter(([, v]) => isObj(v)) as [string, Obj][];
  if (!steps.length) return <p className="text-xs text-ink-3">Tidak ada langkah bersegel pada verifikasi ini.</p>;
  return (
    <ul className="space-y-1.5" aria-label="Segel hasil per langkah">
      {steps.map(([step, s]) => (
        <li key={step} className="rounded-md border border-line p-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <Mono>{step}</Mono>
            <Status
              status={str(s.status) || null}
              label={s.status === "MISSING" ? "Segel tidak ada (gagal tertutup)" : s.status === "FAIL" ? "Segel tidak konsisten (tamper)" : undefined}
            />
            <span className="text-ink-2">
              kunci {s.key_consumed === true ? "terpakai dan dihapus" : s.key_consumed === false ? "MASIH ADA setelah langkah" : "tidak diketahui"}
            </span>
            {Number(s.other_session_lines) > 0 && <span className="text-ink-3">{str(s.other_session_lines)} baris sesi lain diabaikan</span>}
          </div>
          {strs(s.problems).length > 0 && <Lines items={strs(s.problems)} tone="text-bad" />}
        </li>
      ))}
    </ul>
  );
}

/** Structured VERIFICATION_REPORT (§31 impact checks, E1 step checks, E2 tamper/seal). */
export function VerificationReportView({ report, onOpen }: { report: Obj; onOpen?: (id: string) => void }) {
  const checks = isObj(report.checks) ? (Object.entries(report.checks).filter(([, v]) => isObj(v)) as [string, Obj][]) : [];
  const impact = isObj(report.impact_checks) ? report.impact_checks : {};
  const tamper = isObj(report.tamper) ? report.tamper : {};
  const seals = isObj(report.result_seal) ? report.result_seal : {};
  return (
    <div className="space-y-4" data-testid="verification-report">
      <KeyValue
        items={[
          ["Hasil verifier", <Status key="s" status={str(report.status) || null} />],
          ["Ringkasan", str(report.summary) || "—"],
          [
            "Isolasi",
            <span key="i" className="flex flex-wrap items-center gap-2">
              <Status status={str(report.verifier_isolation) || null} />
              <span className="text-xs text-ink-3">{str(report.verifier_isolation_note)}</span>
            </span>,
          ],
          [
            "Tes tepercaya",
            report.tests_passed === undefined || report.tests_passed === null
              ? "—"
              : `${str(report.tests_passed)} lulus · ${str(report.tests_failed ?? "?")} gagal · ${str(report.tests_skipped ?? "?")} dilewati`,
          ],
          ["Kategori dampak", strs(report.impact_categories).join(", ") || "—"],
        ]}
      />
      <Section title="Cek verifier">
        {checks.length ? (
          <ul className="space-y-1.5">
            {checks.map(([name, c]) => (
              <li key={name} className="rounded-md border border-line p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <CheckBadge name={name} status={c.status} />
                  {str(c.evidence_id) && <EvidenceButton id={str(c.evidence_id)} onOpen={onOpen} label={name === "ui_a11y" ? "Buka laporan ui-a11y" : "Buka bukti"} />}
                </div>
                {str(c.summary) && <p className="mt-1 text-xs text-ink-2 wrap-anywhere">{str(c.summary)}</p>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-ink-3">Laporan ini tidak mencantumkan cek.</p>
        )}
      </Section>
      <Section title="Cek per kategori dampak (§31: cakupan uji mengikuti dampak)">
        <ImpactChecks impact={impact} />
      </Section>
      {strs(report.reasons).length > 0 && (
        <Section title="Alasan gagal">
          <Lines items={strs(report.reasons)} tone="text-bad" />
        </Section>
      )}
      {strs(report.not_proven).length > 0 && (
        <Section title="Belum terbukti (NOT-PROVEN)">
          <Lines items={strs(report.not_proven)} tone="text-warn" />
        </Section>
      )}
      {Object.keys(tamper).length > 0 && (
        <Section title="Tamper & netralisasi (E2)">
          <TamperView tamper={tamper} />
        </Section>
      )}
      <Section title="Segel hasil (result_seal)">
        <SealView seals={seals} />
      </Section>
      {strs(report.residual_risks).length > 0 && (
        <Section title="Risiko residual runner">
          <Lines items={strs(report.residual_risks)} />
        </Section>
      )}
    </div>
  );
}

/** The verifier's ui-a11y TEST_REPORT: renders per state, probe violations and renders that prove nothing. */
export function UiA11yReportView({ report }: { report: Obj }) {
  const states = arr(report.states).filter(isObj);
  const violations = arr(report.violations).filter(isObj);
  const unproven = strs(report.unproven_states);
  return (
    <div className="space-y-3" data-testid="ui-a11y-report">
      <KeyValue
        items={[
          ["Hasil", <Status key="s" status={str(report.status) || null} />],
          ["Probe", str(report.probe) || "—"],
          ["Browser", str(report.browser) || "—"],
          ["Aplikasi kandidat", str(report.live_app) || (str(report.live_app_error) ? `tidak menjawab: ${str(report.live_app_error)}` : "—")],
        ]}
      />
      <Section title={`Pelanggaran aksesibilitas (${violations.length})`}>
        <RowList rows={violations} cols={[["state", "State"], ["viewport", "Viewport"], ["rule", "Aturan"], ["wcag", "WCAG"], ["target", "Target"], ["detail", "Detail"]]} />
      </Section>
      <Section title={`Render yang bukan bukti state (${unproven.length})`}>
        {unproven.length ? (
          <Notice tone="warn">
            Render ini memunculkan error skrip di bawah jawaban API generik verifier, jadi bukan bukti state tersebut (NOT-PROVEN): {unproven.join(", ")}
          </Notice>
        ) : (
          <span className="text-xs text-ink-3">Tidak ada</span>
        )}
      </Section>
      <Section title={`State yang dirender (${states.length})`}>
        <ul className="space-y-1 text-xs">
          {states.map((s, i) => (
            <li key={i} className="flex flex-wrap items-center gap-2">
              <Mono>{`${str(s.state)}/${str(s.viewport)}`}</Mono>
              <span className="text-ink-3">dijawab {str(s.answered_by) || "verifier"}</span>
              {arr(s.page_errors).length > 0 ? <StatusBadge status="FAILED" label="Error skrip — bukan bukti state" /> : <span className="text-ink-3">tanpa error skrip</span>}
              <span className="text-ink-3">{arr(s.violations).length} pelanggaran</span>
            </li>
          ))}
        </ul>
      </Section>
      {strs(report.not_covered).length > 0 && (
        <Section title="Tidak dicakup probe (NOT-PROVEN)">
          <Lines items={strs(report.not_covered)} tone="text-warn" />
        </Section>
      )}
    </div>
  );
}

/** SCREENSHOT evidence: a verifier UI-state render (ui_state/viewport/answered_by/render_error) or a target page. */
export function ScreenshotFacts({ env }: { env: Obj }) {
  const uiState = str(env.ui_state);
  return (
    <div className="space-y-2" data-testid="screenshot-facts">
      {uiState ? (
        <>
          <KeyValue
            items={[
              ["State UI", <Mono key="s">{uiState}</Mono>],
              ["Viewport", str(env.viewport) || "—"],
              ["API dijawab oleh", str(env.answered_by) || "verifier"],
              ["Commit", <Mono key="c">{shortHash(str(env.commit) || null, 12)}</Mono>],
            ]}
          />
          {env.render_error === true ? (
            <div role="alert" className="flex items-start gap-2 rounded-md border border-bad/40 bg-bad-bg p-3 text-sm text-bad">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>
                <strong>Bukan bukti state:</strong> render ini memunculkan error skrip yang tidak tertangani, sehingga tangkapan layar ini tidak
                membuktikan state <Mono>{uiState}</Mono>.
              </span>
            </div>
          ) : (
            <StatusBadge status="CONFIGURED" label="Render tanpa error skrip (bukti tampilan, bukan kelulusan)" />
          )}
        </>
      ) : (
        <KeyValue
          items={[
            ["Lingkungan", str(env.name) || "—"],
            ["Rute", <Mono key="r">{str(env.route) || "—"}</Mono>],
            ["Commit", <Mono key="c">{shortHash(str(env.commit) || null, 12)}</Mono>],
            ["Viewport", str(env.viewport) || "—"],
            ["HTTP halaman", str(env.page_http_status) || "—"],
            ["Teks diperiksa", str(env.text_checked) || "—"],
          ]}
        />
      )}
      <p className="text-xs text-ink-3">Gambar tidak ditampilkan di sini (isi biner); hash isinya tercatat di atas.</p>
    </div>
  );
}
