import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const appRoot = fileURLToPath(new URL("../../app/", import.meta.url));

function sourceFilesUnder(relativeDirectory: string): string[] {
  const directory = fileURLToPath(
    new URL(`../../app/${relativeDirectory}/`, import.meta.url),
  );
  const files: string[] = [];
  const visit = (current: string) => {
    for (const entry of readdirSync(current)) {
      const path = `${current}/${entry}`;
      if (statSync(path).isDirectory()) visit(path);
      else if (/\.(?:ts|tsx)$/.test(entry)) files.push(path);
    }
  };
  visit(directory);
  return files;
}

describe("server-only boundary", () => {
  it("keeps private client, fixture, and secret references out of browser components", () => {
    expect(appRoot).toContain("/website/app/");
    for (const file of [...sourceFilesUnder("components"), `${appRoot}/root.tsx`]) {
      const source = readFileSync(file, "utf8");
      expect(source).not.toMatch(
        /python\.server|fixture_server|app\/(?:server|services)\//,
      );
      expect(source).not.toMatch(/CLASHLENS_PYTHON_(?:API_URL|HMAC_SECRET|HMAC_KEY)/);
    }
  });

  it("does not define client loaders or client actions for private data", () => {
    for (const file of sourceFilesUnder("routes")) {
      const source = readFileSync(file, "utf8");
      expect(source).not.toMatch(/\bclientLoader\b|\bclientAction\b/);
    }
  });
});
