/** Browser-visible endpoints for a local dev or Quadlet fixture stack. */
export const websiteOrigin = process.env.CLASHLENS_E2E_ORIGIN ?? "http://127.0.0.1:5173";
/** A website the browser tests start on their own, serving only the fixture blog folder. */
export const blogFixtureOrigin = "http://127.0.0.1:5181";
/** The same website again with visitor counting on; the browser tests answer for PostHog. */
export const analyticsFixtureOrigin = "http://127.0.0.1:5182";
export const analyticsFixtureToken = `phc_${"A1b2C3d4E5".repeat(4)}`;
/** The login secret `dev` gives the local website, so tests can sign aged login cookies. */
export const devLoginSecret = Buffer.from(
  "QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8",
  "base64url",
);
