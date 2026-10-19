import { useQuery } from "@tanstack/react-query";
import { Building2, User } from "lucide-react";
import { useState } from "react";
import { get } from "../../api/client";
import { useAuth, type Domain } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { ErrorBox, Freshness } from "../../components/ui";

interface CapabilityRow {
  capability: string;
  label: string;
  domain: string | null;
  status: string;
  reasons: string[];
}

/** §05 step 1: readiness is the first thing shown after login - the platform-level gates (no domain data)
 * are visible before a domain is chosen; domain-specific readiness follows on the Kesiapan page. */
export function PlatformReadiness() {
  const q = useQuery({
    queryKey: ["doctor-capabilities", "platform-gate"],
    queryFn: () => get<{ items: CapabilityRow[]; checked_at: string | null }>("/api/doctor/capabilities"),
    retry: false,
  });
  if (q.isLoading) return <p className="text-sm text-ink-3">Memeriksa kesiapan platform…</p>;
  if (q.error) return <ErrorBox error={q.error} />;
  const rows = (q.data?.items ?? []).filter((c) => c.domain === null);
  return (
    <section aria-label="Kesiapan platform" className="mb-5 rounded-lg border border-line bg-surface p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Kesiapan platform</h2>
        <Freshness at={q.data?.checked_at ?? null} label="Diperiksa" />
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-ink-3">Belum ada hasil Readiness Doctor; jalankan aeos doctor. Status UNKNOWN bukan lulus.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {rows.map((c) => (
            <li key={c.capability} className="flex flex-wrap items-center gap-2">
              <StatusBadge status={c.status} />
              <span>{c.label}</span>
              {c.status !== "READY" && c.reasons[0] && <span className="text-xs text-ink-3">— {c.reasons[0]}</span>}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-ink-3">Kesiapan per domain dan perbaikannya ada di halaman Kesiapan setelah domain dipilih.</p>
    </section>
  );
}

const COPY: Record<Domain, { title: string; body: string }> = {
  PERSONAL: {
    title: "PERSONAL",
    body: "GitHub + Google Docs dalam folder Google Drive per proyek. Route model sesuai klasifikasi data.",
  },
  OFFICE: {
    title: "OFFICE",
    body: "Bitbucket + Confluence + Jira. Worker eksternal ditolak secara default sampai disetujui kantor.",
  },
};

/** U-05 / §05 step 2: the human chooses the domain before any prompt. Never inferred by a model. */
export default function DomainGatePage() {
  const { selectContext, session } = useAuth();
  const [error, setError] = useState<unknown>(null);
  const allowed = session?.allowed_domains ?? [];
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-2xl">
        <PlatformReadiness />
        <h1 className="text-xl font-semibold">Pilih domain kerja</h1>
        <p className="mb-5 text-ink-2">Domain dipilih manusia sebelum prompt apa pun. Model tidak dapat menebak, menyarankan atau mengganti domain.</p>
        <div className="grid gap-4 sm:grid-cols-2">
          {(["PERSONAL", "OFFICE"] as Domain[]).map((d) => (
            <button
              key={d}
              disabled={!allowed.includes(d)}
              onClick={async () => {
                try {
                  await selectContext(d);
                } catch (e) {
                  setError(e);
                }
              }}
              className="rounded-lg border border-line bg-surface p-5 text-left hover:border-accent disabled:opacity-50"
            >
              <div className="flex items-center gap-2 text-base font-semibold">
                {d === "PERSONAL" ? <User className="h-5 w-5" aria-hidden /> : <Building2 className="h-5 w-5" aria-hidden />}
                {COPY[d].title}
              </div>
              <p className="mt-2 text-sm text-ink-2">{COPY[d].body}</p>
            </button>
          ))}
        </div>
        <div className="mt-4">
          <ErrorBox error={error} />
        </div>
      </div>
    </div>
  );
}
