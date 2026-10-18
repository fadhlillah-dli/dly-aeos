import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, ErrorBox, Field, inputClass, Mono, Notice } from "../../components/ui";

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const boot = useQuery({ queryKey: ["bootstrap-status"], queryFn: () => get<{ bootstrap_complete: boolean }>("/api/auth/bootstrap-status") });

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-line bg-surface p-6">
        <div>
          <h1 className="text-lg font-semibold">DLY AEOS</h1>
          <p className="text-sm text-ink-3">Masuk ke Control Center lokal</p>
        </div>
        {boot.data && !boot.data.bootstrap_complete && (
          <Notice tone="warn">
            Belum ada akun SUPER_ADMIN. Jalankan di terminal repo: <Mono>aeos admin create</Mono> — tidak ada kredensial bawaan.
          </Notice>
        )}
        <Field label="Username">
          <input className={inputClass} autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </Field>
        <Field label="Password">
          <input className={inputClass} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        <ErrorBox error={error} />
        <Button variant="primary" type="submit" busy={busy} className="w-full">
          Masuk
        </Button>
      </form>
    </div>
  );
}
