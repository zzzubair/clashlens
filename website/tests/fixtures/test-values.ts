/** Browser-visible endpoints for a local dev or Quadlet fixture stack. */
export const websiteOrigin = process.env.CLASHLENS_E2E_ORIGIN ?? "http://127.0.0.1:5173";
export const websiteHealthUrl = `${websiteOrigin}/healthz`;
/** A website the browser tests start on their own, serving only the fixture blog folder. */
export const blogFixtureOrigin = "http://127.0.0.1:5181";
