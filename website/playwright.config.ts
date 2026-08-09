import { defineConfig, devices } from "@playwright/test";

import {
  fixtureApiHealthUrl,
  fixtureApiUrl,
  fixtureHmacCaller,
  fixtureHmacKeyId,
  fixtureKey,
  loginSecretB64,
  oidcClientId,
  oidcClientSecret,
  oidcIssuerUrl,
  oidcProviderHealthUrl,
  oidcRedirectUri,
  oidcSubject,
  websiteHealthUrl,
  websiteOrigin,
} from "./tests/fixtures/test-values";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  // One worker keeps the shared in-memory fixture servers deterministic:
  // account state is shared across every spec in the run.
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: websiteOrigin,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "python3 fixture_server.py --host 127.0.0.1 --port 8010",
      cwd: ".",
      url: fixtureApiHealthUrl,
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        CLASHLENS_FIXTURE_HMAC_SECRET_B64: fixtureKey,
        CLASHLENS_FIXTURE_HMAC_CALLER: fixtureHmacCaller,
        CLASHLENS_FIXTURE_HMAC_KEY_ID: fixtureHmacKeyId,
        CLASHLENS_FIXTURE_REFRESH_STATUS_ERROR_TAG: "#2PY",
      },
    },
    {
      command: "node ./tests/fixtures/oidc-provider.ts",
      cwd: ".",
      url: oidcProviderHealthUrl,
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        CLASHLENS_FIXTURE_OIDC_CLIENT_ID: oidcClientId,
        CLASHLENS_FIXTURE_OIDC_CLIENT_SECRET: oidcClientSecret,
        CLASHLENS_FIXTURE_OIDC_SUBJECT: oidcSubject,
        CLASHLENS_FIXTURE_OIDC_REDIRECT_URI: oidcRedirectUri,
      },
    },
    {
      command: "npm run start",
      cwd: ".",
      url: websiteHealthUrl,
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        NODE_ENV: "test",
        PORT: "5173",
        CLASHLENS_PYTHON_API_URL: fixtureApiUrl,
        CLASHLENS_PYTHON_HMAC_CALLER: fixtureHmacCaller,
        CLASHLENS_PYTHON_HMAC_KEY_ID: fixtureHmacKeyId,
        CLASHLENS_PYTHON_HMAC_SECRET_B64: fixtureKey,
        CLASHLENS_TRUST_PROXY: "true",
        CLASHLENS_LOGIN_ENABLED: "true",
        CLASHLENS_PUBLIC_ORIGIN: websiteOrigin,
        CLASHLENS_GOOGLE_ISSUER_URL: oidcIssuerUrl,
        CLASHLENS_GOOGLE_CLIENT_ID: oidcClientId,
        CLASHLENS_GOOGLE_CLIENT_SECRET: oidcClientSecret,
        CLASHLENS_LOGIN_SECRET_B64: loginSecretB64,
      },
    },
  ],
});
