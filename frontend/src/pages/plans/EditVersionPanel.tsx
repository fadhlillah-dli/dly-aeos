import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { post } from "../../api/client";
import type { PlanContent, VersionView } from "../../components/planning/types";
import { Button, Card, ErrorBox, Field, inputClass, Notice } from "../../components/ui";

/** §14: a human edit never mutates a reviewed version — it creates a new PlanVersion (HUMAN_EDIT). */
export function EditVersionPanel({ planId, base, onDone }: { planId: string; base: VersionView; onDone: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState(() => JSON.stringify(base.content, null, 2));
  const [rationale, setRationale] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (content: PlanContent) => post(`/api/plans/${planId}/versions`, { parent_version_id: base.id, content, rationale }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["plan", planId] });
      onDone();
    },
  });
  return (
    <Card title={`Edit sebagai versi baru (berbasis v${base.version})`}>
      <div className="grid gap-3">
        <Notice tone="warn">
          Menyimpan membuat versi baru dan membatalkan persetujuan versi lama. Isi divalidasi terhadap schema dan DAG (node tak dikenal, self-dependency, siklus
          ditolak).
        </Notice>
        <Field label="Konten rencana (JSON sesuai contracts/planning/plan-content.schema.json)">
          <textarea className={`${inputClass} mono min-h-96 text-xs`} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
        </Field>
        <Field label="Alasan perubahan">
          <input className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} />
        </Field>
        {parseError && <div className="text-sm text-bad">JSON tidak valid: {parseError}</div>}
        <ErrorBox error={save.error} />
        <div className="flex gap-2">
          <Button
            variant="primary"
            busy={save.isPending}
            disabled={rationale.trim().length < 5}
            onClick={() => {
              try {
                const content = JSON.parse(text) as PlanContent;
                setParseError(null);
                save.mutate(content);
              } catch (e) {
                setParseError(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            Simpan versi baru
          </Button>
          <Button onClick={onDone}>Batal</Button>
        </div>
      </div>
    </Card>
  );
}
