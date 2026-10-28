// R28: after `aeos serve` rebuilt the Control Center, a tab opened on the old index.html asks for chunks that no
// longer exist (the server answers 404 for a missing /assets file). Vite then fires `vite:preloadError`; reloading
// once fetches the new index.html (served with Cache-Control: no-cache). A marker with its time in sessionStorage
// stops a reload loop: a second failure within the window is left to surface as an error.

export const PRELOAD_RELOAD_KEY = "aeos.preload-reload-at";
export const PRELOAD_RELOAD_WINDOW_MS = 60_000;

type MarkerStore = Pick<Storage, "getItem" | "setItem">;

/** True when this failure may reload the page; records the reload time. No storage -> no reload (no loop guard). */
export function claimPreloadReload(store: MarkerStore | null, now: number): boolean {
  if (!store) return false;
  try {
    const last = Number(store.getItem(PRELOAD_RELOAD_KEY));
    if (Number.isFinite(last) && last > 0 && now - last < PRELOAD_RELOAD_WINDOW_MS) return false;
    store.setItem(PRELOAD_RELOAD_KEY, String(now));
    return true;
  } catch {
    return false;
  }
}

export interface PreloadTarget {
  addEventListener(type: "vite:preloadError", listener: (event: Event) => void): void;
  readonly location: { reload(): void };
  readonly sessionStorage: MarkerStore;
}

function sessionStore(target: PreloadTarget): MarkerStore | null {
  try {
    return target.sessionStorage;
  } catch {
    return null;
  }
}

/** Reload once when a lazily loaded chunk of an older build is gone. */
export function installPreloadRecovery(target: PreloadTarget = window, now: () => number = Date.now): void {
  target.addEventListener("vite:preloadError", (event) => {
    if (claimPreloadReload(sessionStore(target), now())) {
      event.preventDefault();
      target.location.reload();
    }
  });
}
