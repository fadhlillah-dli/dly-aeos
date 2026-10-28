/// <reference types="vitest/config" />
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * §09 zones: record which committed frontend source tree a build came from (`dist/aeos-build.json`), so a release
 * can prove its Control Center bundle corresponds to the release commit (git tree of `frontend/` at that commit).
 * The record goes into the configured outDir: `aeos serve` builds into `dist.next` (`--outDir dist.next`) and swaps
 * it in only after the build succeeded, so a failed build never leaves `dist` empty (R28).
 */
export function aeosBuildInfo(): Plugin {
  const git = (...args: string[]): string | null => {
    try {
      return execFileSync("git", args, { cwd: process.cwd(), encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    } catch {
      return null;
    }
  };
  let outDir = resolve(process.cwd(), "dist");
  return {
    name: "aeos-build-info",
    apply: "build",
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      const tree = git("rev-parse", "HEAD:./");
      const status = git("status", "--porcelain", "--untracked-files=normal", "--", ".");
      const info = {
        schema: 1,
        frontend_tree: tree,
        head: git("rev-parse", "HEAD"),
        clean: status === "",
        built_at: new Date().toISOString(),
        note: tree ? null : "not built inside a git checkout: correspondence to a commit cannot be shown",
      };
      writeFileSync(resolve(outDir, "aeos-build.json"), JSON.stringify(info, null, 2) + "\n", "utf8");
    },
  };
}

// Dev mode: Vite on 127.0.0.1:5173 proxies /api to the control plane on 127.0.0.1:8787.
// The backend allow-lists this origin only when started with `aeos serve --dev` (§06).
export default defineConfig({
  plugins: [react(), tailwindcss(), aeosBuildInfo()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: `http://127.0.0.1:${process.env.AEOS_PORT ?? "8787"}`, changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 4096,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
