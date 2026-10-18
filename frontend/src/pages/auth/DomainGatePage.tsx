import { Building2, User } from "lucide-react";
import { useState } from "react";
import { useAuth, type Domain } from "../../auth/AuthContext";
import { ErrorBox } from "../../components/ui";

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
