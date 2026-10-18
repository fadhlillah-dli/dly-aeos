import { useState, type ReactNode } from "react";
import { ErrorBox, Field, inputClass, Button } from "../ui";

/**
 * Admin-only action with an explicit confirmation step and a reason. For VIEWER the button is
 * disabled with an explanation — the backend still enforces RBAC (§35 "menyembunyikan tombol di UI
 * bukan enforcement").
 */
export function ConfirmAction({
  label,
  confirmText,
  isAdmin,
  onConfirm,
  variant = "secondary",
  icon,
  extra,
  requireReason = false,
}: {
  label: string;
  confirmText: string;
  isAdmin: boolean;
  onConfirm: (reason: string) => Promise<unknown>;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  icon?: ReactNode;
  extra?: ReactNode;
  requireReason?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!isAdmin) {
    return (
      <Button variant={variant} disabled title="Hanya SUPER_ADMIN; peran VIEWER hanya-baca">
        {icon}
        {label}
      </Button>
    );
  }
  if (!open) {
    return (
      <Button variant={variant} onClick={() => setOpen(true)}>
        {icon}
        {label}
      </Button>
    );
  }
  return (
    <div className="w-full max-w-md rounded-md border border-line bg-surface-2 p-3 text-sm" role="dialog" aria-label={label}>
      <p className="mb-2 font-medium">{confirmText}</p>
      <Field label="Alasan (dicatat di audit)">
        <input className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
      </Field>
      {extra}
      {error ? <div className="mt-2"><ErrorBox error={error} /></div> : null}
      <div className="mt-3 flex gap-2">
        <Button
          variant={variant === "danger" ? "danger" : "primary"}
          busy={busy}
          disabled={requireReason && !reason.trim()}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await onConfirm(reason);
              setOpen(false);
              setReason("");
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          Konfirmasi
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Batal
        </Button>
      </div>
    </div>
  );
}
