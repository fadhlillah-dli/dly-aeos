import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../../auth/AuthContext";
import { Card, KeyValue, Mono, Notice } from "../../components/ui";
import { shortHash } from "../../lib/format";
import type { GatesResponse, PromotionTarget } from "./api";
import { ArtifactRecordForm } from "./DeliveryActions";
import { Outcome } from "./outcomes";

const ENV_LABEL: Record<string, string> = { staging: "Staging", production: "Produksi" };

/**
 * §31 "artefak dibangun sekali ... diberi digest/provenance, lalu dipromosikan": PASS only when G5 and G7 pass, the
 * provider CI recorded a digest of the merge commit and every registered target reports exactly that digest.
 * Promotion itself stays a human action: nothing here deploys or promotes.
 */
export function PromotionCard({ gates }: { gates: GatesResponse }) {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const p = gates.promotion;
  if (!p) return null;
  const targets = Object.entries(p.targets ?? {}).filter(([, t]) => !!t) as [string, PromotionTarget][];
  const canRecord = isAdmin && !!gates.project_id && !!p.merge_commit && gates.domain !== "OFFICE";
  return (
    <Card title="Promosi artefak (build sekali, digest)">
      <div className="space-y-3" data-testid="promotion">
        <div className="flex flex-wrap items-center gap-2">
          <Outcome status={p.status} />
          <Outcome status={p.authority} label="Promosi: tindakan manusia (AEOS tidak pernah deploy/promosi)" />
        </div>
        {p.reasons.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-ink-2">
            {p.reasons.map((r) => (
              <li key={r} className="wrap-anywhere">
                {r}
              </li>
            ))}
          </ul>
        )}
        <KeyValue
          items={[
            ["Merge commit", p.merge_commit ? <Mono key="m">{shortHash(p.merge_commit, 12)}</Mono> : "belum di-merge"],
            [
              "Digest CI tercatat",
              p.recorded_digests.length ? (
                <span key="d" className="flex flex-col gap-0.5">
                  {p.recorded_digests.map((d) => (
                    <Mono key={d}>{d}</Mono>
                  ))}
                </span>
              ) : (
                <span key="d" className="text-warn">belum ada (provider-ci ARTIFACT_DIGEST)</span>
              ),
            ],
            ["Bukti digest", p.digest_evidence_id ? <Mono key="e">{p.digest_evidence_id}</Mono> : "—"],
          ]}
        />
        {targets.length === 0 ? (
          <p className="text-xs text-ink-3">Belum ada target deployment terdaftar yang melaporkan digest (Portofolio → proyek → Integrasi).</p>
        ) : (
          <ul className="space-y-1.5" aria-label="Digest per target">
            {targets.map(([env, t]) => (
              <li key={env} className="rounded-md border border-line p-2 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{ENV_LABEL[env] ?? env}</span>
                  <Outcome status={t.status} />
                </div>
                <div className="mt-1 text-ink-2">
                  Digest dilaporkan: {t.reported_digest ? <Mono>{t.reported_digest}</Mono> : "—"} · observasi{" "}
                  {t.observation_id ? <Mono>{shortHash(t.observation_id, 8)}</Mono> : "belum ada"}
                </div>
              </li>
            ))}
          </ul>
        )}
        {p.status === "PASS" && (
          <Notice tone="info">Syarat digest terpenuhi untuk keputusan manusia; promosi tetap dilakukan manusia di platform deploy proyek.</Notice>
        )}
        {canRecord && (
          <ArtifactRecordForm
            projectId={gates.project_id!}
            defaultSha={p.merge_commit ?? ""}
            onDone={() => qc.invalidateQueries({ predicate: (x) => ["gates", "evidence"].includes(String(x.queryKey[0])) })}
          />
        )}
      </div>
    </Card>
  );
}
