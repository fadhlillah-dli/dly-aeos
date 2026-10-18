import { clsx } from "clsx";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  Clock,
  FlaskConical,
  Loader2,
  MinusCircle,
  PlugZap,
  Settings2,
  XCircle,
} from "lucide-react";
import type { ComponentType } from "react";

type Tone = "ok" | "warn" | "bad" | "info" | "muted" | "mock";

interface Spec {
  tone: Tone;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}

// Every status renders text + icon + colour (§35: colour never the only signal), and labels are
// human-friendly Indonesian first; the raw code stays visible in the tooltip.
const SPECS: Record<string, Spec> = {
  READY: { tone: "ok", label: "Siap", icon: CheckCircle2 },
  VERIFIED: { tone: "ok", label: "Terverifikasi", icon: CheckCircle2 },
  ACCEPTED: { tone: "ok", label: "Diterima", icon: CheckCircle2 },
  PASS: { tone: "ok", label: "Lulus", icon: CheckCircle2 },
  "PASS-STATIC": { tone: "ok", label: "Lulus (statis)", icon: CheckCircle2 },
  "PASS-CONTROLLED": { tone: "ok", label: "Lulus (uji terkendali)", icon: CheckCircle2 },
  "PASS-STAGING": { tone: "ok", label: "Lulus (staging)", icon: CheckCircle2 },
  "PASS-PRODUCTION": { tone: "ok", label: "Lulus (produksi)", icon: CheckCircle2 },
  DONE: { tone: "ok", label: "Selesai", icon: CheckCircle2 },
  APPROVED: { tone: "ok", label: "Disetujui", icon: CheckCircle2 },
  PUBLISHED: { tone: "ok", label: "Terpublikasi", icon: CheckCircle2 },
  SUCCEEDED: { tone: "ok", label: "Berhasil", icon: CheckCircle2 },
  ENABLED: { tone: "info", label: "Diaktifkan (belum tentu dipakai)", icon: Settings2 },
  CONNECTED: { tone: "info", label: "Terhubung (belum diverifikasi fungsi)", icon: PlugZap },
  CONFIGURED: { tone: "info", label: "Terkonfigurasi", icon: Settings2 },
  INSTALLED: { tone: "info", label: "Terpasang", icon: Settings2 },
  IN_USE: { tone: "ok", label: "Sedang digunakan", icon: Loader2 },
  RUNNING: { tone: "info", label: "Berjalan", icon: Loader2 },
  LEASED: { tone: "info", label: "Di-lease", icon: Clock },
  QUEUED: { tone: "muted", label: "Antre", icon: Clock },
  REVIEW: { tone: "warn", label: "Menunggu review/bukti", icon: Clock },
  PENDING: { tone: "muted", label: "Tertunda", icon: Clock },
  LAST_USED: { tone: "muted", label: "Terakhir dipakai", icon: Clock },
  IDLE: { tone: "muted", label: "Tersedia, tidak aktif", icon: CircleDashed },
  NEVER_OBSERVED: { tone: "muted", label: "Belum pernah teramati", icon: CircleDashed },
  NOT_CHECKED: { tone: "muted", label: "Belum diperiksa", icon: CircleDashed },
  NOT_TESTED: { tone: "muted", label: "Belum diuji", icon: CircleDashed },
  NOT_INSTALLED: { tone: "muted", label: "Belum terpasang", icon: MinusCircle },
  DISABLED: { tone: "muted", label: "Nonaktif", icon: MinusCircle },
  NOT_APPLICABLE: { tone: "muted", label: "Tidak berlaku", icon: MinusCircle },
  CANDIDATE: { tone: "muted", label: "Kandidat", icon: FlaskConical },
  PLANNED: { tone: "muted", label: "Direncanakan", icon: CircleDashed },
  SELECTED_DESIGN: { tone: "muted", label: "Desain terpilih", icon: CircleDashed },
  DESIGN: { tone: "mock", label: "Desain (bukan runtime)", icon: CircleDashed },
  MISSING: { tone: "bad", label: "Belum ada", icon: XCircle },
  FAILED: { tone: "bad", label: "Gagal", icon: XCircle },
  FAIL: { tone: "bad", label: "Gagal", icon: XCircle },
  REJECTED: { tone: "bad", label: "Ditolak", icon: XCircle },
  UNREACHABLE: { tone: "bad", label: "Tidak terjangkau", icon: XCircle },
  BLOCKED: { tone: "bad", label: "Terblokir", icon: Ban },
  DENIED: { tone: "bad", label: "Ditolak", icon: Ban },
  CANCELLED: { tone: "muted", label: "Dibatalkan", icon: Ban },
  EXPIRED: { tone: "warn", label: "Kedaluwarsa", icon: AlertTriangle },
  EXPIRED_AUTH: { tone: "warn", label: "Auth kedaluwarsa", icon: AlertTriangle },
  STALE: { tone: "warn", label: "Usang — perlu dicek ulang", icon: AlertTriangle },
  DEGRADED: { tone: "warn", label: "Terbatas", icon: AlertTriangle },
  PARTIAL: { tone: "warn", label: "Sebagian", icon: AlertTriangle },
  CONFLICT: { tone: "warn", label: "Konflik revisi", icon: AlertTriangle },
  DRIFTED: { tone: "warn", label: "Menyimpang dari pin", icon: AlertTriangle },
  "NOT-PROVEN": { tone: "warn", label: "Belum terbukti", icon: CircleHelp },
  NOT_PROVEN: { tone: "warn", label: "Belum terbukti", icon: CircleHelp },
  UNVERIFIED: { tone: "warn", label: "Belum diverifikasi", icon: CircleHelp },
  UNKNOWN: { tone: "warn", label: "Tidak diketahui", icon: CircleHelp },
  // Intake / portfolio lifecycle
  CLEAN: { tone: "ok", label: "Bersih", icon: CheckCircle2 },
  FLAGGED: { tone: "warn", label: "Ditandai", icon: AlertTriangle },
  UNSCANNED: { tone: "warn", label: "Belum dipindai", icon: CircleHelp },
  PARSED: { tone: "ok", label: "Terurai", icon: CheckCircle2 },
  SKIPPED: { tone: "muted", label: "Dilewati", icon: MinusCircle },
  OK: { tone: "info", label: "OK", icon: CheckCircle2 },
  MISMATCH: { tone: "warn", label: "Tidak cocok", icon: AlertTriangle },
  ACTIVE: { tone: "info", label: "Aktif", icon: Settings2 },
  ARCHIVED: { tone: "muted", label: "Diarsipkan", icon: MinusCircle },
  RETIRING: { tone: "warn", label: "Sedang dipensiunkan", icon: AlertTriangle },
  RETIRED: { tone: "muted", label: "Dipensiunkan", icon: MinusCircle },
  PROPOSED: { tone: "muted", label: "Diusulkan", icon: CircleDashed },
  SUPERSEDED: { tone: "muted", label: "Digantikan", icon: MinusCircle },
  CURRENT: { tone: "ok", label: "Terkini", icon: CheckCircle2 },
  // Quota / routing
  EXHAUSTED: { tone: "bad", label: "Kuota habis", icon: Ban },
  WARN: { tone: "warn", label: "Peringatan", icon: AlertTriangle },
  WAITING_QUOTA: { tone: "warn", label: "Menunggu kuota", icon: Clock },
  // Verification / delivery
  QUARANTINED: { tone: "bad", label: "Dikarantina", icon: Ban },
  NOT_ENFORCEABLE: { tone: "warn", label: "Tidak dapat ditegakkan", icon: AlertTriangle },
  NOT_CONFIGURED: { tone: "warn", label: "Belum dikonfigurasi", icon: AlertTriangle },
  NEEDS_DECISION: { tone: "warn", label: "Perlu keputusan", icon: CircleHelp },
  NO_CHECKS: { tone: "muted", label: "Tidak ada pemeriksaan", icon: MinusCircle },
  NOT_RUN: { tone: "muted", label: "Belum dijalankan", icon: CircleDashed },
  INVALIDATED: { tone: "warn", label: "Dibatalkan validitasnya", icon: AlertTriangle },
  OPEN: { tone: "info", label: "Terbuka", icon: CircleDashed },
  MERGED: { tone: "info", label: "Di-merge (oleh manusia)", icon: CheckCircle2 },
  CLOSED: { tone: "muted", label: "Ditutup", icon: MinusCircle },
  // Publication / documents
  NOT_STARTED: { tone: "muted", label: "Belum dimulai", icon: CircleDashed },
  IN_FLIGHT: { tone: "info", label: "Sedang dikirim", icon: Loader2 },
  DRAFT: { tone: "muted", label: "Draf", icon: CircleDashed },
  PREVIEW: { tone: "info", label: "Preview", icon: CircleDashed },
  NOT_PUBLISHED: { tone: "muted", label: "Belum dipublikasikan", icon: MinusCircle },
  VISUAL_PENDING: { tone: "warn", label: "Visual tertunda", icon: Clock },
  NOT_SUPPORTED: { tone: "bad", label: "Tidak didukung", icon: Ban },
  SYNCED: { tone: "info", label: "Tersinkron", icon: CheckCircle2 },
  PROJECTION_ONLY: { tone: "info", label: "Proyeksi", icon: CircleDashed },
  NOT_RECORDED: { tone: "muted", label: "Belum tercatat", icon: CircleDashed },
  UPDATED: { tone: "info", label: "Diperbarui", icon: Settings2 },
  HUMAN_REQUIRED: { tone: "warn", label: "Menunggu manusia", icon: Clock },
  MOCK: { tone: "mock", label: "MOCK", icon: FlaskConical },
  DEMO: { tone: "mock", label: "DEMO", icon: FlaskConical },
  ESTIMATE: { tone: "mock", label: "Estimasi", icon: FlaskConical },
  LIVE: { tone: "ok", label: "LIVE", icon: CheckCircle2 },
};

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  bad: "bg-bad-bg text-bad",
  info: "bg-info-bg text-info",
  muted: "bg-muted-bg text-muted",
  mock: "bg-mock-bg text-mock",
};

export function statusSpec(status: string | null | undefined): Spec {
  if (!status) return SPECS.UNKNOWN;
  return SPECS[status] ?? { tone: "muted", label: status, icon: CircleHelp };
}

export function StatusBadge({ status, label, className }: { status: string | null | undefined; label?: string; className?: string }) {
  const spec = statusSpec(status);
  const Icon = spec.icon;
  return (
    <span
      className={clsx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap", TONE_CLASS[spec.tone], className)}
      title={status ?? "UNKNOWN"}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      <span>{label ?? spec.label}</span>
    </span>
  );
}
