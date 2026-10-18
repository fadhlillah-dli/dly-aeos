import { useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Monitor, Moon, Sun } from "lucide-react";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Card, KeyValue, PageHeader } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { setTheme, useTheme, type ThemeChoice } from "../../lib/theme";
import PasswordChangePage from "../auth/PasswordChangePage";

const THEMES: { choice: ThemeChoice | null; label: string; icon: typeof Sun }[] = [
  { choice: "light", label: "Terang", icon: Sun },
  { choice: "dark", label: "Gelap", icon: Moon },
  { choice: null, label: "Ikuti sistem", icon: Monitor },
];

export default function SettingsPage() {
  const { session, projectId, domain } = useAuth();
  const qc = useQueryClient();
  // Same key/endpoint as the header project switch (shared cache).
  const projects = useQuery({
    queryKey: ["projects", "switch", domain],
    queryFn: () => get<{ items: { id: string; name: string; project_key: string }[] }>("/api/projects"),
    enabled: !!domain && !!projectId,
    retry: false,
  });
  const project = projects.data?.items.find((p) => p.id === projectId);
  const fetchedAt = qc.getQueryState(["session"])?.dataUpdatedAt;
  const { choice, theme } = useTheme();
  return (
    <div className="space-y-5">
      <PageHeader
        title="Pengaturan"
        question="Akun, sesi dan konteks kerja"
        source="Sesi server (/api/auth/session); preferensi tema disimpan lokal di browser ini"
        checkedAt={fetchedAt ? new Date(fetchedAt).toISOString() : null}
        evidenceLimit="Peran dan domain ditegakkan backend; tampilan ini hanya membaca sesi"
      />
      <Card title="Akun">
        <KeyValue
          items={[
            ["Username", session?.user.username],
            ["Peran", session?.user.role],
            ["Domain yang diizinkan", session?.allowed_domains.join(", ")],
            ["Domain aktif", session?.context.domain ?? "belum dipilih"],
            ["Proyek aktif", projectId ? (project ? `${project.name} (${project.project_key})` : projectId) : "belum dipilih"],
            ["Sesi berakhir", absoluteTime(session?.expires_at)],
          ]}
        />
      </Card>
      <Card title="Tampilan">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Tema">
          {THEMES.map((t) => (
            <button
              key={t.label}
              type="button"
              aria-pressed={choice === t.choice}
              onClick={() => setTheme(t.choice)}
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm",
                choice === t.choice ? "border-accent bg-accent text-accent-ink" : "border-line bg-surface hover:bg-surface-3",
              )}
            >
              <t.icon className="h-4 w-4" aria-hidden /> {t.label}
            </button>
          ))}
        </div>
        <p className="mt-2 text-xs text-ink-3">
          Aktif sekarang: {theme === "dark" ? "gelap" : "terang"}. Disimpan hanya di browser ini (localStorage); tanpa pilihan, tema mengikuti sistem
          operasi.
        </p>
      </Card>
      <PasswordChangePage />
    </div>
  );
}
