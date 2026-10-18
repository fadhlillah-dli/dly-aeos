import { useEffect, useId, useState } from "react";
import { useTheme } from "../../lib/theme";
import { Mono } from "../ui";

/**
 * Renders a Mermaid source lazily (mermaid is loaded only when a diagram is shown). Diagrams are design
 * artefacts to be verified against contracts (§29) — never runtime evidence. Rendering uses mermaid's
 * strict security level; on failure the source is shown instead.
 */
export function MermaidDiagram({ source, title }: { source: string; title?: string }) {
  const rawId = useId();
  const id = `mmd-${rawId.replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const { theme } = useTheme();

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setError(null);
    if (!source.trim()) {
      setError("Sumber diagram kosong");
      return;
    }
    import("mermaid")
      .then(async (m) => {
        const mermaid = m.default;
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: theme === "dark" ? "dark" : "neutral" });
        const out = await mermaid.render(id, source);
        if (!cancelled) setSvg(out.svg);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [source, id, theme]);

  return (
    <figure className="rounded-md border border-line bg-surface-2 p-3">
      {title && <figcaption className="mb-2 text-sm font-medium">{title}</figcaption>}
      {svg && !showSource ? (
        // mermaid output with securityLevel "strict" (sanitised SVG)
        <div className="overflow-x-auto [&_svg]:max-w-full" dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <pre className="mono overflow-x-auto whitespace-pre-wrap text-xs">{source}</pre>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-3">
        {error && <span className="text-warn">Tidak dapat dirender: {error}</span>}
        {svg && (
          <button type="button" className="underline" onClick={() => setShowSource((v) => !v)}>
            {showSource ? "Tampilkan diagram" : "Tampilkan sumber"}
          </button>
        )}
        <span>
          Status: <Mono>DESIGN</Mono> — perlu diverifikasi terhadap kontrak
        </span>
      </div>
    </figure>
  );
}
