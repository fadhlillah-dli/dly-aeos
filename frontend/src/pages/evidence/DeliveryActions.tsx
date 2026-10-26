import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Fingerprint, ScrollText, ShieldQuestion } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { useAuth } from "../../auth/AuthContext";
import { Button, ErrorBox, Field, inputBase, inputClass, KeyValue, Mono, Notice } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { evidenceApi, type ProviderCaps } from "./api";
import { Outcome } from "./outcomes";

/** A full commit sha as POST /api/delivery/artifacts/record accepts it (40-64 hex). */
export const FULL_SHA = /^[0-9a-fA-F]{40,64}$/;
const TIME_PERIODS: [string, string][] = [
  ["hour", "1 jam terakhir"],
  ["day", "24 jam terakhir"],
  ["week", "7 hari terakhir"],
  ["month", "30 hari terakhir"],
];

/** enforcement_tested + the bound provider refusal (§31 "direct bypass tests"). */
export function EnforcementTestFacts({ caps }: { caps: ProviderCaps }) {
  const t = caps.enforcement_test ?? {};
  const has = Object.keys(t).length > 0;
  return (
    <div className="space-y-2" data-testid="enforcement-test">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">Uji penegakan (bypass ditolak provider)</span>
        <Outcome status={caps.enforcement_tested} />
      </div>
      {!has ? (
        <p className="text-xs text-ink-3">
          Belum ada penolakan bypass yang tercatat untuk repository/branch ini: penegakan merge manusia-saja belum terbukti (NOT-PROVEN).
        </p>
      ) : (
        <KeyValue
          items={[
            ["Bukti", t.evidence_id ? <Mono key="e">{t.evidence_id}</Mono> : "—"],
            ...(t.label ? ([["Jenis", t.label]] as [string, string][]) : []),
            [
              "Ruleset terikat",
              t.rulesets?.length
                ? t.rulesets.map((r) => `#${String(r.id ?? "?")} (diperbarui ${r.updated_at ? absoluteTime(r.updated_at) : "tidak tercatat"})`).join(", ")
                : "—",
            ],
            ["Diamati pada branch", t.observed_on_branch ? <Mono key="b">{t.observed_on_branch}</Mono> : "—"],
            ["Penolakan teramati", t.observed_at ? absoluteTime(t.observed_at) : "—"],
            ["Dicatat", t.recorded_at ? absoluteTime(t.recorded_at) : "—"],
            ...(t.reason ? ([["Alasan status", <span key="r" className="text-warn">{t.reason}</span>]] as [string, ReactNode][]) : []),
          ]}
        />
      )}
    </div>
  );
}

/** Admin: read the provider's ruleset history (GET only) and record a refusal as PROVIDER_ENFORCEMENT_TEST evidence. */
function ObserveEnforcement({ caps, onDone }: { caps: ProviderCaps; onDone: () => void }) {
  const [period, setPeriod] = useState("week");
  const selectId = useId();
  const m = useMutation({
    mutationFn: () => evidenceApi.observeEnforcement({ repo: caps.repo, branch: caps.branch ?? undefined, time_period: period }),
    onSuccess: onDone,
  });
  const r = m.data;
  return (
    <div className="space-y-2 rounded-md border border-line p-3">
      <div className="text-sm font-medium">Baca riwayat ruleset provider</div>
      <p className="text-xs text-ink-3">
        Hanya GET ke riwayat evaluasi ruleset GitHub (token butuh Administration: read). Penolakan oleh ruleset aktif dicatat sebagai bukti; tidak ada
        penolakan bukan bukti.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label htmlFor={selectId} className="text-xs">
          <span className="mb-1 block text-ink-3">Periode</span>
          <select id={selectId} className={`${inputBase} w-44`} value={period} onChange={(e) => setPeriod(e.target.value)}>
            {TIME_PERIODS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <Button busy={m.isPending} onClick={() => m.mutate()}>
          <ScrollText className="h-4 w-4" aria-hidden /> Observasi penegakan
        </Button>
      </div>
      <ErrorBox error={m.error} />
      <div aria-live="polite">
        {r && (
          <div className="space-y-1 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Outcome status={r.status} /> <span>{r.refusals.length} penolakan teramati pada <Mono>{`${r.repo}@${r.branch}`}</Mono></span>
            </div>
            {r.error && <p className="text-warn">{r.error}</p>}
            {r.evidence_id ? <p>Dicatat sebagai bukti <Mono>{r.evidence_id}</Mono>.</p> : <p className="text-ink-3">Tidak ada bukti yang dicatat.</p>}
          </div>
        )}
      </div>
    </div>
  );
}

/** Owner fallback: the owner's own statement of a refused bypass attempt, recorded as an untrusted HUMAN claim. */
function AttestEnforcement({ caps, onDone }: { caps: ProviderCaps; onDone: () => void }) {
  const [statement, setStatement] = useState("");
  const [confirming, setConfirming] = useState(false);
  const m = useMutation({
    mutationFn: () => evidenceApi.attestEnforcement({ repo: caps.repo, branch: caps.branch ?? undefined, statement: statement.trim() }),
    onSuccess: () => {
      setConfirming(false);
      setStatement("");
      onDone();
    },
  });
  const ok = statement.trim().length >= 10;
  return (
    <div className="space-y-2 rounded-md border border-line p-3">
      <div className="text-sm font-medium">Atestasi pemilik (cadangan)</div>
      <Notice tone="warn">
        Pernyataan Anda dicatat sebagai klaim manusia (E0, tidak tepercaya): status penegakan menjadi <strong>CLAIMED</strong>, tidak pernah VERIFIED.
      </Notice>
      <Field label="Pernyataan (min. 10 karakter)" hint="Mis. push langsung ke main dengan credential non-bypass ditolak dengan GH013 pada tanggal …">
        <textarea className={inputClass} rows={2} value={statement} onChange={(e) => setStatement(e.target.value)} maxLength={2000} />
      </Field>
      {!confirming ? (
        <Button disabled={!ok} onClick={() => setConfirming(true)}>
          <ShieldQuestion className="h-4 w-4" aria-hidden /> Tinjau atestasi
        </Button>
      ) : (
        <div role="group" aria-label="Konfirmasi atestasi" className="space-y-2 rounded-md bg-surface-2 p-2 text-xs">
          <p>
            Catat pernyataan Anda untuk <Mono>{`${caps.repo}@${caps.branch ?? "default"}`}</Mono> sebagai klaim pemilik?
          </p>
          <div className="flex gap-2">
            <Button variant="primary" busy={m.isPending} onClick={() => m.mutate()}>
              Konfirmasi &amp; catat klaim
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Batal
            </Button>
          </div>
        </div>
      )}
      <ErrorBox error={m.error} />
      <div aria-live="polite" className="text-xs">
        {m.data && (
          <span className="flex flex-wrap items-center gap-2">
            <Outcome status={m.data.enforcement_tested} /> {m.data.label} · bukti <Mono>{m.data.evidence_id}</Mono>
          </span>
        )}
      </div>
    </div>
  );
}

/** Admin: read-only GETs of the provider CI artifacts of one commit, recorded as provider-ci ARTIFACT_DIGEST evidence. */
export function ArtifactRecordForm({ projectId, repo, defaultSha = "", onDone }: { projectId: string; repo?: string; defaultSha?: string; onDone?: () => void }) {
  const [sha, setSha] = useState(defaultSha);
  const shaId = useId();
  const m = useMutation({
    mutationFn: () => evidenceApi.recordArtifacts({ project_id: projectId, sha: sha.trim(), ...(repo ? { repo } : {}) }),
    onSuccess: () => onDone?.(),
  });
  const valid = FULL_SHA.test(sha.trim());
  const r = m.data;
  return (
    <div className="space-y-2 rounded-md border border-line p-3">
      <div className="text-sm font-medium">Catat digest artefak CI (build sekali)</div>
      <p className="text-xs text-ink-3">
        GET artefak GitHub Actions commit ini; hanya run sukses pada commit persis ini yang menghasilkan digest yang layak dipromosikan. Tanda tangan
        attestation tidak diverifikasi AEOS.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label htmlFor={shaId} className="min-w-0 flex-1 text-xs">
          <span className="mb-1 block text-ink-3">Commit penuh (40 hex)</span>
          <input
            id={shaId}
            className={`${inputClass} mono`}
            value={sha}
            onChange={(e) => setSha(e.target.value)}
            aria-invalid={(!!sha.trim() && !valid) || undefined}
            placeholder="sha commit"
          />
        </label>
        <Button busy={m.isPending} disabled={!valid} onClick={() => m.mutate()}>
          <Fingerprint className="h-4 w-4" aria-hidden /> Catat digest
        </Button>
      </div>
      {!!sha.trim() && !valid && <p className="text-xs text-warn">Butuh sha commit penuh (40-64 karakter hex).</p>}
      <ErrorBox error={m.error} />
      <div aria-live="polite" className="text-xs">
        {r && (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Outcome status={r.status} /> <span>{r.eligible_digests.length} digest layak · commit <Mono>{shortHash(r.sha, 12)}</Mono></span>
            </div>
            {r.error && <p className="text-warn">{r.error}</p>}
            {r.eligible_digests.map((dg) => (
              <div key={dg}>
                <Mono>{dg}</Mono>
              </div>
            ))}
            {r.artifacts.some((a) => a.attestation?.status === "PRESENT_UNVERIFIED") && <p className="text-ink-3">{r.note}</p>}
            {r.evidence_id ? <p>Bukti <Mono>{r.evidence_id}</Mono></p> : <p className="text-ink-3">Tidak dicatat (pembacaan gagal tidak pernah menjadi bukti).</p>}
          </div>
        )}
      </div>
    </div>
  );
}

/** The admin actions of the PR & provider tab (GitHub only: Bitbucket has no rule history/artifact reader). */
export function DeliveryActions({ caps }: { caps: ProviderCaps }) {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  if (!isAdmin) return null;
  if ((caps.provider ?? "").toUpperCase() !== "GITHUB") {
    return (
      <p className="text-xs text-ink-3">
        Observasi penegakan, atestasi dan digest artefak hanya tersedia untuk GitHub (Bitbucket: tidak didukung di build ini).
      </p>
    );
  }
  const refresh = () => qc.invalidateQueries({ predicate: (x) => ["provider-caps", "evidence", "gates"].includes(String(x.queryKey[0])) });
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <ObserveEnforcement caps={caps} onDone={refresh} />
      <AttestEnforcement caps={caps} onDone={refresh} />
      {caps.project_id && (
        <div className="lg:col-span-2">
          <ArtifactRecordForm projectId={caps.project_id} repo={caps.repo} onDone={refresh} />
        </div>
      )}
    </div>
  );
}
