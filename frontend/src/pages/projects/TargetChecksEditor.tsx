import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { put } from "../../api/client";
import { Button, ErrorBox, Field, inputClass, Notice } from "../../components/ui";
import { Outcome, ROLLBACK_LABELS } from "../evidence/outcomes";
import {
  CHECK_NAME,
  JSON_KEY,
  MAX_CHECKS,
  MAX_JSON_KEYS,
  ROLLBACK_CONDITIONS,
  targetPathOk,
  type TargetSettings,
  type TargetView,
} from "./deliveryTypes";

export interface SmokeRow {
  name: string;
  path: string;
  expect_status: string;
  json_keys: string;
  kind: string;
}

export interface SliRow {
  name: string;
  path: string;
  json_key: string;
  min: string;
  max: string;
}

export interface ChecksState {
  health_path: string;
  smoke: SmokeRow[];
  sli: SliRow[];
  rollback_on: string[];
  require_artifact_digest: boolean;
}

export function stateFrom(s: TargetSettings): ChecksState {
  return {
    health_path: s.health_path ?? "",
    smoke: (s.smoke_checks ?? []).map((c) => ({
      name: c.name ?? "",
      path: c.path ?? "",
      expect_status: String(c.expect_status ?? 200),
      json_keys: (c.json_keys ?? []).join(", "),
      kind: c.kind ?? "smoke",
    })),
    sli: (s.sli_checks ?? []).map((c) => ({
      name: c.name ?? "",
      path: c.path ?? "",
      json_key: c.json_key ?? "",
      min: c.min === null || c.min === undefined ? "" : String(c.min),
      max: c.max === null || c.max === undefined ? "" : String(c.max),
    })),
    rollback_on: [...(s.rollback_on ?? [])],
    require_artifact_digest: !!s.require_artifact_digest,
  };
}

const keyList = (text: string) =>
  text
    .split(/[\s,]+/)
    .map((k) => k.trim())
    .filter(Boolean);

function num(text: string): number | null | "invalid" {
  const t = text.trim();
  if (!t) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : "invalid";
}

/** The backend's default check name (deployment._default_name / the SLI json_key). */
const smokeDefaultName = (path: string) => path.replace(/^\/+|\/+$/g, "").replace(/\//g, "-").slice(0, 64) || "root";

/**
 * PUT /api/delivery/targets/{id}/checks body plus the problems that keep it from being sent. Every key is sent: an
 * empty health_path returns to the default /health, empty lists clear the checks and an empty rollback_on leaves the
 * rollback criteria UNDECIDED. Thresholds are owner values: an empty min/max is sent as null (NO_TARGET).
 */
export function checksBody(st: ChecksState): { body: Record<string, unknown>; problems: string[] } {
  const problems: string[] = [];
  const hp = st.health_path.trim();
  if (hp && !targetPathOk(hp)) problems.push("Path health harus path absolut seperti /health (tanpa host, query, '@' atau '..').");
  if (st.smoke.length > MAX_CHECKS) problems.push(`Maksimal ${MAX_CHECKS} smoke check.`);
  if (st.sli.length > MAX_CHECKS) problems.push(`Maksimal ${MAX_CHECKS} SLI.`);
  const smoke = st.smoke.map((r, i) => {
    const n = i + 1;
    const path = r.path.trim();
    const name = r.name.trim();
    const status = Number(r.expect_status.trim() || "200");
    const keys = keyList(r.json_keys);
    if (!targetPathOk(path)) problems.push(`Smoke check ${n}: path harus absolut, mis. /api/status.`);
    if (name && !CHECK_NAME.test(name)) problems.push(`Smoke check ${n}: nama hanya huruf, angka, spasi, . _ -.`);
    if (!Number.isInteger(status) || status < 100 || status > 599) problems.push(`Smoke check ${n}: status HTTP harus 100-599.`);
    if (keys.length > MAX_JSON_KEYS) problems.push(`Smoke check ${n}: maksimal ${MAX_JSON_KEYS} kunci JSON.`);
    if (keys.some((k) => !JSON_KEY.test(k))) problems.push(`Smoke check ${n}: kunci JSON tidak valid (boleh bertitik, mis. data.items).`);
    return { ...(name ? { name } : {}), path, expect_status: status, json_keys: keys, kind: r.kind === "critical_flow" ? "critical_flow" : "smoke" };
  });
  const sli = st.sli.map((r, i) => {
    const n = i + 1;
    const path = r.path.trim();
    const name = r.name.trim();
    const key = r.json_key.trim();
    const min = num(r.min);
    const max = num(r.max);
    if (!targetPathOk(path)) problems.push(`SLI ${n}: path harus absolut, mis. /metrics.`);
    if (name && !CHECK_NAME.test(name)) problems.push(`SLI ${n}: nama hanya huruf, angka, spasi, . _ -.`);
    if (!JSON_KEY.test(key)) problems.push(`SLI ${n}: kunci JSON angka wajib (boleh bertitik, mis. latency.p95_ms).`);
    if (min === "invalid" || max === "invalid") problems.push(`SLI ${n}: ambang harus angka atau kosong.`);
    if (typeof min === "number" && typeof max === "number" && min > max) problems.push(`SLI ${n}: min di atas max.`);
    return { ...(name ? { name } : {}), path, json_key: key, min: typeof min === "number" ? min : null, max: typeof max === "number" ? max : null };
  });
  const smokeNames = smoke.map((c) => c.name ?? smokeDefaultName(c.path));
  if (new Set(smokeNames).size !== smokeNames.length) problems.push("Nama smoke check harus unik.");
  const sliNames = sli.map((c) => c.name ?? c.json_key);
  if (new Set(sliNames).size !== sliNames.length) problems.push("Nama SLI harus unik.");
  return {
    body: {
      health_path: hp,
      smoke_checks: smoke,
      sli_checks: sli,
      rollback_on: ROLLBACK_CONDITIONS.filter((c) => st.rollback_on.includes(c)),
      require_artifact_digest: st.require_artifact_digest,
    },
    problems,
  };
}

const EMPTY_SMOKE: SmokeRow = { name: "", path: "", expect_status: "200", json_keys: "", kind: "smoke" };
const EMPTY_SLI: SliRow = { name: "", path: "", json_key: "", min: "", max: "" };

/** Owner configuration of one target's health path, GET checks, SLIs (owner thresholds), rollback criteria and digest rule. */
export function TargetChecksEditor({ target, onSaved }: { target: TargetView; onSaved?: () => void }) {
  const qc = useQueryClient();
  const [st, setSt] = useState<ChecksState>(() => stateFrom(target.settings));
  const { body, problems } = checksBody(st);
  const save = useMutation({
    mutationFn: () => put<TargetView>(`/api/delivery/targets/${encodeURIComponent(target.integration_id)}/checks`, body),
    onSuccess: (view) => {
      setSt(stateFrom(view.settings));
      qc.invalidateQueries({ queryKey: ["delivery-targets", target.project_id] });
      qc.invalidateQueries({ queryKey: ["integrations", target.project_id] });
      onSaved?.();
    },
  });
  const setSmoke = (i: number, patch: Partial<SmokeRow>) => setSt({ ...st, smoke: st.smoke.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const setSli = (i: number, patch: Partial<SliRow>) => setSt({ ...st, sli: st.sli.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const env = target.environment === "production" ? "produksi" : "staging";
  return (
    <form
      aria-label={`Pemeriksaan target ${env}`}
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!problems.length) save.mutate();
      }}
    >
      <Notice tone="info">
        Semua pemeriksaan hanya GET read-only ke URL dasar target. Mengubah pemeriksaan membuat observasi sebelumnya usang (STALE) untuk gate; observasi ulang
        sesudahnya.
      </Notice>
      <Field label="Path health/version" hint="Kosong = /health. Respons harus menyebut commit yang berjalan (dan boleh artifact_digest).">
        <input
          className={`${inputClass} mono`}
          value={st.health_path}
          onChange={(e) => setSt({ ...st, health_path: e.target.value })}
          placeholder="/health"
        />
      </Field>

      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">Smoke check & alur kritis (GET)</legend>
        {st.smoke.length === 0 && <p className="text-xs text-ink-3">Belum ada smoke check: observasi hanya membuktikan health.</p>}
        {st.smoke.map((r, i) => (
          <fieldset key={i} className="grid gap-2 rounded-md border border-line p-2 sm:grid-cols-2">
            <legend className="px-1 text-xs font-medium">Smoke check {i + 1}</legend>
            <Field label="Path">
              <input className={`${inputClass} mono`} value={r.path} onChange={(e) => setSmoke(i, { path: e.target.value })} placeholder="/api/status" />
            </Field>
            <Field label="Nama (opsional)">
              <input className={inputClass} value={r.name} onChange={(e) => setSmoke(i, { name: e.target.value })} />
            </Field>
            <Field label="Status HTTP diharapkan">
              <input className={inputClass} inputMode="numeric" value={r.expect_status} onChange={(e) => setSmoke(i, { expect_status: e.target.value })} />
            </Field>
            <Field label="Jenis">
              <select className={inputClass} value={r.kind} onChange={(e) => setSmoke(i, { kind: e.target.value })}>
                <option value="smoke">smoke</option>
                <option value="critical_flow">alur kritis (critical_flow)</option>
              </select>
            </Field>
            <div className="sm:col-span-2">
              <Field label="Kunci JSON wajib (opsional)" hint={`Pisahkan dengan koma; boleh bertitik (data.items); maks. ${MAX_JSON_KEYS}. Isi respons tidak pernah disimpan.`}>
                <input className={`${inputClass} mono`} value={r.json_keys} onChange={(e) => setSmoke(i, { json_keys: e.target.value })} />
              </Field>
            </div>
            <div className="sm:col-span-2">
              <Button type="button" variant="ghost" className="text-xs" onClick={() => setSt({ ...st, smoke: st.smoke.filter((_, j) => j !== i) })}>
                <Trash2 className="h-3.5 w-3.5" aria-hidden /> Hapus smoke check {i + 1}
              </Button>
            </div>
          </fieldset>
        ))}
        <Button type="button" className="text-xs" disabled={st.smoke.length >= MAX_CHECKS} onClick={() => setSt({ ...st, smoke: [...st.smoke, { ...EMPTY_SMOKE }] })}>
          <Plus className="h-3.5 w-3.5" aria-hidden /> Tambah smoke check
        </Button>
      </fieldset>

      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">SLI produk (angka JSON, ambang pemilik)</legend>
        <p className="text-xs text-ink-3">Ambang adalah nilai pemilik. Kosong = tanpa ambang: nilai hanya dilaporkan (NO_TARGET), tidak pernah “dalam target”.</p>
        {st.sli.map((r, i) => (
          <fieldset key={i} className="grid gap-2 rounded-md border border-line p-2 sm:grid-cols-2">
            <legend className="px-1 text-xs font-medium">SLI {i + 1}</legend>
            <Field label="Path">
              <input className={`${inputClass} mono`} value={r.path} onChange={(e) => setSli(i, { path: e.target.value })} placeholder="/metrics" />
            </Field>
            <Field label="Kunci JSON angka">
              <input className={`${inputClass} mono`} value={r.json_key} onChange={(e) => setSli(i, { json_key: e.target.value })} placeholder="latency.p95_ms" />
            </Field>
            <Field label="Nama (opsional)">
              <input className={inputClass} value={r.name} onChange={(e) => setSli(i, { name: e.target.value })} />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Min (pemilik)">
                <input className={inputClass} inputMode="decimal" value={r.min} onChange={(e) => setSli(i, { min: e.target.value })} placeholder="kosong" />
              </Field>
              <Field label="Max (pemilik)">
                <input className={inputClass} inputMode="decimal" value={r.max} onChange={(e) => setSli(i, { max: e.target.value })} placeholder="kosong" />
              </Field>
            </div>
            <div className="sm:col-span-2">
              <Button type="button" variant="ghost" className="text-xs" onClick={() => setSt({ ...st, sli: st.sli.filter((_, j) => j !== i) })}>
                <Trash2 className="h-3.5 w-3.5" aria-hidden /> Hapus SLI {i + 1}
              </Button>
            </div>
          </fieldset>
        ))}
        <Button type="button" className="text-xs" disabled={st.sli.length >= MAX_CHECKS} onClick={() => setSt({ ...st, sli: [...st.sli, { ...EMPTY_SLI }] })}>
          <Plus className="h-3.5 w-3.5" aria-hidden /> Tambah SLI
        </Button>
      </fieldset>

      <fieldset className="space-y-1">
        <legend className="text-sm font-semibold">Kriteria rollback pemilik (rollback_on)</legend>
        <p className="text-xs text-ink-3">Kondisi yang menurut Anda memerlukan rollback manual. AEOS tidak pernah melakukan rollback; ia hanya merekomendasikan.</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          {ROLLBACK_CONDITIONS.map((c) => (
            <label key={c} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={st.rollback_on.includes(c)}
                onChange={(e) => setSt({ ...st, rollback_on: e.target.checked ? [...st.rollback_on, c] : st.rollback_on.filter((x) => x !== c) })}
              />
              {ROLLBACK_LABELS[c]} <span className="mono text-xs text-ink-3">{c}</span>
            </label>
          ))}
        </div>
        {st.rollback_on.length === 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-warn">
            <Outcome status="UNDECIDED" /> tanpa kriteria, rekomendasi pasca-rilis tetap UNDECIDED.
          </div>
        )}
      </fieldset>

      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={st.require_artifact_digest}
          onChange={(e) => setSt({ ...st, require_artifact_digest: e.target.checked })}
        />
        <span>
          Wajibkan digest artefak (require_artifact_digest): observasi lulus hanya bila target melaporkan digest yang sama dengan digest CI commit itu.
        </span>
      </label>

      <ErrorBox error={save.error} />
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" busy={save.isPending} disabled={problems.length > 0}>
          Simpan pemeriksaan {env}
        </Button>
        <span aria-live="polite" className="text-xs">
          {problems.length > 0 ? (
            <span className="text-bad">{problems.join(" ")}</span>
          ) : save.isSuccess ? (
            <span className="text-ink-2">Tersimpan. Observasi sebelumnya untuk konfigurasi lama kini usang.</span>
          ) : (
            ""
          )}
        </span>
      </div>
    </form>
  );
}
