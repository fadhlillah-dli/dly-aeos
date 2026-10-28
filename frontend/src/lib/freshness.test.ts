import { bundleFrontendTree, freshnessNotices, type Health } from "./freshness";

const A = "a".repeat(40);
const B = "b".repeat(40);

function health(served: string | null, drift = "CURRENT", ui = "FRESH"): Health {
  return {
    status: "ok",
    version: "0.1.0",
    environment: "local",
    ui_build: { status: ui },
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
    expect(freshnessNotices(health(A, "CURRENT", "MISSING"), A)[0].text).toContain("tidak punya bundle tampilan (MISSING)");
    for (const s of ["UNVERIFIED", "REBUILT", "NOT_RECORDED"]) expect(freshnessNotices(health(A, "CURRENT", s), A)).toEqual([]);
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
