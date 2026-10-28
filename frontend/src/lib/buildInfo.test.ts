// @vitest-environment node
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { aeosBuildInfo } from "../../vite.config";

/**
 * R28: `aeos serve` builds into `dist.next` (`npm run build -- --outDir dist.next`) and swaps it in after success.
 * The build record must land in the configured outDir, never in `dist`, or the swapped bundle has no record and
 * the old `dist` gets a record that does not describe it.
 */
type Hook = (...args: unknown[]) => void;

function run(root: string, outDir: string): void {
  const plugin = aeosBuildInfo();
  (plugin.configResolved as Hook)({ root, build: { outDir } });
  (plugin.closeBundle as Hook).call({});
}

describe("aeos-build-info plugin", () => {
  let root = "";

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "aeos-build-info-"));
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("writes aeos-build.json into the configured outDir, not into dist", () => {
    mkdirSync(join(root, "dist.next"));
    mkdirSync(join(root, "dist"));
    run(root, "dist.next");
    const info = JSON.parse(readFileSync(join(root, "dist.next", "aeos-build.json"), "utf8"));
    expect(info.schema).toBe(1);
    expect(existsSync(join(root, "dist", "aeos-build.json"))).toBe(false);
  });

  it("keeps the default dist outDir", () => {
    mkdirSync(join(root, "dist"));
    run(root, "dist");
    expect(existsSync(join(root, "dist", "aeos-build.json"))).toBe(true);
  });
});
