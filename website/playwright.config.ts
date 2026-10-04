import { defineConfig, devices } from "@playwright/test";

import { blogFixtureOrigin, websiteOrigin } from "./tests/fixtures/test-values";

const externalStack = process.env.CLASHLENS_E2E_EXTERNAL_STACK === "1";

// The built website again, reading tests/fixtures/blog with login off, so the
// blog tests control which posts exist without touching the shared stack.
const blogServer = {
  command: "node server.ts",
  cwd: ".",
  url: `${blogFixtureOrigin}/blog`,
  reuseExistingServer: false,
  env: {
    NODE_ENV: "test",
    HOST: "127.0.0.1",
    PORT: new URL(blogFixtureOrigin).port,
    CLASHLENS_PUBLIC_ORIGIN: blogFixtureOrigin,
    CLASHLENS_BLOG_DIR: "tests/fixtures/blog",
    CLASHLENS_BLOG_OWNER: "google:blog-owner",
  },
};

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  // One worker keeps mutations against the shared development database ordered.
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: websiteOrigin,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: externalStack
    ? [blogServer]
    : [
        blogServer,
        {
          command: "../dev e2e-server",
          cwd: ".",
          // The health page responds before the first player and login fixtures are ready.
          wait: { stdout: /Clash Lens is ready at / },
          reuseExistingServer: false,
          timeout: 300_000,
          // Let dev's trap stop the pod and remove its disposable volumes.
          gracefulShutdown: { signal: "SIGTERM", timeout: 60_000 },
        },
      ],
});
