import { defineConfig, devices } from "@playwright/test";

import { websiteHealthUrl, websiteOrigin } from "./tests/fixtures/test-values";

const externalStack = process.env.CLASHLENS_E2E_EXTERNAL_STACK === "1";

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
    ? []
    : [
        {
          command: "../dev e2e-server",
          cwd: ".",
          url: websiteHealthUrl,
          reuseExistingServer: false,
          timeout: 300_000,
        },
      ],
});
