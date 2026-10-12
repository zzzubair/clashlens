import type { PlayerLookup } from "../lib/contracts";
import { allowPublicRefresh } from "../server/abuse.server";
import { PythonApiError, requestJson } from "./python.server";

export async function getPlayerLookup(tag: string): Promise<PlayerLookup> {
  return lookupRequest(tag, "GET");
}

export async function startPlayerLookup(
  identity: string | undefined,
  tag: string,
): Promise<PlayerLookup> {
  if (!identity) {
    throw new PythonApiError(503, { error: "service_unavailable" });
  }
  if (!allowPublicRefresh(identity)) {
    throw new PythonApiError(429, { error: "rate_limited", retry_after_seconds: 60 });
  }
  return lookupRequest(tag, "POST");
}

async function lookupRequest(tag: string, method: "GET" | "POST"): Promise<PlayerLookup> {
  const payload = await requestJson<PlayerLookup>(
    `/v1/players/${encodeURIComponent(tag)}/lookup`,
    method,
    undefined,
    undefined,
    method === "POST" ? globalThis.crypto.randomUUID() : undefined,
  );
  if (
    payload?.tag !== tag ||
    ![
      "unknown",
      "checking",
      "tracking",
      "not_found",
      "not_in_legend",
      "uncertain",
      "failed",
    ].includes(payload.state) ||
    !(
      payload.reason === undefined ||
      [
        "pending",
        "no_legend_battles",
        "season_unconfirmed",
        "unknown_tier",
        "profile_rejected",
      ].includes(payload.reason)
    ) ||
    !(
      payload.profile === undefined ||
      (typeof payload.profile?.name === "string" &&
        (payload.profile.clan === null || typeof payload.profile.clan === "string") &&
        Number.isInteger(payload.profile.trophies) &&
        ["undefined", "string"].includes(typeof payload.profile.league))
    )
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return payload;
}
