// R28: tell the owner when the page or the server is out of date. The server reports (GET /api/health, cached 30 s on
// the server) the build stamp of the bundle it serves now and whether it still runs the backend commit it loaded;
// the page knows the frontend tree it was built from (baked in by vite.config.ts aeosBuildInfo).

export interface UiBuild {
  status: string;
  reason?: string | null;
  remedy?: string | null;
  /** The frontend tree of the bundle ``aeos serve`` judged at start (null when there was none). */
  bundle_frontend_tree?: string | null;
}

export interface CodeDrift {
  status: string;
  loaded_commit?: string | null;
  head_commit?: string | null;
  changed_count?: number | null;
  reason?: string | null;
}

export interface Health {
  status: string;
  version: string;
  environment: string;
  ui_build?: UiBuild;
  freshness?: {
    served_bundle?: { frontend_tree?: string | null; built_at?: string | null };
    code_drift?: CodeDrift;
    checked_at?: string;
  };
}

export type NoticeKind = "reload" | "restart" | "ui_build";

export interface Notice {
  kind: NoticeKind;
  text: string;
  detail?: string;
}

/** The frontend tree baked into this bundle at build time; null in dev mode and tests (nothing to compare). */
export function bundleFrontendTree(): string | null {
  const tree = typeof __AEOS_FRONTEND_TREE__ === "undefined" ? null : __AEOS_FRONTEND_TREE__;
  return typeof tree === "string" && tree ? tree : null;
}

const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 12) : "?");

/** What the banner says; empty when page and server are current (or when nothing can be compared). */
export function freshnessNotices(health: Health | undefined, bundleTree: string | null): Notice[] {
  if (!health) return [];
  const out: Notice[] = [];
  const served = health.freshness?.served_bundle?.frontend_tree ?? null;
  if (bundleTree && served && bundleTree !== served) {
    out.push({ kind: "reload", text: "Tampilan lebih lama dari server: muat ulang halaman" });
  }
  const drift = health.freshness?.code_drift;
  if (drift?.status === "DRIFT") {
    out.push({
      kind: "restart",
      text: `Server menjalankan kode lama (${short(drift.loaded_commit)}): restart ./aeos serve`,
      detail: drift.reason ?? undefined,
    });
  }
  // ui_build was judged once, at server start. It describes this page only when the page is a baked bundle (not the
  // Vite dev server) and the server still serves the bundle that was judged (a manual rebuild while serving replaces
  // it, and the STALE / MISSING verdict is then about a bundle nobody is looking at).
  const ui = health.ui_build?.status;
  const judged = health.ui_build?.bundle_frontend_tree ?? null;
  if (bundleTree && served === judged && (ui === "STALE" || ui === "MISSING")) {
    const what = ui === "MISSING" ? "tidak punya bundle tampilan" : "menyajikan bundle tampilan usang";
    out.push({
      kind: "ui_build",
      text: `Server ${what} (${ui}): restart ./aeos serve atau build ulang frontend`,
      detail: [health.ui_build?.reason, health.ui_build?.remedy].filter(Boolean).join(" · ") || undefined,
    });
  }
  return out;
}
