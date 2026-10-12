/**
 * Sign-in steps, counted by the website server because only it sees the
 * provider's answer. Like the private API's counts, each is an anonymous
 * total: a fresh random ID per count, no person profile, no account or
 * provider identity, and no location lookup. Nothing is sent without the
 * project token, and a lost count never fails the sign-in.
 */
import { randomUUID } from "node:crypto";

import { POSTHOG_HOST } from "../lib/analytics";
import { loadAnalyticsSettings } from "./config.server";

export function countSignInStep(
  provider: "google" | "discord",
  stage: "started" | "returned" | "succeeded" | "failed",
): void {
  const settings = loadAnalyticsSettings();
  if (settings === null) return;
  fetch(`${POSTHOG_HOST}/i/v0/e/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      api_key: settings.token,
      event: "sign_in_step",
      distinct_id: randomUUID(),
      properties: {
        provider,
        stage,
        $process_person_profile: false,
        $geoip_disable: true,
      },
    }),
    signal: AbortSignal.timeout(10_000),
  }).catch(() => undefined);
}
