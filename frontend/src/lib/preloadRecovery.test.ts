import { vi } from "vitest";
import {
  claimPreloadReload,
  installPreloadRecovery,
  PRELOAD_RELOAD_KEY,
  PRELOAD_RELOAD_WINDOW_MS,
  type PreloadTarget,
} from "./preloadRecovery";

function memoryStore(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, v),
  };
}

function fakeWindow(store: Storage) {
  const listeners: ((event: Event) => void)[] = [];
  const reload = vi.fn();
  const target: PreloadTarget = {
    addEventListener: (_type, listener) => void listeners.push(listener),
    location: { reload },
    sessionStorage: store,
  };
  const fire = () => {
    const event = new Event("vite:preloadError", { cancelable: true });
    listeners.forEach((l) => l(event));
    return event;
  };
  return { target, reload, fire };
}

describe("preload recovery", () => {
  it("reloads once on a missing chunk and records the time", () => {
    const store = memoryStore();
    const { target, reload, fire } = fakeWindow(store);
    installPreloadRecovery(target, () => 1_000_000);
    const event = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(store.getItem(PRELOAD_RELOAD_KEY)).toBe("1000000");
  });

  it("does not loop: a second failure inside the window surfaces as an error", () => {
    const store = memoryStore();
    let now = 1_000_000;
    const { target, reload, fire } = fakeWindow(store);
    installPreloadRecovery(target, () => now);
    fire();
    now += 5_000;
    const second = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(second.defaultPrevented).toBe(false);
    now += PRELOAD_RELOAD_WINDOW_MS;
    fire();
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("never reloads without storage for the loop guard", () => {
    expect(claimPreloadReload(null, 1)).toBe(false);
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => undefined };
    expect(claimPreloadReload(broken, 1)).toBe(false);
  });
});
