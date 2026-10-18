import { useState, type FormEvent } from "react";
import { post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, ErrorBox, Field, inputClass, Notice } from "../../components/ui";

export default function PasswordChangePage({ forced = false }: { forced?: boolean }) {
  const { refresh, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== repeat) {
      setError(new Error("Password baru tidak sama"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/password", { current_password: current, new_password: next });
      setDone(true);
      setCurrent("");
      setNext("");
      setRepeat("");
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={forced ? "flex min-h-full items-center justify-center p-6" : ""}>
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-line bg-surface p-6">
        <h2 className="text-base font-semibold">Ganti password</h2>
        {forced && <Notice tone="warn">Password sementara harus diganti sebelum melanjutkan.</Notice>}
        <Field label="Password saat ini">
          <input className={inputClass} type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        </Field>
        <Field label="Password baru" hint="Minimal 12 karakter, tidak memuat username.">
          <input className={inputClass} type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
        </Field>
        <Field label="Ulangi password baru">
          <input className={inputClass} type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
        </Field>
        <ErrorBox error={error} />
        {done && <Notice>Password diganti. Sesi lain telah dicabut.</Notice>}
        <div className="flex gap-2">
          <Button variant="primary" type="submit" busy={busy}>
            Simpan
          </Button>
          {forced && (
            <Button type="button" onClick={() => logout()}>
              Keluar
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
