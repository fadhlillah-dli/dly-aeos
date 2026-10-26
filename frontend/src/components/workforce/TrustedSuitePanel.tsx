import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, Card, ErrorBox, Field, Loading, Mono, Notice, inputClass } from "../ui";

interface SuiteStatus {
  project_id: string;
  status: string; // REGISTERED | CONTROLLER | BLOCKED
  code?: string;
  reason?: string;
  kind?: string;
  suite_hash?: string;
  decision_id?: string | null;
  source_commit?: string | null;
  checked_at: string;
}

const COMMIT_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/;

/**
 * The project's trusted acceptance suite (ADR-0060, §21 "worker tidak mengubah grader"): the owner registers
 * ``tests/acceptance`` of a reviewed commit of the project's own repository, copied outside every candidate and bound
 * to a human POLICY decision on the suite hash. The platform project's suite is the controller checkout.
 */
export function TrustedSuitePanel({ projectId, isPlatform, isAdmin }: { projectId: string; isPlatform: boolean; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["trusted-suite", projectId], queryFn: () => get<SuiteStatus>(`/api/projects/${encodeURIComponent(projectId)}/trusted-suite`) });
  const [rationale, setRationale] = useState("");
  const [commit, setCommit] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const commitOk = !commit.trim() || COMMIT_RE.test(commit.trim());
  const st = q.data;
  return (
    <Card title={<span className="inline-flex items-center gap-2"><ShieldCheck className="h-4 w-4" aria-hidden />Suite acceptance tepercaya</span>}>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : st ? (
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              status={st.status === "REGISTERED" || st.status === "CONTROLLER" ? "CONFIGURED" : "BLOCKED"}
              label={st.status === "REGISTERED" ? "Terdaftar (keputusan pemilik)" : st.status === "CONTROLLER" ? "Checkout controller (proyek platform)" : "Belum terdaftar / tidak valid"}
            />
            {st.code && <Mono>{st.code}</Mono>}
          </div>
          {st.reason && <p className="text-xs text-ink-2">{st.reason}</p>}
          {st.suite_hash && (
            <p className="text-xs text-ink-3">
              Hash suite <Mono>{st.suite_hash.slice(0, 23)}</Mono>
              {st.source_commit ? (
                <>
                  {" "}
                  dari commit <Mono>{st.source_commit.slice(0, 12)}</Mono>
                </>
              ) : null}
              {st.decision_id ? (
                <>
                  {" "}
                  · keputusan <Mono>{st.decision_id}</Mono>
                </>
              ) : null}
            </p>
          )}
          <p className="text-xs text-ink-3">
            Terdaftar ≠ lulus: suite hanya dipakai verifier independen untuk menilai kandidat; worker tidak dapat mengubahnya. Hasil verifikasi
            tetap per run.
          </p>
          {isPlatform ? (
            <Notice tone="info">Suite proyek platform DLY AEOS adalah checkout controller tepercaya; tidak ada yang perlu didaftarkan.</Notice>
          ) : (
            <div className="space-y-2 rounded-md border border-line bg-surface-2 p-3">
              <Notice tone="info">
                Keputusan yang akan dicatat: <b>POLICY trusted_suite</b> oleh Anda (manusia), terikat ke hash isi <Mono>tests/acceptance</Mono> dari commit
                yang Anda tinjau pada repository lokal terdaftar proyek ini. Pendaftaran baru menggantikan yang lama.
              </Notice>
              <Field label="Commit atau branch yang ditinjau (opsional)" hint="Kosong = head branch default repository.">
                <input className={inputClass} value={commit} onChange={(e) => setCommit(e.target.value)} placeholder="sha atau nama branch" />
              </Field>
              {!commitOk && <p className="text-xs text-warn">Format commit/branch tidak valid.</p>}
              <Field label="Alasan (wajib, min 5 karakter)" hint="Mis. grader fixture sudah ditinjau.">
                <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
              </Field>
              <ErrorBox error={error} />
              <Button
                variant="primary"
                busy={busy}
                disabled={!isAdmin || rationale.trim().length < 5 || !commitOk}
                title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"}
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await post(`/api/projects/${encodeURIComponent(projectId)}/trusted-suite`, { rationale: rationale.trim(), commit: commit.trim() || null });
                    setRationale("");
                    qc.invalidateQueries({ queryKey: ["trusted-suite", projectId] });
                  } catch (e) {
                    setError(e);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Daftarkan suite tepercaya
              </Button>
              <p className="text-xs text-ink-3">
                CLI: <Mono>aeos verify suite-register &lt;project_id&gt; --as &lt;admin&gt; --domain &lt;domain&gt; --rationale &lt;teks&gt; [--commit &lt;sha&gt;]</Mono>
              </p>
            </div>
          )}
        </div>
      ) : null}
    </Card>
  );
}
