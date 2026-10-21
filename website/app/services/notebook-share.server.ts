import type { BaseShare } from "../lib/notebook";
import {
  parseSharedBase,
  parseShareSaved,
  type SharedBase,
  type ShareChange,
} from "../lib/notebook-share";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** Share a base, change what its page shows, or stop sharing it; returns
 * how it is shared now, null once stopped. */
export async function saveShare(
  identity: GoogleAccountIdentity,
  change: ShareChange,
): Promise<BaseShare | null> {
  const payload = await requestJson<unknown>(
    "/v1/account/notebook/share",
    "POST",
    Buffer.from(JSON.stringify(change), "utf8"),
    "notebook-share",
    undefined,
    identity,
  );
  const share = parseShareSaved(payload);
  if (share === undefined) throw new PythonApiError(502, { error: "malformed" });
  return share;
}

/** What a shared page shows, by its unlisted link; null once stopped. */
export async function readSharedBase(link: string): Promise<SharedBase | null> {
  const payload = await requestJson<unknown>(
    `/v1/notebook-shares/${encodeURIComponent(link)}`,
    "GET",
    undefined,
    undefined,
    undefined,
    undefined,
    [404],
  );
  if ((payload as { error?: unknown } | null)?.error === "share_not_found") return null;
  const shared = parseSharedBase(payload);
  if (shared === null) throw new PythonApiError(502, { error: "malformed" });
  return shared;
}
