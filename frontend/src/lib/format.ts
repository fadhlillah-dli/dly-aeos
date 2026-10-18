const rtf = new Intl.RelativeTimeFormat("id", { numeric: "auto" });
const dtf = new Intl.DateTimeFormat("id-ID", { dateStyle: "medium", timeStyle: "short" });

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "belum pernah";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "tidak diketahui";
  const diff = (t - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : dtf.format(d);
}

export function ageSeconds(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (Date.now() - t) / 1000;
}

export function usd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "UNKNOWN";
  return `$${v.toFixed(v < 1 ? 4 : 2)}`;
}

export function shortHash(h: string | null | undefined, n = 10): string {
  if (!h) return "—";
  const s = h.startsWith("sha256:") ? h.slice(7) : h;
  return s.slice(0, n);
}
