import { bundleFrontendTree, freshnessNotices, type Health } from "./freshness";

const A = "a".repeat(40);
const B = "b".repeat(40);

function health(served: string | null, drift = "CURRENT", ui = "FRESH", judged: string | null = served): Health {
  return {
    status: "ok",
    version: "0.1.0",
    environment: "local",
    ui_build: { status: ui, bundle_frontend_tree: judged },
    freshness: { served_bundle: { frontend_tree: served }, code_drift: { status: drift, loaded_commit: "c".repeat(40) } },
  };
}

describe("freshnessNotices", () => {
  it("is empty when everything is current or nothing is loaded yet", () => {
    expect(freshnessNotices(health(A), A)).toEqual([]);
    expect(freshnessNotices(undefined, A)).toEqual([]);
  });

  it("asks for a reload only when both trees are known and differ", () => {
    expect(freshnessNotices(health(B), A).map((n) => n.kind)).toEqual(["reload"]);
    expect(freshnessNotices(health(B), null)).toEqual([]); // dev server: no baked tree
    expect(freshnessNotices(health(null), A)).toEqual([]); // served bundle without a build record
  });

  it("asks for a restart on backend drift only (OUTSIDE_BACKEND, RELOAD, UNKNOWN stay quiet)", () => {
    expect(freshnessNotices(health(A, "DRIFT"), A)[0].text).toBe("Server menjalankan kode lama (cccccccccccc): restart ./aeos serve");
    for (const s of ["OUTSIDE_BACKEND", "RELOAD", "UNKNOWN", "NOT_RECORDED"]) expect(freshnessNotices(health(A, s), A)).toEqual([]);
  });

  it("reports ui_build STALE or MISSING, not UNVERIFIED or REBUILT", () => {
    // near miss: baked tree == served tree == the tree ui_build judged, and the verdict was STALE
    expect(freshnessNotices(health(A, "CURRENT", "STALE"), A).map((n) => n.kind)).toEqual(["ui_build"]);
    // MISSING at start and still nothing served: judged and served are both null
    expect(freshnessNotices(health(null, "CURRENT", "MISSING"), A)[0].text).toContain("tidak punya bundle tampilan (MISSING)");
    for (const s of ["UNVERIFIED", "REBUILT", "NOT_RECORDED"]) expect(freshnessNotices(health(A, "CURRENT", s), A)).toEqual([]);
  });

  it("keeps the ui_build notice out of the Vite dev server (no baked tree)", () => {
    expect(freshnessNotices(health(A, "CURRENT", "STALE"), null)).toEqual([]);
    expect(freshnessNotices(health(null, "CURRENT", "MISSING"), null)).toEqual([]);
  });

  it("drops the ui_build notice once the served bundle is not the one ui_build judged (manual rebuild)", () => {
    // started STALE on tree B, rebuilt by hand while serving: the server now serves A, the page runs A
    expect(freshnessNotices(health(A, "CURRENT", "STALE", B), A)).toEqual([]);
    // started MISSING, built by hand while serving: the server now serves A
    expect(freshnessNotices(health(A, "CURRENT", "MISSING", null), A)).toEqual([]);
    // the page is older than the rebuilt bundle: only the reload notice
    expect(freshnessNotices(health(A, "CURRENT", "STALE", B), B).map((n) => n.kind)).toEqual(["reload"]);
  });
});

describe("bundleFrontendTree", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is null without a baked tree and the tree when the build defined one", () => {
    expect(bundleFrontendTree()).toBeNull();
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", A);
    expect(bundleFrontendTree()).toBe(A);
    vi.stubGlobal("__AEOS_FRONTEND_TREE__", null);
    expect(bundleFrontendTree()).toBeNull();
  });
});
