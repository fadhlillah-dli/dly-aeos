/**
 * §41 "Setiap halaman menyediakan 'Sumber data', 'Diperiksa pada', 'Batas bukti', 'Keputusan terkait',
 * 'Usulkan koreksi'". PageHeader renders all five labels from these props (render test:
 * components/PageHeader.test.tsx; decisions/relatedDecisions.test.tsx covers the 'Keputusan terkait' link and the
 * register filter; the browser smoke tests/e2e/test_ui_smoke.py checks the provenance block on every route).
 * This static test checks every page's main header passes them, and that 'Keputusan terkait' opens a filtered
 * register link (only register-wide pages link to the whole register). Limit: it does not check that the
 * target_type in that link is the type of the page's own records - any target_type passes.
 */
const sources = import.meta.glob("./**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

// Pages whose subject IS the whole decision history (the unfiltered register is the related set).
const REGISTER_WIDE = new Set(["./overview/index.tsx", "./history/index.tsx", "./decisions/index.tsx"]);

function mainHeaders(src: string): string[] {
  const out: string[] = [];
  const re = /<PageHeader\b([\s\S]*?)\n\s*\/>/g;
  for (let m = re.exec(src); m; m = re.exec(src)) {
    if (/\bsource=/.test(m[1])) out.push(m[1]);
  }
  return out;
}

const pages = Object.entries(sources).filter(([path, src]) => !path.includes(".test.") && src.includes("<PageHeader"));

describe("§41 page provenance labels", () => {
  it("finds the routed pages", () => {
    expect(pages.length).toBeGreaterThanOrEqual(25);
  });

  it.each(pages.map(([p]) => p))("%s states Sumber data, Diperiksa pada and Batas bukti", (path) => {
    const headers = mainHeaders(sources[path]);
    expect(headers.length, `${path}: no PageHeader with source=`).toBeGreaterThan(0);
    for (const h of headers) {
      expect(h, path).toMatch(/\bcheckedAt=/);
      expect(h, path).toMatch(/\bevidenceLimit=/);
      expect(h, `${path}: 'Usulkan koreksi' must not be hidden`).not.toMatch(/correctionHref=\{null\}/);
    }
  });

  it.each(pages.map(([p]) => p))("%s links 'Keputusan terkait' to its own decisions", (path) => {
    for (const h of mainHeaders(sources[path])) {
      if (REGISTER_WIDE.has(path)) {
        expect(h, path).not.toMatch(/relatedDecisionsHref=\{null\}/);
        continue;
      }
      expect(h, `${path}: relatedDecisionsHref missing (defaults to the unfiltered register)`).toMatch(
        /relatedDecisionsHref=(["{`])[^\n]*\/decisions\?target_type=/,
      );
    }
  });
});
