/** Browser-visible endpoints for a local dev or Quadlet fixture stack. */
export const websiteOrigin = process.env.CLASHLENS_E2E_ORIGIN ?? "http://127.0.0.1:5173";
export const websiteHealthUrl = `${websiteOrigin}/healthz`;
