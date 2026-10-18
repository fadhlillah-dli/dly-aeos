import type { ReactNode } from "react";

// Minimal, safe Markdown preview for renderer output (headings, lists, tables, quotes, code). It builds
// React elements only — no HTML injection — and hides AEOS anchor comments. Not a general Markdown engine.

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|_[^_\s][^_]*_)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (tok.startsWith("**")) out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={k} className="mono rounded bg-surface-3 px-1">{tok.slice(1, -1)}</code>);
    else if (tok.startsWith("[")) {
      const [labelText, url] = tok.slice(1, -1).split("](");
      out.push(
        /^https?:\/\//i.test(url) ? (
          <a key={k} href={url} target="_blank" rel="noreferrer noopener" className="text-accent underline">
            {labelText}
          </a>
        ) : (
          <span key={k}>{labelText}</span>
        ),
      );
    } else out.push(<em key={k}>{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function MarkdownPreview({ markdown }: { markdown: string }) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const s = lines[i].trim();
    const k = `b${key++}`;
    if (!s || s.startsWith("<!--")) {
      i++;
      continue;
    }
    if (s.startsWith("```")) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i++;
      blocks.push(
        <pre key={k} className="mono overflow-x-auto rounded bg-surface-3 p-2 text-xs">
          {code.join("\n")}
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(s);
    if (h) {
      const level = h[1].length;
      const cls = level === 1 ? "text-lg font-semibold" : level === 2 ? "mt-3 text-base font-semibold" : "font-semibold";
      blocks.push(
        <div key={k} role="heading" aria-level={level} className={cls}>
          {inline(h[2], k)}
        </div>,
      );
      i++;
      continue;
    }
    if (s.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-{3,}:?$/.test(c))) rows.push(cells);
        i++;
      }
      blocks.push(
        <div key={k} className="overflow-x-auto">
          <table className="my-1 min-w-full border border-line text-xs">
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className={ri === 0 ? "bg-surface-2 font-medium" : ""}>
                  {r.map((c, ci) => (
                    <td key={ci} className="border border-line px-2 py-1 align-top">
                      {inline(c, `${k}-${ri}-${ci}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^[-*]\s+/.test(s) || /^\d+[.)]\s+/.test(s)) {
      const ordered = /^\d/.test(s);
      const items: string[] = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*]\s+/).test(lines[i])) {
        items.push(lines[i].trim().replace(ordered ? /^\d+[.)]\s+/ : /^[-*]\s+/, ""));
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={k} className={ordered ? "list-decimal pl-5" : "list-disc pl-5"}>
          {items.map((it, ii) => (
            <li key={ii}>{inline(it, `${k}-${ii}`)}</li>
          ))}
        </List>,
      );
      continue;
    }
    if (s.startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) quote.push(lines[i++].trim().replace(/^>\s?/, ""));
      blocks.push(
        <blockquote key={k} className="border-l-2 border-warn pl-3 text-ink-2">
          {inline(quote.filter(Boolean).join(" "), k)}
        </blockquote>,
      );
      continue;
    }
    const img = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(s);
    if (img) {
      blocks.push(
        <div key={k} className="rounded border border-dashed border-line p-2 text-xs text-ink-3">
          Gambar: {img[1]} (tersemat pada snapshot DOCX / attachment; tidak dimuat dari URL publik)
        </div>,
      );
      i++;
      continue;
    }
    blocks.push(
      <p key={k} className="my-1">
        {inline(s, k)}
      </p>,
    );
    i++;
  }
  return <div className="space-y-1 text-sm">{blocks}</div>;
}
