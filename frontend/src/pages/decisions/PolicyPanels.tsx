import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import type { AdrDetail, AdrSummary } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, inputClass, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { relativeTime, shortHash } from "../../lib/format";

export interface PackMapping {
  key: string;
  mci_controls: string[];
  mapping_status: string;
  evidence_contract_status: string;
  mapping_source: string;
  note: string;
  decision_id: string | null;
  binding: { kind: string; target_type: string; target_id: string; target_hash: string };
}

export interface MciView {
  name: string;
  declared_status: string;
  status: string;
  verification_code: string;
  compliance: string;
  version: string | null;
  content_hash: string | null;
  source_location: string | null;
  source_placement: string;
  local_directory: string;
  note: string;
  verification_note: string;
  packs: string[];
  pack_mappings: PackMapping[];
  pack_mapping_file: string;
  pack_mapping_state: string;
  packs_missing: string[];
  packs_mapping_status: string;
  risk_classes: { key: string; label: string; threshold_status: string }[];
  checked_at: string;
}

interface SnapshotView {
  active: {
    id: string;
    version: string;
    content_hash: string;
    created_at: string | null;
    source: string;
    files: Record<string, { path: string; sha256: string; status: string }>;
    decision_count: number;
  } | null;
  status: string;
  current_content_hash: string;
  drift: boolean;
  remedy: string | null;
  mci_status: string;
  checked_at: string;
}

/** §02/§34: MCI status as verified on THIS machine (the document is local-only; the public repository holds the
 * reference), compliance never claimed, and every §34 pack slot with its mapping status. */
export function MciBanner() {
  const q = useQuery({ queryKey: ["policy", "mci"], queryFn: () => get<MciView>("/api/policy/mci") });
  if (!q.data) return null;
  const m = q.data;
  const packs = m.pack_mappings ?? [];
  const approved = packs.filter((p) => p.mapping_status === "APPROVED").length;
  return (
    <Notice tone="warn">
      <div className="flex flex-wrap items-center gap-2">
        <strong>{m.name}</strong> <StatusBadge status={m.status} /> · kepatuhan <StatusBadge status={m.compliance} />
        {m.verification_code && <Mono>{m.verification_code}</Mono>}
      </div>
      <div className="mt-1">{m.verification_note}</div>
      {(m.version || m.content_hash) && (
        <div className="mt-1 text-xs">
          Versi {m.version ?? "—"} · hash <Mono>{shortHash(m.content_hash ?? "", 14)}</Mono>
        </div>
      )}
      <div className="mt-1 text-xs" data-testid="mci-local-only">
        Dokumen MCI hanya disimpan lokal di mesin ini ({m.local_directory}
        {m.source_location ? `, lokasi ${m.source_location}` : ""}); repositori publik hanya memuat referensi (status, versi, hash).
      </div>
      <div className="mt-1 text-xs">
        Kelas risiko {m.risk_classes.map((r) => `${r.key} (${r.label})`).join(", ")} — ambang {m.risk_classes[0]?.threshold_status ?? "UNVERIFIED"}; tidak ada
        ambang yang dikarang.
      </div>
      {packs.length > 0 && (
        <details className="mt-2 text-xs" data-testid="mci-packs">
          <summary className="cursor-pointer">
            Pemetaan {packs.length} pack §34 ke kontrol MCI: <StatusBadge status={m.packs_mapping_status} /> ({approved}/{packs.length} disetujui pemilik)
          </summary>
          <p className="mt-1 text-ink-3">
            Kontrol MCI dan evidence contract per pack diturunkan dari MCI_V2, jadi hanya ada di file lokal <Mono>{m.pack_mapping_file}</Mono> (
            {m.pack_mapping_state === "PRESENT" ? "ada" : m.pack_mapping_state === "INVALID" ? "tidak valid" : "belum ada"}). Disetujui hanya bila MCI VERIFIED dan ada
            keputusan POLICY pemilik pada <Mono>mci_pack_mapping/&lt;pack&gt;</Mono> yang terikat hash di bawah.
          </p>
          {m.packs_missing.length > 0 && <p className="text-bad">Pack master yang tidak ada: {m.packs_missing.join(", ")}</p>}
          <table className="mt-1 w-full text-left">
            <thead>
              <tr>
                <th className="pr-2">Pack</th>
                <th className="pr-2">Pemetaan</th>
                <th className="pr-2">Kontrol</th>
                <th className="pr-2">Evidence contract</th>
                <th className="pr-2">Hash pengikat</th>
              </tr>
            </thead>
            <tbody>
              {packs.map((p) => (
                <tr key={p.key} title={p.note}>
                  <td className="pr-2">
                    <Mono>{p.key}</Mono>
                  </td>
                  <td className="pr-2">
                    <StatusBadge status={p.mapping_status} />
                  </td>
                  <td className="pr-2">{p.mci_controls.length || "—"}</td>
                  <td className="pr-2">
                    <StatusBadge status={p.evidence_contract_status === "DECLARED" ? "CONFIGURED" : "NOT_CONFIGURED"} label={p.evidence_contract_status} />
                  </td>
                  <td className="pr-2">
                    <Mono>{shortHash(p.binding.target_hash, 12)}</Mono>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Notice>
  );
}

export function SnapshotCard({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const { domain } = useAuth();
  // The bound-decision list is scoped to the active domain by the backend, so the cache key is too.
  const q = useQuery({ queryKey: ["policy", "snapshot", domain], queryFn: () => get<SnapshotView>("/api/policy/snapshot") });
  const refresh = useMutation({
    mutationFn: () => post("/api/policy/snapshot/refresh"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["policy"] }),
  });
  const s = q.data;
  return (
    <Card
      title="Policy snapshot aktif"
      actions={
        isAdmin && s && (s.drift || !s.active) ? (
          <Button busy={refresh.isPending} onClick={() => refresh.mutate()}>
            Aktifkan snapshot terbaru
          </Button>
        ) : undefined
      }
    >
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? refresh.error} />
      {s && !s.active && (
        <EmptyState title="Belum ada policy snapshot aktif">
          Jalankan <Mono>{s.remedy ?? "aeos seed"}</Mono>
        </EmptyState>
      )}
      {s?.active && (
        <div className="grid gap-2">
          <KeyValue
            items={[
              ["Hash", <Mono key="h">{s.active.content_hash}</Mono>],
              ["Versi", `v${s.active.version} · ${relativeTime(s.active.created_at)} · ${s.active.source}`],
              ["Keputusan terikat (domain aktif + global)", String(s.active.decision_count)],
              ["MCI", <StatusBadge key="m" status={s.mci_status} />],
              ["Drift", s.drift ? <StatusBadge key="d" status="DRIFTED" label="Berubah sejak snapshot" /> : <StatusBadge key="d" status="CONFIGURED" label="Sesuai" />],
            ]}
          />
          <ul className="text-xs text-ink-3">
            {Object.entries(s.active.files).map(([name, f]) => (
              <li key={name}>
                {f.path} <Mono>{shortHash(f.sha256)}</Mono> [{f.status}]
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

interface WriteEnablement {
  items: { provider: string; flag: boolean; decision_id: string | null; decision_expires_at: string | null; effective: boolean }[];
  domain: string;
}

export function WriteEnablementCard() {
  const q = useQuery({ queryKey: ["policy", "write-enablement"], queryFn: () => get<WriteEnablement>("/api/policy/write-enablement") });
  return (
    <Card title="Izin tulis eksternal per provider">
      <ErrorBox error={q.error} />
      {q.data && (
        <Table
          rows={q.data.items}
          rowKey={(i) => i.provider}
          columns={[
            { header: "Provider", cell: (i) => <Mono>{i.provider}</Mono> },
            { header: "Flag", cell: (i) => (i.flag ? "aktif" : "nonaktif") },
            { header: "Keputusan manusia", cell: (i) => (i.decision_id ? <Mono>{i.decision_id.slice(0, 8)}</Mono> : "—") },
            { header: "Efektif", cell: (i) => (i.effective ? <StatusBadge status="ENABLED" label="Diizinkan (tetap lewat interlock)" /> : <StatusBadge status="DISABLED" />) },
          ]}
        />
      )}
      <p className="mt-2 text-xs text-ink-3">
        Izin tulis hanya salah satu syarat interlock: bootstrap, AUTH aktor, domain/provider/target, versi + hash yang disetujui, dan capability yang belum
        kedaluwarsa tetap diperiksa setiap penulisan.
      </p>
    </Card>
  );
}

interface OfficePolicy {
  external_worker_allowed: boolean;
  approved_worker_routes: string[];
  claude_max_allowed_for_office_data: boolean;
  atlassian_deployment: string;
  office_authorization: boolean;
  authorized_providers: string[];
  approved_targets?: Record<string, string>[];
  approved_data_classes?: string[];
  approved_identities?: Record<string, string>;
  approved_target_count?: number;
  approved_identity_count?: number;
  decision_refs_hidden?: boolean;
  source: string;
}

function targetText(t: Record<string, string>): string {
  return `${t.provider}(${Object.entries(t)
    .filter(([k]) => k !== "provider")
    .map(([k, v]) => `${k}=${v}`)
    .join(", ")})`;
}

/** UNDECIDED is never shown as allowed (§34): an empty company-approved list keeps every consumer closed. */
function undecided(key: string, label = "Belum diputuskan (semua ditolak)") {
  return <StatusBadge key={key} status="UNKNOWN" label={label} />;
}

export function OfficePolicyCard() {
  const { domain, isAdmin } = useAuth();
  const q = useQuery({ queryKey: ["policy", "office", domain], queryFn: () => get<OfficePolicy>("/api/policy/office") });
  const p = q.data;
  const targets = p?.approved_targets ?? [];
  const classes = p?.approved_data_classes ?? [];
  const identities = Object.entries(p?.approved_identities ?? {});
  return (
    <Card title="Kebijakan OFFICE (default deny)">
      <ErrorBox error={q.error} />
      {p && (
        <KeyValue
          items={[
            ["Otorisasi perusahaan", p.office_authorization ? <StatusBadge key="a" status="CONFIGURED" label="Tercatat" /> : <StatusBadge key="a" status="MISSING" label="Belum ada" />],
            ["Worker eksternal", p.external_worker_allowed ? "diizinkan" : <StatusBadge key="w" status="DENIED" label="Ditolak" />],
            ["Route worker disetujui", p.approved_worker_routes.join(", ") || "—"],
            ["Claude Max untuk data kantor", p.claude_max_allowed_for_office_data ? "diizinkan" : <StatusBadge key="c" status="DENIED" label="Belum disetujui" />],
            ["Provider diotorisasi", p.authorized_providers.join(", ") || "—"],
            [
              "Target disetujui perusahaan",
              p.decision_refs_hidden
                ? `${p.approved_target_count ?? 0} target (hanya terlihat di sesi OFFICE)`
                : targets.length
                  ? targets.map((t) => <Mono key={targetText(t)}>{targetText(t)}</Mono>)
                  : undecided("t"),
            ],
            ["Kelas data disetujui untuk AI", classes.length ? classes.join(", ") : undecided("k", "Belum diputuskan (data OFFICE tidak dikirim ke model)")],
            [
              "Identitas credential disetujui",
              p.decision_refs_hidden
                ? `${p.approved_identity_count ?? 0} identitas (hanya terlihat di sesi OFFICE)`
                : identities.length
                  ? identities.map(([ref, v]) => <Mono key={ref}>{`${ref}=${v}`}</Mono>)
                  : undecided("i", "Belum dicatat (opsional; tidak dibuktikan)"),
            ],
            ["Atlassian", <StatusBadge key="d" status={p.atlassian_deployment === "UNKNOWN" ? "UNKNOWN" : "CONFIGURED"} label={p.atlassian_deployment} />],
            ["Sumber", p.source],
          ]}
        />
      )}
      {p && isAdmin && domain === "OFFICE" && <OfficeRouteForm approvedRoutes={p.approved_worker_routes} authorised={p.office_authorization} />}
    </Card>
  );
}

/**
 * §23 controlled path (W6-185): make a company-approved worker route OFFICE-eligible. Only in an OFFICE session, only
 * for a route in approved_worker_routes and with an office/ credential reference; the backend re-checks everything and
 * records the human decision. Activation stays a separate, hash-bound decision (Model & route).
 */
export function OfficeRouteForm({ approvedRoutes, authorised }: { approvedRoutes: string[]; authorised: boolean }) {
  const qc = useQueryClient();
  const [routeKey, setRouteKey] = useState(approvedRoutes[0] ?? "");
  const [credentialRef, setCredentialRef] = useState("office/");
  const [rationale, setRationale] = useState("");
  const save = useMutation({
    mutationFn: () => post("/api/policy/office/routes", { route_key: routeKey.trim(), credential_ref: credentialRef.trim(), rationale: rationale.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["policy"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["routes"] });
      setRationale("");
    },
  });
  const refOk = /^office\/[A-Za-z0-9._/-]{1,190}$/.test(credentialRef.trim()) && !credentialRef.includes("..");
  return (
    <form
      aria-label="Kelayakan route OFFICE"
      className="mt-3 grid gap-2 rounded-md border border-line p-3"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="text-sm font-medium">Jadikan route worker layak OFFICE</div>
      <Notice tone="info">
        Hanya route dalam approved_worker_routes otorisasi perusahaan, dengan credential <Mono>office/…</Mono>. Route menjadi khusus OFFICE; aktivasi tetap
        keputusan terpisah yang terikat hash (halaman Model &amp; route). CLI: <Mono>aeos policy office-route &lt;route_key&gt; --as &lt;admin&gt; --credential-ref office/&lt;provider&gt;</Mono>
      </Notice>
      {!authorised && <StatusBadge status="BLOCKED" label="Belum ada otorisasi perusahaan: permintaan akan ditolak" />}
      <label className="grid gap-1 text-sm">
        <span>Route key</span>
        {approvedRoutes.length ? (
          <select aria-label="Route key" className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={routeKey} onChange={(e) => setRouteKey(e.target.value)}>
            {approvedRoutes.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        ) : (
          <input aria-label="Route key" className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={routeKey} onChange={(e) => setRouteKey(e.target.value)} />
        )}
      </label>
      <label className="grid gap-1 text-sm">
        <span>Referensi credential OFFICE</span>
        <input aria-label="Referensi credential OFFICE" className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={credentialRef} onChange={(e) => setCredentialRef(e.target.value)} placeholder="office/openrouter" />
      </label>
      <label className="grid gap-1 text-sm">
        <span>Rationale (min. 10 karakter)</span>
        <textarea aria-label="Rationale route OFFICE" className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={rationale} onChange={(e) => setRationale(e.target.value)} />
      </label>
      <ErrorBox error={save.error} />
      {save.isSuccess && <StatusBadge status="CONFIGURED" label="Keputusan tercatat: route khusus OFFICE (belum aktif)" />}
      <div>
        <Button type="submit" variant="primary" busy={save.isPending} disabled={!routeKey.trim() || !refOk || rationale.trim().length < 10}>
          Catat kelayakan OFFICE
        </Button>
        {!refOk && <span className="ml-2 text-xs text-warn">Referensi harus office/&lt;provider&gt;</span>}
      </div>
    </form>
  );
}

const ADR_OUTCOMES = ["ACCEPTED", "REJECTED", "SUPERSEDED"] as const;
type AdrOutcome = (typeof ADR_OUTCOMES)[number];

function fileBadge(status: string) {
  return status === "ACCEPTED" ? "ACCEPTED" : status === "SUPERSEDED" ? "STALE" : status === "REJECTED" ? "REJECTED" : "PENDING";
}

function OwnerStatus({ a }: { a: AdrSummary }) {
  if (a.owner_status_state === "RECORDED" && a.owner_status) {
    return <StatusBadge status={a.owner_status} label={`Pemilik: ${a.owner_status}`} />;
  }
  if (a.owner_status_state === "STALE" && a.owner_status && a.owner_status !== "ACCEPTED") {
    // an owner rejection/supersession stays in force after a file edit: only the owner can lift it
    return <StatusBadge status={a.owner_status} label={`Pemilik: ${a.owner_status} (tetap berlaku; file berubah sesudahnya, tinjau ulang)`} />;
  }
  if (a.owner_status_state === "STALE") {
    return <StatusBadge status="STALE" label={`Keputusan pemilik usang (${a.owner_status ?? "?"}, file berubah)`} />;
  }
  return <StatusBadge status="NOT_RECORDED" label="Pemilik belum memutuskan" />;
}

/** §02 change control: the owner's own ADR decision, bound to the content hash shown (live SUPER_ADMIN session). */
export function AdrDecisionForm({ adr, keys, onDone }: { adr: AdrSummary; keys: string[]; onDone?: () => void }) {
  const qc = useQueryClient();
  const [outcome, setOutcome] = useState<AdrOutcome>("ACCEPTED");
  const [rationale, setRationale] = useState("");
  const [successor, setSuccessor] = useState("");
  const save = useMutation({
    mutationFn: () =>
      post(`/api/adrs/${adr.key}/decision`, {
        outcome,
        rationale: rationale.trim(),
        content_hash: adr.content_hash,
        superseded_by: outcome === "SUPERSEDED" ? successor : undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["adrs"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      setRationale("");
      onDone?.();
    },
  });
  const ready = rationale.trim().length >= 10 && !!adr.content_hash && (outcome !== "SUPERSEDED" || !!successor);
  return (
    <div className="grid gap-2 rounded-md border border-line p-2" data-testid={`adr-decide-${adr.key}`}>
      <div className="text-xs text-ink-2">
        Keputusan pemilik atas <Mono>{adr.key}</Mono> terikat hash isi <Mono>{shortHash(adr.content_hash ?? "", 14)}</Mono>; bila file diubah kemudian, keputusan ini
        menjadi usang (STALE).
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <Field label="Keputusan ADR">
          <select aria-label={`Keputusan ${adr.key}`} className={inputClass} value={outcome} onChange={(e) => setOutcome(e.target.value as AdrOutcome)}>
            <option value="ACCEPTED">ACCEPTED — diterima</option>
            <option value="REJECTED">REJECTED — ditolak</option>
            <option value="SUPERSEDED">SUPERSEDED — digantikan ADR lain</option>
          </select>
        </Field>
        {outcome === "SUPERSEDED" && (
          <Field label="Digantikan oleh">
            <select aria-label={`Pengganti ${adr.key}`} className={inputClass} value={successor} onChange={(e) => setSuccessor(e.target.value)}>
              <option value="">Pilih ADR</option>
              {keys
                .filter((k) => k !== adr.key)
                .map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
            </select>
          </Field>
        )}
      </div>
      <Field label="Alasan (wajib, min. 10 karakter)">
        <textarea aria-label={`Alasan ${adr.key}`} className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} />
      </Field>
      <ErrorBox error={save.error} />
      <div>
        <Button variant="primary" busy={save.isPending} disabled={!ready} onClick={() => save.mutate()}>
          Catat keputusan ADR
        </Button>
      </div>
    </div>
  );
}

export function AdrList() {
  const { isAdmin } = useAuth();
  const q = useQuery({
    queryKey: ["adrs"],
    queryFn: () => get<{ items: AdrSummary[]; remedy: string | null; owner_status_counts?: Record<string, number> }>("/api/adrs"),
  });
  const [open, setOpen] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["adrs", open], queryFn: () => get<AdrDetail>(`/api/adrs/${open}`), enabled: !!open });
  const keys = (q.data?.items ?? []).map((a) => a.key);
  const counts = q.data?.owner_status_counts ?? {};
  return (
    <Card title="Register ADR">
      <ErrorBox error={q.error} />
      {q.data && !q.data.items.length && (
        <EmptyState title="ADR belum dimuat">
          Jalankan <Mono>{q.data.remedy ?? "aeos policy adrs --load"}</Mono>
        </EmptyState>
      )}
      {q.data && q.data.items.length > 0 && (
        <>
          <p className="mb-2 text-xs text-ink-3">
            Status file ditulis oleh penulis file (saat seed build: builder), bukan penerimaan pemilik. Keputusan pemilik tercatat: {counts.RECORDED ?? 0}, usang:{" "}
            {counts.STALE ?? 0}, belum: {counts.NOT_RECORDED ?? 0}.
          </p>
          <ul className="grid gap-2">
            {q.data.items.map((a) => (
              <li key={a.key} className="rounded border border-line p-2">
                <button type="button" className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === a.key ? null : a.key)} aria-expanded={open === a.key}>
                  <Mono>{a.key}</Mono>
                  <span className="font-medium">{a.title}</span>
                  <StatusBadge status={fileBadge(a.file_status ?? a.status)} label={`File: ${a.file_status ?? a.status}`} />
                  <OwnerStatus a={a} />
                  <span className="text-xs text-ink-3">
                    {a.source_path} · <Mono>{shortHash(a.content_hash)}</Mono>
                  </span>
                </button>
                {open === a.key && detail.data && (
                  <div className="mt-2 grid gap-2 text-sm">
                    {Object.entries(detail.data.sections).map(([h, body]) => (
                      <div key={h}>
                        <div className="font-medium">{h}</div>
                        <p className="whitespace-pre-wrap text-ink-2">{body}</p>
                      </div>
                    ))}
                    {detail.data.linked_decisions.length > 0 && (
                      <div className="text-xs">Keputusan terkait: {detail.data.linked_decisions.map((d) => `${d.kind} ${d.status} (${d.id.slice(0, 8)})`).join(", ")}</div>
                    )}
                  </div>
                )}
                {open === a.key && isAdmin && <AdrDecisionForm adr={a} keys={keys} />}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
