/**
 * §41 "Setiap halaman menyediakan 'Sumber data', 'Diperiksa pada', 'Batas bukti', 'Keputusan terkait',
 * 'Usulkan koreksi'". PageHeader renders all five labels from these props (render test:
 * components/PageHeader.test.tsx; decisions/relatedDecisions.test.tsx covers the 'Keputusan terkait' link and the
 * register filter; the browser smoke tests/e2e/test_ui_smoke.py checks the provenance block on every route).
 * This static test checks every page's main header passes them, and that 'Keputusan terkait' opens the register
 * filtered on the decision target_type of THIS page's own records (PAGE_DECISION_TARGETS, reviewed per page; only
 * register-wide pages link to the whole register). tests/unit/test_ui_decision_targets.py checks that each of those
 * target_types is one the backend records decisions/records under, so the filter is never an invented type.
 */
const sources = import.meta.glob("./**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

// Pages whose subject IS the whole decision history (the unfiltered register is the related set).
const REGISTER_WIDE = new Set(["./overview/index.tsx", "./history/index.tsx", "./decisions/index.tsx"]);

/** §41 W6-113: the decision target_type of each page's OWN records ("Keputusan terkait" = decisions about them).
 * A page pointing at another record type's decisions fails here; a new page must state its type. */
const PAGE_DECISION_TARGETS: Record<string, string> = {
  "./costs/index.tsx": "budget",
  "./dag/index.tsx": "plan_version",
  "./evaluation/index.tsx": "eval_workload_weights",
  "./evidence/index.tsx": "trusted_suite",
  "./integrations/index.tsx": "provider_write",
  "./models/index.tsx": "model_route",
  "./organization/index.tsx": "workforce",
  "./plans/PlanDetailPage.tsx": "plan_version",
  "./plans/index.tsx": "plan_version",
  "./projects/NewProjectPage.tsx": "project",
  "./projects/ProjectDetailPage.tsx": "project",
  "./projects/index.tsx": "project",
  "./proposals/ProposalDetailPage.tsx": "change_proposal",
  "./proposals/index.tsx": "change_proposal",
  "./publications/index.tsx": "publication",
  "./readiness/index.tsx": "secret_exposure",
  "./research/index.tsx": "change_proposal",
  "./settings/index.tsx": "office_policy",
  "./system-map/index.tsx": "architecture_snapshot",
  "./tools/ComponentDetailPage.tsx": "component",
  "./tools/index.tsx": "component",
  "./work/RunDetailPage.tsx": "workforce",
  "./work/index.tsx": "workforce",
};

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
      // W6-113: the filter names the type of this page's own records, not any target_type
      const used = /relatedDecisionsHref=[^\n]*\/decisions\?target_type=([A-Za-z0-9_.-]+)/.exec(h)?.[1];
      expect(PAGE_DECISION_TARGETS[path], `${path}: state the decision target_type of this page's records`).toBeDefined();
      expect(used, `${path}: 'Keputusan terkait' must filter on its own records`).toBe(PAGE_DECISION_TARGETS[path]);
    }
  });

  it("names no page that does not exist (the map is kept in step with the pages)", () => {
    const routed = new Set(pages.map(([p]) => p));
    expect(Object.keys(PAGE_DECISION_TARGETS).filter((p) => !routed.has(p))).toEqual([]);
  });
});
