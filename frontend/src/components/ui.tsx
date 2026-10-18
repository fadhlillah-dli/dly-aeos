import { clsx } from "clsx";
import { AlertTriangle, Database, Info, Loader2 } from "lucide-react";
import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link, useInRouterContext } from "react-router-dom";
import { ApiError } from "../api/client";
import { absoluteTime, ageSeconds, relativeTime } from "../lib/format";
import { StatusBadge } from "./StatusBadge";

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={clsx("rounded-lg border border-line bg-surface", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

/**
 * Every page answers one human question first, then shows where its data came from (§35, §41:
 * "Sumber data", "Diperiksa pada", "Batas bukti", "Keputusan terkait", "Usulkan koreksi").
 */
export function PageHeader({
  title,
  question,
  source,
  checkedAt,
  evidenceLimit,
  actions,
  mode,
  relatedDecisionsHref,
  correctionHref,
}: {
  title: string;
  question: string;
  source?: string;
  checkedAt?: string | null;
  evidenceLimit?: string;
  actions?: ReactNode;
  mode?: string;
  /** "Keputusan terkait" link; defaults to the decision register, null hides it. */
  relatedDecisionsHref?: string | null;
  /** "Usulkan koreksi" link; defaults to a COMPARE proposal about this page, null hides it. */
  correctionHref?: string | null;
}) {
  const inRouter = useInRouterContext();
  const decisionsHref = relatedDecisionsHref === undefined ? "/decisions" : relatedDecisionsHref;
  // A correction is a COMPARE proposal about this page (reviewing it never installs or activates anything).
  const fixHref =
    correctionHref === undefined
      ? `/proposals?kind=COMPARE&source=${encodeURIComponent(`DLY AEOS ${title}`)}`
      : correctionHref;
  const RelLink = ({ to, children }: { to: string; children: ReactNode }) =>
    inRouter ? (
      <Link to={to} className="text-accent hover:underline">
        {children}
      </Link>
    ) : (
      <a href={to} className="text-accent hover:underline">
        {children}
      </a>
    );
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-0.5 text-ink-2">{question}</p>
        {(source || checkedAt !== undefined || evidenceLimit) && (
          <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-ink-3">
            {source && (
              <div className="flex items-center gap-1">
                <Database className="h-3.5 w-3.5" aria-hidden />
                <dt>Sumber data:</dt>
                <dd className="text-ink-2">{source}</dd>
              </div>
            )}
            {checkedAt !== undefined && (
              <div className="flex items-center gap-1">
                <dt>Diperiksa pada:</dt>
                <dd className="text-ink-2" title={absoluteTime(checkedAt)}>
                  {checkedAt ? relativeTime(checkedAt) : "belum pernah"}
                </dd>
              </div>
            )}
            {evidenceLimit && (
              <div className="flex items-center gap-1">
                <dt>Batas bukti:</dt>
                <dd className="text-ink-2">{evidenceLimit}</dd>
              </div>
            )}
            {mode && mode !== "LIVE" && <StatusBadge status={mode} />}
          </dl>
        )}
        {(decisionsHref || fixHref) && (
          <div className="mt-1.5 flex flex-wrap gap-x-4 text-xs">
            {decisionsHref && <RelLink to={decisionsHref}>Keputusan terkait</RelLink>}
            {fixHref && <RelLink to={fixHref}>Usulkan koreksi</RelLink>}
          </div>
        )}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Button({
  variant = "secondary",
  busy,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost"; busy?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || busy}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" && "bg-accent text-accent-ink hover:opacity-90",
        variant === "secondary" && "border border-line bg-surface hover:bg-surface-3",
        variant === "danger" && "bg-bad text-white hover:opacity-90",
        variant === "ghost" && "hover:bg-surface-3",
        className,
      )}
    >
      {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Loading({ label = "Memuat…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-ink-3" role="status">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> {label}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : null;
  const message = e ? e.message : error instanceof Error ? error.message : String(error);
  const reasons = (e?.details?.reasons as string[] | undefined) ?? [];
  return (
    <div className="rounded-md border border-bad/30 bg-bad-bg p-3 text-sm text-bad" role="alert">
      <div className="flex items-center gap-2 font-medium">
        <AlertTriangle className="h-4 w-4" aria-hidden />
        {e?.code ?? "ERROR"}
      </div>
      <div className="mt-1">{message}</div>
      {reasons.length > 0 && (
        <ul className="mt-1 list-disc pl-5">
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line p-8 text-center">
      <div className="font-medium">{title}</div>
      {children && <div className="mt-1 text-ink-3">{children}</div>}
    </div>
  );
}

export function Freshness({ at, staleAfterSeconds = 900, label = "Diperiksa" }: { at: string | null | undefined; staleAfterSeconds?: number; label?: string }) {
  const age = ageSeconds(at);
  const stale = age === null || age > staleAfterSeconds;
  return (
    <span className={clsx("inline-flex items-center gap-1 text-xs", stale ? "text-warn" : "text-ink-3")} title={absoluteTime(at)}>
      {stale && <AlertTriangle className="h-3 w-3" aria-hidden />}
      {label} {relativeTime(at)}
      {stale && age !== null && " — usang"}
    </span>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(120px,max-content)_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-ink-3">{k}</dt>
          <dd className="min-w-0 break-words">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Table<T>({
  rows,
  columns,
  rowKey,
  empty = "Tidak ada data",
  onRowClick,
}: {
  rows: T[];
  columns: { header: ReactNode; cell: (row: T) => ReactNode; className?: string }[];
  rowKey: (row: T) => string;
  empty?: string;
  onRowClick?: (row: T) => void;
}) {
  if (!rows.length) return <EmptyState title={empty} />;
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="min-w-full text-sm">
        <thead className="bg-surface-2 text-left text-xs uppercase tracking-wide text-ink-3">
          <tr>
            {columns.map((c, i) => (
              <th key={i} className={clsx("px-3 py-2 font-medium", c.className)}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line bg-surface">
          {rows.map((r) => (
            <tr key={rowKey(r)} className={clsx(onRowClick && "cursor-pointer hover:bg-surface-2")} onClick={onRowClick ? () => onRowClick(r) : undefined}>
              {columns.map((c, i) => (
                <td key={i} className={clsx("px-3 py-2 align-top", c.className)}>
                  {c.cell(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Tabs({ tabs, initial }: { tabs: { id: string; label: string; content: ReactNode }[]; initial?: string }) {
  const [active, setActive] = useState(initial ?? tabs[0]?.id);
  return (
    <div>
      <div role="tablist" className="mb-4 flex flex-wrap gap-1 border-b border-line">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={active === t.id}
            onClick={() => setActive(t.id)}
            className={clsx("-mb-px border-b-2 px-3 py-2 text-sm", active === t.id ? "border-accent font-medium" : "border-transparent text-ink-3 hover:text-ink")}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tabs.find((t) => t.id === active)?.content}
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "mock" | "bad"; children: ReactNode }) {
  return (
    <div
      className={clsx(
        "flex items-start gap-2 rounded-md p-3 text-sm",
        tone === "info" && "bg-info-bg text-info",
        tone === "warn" && "bg-warn-bg text-warn",
        tone === "mock" && "bg-mock-bg text-mock",
        tone === "bad" && "bg-bad-bg text-bad",
      )}
    >
      <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div>{children}</div>
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="mono rounded bg-surface-3 px-1 py-0.5">{children}</code>;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-3">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "w-full rounded-md border border-line bg-surface px-3 py-1.5 text-sm placeholder:text-ink-3 focus:border-accent focus:outline-none";
