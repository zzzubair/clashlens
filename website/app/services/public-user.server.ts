import type { PublicUser } from "../lib/account-contracts";
import { mapPublicUser } from "../lib/account-contracts";
import { normalizeUsername } from "../lib/account-validation";
import { PythonApiError } from "./python-response.server";
import { requestJson } from "./python-transport.server";

export async function getPublicUser(username: string): Promise<PublicUser> {
  const normalized = normalizeUsername(username);
  if (normalized === null) {
    throw new PythonApiError(404, { error: "user_not_found" });
  }
  const payload = await requestJson<unknown>(
    `/v1/users/${encodeURIComponent(normalized)}`,
    { method: "GET" },
  );
  return mappedOrMalformed(mapPublicUser(payload));
}

function mappedOrMalformed<T>(mapped: T | null): T {
  if (mapped === null) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return mapped;
}
