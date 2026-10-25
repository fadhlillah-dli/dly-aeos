/** §29 evidence chart: recorded points only (each bound to its record id); UNKNOWN values are gaps, never 0. */
export interface ChartPoint {
  ref: string;
  at: string;
  value: number | null;
}

export interface EvidenceChartData {
  status: "EVIDENCE_BOUND" | "NO_DATA";
  title: string;
  unit: string;
  source: string;
  points: ChartPoint[];
  unknown_points: number;
  refs: string[];
  reason?: string;
}

export function EvidenceChart({ data, width = 260, height = 56 }: { data: EvidenceChartData; width?: number; height?: number }) {
  const measured = data.points.filter((p): p is ChartPoint & { value: number } => typeof p.value === "number");
  if (data.status !== "EVIDENCE_BOUND" || measured.length === 0) {
    return <p className="text-xs text-ink-3">{data.reason ?? "Belum ada nilai tercatat; tidak ada grafik yang dikarang."}</p>;
  }
  const values = measured.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = measured.length > 1 ? (width - 8) / (measured.length - 1) : 0;
  const coords = measured.map((p, i) => [4 + i * step, height - 4 - ((p.value - min) / span) * (height - 8)] as const);
  return (
    <figure className="space-y-1">
      <svg role="img" aria-label={`${data.title}: ${measured.length} nilai tercatat`} width={width} height={height} className="text-accent">
        <polyline fill="none" stroke="currentColor" strokeWidth={1.5} points={coords.map(([x, y]) => `${x},${y}`).join(" ")} />
        {coords.map(([x, y], i) => (
          <circle key={measured[i].ref} cx={x} cy={y} r={2} fill="currentColor">
            <title>{`${measured[i].at}: ${measured[i].value} ${data.unit} (record ${measured[i].ref})`}</title>
          </circle>
        ))}
      </svg>
      <figcaption className="text-xs text-ink-3">
        {measured.length} nilai tercatat (min {min}, maks {max} {data.unit}){data.unknown_points ? `, ${data.unknown_points} UNKNOWN (celah, bukan 0)` : ""} — sumber{" "}
        {data.source}; data tercatat, bukan verdict.
      </figcaption>
    </figure>
  );
}
