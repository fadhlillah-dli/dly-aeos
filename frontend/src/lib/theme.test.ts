import { act, renderHook } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { applyTheme, readStoredTheme, resolveTheme, setTheme, THEME_STORAGE_KEY, useTheme } from "./theme";

afterEach(() => {
  vi.restoreAllMocks();
  try {
    window.localStorage.removeItem(THEME_STORAGE_KEY);
  } catch {
    /* blocked */
  }
  delete document.documentElement.dataset.theme;
});

describe("theme preference", () => {
  it("persists and applies an explicit choice", () => {
    setTheme("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(resolveTheme()).toBe("dark");
    setTheme(null);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("ignores garbage in storage", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "<script>");
    expect(readStoredTheme()).toBeNull();
  });

  it("still works when storage throws (private window / blocked site data)", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(readStoredTheme()).toBeNull();
    expect(() => setTheme("dark")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(() => applyTheme()).not.toThrow();
  });
});

describe("theme across tabs", () => {
  it("applies a choice made in another tab (storage event) to this document, not only to the toggle state", () => {
    const { result } = renderHook(() => useTheme());
    expect(document.documentElement.dataset.theme).toBeUndefined();
    act(() => {
      window.localStorage.setItem(THEME_STORAGE_KEY, "dark"); // written by the other tab
      window.dispatchEvent(new StorageEvent("storage", { key: THEME_STORAGE_KEY, newValue: "dark" }));
    });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(result.current.theme).toBe("dark");
    expect(result.current.choice).toBe("dark");
  });
});
