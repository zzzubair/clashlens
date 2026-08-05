import { defineConfig, devices } from "@playwright/test";

const fixtureKey = Buffer.from(Array.from({ length: 32 }, (_, index) => index)).toString(
  "base64url",
);

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://127.0.0.1:3001",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "python3 fixture_server.py --host 127.0.0.1 --port 8011",
      cwd: ".",
      url: "http://127.0.0.1:8011/healthz",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        CLASHLENS_FIXTURE_HMAC_SECRET_B64: fixtureKey,
        CLASHLENS_FIXTURE_HMAC_CALLER: "typescript-website",
        CLASHLENS_FIXTURE_HMAC_KEY_ID: "2026-08-a",
        CLASHLENS_FIXTURE_REFRESH_STATUS_ERROR_TAG: "#2PY",
      },
    },
    {
      command: "npm run start",
      cwd: ".",
      url: "http://127.0.0.1:3001",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        NODE_ENV: "test",
        PORT: "3001",
        CLASHLENS_PYTHON_API_URL: "http://127.0.0.1:8011",
        CLASHLENS_PYTHON_HMAC_CALLER: "typescript-website",
        CLASHLENS_PYTHON_HMAC_KEY_ID: "2026-08-a",
        CLASHLENS_PYTHON_HMAC_SECRET_B64: fixtureKey,
        CLASHLENS_TRUST_PROXY: "true",
      },
    },
  ],
});
