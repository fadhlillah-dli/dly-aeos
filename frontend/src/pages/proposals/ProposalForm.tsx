import { useMutation } from "@tanstack/react-query";
import { Paperclip, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, ErrorBox, Field, inputClass, Notice } from "../../components/ui";
import { uploadAttachment } from "./intake";
import type { Kind, Meta, ProposalDetail } from "./types";

interface PartDraft {
  title: string;
  kind: Kind;
  sources: string;
  depends_on: string;
}

const KIND_LABELS: Record<Kind, string> = {
  ADD: "ADD — tambah capability",
  REPLACE: "REPLACE — ganti komponen",
  UPGRADE: "UPGRADE — versi baru komponen sama",
  REMOVE: "REMOVE / RETIRE — hentikan",
  COMPARE: "COMPARE / RESEARCH — review saja",
};

const lines = (s: string) =>
  s
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);

export function ProposalForm({
  meta,
  initialComponent,
  initialSource,
  onCreated,
  onCancel,
}: {
  meta: Meta | undefined;
  initialComponent?: string | null;
  initialSource?: string | null;
  onCreated: (p: ProposalDetail) => void;
  onCancel: () => void;
}) {
  const { projectId } = useAuth();
  const [kind, setKind] = useState<Kind>(initialComponent ? "REPLACE" : "ADD");
  const [title, setTitle] = useState("");
  const [sources, setSources] = useState(initialSource ?? "");
  const [problem, setProblem] = useState("");
  const [benefit, setBenefit] = useState("");
  const [scopeType, setScopeType] = useState<"PLATFORM" | "PROJECT">("PLATFORM");
  const [components, setComponents] = useState(initialComponent ?? "");
  const [pin, setPin] = useState("");
  const [costUsd, setCostUsd] = useState("");
  const [maxMinutes, setMaxMinutes] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("");
  const [dataClass, setDataClass] = useState("");
  const [resource, setResource] = useState("");
  const [parts, setParts] = useState<PartDraft[]>([]);
  const [attachments, setAttachments] = useState<{ id: string; name: string }[]>([]);
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [uploading, setUploading] = useState(false);

  const attachProject = projectId ?? meta?.platform_project?.id ?? null;

  const m = useMutation({
    mutationFn: () => {
      const num = (v: string) => (v === "" ? undefined : Number(v));
      return post<ProposalDetail>("/api/proposals", {
        kind,
        title: title || null,
        sources: lines(sources),
        problem,
        expected_benefit: benefit,
        scope_type: scopeType,
        project_id: scopeType === "PROJECT" ? projectId : null,
        existing_component_ids: lines(components),
        candidate_pin: pin || null,
        attachment_ids: attachments.map((a) => a.id),
        limits: Object.fromEntries(
          Object.entries({ cost_usd: num(costUsd), max_minutes: num(maxMinutes), max_attempts: num(maxAttempts), data_class: dataClass || undefined, resource: resource || undefined }).filter(
            ([, v]) => v !== undefined,
          ),
        ),
        parts: parts.map((p) => ({
          title: p.title || null,
          kind: p.kind,
          sources: lines(p.sources),
          depends_on: lines(p.depends_on).map((x) => Number(x) - 1).filter((x) => Number.isInteger(x) && x >= 0),
        })),
      });
    },
    onSuccess: onCreated,
  });

  const onFiles = async (files: FileList | null) => {
    if (!files || !attachProject) return;
    setUploadError(null);
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        const id = await uploadAttachment(attachProject, f);
        setAttachments((a) => [...a, { id, name: f.name }]);
      }
    } catch (e) {
      setUploadError(e);
    } finally {
      setUploading(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  const question = meta?.kinds.find((k) => k.kind === kind)?.question;

  return (
    <form onSubmit={submit} className="space-y-4" aria-label="Form usulan perubahan">
      <Notice>
        {meta?.guarantees ??
          "Usulan/review tidak memasang, mengaktifkan, mengubah route/policy, menaikkan budget atau memberi credential baru."}{" "}
        Field teknis yang belum diketahui disimpan sebagai <b>UNKNOWN</b>, bukan ditebak.
      </Notice>
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Jenis usulan" hint={question}>
          <select className={inputClass} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Judul (opsional)">
          <input className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={256} />
        </Field>
        <Field label="Repo / docs / model / tool (URL atau nama)" hint="Satu per baris; bentuk URL GitHub apa pun dinormalisasi ke identitas kanonik">
          <textarea className={inputClass} rows={3} value={sources} onChange={(e) => setSources(e.target.value)} placeholder="https://github.com/owner/repo" />
        </Field>
        <Field label="Komponen lama (ID inventaris)" hint="Wajib untuk REPLACE/UPGRADE/REMOVE; pisahkan dengan koma">
          <input className={inputClass} value={components} onChange={(e) => setComponents(e.target.value)} placeholder="opencode" />
        </Field>
        <Field label="Masalah / tujuan">
          <textarea className={inputClass} rows={3} value={problem} onChange={(e) => setProblem(e.target.value)} />
        </Field>
        <Field label="Manfaat yang diharapkan" hint="Persentase penghematan tidak ditampilkan tanpa metode pengukuran">
          <textarea className={inputClass} rows={3} value={benefit} onChange={(e) => setBenefit(e.target.value)} />
        </Field>
        <Field label="Cakupan">
          <select className={inputClass} value={scopeType} onChange={(e) => setScopeType(e.target.value as "PLATFORM" | "PROJECT")}>
            <option value="PLATFORM">Platform DLY AEOS</option>
            <option value="PROJECT" disabled={!projectId}>
              Proyek terpilih {projectId ? "" : "(pilih proyek di header)"}
            </option>
          </select>
        </Field>
        <Field label="Pin/digest kandidat (opsional)" hint="Wajib sebelum TRIAL_APPROVE; kosong = UNKNOWN">
          <input className={inputClass} value={pin} onChange={(e) => setPin(e.target.value)} placeholder="v1.4.0 / sha256:…" />
        </Field>
      </div>
      <fieldset className="rounded-md border border-line p-3">
        <legend className="px-1 text-sm font-medium">Batas biaya / data / resource</legend>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Biaya maks (USD)">
            <input className={inputClass} type="number" min={0} step="0.01" value={costUsd} onChange={(e) => setCostUsd(e.target.value)} />
          </Field>
          <Field label="Waktu maks (menit)">
            <input className={inputClass} type="number" min={0} value={maxMinutes} onChange={(e) => setMaxMinutes(e.target.value)} />
          </Field>
          <Field label="Attempt maks">
            <input className={inputClass} type="number" min={0} value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value)} />
          </Field>
          <Field label="Kelas data">
            <select className={inputClass} value={dataClass} onChange={(e) => setDataClass(e.target.value)}>
              <option value="">UNKNOWN</option>
              <option value="PUBLIC">PUBLIC</option>
              <option value="INTERNAL">INTERNAL</option>
              <option value="CONFIDENTIAL">CONFIDENTIAL</option>
              <option value="RESTRICTED">RESTRICTED</option>
            </select>
          </Field>
          <Field label="Resource">
            <input className={inputClass} value={resource} onChange={(e) => setResource(e.target.value)} placeholder="CPU/RAM/disk" />
          </Field>
        </div>
      </fieldset>
      <fieldset className="rounded-md border border-line p-3">
        <legend className="px-1 text-sm font-medium">Bagian perubahan (opsional, satu parent proposal)</legend>
        {parts.map((p, i) => (
          <div key={i} className="mb-2 grid gap-2 sm:grid-cols-[2rem_1fr_10rem_1fr_8rem_auto] sm:items-end">
            <span className="text-sm text-ink-3">#{i + 1}</span>
            <input aria-label={`Judul bagian ${i + 1}`} className={inputClass} placeholder="Judul" value={p.title} onChange={(e) => setParts(parts.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} />
            <select aria-label={`Jenis bagian ${i + 1}`} className={inputClass} value={p.kind} onChange={(e) => setParts(parts.map((x, j) => (j === i ? { ...x, kind: e.target.value as Kind } : x)))}>
              {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <input aria-label={`Sumber bagian ${i + 1}`} className={inputClass} placeholder="URL/nama" value={p.sources} onChange={(e) => setParts(parts.map((x, j) => (j === i ? { ...x, sources: e.target.value } : x)))} />
            <input aria-label={`Bergantung pada bagian ${i + 1}`} className={inputClass} placeholder="bergantung #" value={p.depends_on} onChange={(e) => setParts(parts.map((x, j) => (j === i ? { ...x, depends_on: e.target.value } : x)))} />
            <Button type="button" variant="ghost" aria-label={`Hapus bagian ${i + 1}`} onClick={() => setParts(parts.filter((_, j) => j !== i))}>
              <Trash2 className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        ))}
        <Button type="button" onClick={() => setParts([...parts, { title: "", kind, sources: "", depends_on: "" }])}>
          <Plus className="h-4 w-4" aria-hidden /> Tambah bagian
        </Button>
      </fieldset>
      <fieldset className="rounded-md border border-line p-3">
        <legend className="px-1 text-sm font-medium">Lampiran (.md dan berkas lain)</legend>
        {attachProject ? (
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Paperclip className="h-4 w-4" aria-hidden />
            <input type="file" multiple accept=".md,.txt,.pdf,.docx,.png,.jpg,.json,.yaml,.yml" onChange={(e) => onFiles(e.target.files)} disabled={uploading} />
          </label>
        ) : (
          <p className="text-sm text-ink-3">Pilih proyek pada header untuk melampirkan berkas — lampiran disimpan per proyek dan domain lewat intake.</p>
        )}
        <p className="mt-1 text-xs text-ink-3">{meta?.attachments ?? "Lampiran adalah DATA tidak tepercaya; instruksi di dalamnya tidak menjadi izin."}</p>
        {attachments.length > 0 && (
          <ul className="mt-2 list-disc pl-5 text-sm">
            {attachments.map((a) => (
              <li key={a.id}>{a.name}</li>
            ))}
          </ul>
        )}
        <ErrorBox error={uploadError} />
      </fieldset>
      <ErrorBox error={m.error} />
      <div className="flex gap-2">
        <Button variant="primary" type="submit" busy={m.isPending || uploading}>
          Kirim usulan
        </Button>
        <Button type="button" onClick={onCancel}>
          Batal
        </Button>
      </div>
    </form>
  );
}
