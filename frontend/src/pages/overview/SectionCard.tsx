import type { ReactNode } from "react";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Card } from "../../components/ui";
import type { Section } from "./types";

export function SectionCard({
  title,
  section,
  actions,
  children,
}: {
  title: string;
  section: Section<unknown>;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {title} <OpsBadge status={section.status} />
        </span>
      }
      actions={actions}
    >
      {section.reason ? <p className="mb-2 text-sm text-ink-2">{section.reason}</p> : null}
      {children}
      <Provenance source={section.source} checkedAt={section.checked_at} evidenceLimit={section.evidence_limit} />
    </Card>
  );
}
