import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, ErrorBox, Field, inputClass, KeyValue, Mono, Notice, Table } from "../../components/ui";
import type { IntegrityFinding, IntegrityHold, SandboxReadiness } from "../../components/workforce/types";
import { absoluteTime } from "../../lib/format";
import { CopyCommand } from "../integrations/CopyCommand";

/** Backend worker_runtime.integrity: the human decision that reconciles exactly one hold. */
export const HOLD_TARGET_TYPE = "worker_integrity_hold";
export const HOLD_RECONCILE_KIND = "RISK_ACCEPTANCE";

const SOURCE_LABEL: Record<string, string> = { "worker run": "run worker", verification: "verifikasi" };
const CHANGE_LABEL: Record<string, string> = {
  modified: "diubah",
  added: "ditambahkan",
  removed: "dihapus",
  incomplete: "perbandingan tidak lengkap (batas snapshot, fail closed)",
};

/** What the runtime did with a changed controller/config/bytecode file (its English detail text), never a pass. */
export function neutralisationSpec(detail: string | undefined): { status: string; label: string } | null {
  const d = detail ?? "";
  if (/could NOT be removed|NOT restored/.test(d)) return { status: "FAILED", label: "Netralisasi gagal - pulihkan manual" };
  if (/restored to the pre-run content/.test(d)) return { status: "UPDATED", label: "Config dipulihkan ke isi sebelum run" };
  if (/cache file removed|removed \(did not exist before the run\)/.test(d)) return { status: "UPDATED", label: "File dihapus oleh runtime" };
  return null;
}

export function changeLabel(change: string | undefined): string {
  if (!change) return "UNKNOWN";
  return CHANGE_LABEL[change] ?? change;
}

export function isIntegrityHold(sb: SandboxReadiness | null | undefined): sb is SandboxReadiness & { integrity_hold: IntegrityHold } {
  return !!sb && sb.code === "INTEGRITY_HOLD" && !!sb.integrity_hold;
}

/** The reconciliation command inside the backend remedy text (domain placeholder kept: the owner chooses). */
export function reconcileCommand(remedies: string[]): string | null {
  for (const r of remedies) {
    const i = r.indexOf("aeos policy decide");
    if (i >= 0) return r.slice(i).trim();
  }
  return null;
}

function FindingsTable({ findings }: { findings: IntegrityFinding[] }) {
  return (
    <Table
      rows={findings}
      rowKey={(f) => `${f.kind}:${f.path}:${f.change}:${f.subject_id ?? ""}`}
      empty="Tidak ada temuan dari domain ini"
      columns={[
        { header: "Jenis", cell: (f) => (f.kind === "db" ? "Tabel DB tepercaya" : f.kind === "controller" ? "Controller/config/bytecode" : f.kind ?? "UNKNOWN") },
        { header: "Path / baris", cell: (f) => <Mono>{f.path ?? "*"}</Mono> },
        { header: "Perubahan", cell: (f) => <span className="text-xs">{changeLabel(f.change)}</span> },
        {
          header: "Tindakan runtime / detail",
          cell: (f) => {
            const n = neutralisationSpec(f.detail);
            return (
              <div className="max-w-72 space-y-1 text-xs">
                {n && <StatusBadge status={n.status} label={n.label} />}
                {f.detail && <div className="text-ink-3 wrap-anywhere">{f.detail}</div>}
              </div>
            );
          },
        },
      ]}
    />
  );
}

/**
 * The owner's reconciliation through the existing Decision API (POST /api/decisions): a HUMAN RISK_ACCEPTANCE bound
 * to exactly this hold id. Two explicit steps; the domain is the one the human chose for this session (never
 * suggested). A later finding raises a NEW hold that needs its own reconciliation.
 */
function ReconcileForm({ hold, domain, onDone }: { hold: IntegrityHold & { id: string }; domain: string; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [checked, setChecked] = useState(false);
  const [rationale, setRationale] = useState("");
  const save = useMutation({
    mutationFn: () =>
      post<{ id: string }>("/api/decisions", {
        kind: HOLD_RECONCILE_KIND,
        target_type: HOLD_TARGET_TYPE,
        target_id: hold.id,
        rationale: rationale.trim(),
        auth_scope: { integrity_hold: hold.id, findings_reviewed: "yes", controller_and_data_restored: "yes", recorded_via: "Control Center" },
        allowed_operations: [],
        expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      }),
    onSuccess: () => onDone(),
  });
  if (save.data) {
    return (
      <div aria-live="polite">
        <Notice tone="info">
          <span className="inline-flex items-center gap-1 font-medium">
            <ShieldCheck className="h-4 w-4" aria-hidden /> Rekonsiliasi tercatat sebagai keputusan <Mono>{save.data.id}</Mono>.
          </span>{" "}
          Admission worker dibaca ulang dari backend; temuan baru akan memunculkan hold baru yang memerlukan rekonsiliasi baru.
        </Notice>
      </div>
    );
  }
  if (!open) {
    return (
      <Button variant="danger" onClick={() => setOpen(true)}>
        <ShieldCheck className="h-4 w-4" aria-hidden /> Catat rekonsiliasi…
      </Button>
    );
  }
  const valid = checked && rationale.trim().length >= 10;
  return (
    <div className="space-y-3 rounded-md border border-line bg-surface-2 p-3 text-sm" role="group" aria-label="Rekonsiliasi integrity hold">
      <p>
        Keputusan yang akan dicatat: <b>{HOLD_RECONCILE_KIND}</b> oleh Anda (manusia), target{" "}
        <Mono>
          {HOLD_TARGET_TYPE}:{hold.id}
        </Mono>
        , domain sesi <b>{domain}</b> (pilihan Anda), berlaku 1 hari. Hold ini berlaku untuk semua domain; keputusan dari domain mana pun
        mengangkatnya. AEOS tidak memulihkan apa pun untuk Anda.
      </p>
      <label className="flex items-start gap-2">
        <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
        Saya sudah meninjau temuan, memulihkan checkout controller (git status / git checkout) dan data, dan menerima risiko residualnya.
      </label>
      <Field label="Apa yang Anda periksa dan pulihkan (wajib)" hint="Minimal 10 karakter; tersimpan pada keputusan dan audit.">
        <textarea aria-label="Apa yang Anda periksa dan pulihkan" className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
      </Field>
      <ErrorBox error={save.error} />
      <div className="flex flex-wrap gap-2">
        <Button variant="danger" disabled={!valid} busy={save.isPending} onClick={() => save.mutate()}>
          Konfirmasi keputusan {HOLD_RECONCILE_KIND}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Batal
        </Button>
      </div>
    </div>
  );
}

/**
 * Wave 7 (W6-000/005, master §09 Zone 1): the host-wide integrity hold. Shown prominently (text + icon + colour):
 * it blocks every worker in every domain until the owner reconciles it with a human decision bound to the hold id.
 */
export function IntegrityHoldPanel({ sandbox, compact = false }: { sandbox: SandboxReadiness & { integrity_hold: IntegrityHold }; compact?: boolean }) {
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const hold = sandbox.integrity_hold;
  const command = reconcileCommand(sandbox.remedies ?? []);
  const source = hold.source ? SOURCE_LABEL[hold.source] ?? hold.source : "UNKNOWN";
  return (
    <section className="space-y-3 rounded-lg border-2 border-bad/50 bg-bad-bg p-4 text-sm text-bad" aria-label="Integrity hold aktif" data-testid="integrity-hold">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className="h-5 w-5 shrink-0" aria-hidden />
        <h2 className="text-base font-semibold">Integrity hold aktif: semua worker di semua domain diblokir</h2>
        <StatusBadge status="BLOCKED" label="INTEGRITY_HOLD" title="INTEGRITY_HOLD" />
      </div>
      <p className="text-ink">
        Controller AEOS, config/bytecode-nya atau tabel DB tepercaya berubah saat {source} berjalan. Tidak ada worker yang dimulai di
        domain mana pun (PERSONAL maupun OFFICE) sampai pemilik merekonsiliasi hold ini dengan keputusan manusia yang terikat ID hold ini
        (master §09 Zona 1, fail closed). Run yang memicu hold dikarantina.
      </p>
      {compact ? (
        <p className="text-ink">
          ID hold <Mono>{hold.id ?? "UNKNOWN"}</Mono> · terdeteksi {absoluteTime(hold.detected_at)} · {hold.findings.length} temuan di domain ini
          {hold.other_domain_findings > 0 ? ` + ${hold.other_domain_findings} dari domain lain` : ""}.{" "}
          <Link to="/work" className="text-accent underline">
            Lihat temuan dan langkah pemilik di Pekerjaan Live
          </Link>
        </p>
      ) : (
        <div className="space-y-3 text-ink">
          <KeyValue
            items={[
              ["ID hold", <Mono key="id">{hold.id ?? "UNKNOWN"}</Mono>],
              ["Terdeteksi", absoluteTime(hold.detected_at)],
              ["Sumber", source],
              [
                "Subjek",
                hold.subject_id ? (
                  hold.source === "worker run" ? (
                    <Link key="s" to={`/runs/${hold.subject_id}`} className="text-accent underline">
                      <Mono>{hold.subject_id}</Mono>
                    </Link>
                  ) : (
                    <Mono key="s">{hold.subject_id}</Mono>
                  )
                ) : (
                  "Di domain lain (detail tidak ditampilkan di domain ini)"
                ),
              ],
            ]}
          />
          <FindingsTable findings={hold.findings} />
          {hold.other_domain_findings > 0 && (
            <p className="text-xs text-ink-2">
              + {hold.other_domain_findings} temuan dari domain lain: hanya jumlahnya yang ditampilkan di domain ini (invarian 9).
            </p>
          )}
          {sandbox.reasons.length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer">Alasan dari backend</summary>
              <ul className="mt-1 list-disc pl-5">
                {sandbox.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </details>
          )}
          <div className="space-y-2">
            <div className="font-medium">Langkah pemilik</div>
            <ol className="list-decimal space-y-1 pl-5">
              <li>
                Tinjau temuan (<Mono>aeos worker status</Mono>).
              </li>
              <li>
                Pulihkan checkout controller (<Mono>git status</Mono>, <Mono>git checkout -- &lt;path&gt;</Mono>) dan data.
              </li>
              <li>
                Catat rekonsiliasi sebagai keputusan manusia {HOLD_RECONCILE_KIND} untuk hold ini, di sini atau lewat CLI (Windows{" "}
                <Mono>.\aeos.ps1</Mono>, macOS <Mono>./aeos</Mono>; ganti <Mono>&lt;PERSONAL|OFFICE&gt;</Mono> dengan domain pilihan Anda):
              </li>
            </ol>
            {command ? <CopyCommand command={command} /> : <p className="text-xs">{(sandbox.remedies ?? []).join(" · ") || "UNKNOWN"}</p>}
            {hold.id && (
              <Link to={`/decisions?target_type=${HOLD_TARGET_TYPE}&target_id=${encodeURIComponent(hold.id)}`} className="inline-block text-xs text-accent underline">
                Keputusan terkait hold ini
              </Link>
            )}
          </div>
          {isAdmin && domain && hold.id ? (
            <ReconcileForm
              hold={{ ...hold, id: hold.id }}
              domain={domain}
              onDone={() => {
                qc.invalidateQueries({ queryKey: ["workforce-summary"] });
                qc.invalidateQueries({ queryKey: ["worker-sandbox"] });
                qc.invalidateQueries({ queryKey: ["decisions"] });
              }}
            />
          ) : (
            <p className="text-xs text-ink-3">Hanya SUPER_ADMIN yang dapat mencatat rekonsiliasi; peran VIEWER hanya-baca.</p>
          )}
        </div>
      )}
    </section>
  );
}

/** Ringkasan: the hold read from /api/worker-runtime/sandbox (this domain's view); nothing is shown without a hold. */
export function IntegrityHoldBanner() {
  const { domain } = useAuth();
  const q = useQuery({
    queryKey: ["worker-sandbox", domain],
    queryFn: () => get<SandboxReadiness>("/api/worker-runtime/sandbox"),
    enabled: !!domain,
    refetchInterval: 60_000,
    retry: false,
  });
  if (!isIntegrityHold(q.data)) return null;
  return (
    <div className="mb-4">
      <IntegrityHoldPanel sandbox={q.data} compact />
    </div>
  );
}
