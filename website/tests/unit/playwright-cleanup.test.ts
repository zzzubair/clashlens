import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { expect, it, vi } from "vitest";

const require = createRequire(import.meta.url);
const playwright = require.resolve("@playwright/test");
const cli = join(dirname(require.resolve("@playwright/test/package.json")), "cli.js");

it.each(["failure", "cancellation"])(
  "lets the development server clean up after browser-test %s",
  async (outcome) => {
    vi.stubEnv("CLASHLENS_E2E_EXTERNAL_STACK", "0");
    vi.resetModules();
    const { default: config } = await import("../../playwright.config");
    vi.unstubAllEnvs();
    const webServer = Array.isArray(config.webServer) ? config.webServer[0] : undefined;
    expect(webServer?.command).toBe("../dev e2e-server");
    expect(webServer?.gracefulShutdown?.signal).toBe("SIGTERM");

    const directory = mkdtempSync(join(tmpdir(), "clashlens-e2e-cleanup-"));
    const started = join(directory, "test-started");
    const stopped = join(directory, "server-stopped");
    const listener = createServer();
    await new Promise<void>((resolve) => listener.listen(0, "127.0.0.1", resolve));
    const address = listener.address();
    if (!address || typeof address === "string") throw new Error("No test port");
    const port = address.port;
    await new Promise<void>((resolve, reject) =>
      listener.close((error) => (error ? reject(error) : resolve())),
    );
    const server = join(directory, "server.cjs");
    writeFileSync(
      server,
      `const fs = require('node:fs');
       const server = require('node:http').createServer((_, response) => response.end('ok'));
       server.listen(${port}, '127.0.0.1');
       process.on('SIGTERM', () => {
         fs.writeFileSync(${JSON.stringify(stopped)}, 'SIGTERM');
         server.close(() => process.exit(0));
       });`,
    );
    writeFileSync(
      join(directory, "cleanup.spec.cjs"),
      `const { test } = require(${JSON.stringify(playwright)});
       test('cleanup probe', async () => {
         require('node:fs').writeFileSync(${JSON.stringify(started)}, 'started');
         ${outcome === "failure" ? "throw new Error('Expected test failure');" : "await new Promise(() => {});"}
       });`,
    );
    const configFile = join(directory, "playwright.config.cjs");
    writeFileSync(
      configFile,
      `module.exports = ${JSON.stringify({
        testDir: directory,
        reporter: "line",
        webServer: {
          ...webServer,
          command: `"${process.execPath}" "${server}"`,
          cwd: directory,
          url: `http://127.0.0.1:${port}`,
        },
      })};`,
    );
    const child = spawn(process.execPath, [cli, "test", "--config", configFile], {
      cwd: directory,
      stdio: "ignore",
    });
    const closed = new Promise<number | null>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", resolve);
    });
    try {
      if (outcome === "cancellation") {
        const deadline = Date.now() + 10_000;
        while (!existsSync(started) && Date.now() < deadline && child.exitCode === null) {
          await new Promise((resolve) => setTimeout(resolve, 20));
        }
        expect(existsSync(started)).toBe(true);
        child.kill("SIGINT");
      }
      expect(await closed).not.toBe(0);
      expect(readFileSync(stopped, "utf8")).toBe("SIGTERM");
    } finally {
      if (child.exitCode === null) {
        child.kill("SIGINT");
        await closed;
      }
      rmSync(directory, { recursive: true, force: true });
    }
  },
  20_000,
);
