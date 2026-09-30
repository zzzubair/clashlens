import type { PlayerLookup } from "../lib/contracts";
import { allowPublicPlayerRequest } from "../server/abuse.server";
import { PythonApiError, requestJson } from "./python.server";

export async function getPlayerLookup(tag: string): Promise<PlayerLookup> {
  return lookupRequest(tag, "GET");
}

export async function startPlayerLookup(
  request: Request,
  tag: string,
): Promise<PlayerLookup> {
  if (!allowPublicPlayerRequest(request)) {
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
    ].includes(payload.state)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return payload;
}
