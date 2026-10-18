import { useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { FolderGit2, Lock, Sparkles } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, Card, ErrorBox, Field, inputClass, Mono, Notice, PageHeader } from "../../components/ui";
import { DomainBadge, ModuleBadge } from "./badges";
import { DropZone, QueueList, useUploadQueue } from "./UploadZone";
import { CLASSIFICATIONS, CODE_PROVIDER, type IntakePolicy, type ProjectView } from "./types";

type Kind = "NEW" | "EXISTING";

interface CreateResponse {
  project: ProjectView;
  verification: { verification_status: string; reason: string } | null;
}

function StepTitle({ n, title, hint }: { n: number; title: string; hint?: string }) {
  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-accent text-xs text-accent-ink">{n}</span>
        {title}
      </div>
      {hint && <p className="ml-8 mt-0.5 text-xs text-ink-3">{hint}</p>}
    </div>
  );
}

/** §05 steps 3-4: new vs existing, name + idea, uploads, explicit repository. Domain comes from the session. */
export default function NewProjectPage() {
  const { domain, isAdmin } = useAuth();
  const nav = useNavigate();
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: ["intake-policy"], queryFn: () => get<IntakePolicy>("/api/intake/policy"), staleTime: 300_000 });
  const queue = useUploadQueue(policy.data);
  const [kind, setKind] = useState<Kind>("NEW");
  const [name, setName] = useState("");
  const [idea, setIdea] = useState("");
  const [description, setDescription] = useState("");
  const [classification, setClassification] = useState("INTERNAL");
  const [withRepo, setWithRepo] = useState(false);
  const [owner, setOwner] = useState("");
  const [repoName, setRepoName] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [verify, setVerify] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<CreateResponse | null>(null);

  if (!domain) return null;
  const provider = CODE_PROVIDER[domain];
  const repoRequired = kind === "EXISTING";
  const repoIncluded = repoRequired || withRepo;
  const repoValid = !repoIncluded || (owner.trim() !== "" && repoName.trim() !== "");
  const canSubmit = isAdmin && name.trim() !== "" && repoValid && !busy && !created;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        domain, // the session's human choice, sent explicitly; the backend re-checks it (U-05)
        name: name.trim(),
        idea,
        description,
        project_kind: kind,
        data_classification: classification,
      };
      if (repoIncluded) {
        body.repository = {
          provider,
          owner: owner.trim(),
          name: repoName.trim(),
          local_path: localPath.trim() || null,
          verify: verify && provider === "GITHUB",
        };
      }
      const res = await post<CreateResponse>("/api/projects", body);
      setCreated(res);
      await queue.uploadAll(res.project.id, "wizard");
      qc.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <PageHeader
        title="Proyek baru"
        question="Proyek apa yang ingin dibangun atau dikelola, dan dari mana sumbernya?"
        source="Formulir ini → POST /api/projects (domain dicek ulang di backend)"
        evidenceLimit="Lampiran adalah DATA; repository belum terverifikasi sampai lookup provider berhasil"
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Lock className="h-4 w-4 text-ink-3" aria-hidden />
          <span>Domain proyek:</span>
          <DomainBadge domain={domain} />
          <span className="text-ink-3">
            — dipilih di header sesi, tidak dapat diubah di sini dan tidak pernah ditebak dari ide atau lampiran. Ganti domain lewat tombol domain di
            header bila perlu.
          </span>
        </div>
      </Card>

      {!isAdmin && <Notice tone="warn">Akun VIEWER hanya dapat membaca. Pembuatan proyek memerlukan SUPER_ADMIN (ditegakkan backend).</Notice>}

      <Card>
        <StepTitle n={1} title="Proyek baru atau existing?" hint="Untuk proyek existing, repository dipilih secara eksplisit (§05 langkah 3)." />
        <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Jenis proyek">
          {(
            [
              ["NEW", "Proyek baru", "Mulai dari ide; repository boleh ditambahkan nanti.", Sparkles],
              ["EXISTING", "Proyek existing", "Kode sudah ada; AEOS menginventarisasi repo tanpa scaffold ulang.", FolderGit2],
            ] as const
          ).map(([k, title, body, Icon]) => (
            <button
              key={k}
              role="radio"
              aria-checked={kind === k}
              onClick={() => setKind(k)}
              className={clsx("rounded-lg border p-4 text-left", kind === k ? "border-accent bg-info-bg" : "border-line bg-surface hover:border-accent")}
            >
              <div className="flex items-center gap-2 font-semibold">
                <Icon className="h-4 w-4" aria-hidden /> {title}
              </div>
              <p className="mt-1 text-xs text-ink-2">{body}</p>
            </button>
          ))}
        </div>
      </Card>

      <Card>
        <StepTitle n={2} title="Nama dan ide" hint="Teks yang menyerupai instruksi (mis. “ubah domain”) dicatat sebagai DATA, tidak dijalankan." />
        <div className="space-y-3">
          <Field label="Nama proyek">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} placeholder="mis. Pencatat Keuangan" />
          </Field>
          <Field label="Ide / tujuan" hint="Konteks minimum; pertanyaan tambahan hanya untuk ambiguitas yang material.">
            <textarea className={inputClass + " min-h-28"} value={idea} onChange={(e) => setIdea(e.target.value)} maxLength={20000} />
          </Field>
          <Field label="Deskripsi singkat (opsional)">
            <input className={inputClass} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={5000} />
          </Field>
          <Field label="Klasifikasi data" hint="Lampiran dan memori tidak dapat diklasifikasikan lebih rendah dari proyek.">
            <select className={inputClass} value={classification} onChange={(e) => setClassification(e.target.value)}>
              {CLASSIFICATIONS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      <Card>
        <StepTitle
          n={3}
          title={repoRequired ? "Repository (wajib untuk proyek existing)" : "Repository (opsional)"}
          hint={`Provider mengikuti domain: ${domain} → ${provider}. AEOS tidak menebak repo dari nama atau akun.`}
        />
        {!repoRequired && (
          <label className="mb-3 flex items-center gap-2 text-sm">
            <input type="checkbox" checked={withRepo} onChange={(e) => setWithRepo(e.target.checked)} /> Daftarkan repository sekarang
          </label>
        )}
        {repoIncluded && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Provider">
              <input className={inputClass} value={provider} readOnly aria-readonly />
            </Field>
            <Field label={provider === "GITHUB" ? "Owner (user/organisasi)" : "Workspace"}>
              <input className={inputClass} value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="mis. fadhlillah-dli" />
            </Field>
            <Field label="Nama repository">
              <input className={inputClass} value={repoName} onChange={(e) => setRepoName(e.target.value)} placeholder="mis. my-app" />
            </Field>
            <Field label="Path checkout lokal (opsional)" hint="Path absolut ke root repo; dibaca read-only (branch, head, dirty). Ditampilkan tersamar.">
              <input className={inputClass} value={localPath} onChange={(e) => setLocalPath(e.target.value)} placeholder="C:\\Users\\…\\my-app atau /Users/…/my-app" />
            </Field>
            {provider === "GITHUB" && (
              <label className="flex items-center gap-2 text-sm sm:col-span-2">
                <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} /> Verifikasi identitas sekarang (GET read-only ke API
                GitHub)
              </label>
            )}
          </div>
        )}
      </Card>

      <Card>
        <StepTitle n={4} title="Lampiran (.md, dokumen, gambar, data)" hint="Diunggah setelah proyek dibuat; tiap berkas dipindai dan diurai di proses terisolasi." />
        <Notice>{policy.data?.notice ?? "Lampiran adalah DATA tidak tepercaya dan tidak pernah dieksekusi."}</Notice>
        <div className="mt-3">
          <DropZone onFiles={queue.add} disabled={!isAdmin || busy} policy={policy.data} />
          <QueueList items={queue.items} onRemove={created ? undefined : queue.remove} />
        </div>
      </Card>

      <Card>
        <StepTitle n={5} title="Tinjau dan buat" />
        <dl className="mb-4 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-ink-3">Domain</dt>
          <dd>
            <DomainBadge domain={domain} />
          </dd>
          <dt className="text-ink-3">Jenis</dt>
          <dd>{kind === "EXISTING" ? "Existing" : "Baru"}</dd>
          <dt className="text-ink-3">Nama</dt>
          <dd>{name || "—"}</dd>
          <dt className="text-ink-3">Repository</dt>
          <dd>{repoIncluded ? `${provider} ${owner || "?"}/${repoName || "?"}` : "—"}</dd>
          <dt className="text-ink-3">Lampiran</dt>
          <dd>{queue.items.filter((q) => q.status === "queued").length} siap diunggah</dd>
        </dl>
        {error ? <ErrorBox error={error} /> : null}
        {created ? (
          <div className="space-y-2">
            <Notice>
              Proyek <strong>{created.project.name}</strong> dibuat (<Mono>{created.project.project_key}</Mono>).
              {created.verification && (
                <span className="ml-1 inline-flex items-center gap-1">
                  Verifikasi repository: <ModuleBadge kind="verify" value={created.verification.verification_status} /> — {created.verification.reason}
                </span>
              )}
            </Notice>
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" onClick={() => nav(`/projects/${created.project.id}`)}>
                Buka detail proyek
              </Button>
              {queue.pending > 0 && (
                <Button onClick={() => queue.uploadAll(created.project.id, "wizard")} busy={busy}>
                  Ulangi unggahan yang gagal
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" onClick={submit} disabled={!canSubmit} busy={busy}>
              Buat proyek di {domain}
            </Button>
            <Link to="/projects" className="inline-flex items-center px-3 text-sm text-ink-2 hover:underline">
              Batal
            </Link>
          </div>
        )}
      </Card>
    </div>
  );
}
