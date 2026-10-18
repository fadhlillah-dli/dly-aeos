import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, GitBranch, ScanSearch, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { post } from "../../api/client";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, KeyValue, Mono } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { ModuleBadge } from "./badges";
import { InventoryFacts } from "./InventoryFacts";
import { CODE_PROVIDER, type ProjectView, type RepositoryView } from "./types";

function RepoCard({ repo, isAdmin, onChanged }: { repo: RepositoryView; isAdmin: boolean; onChanged: () => void }) {
  const [localPath, setLocalPath] = useState("");
  const [showInv, setShowInv] = useState(false);
  const verify = useMutation({ mutationFn: () => post(`/api/repositories/${repo.id}/verify`), onSuccess: onChanged });
  const inventory = useMutation({
    mutationFn: () => post(`/api/repositories/${repo.id}/inventory`, {}),
    onSuccess: () => {
      setShowInv(true);
      onChanged();
    },
  });
  const setPath = useMutation({
    mutationFn: () => post(`/api/repositories/${repo.id}/local-path`, { local_path: localPath.trim() }),
    onSuccess: () => {
      setLocalPath("");
      onChanged();
    },
  });
  const err = verify.error || inventory.error || setPath.error;
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <GitBranch className="h-4 w-4 text-ink-3" aria-hidden />
        <span className="text-base font-semibold">{repo.full_name}</span>
        <span className="text-xs text-ink-3">
          {repo.provider} · peran {repo.role === "platform" ? "platform" : "proyek"}
        </span>
        <ModuleBadge kind="verify" value={repo.verification_status} />
        {repo.url && (
          <a href={repo.url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
            {repo.url_source === "provider" ? "Buka" : "URL dideklarasikan"} <ExternalLink className="h-3 w-3" aria-hidden />
          </a>
        )}
      </div>
      <p className="mt-1 text-xs text-ink-2">{repo.verification_reason}</p>
      <div className="mt-3">
        <KeyValue
          items={[
            ["ID repo provider", repo.provider_repo_id ? <Mono>{repo.provider_repo_id}</Mono> : "UNKNOWN (belum diverifikasi)"],
            ["Default branch", repo.default_branch ?? "—"],
            ["Terakhir diverifikasi", repo.last_verified_at ? <Freshness at={repo.last_verified_at} staleAfterSeconds={7 * 86400} /> : "belum pernah"],
            [
              "Lookup terakhir",
              repo.last_attempt?.at ? `${repo.last_attempt.auth_mode ?? "?"} · HTTP ${repo.last_attempt.http_status ?? repo.last_attempt.error ?? "—"}` : "—",
            ],
            [
              "Visibilitas",
              repo.github ? `${repo.github.visibility ?? (repo.github.private ? "private" : "public")}${repo.github.fork ? ` · fork dari ${repo.github.parent ?? "?"}` : ""}${repo.github.archived ? " · archived" : ""}` : "UNKNOWN",
            ],
            ["Checkout lokal", repo.local_path ?? "tidak terdaftar"],
            [
              "Status lokal",
              <span key="l" className="flex flex-wrap items-center gap-2">
                <ModuleBadge kind="local" value={repo.local_status} />
                {repo.local_branch && (
                  <span>
                    branch <Mono>{repo.local_branch}</Mono>
                  </span>
                )}
                {repo.head_commit && (
                  <span>
                    head <Mono>{shortHash(repo.head_commit, 12)}</Mono>
                  </span>
                )}
                {repo.dirty !== null && <span>{repo.dirty ? "dirty" : "bersih"}</span>}
                {repo.local_checked_at && <Freshness at={repo.local_checked_at} />}
              </span>,
            ],
            ...(repo.local?.reason || repo.local?.error ? ([["Catatan lokal", repo.local.reason ?? repo.local.error]] as [string, string][]) : []),
          ]}
        />
      </div>
      {err ? (
        <div className="mt-3">
          <ErrorBox error={err} />
        </div>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button onClick={() => verify.mutate()} busy={verify.isPending} disabled={!isAdmin} title={isAdmin ? "GET read-only ke provider" : "Hanya SUPER_ADMIN"}>
          <ShieldCheck className="h-4 w-4" aria-hidden /> Verifikasi identitas
        </Button>
        <Button
          onClick={() => inventory.mutate()}
          busy={inventory.isPending}
          disabled={!isAdmin || !repo.local_path}
          title={repo.local_path ? "Inventaris read-only pada commit saat ini" : "Daftarkan path checkout lokal dulu"}
        >
          <ScanSearch className="h-4 w-4" aria-hidden /> Inventaris repo
        </Button>
        {repo.inventory && (
          <Button variant="ghost" onClick={() => setShowInv(!showInv)}>
            {showInv ? "Sembunyikan fakta" : "Lihat fakta inventaris"}
          </Button>
        )}
      </div>
      {isAdmin && repo.role !== "platform" && (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (localPath.trim()) setPath.mutate();
          }}
        >
          <div className="min-w-64 flex-1">
            <Field label={repo.local_path ? "Ganti path checkout lokal" : "Path checkout lokal"} hint="Path absolut ke root repository; hanya dibaca.">
              <input className={inputClass} value={localPath} onChange={(e) => setLocalPath(e.target.value)} />
            </Field>
          </div>
          <Button type="submit" busy={setPath.isPending} disabled={!localPath.trim()}>
            Simpan path
          </Button>
        </form>
      )}
      {showInv && repo.inventory && (
        <div className="mt-3">
          <InventoryFacts inv={repo.inventory} />
        </div>
      )}
    </div>
  );
}

function AddRepository({ project, onChanged }: { project: ProjectView; onChanged: () => void }) {
  const provider = CODE_PROVIDER[project.domain];
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [verify, setVerify] = useState(provider === "GITHUB");
  const add = useMutation({
    mutationFn: () =>
      post(`/api/projects/${project.id}/repositories`, {
        provider,
        owner: owner.trim(),
        name: name.trim(),
        local_path: localPath.trim() || null,
        verify,
      }),
    onSuccess: () => {
      setOwner("");
      setName("");
      setLocalPath("");
      onChanged();
    },
  });
  return (
    <Card title="Tambah repository">
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <Field label="Provider" hint={`${project.domain} memakai ${provider}`}>
          <input className={inputClass} value={provider} readOnly aria-readonly />
        </Field>
        <Field label={provider === "GITHUB" ? "Owner" : "Workspace"}>
          <input className={inputClass} value={owner} onChange={(e) => setOwner(e.target.value)} required />
        </Field>
        <Field label="Nama repository">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="Path checkout lokal (opsional)">
          <input className={inputClass} value={localPath} onChange={(e) => setLocalPath(e.target.value)} />
        </Field>
        {provider === "GITHUB" && (
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} /> Verifikasi identitas setelah disimpan
          </label>
        )}
        {add.error ? (
          <div className="sm:col-span-2">
            <ErrorBox error={add.error} />
          </div>
        ) : null}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={add.isPending} disabled={!owner.trim() || !name.trim()}>
            Simpan repository
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function RepositoriesPanel({ project, isAdmin }: { project: ProjectView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["project", project.id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  return (
    <div className="space-y-4">
      {project.repositories.length === 0 ? (
        <EmptyState title="Belum ada repository">
          {project.project_kind === "EXISTING"
            ? "Proyek existing memerlukan repository yang dipilih eksplisit."
            : "Tambahkan repository saat siap; AEOS tidak menebak repo dari nama atau akun."}
        </EmptyState>
      ) : (
        project.repositories.map((r) => <RepoCard key={r.id} repo={r} isAdmin={isAdmin} onChanged={refresh} />)
      )}
      {isAdmin && <AddRepository project={project} onChanged={refresh} />}
    </div>
  );
}
