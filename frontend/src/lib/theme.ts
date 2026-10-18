import { useCallback, useEffect, useState } from "react";

// Light/dark theme (§35 readability). The choice is a per-viewer convenience kept in localStorage;
// every access is wrapped in try/catch because storage can be blocked (private window, policy) and the
// UI must still render. No choice stored = follow the operating system (prefers-color-scheme).

export type ThemeChoice = "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "aeos.theme";
const CHANGE_EVENT = "aeos:theme-change";

export function readStoredTheme(): ThemeChoice | null {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function writeStoredTheme(choice: ThemeChoice | null): void {
  try {
    if (choice) window.localStorage.setItem(THEME_STORAGE_KEY, choice);
    else window.localStorage.removeItem(THEME_STORAGE_KEY);
  } catch {
    // Storage unavailable: the theme still applies for this page view.
  }
}

function systemTheme(): ResolvedTheme {
  try {
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** Apply the stored choice (or none) to <html data-theme>. Called before the first render. */
export function applyTheme(choice: ThemeChoice | null = readStoredTheme()): void {
  const root = document.documentElement;
  if (choice) root.dataset.theme = choice;
  else delete root.dataset.theme;
}

export function resolveTheme(choice: ThemeChoice | null = readStoredTheme()): ResolvedTheme {
  return choice ?? systemTheme();
}

export function setTheme(choice: ThemeChoice | null): void {
  writeStoredTheme(choice);
  applyTheme(choice);
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
}

/** Current resolved theme; re-renders on toggle and (without a stored choice) on OS changes. */
export function useTheme(): { theme: ResolvedTheme; choice: ThemeChoice | null; toggle: () => void } {
  const [state, setState] = useState(() => ({ choice: readStoredTheme(), theme: resolveTheme() }));
  useEffect(() => {
    const update = () => setState({ choice: readStoredTheme(), theme: resolveTheme() });
    window.addEventListener(CHANGE_EVENT, update);
    window.addEventListener("storage", update);
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia?.("(prefers-color-scheme: dark)") ?? null;
      mq?.addEventListener?.("change", update);
    } catch {
      mq = null;
    }
    return () => {
      window.removeEventListener(CHANGE_EVENT, update);
      window.removeEventListener("storage", update);
      mq?.removeEventListener?.("change", update);
    };
  }, []);
  const toggle = useCallback(() => setTheme(state.theme === "dark" ? "light" : "dark"), [state.theme]);
  return { ...state, toggle };
}
