import { mapGroupPlayer, type GroupPlayer } from "../lib/account-contracts";
import type { PlayerLookup } from "../lib/contracts";
import { isCanonicalUuid } from "../lib/validation";
import { type LoginIdentity } from "../server/auth.server";
import { getPlayerLookup, startPlayerLookup } from "./player-lookup.server";
import { PythonApiError } from "./python-response.server";
import { requestJson } from "./python-transport.server";

// How long adding a player waits for the game to answer before asking the
// clasher to press Add again; the check keeps running either way.
const LOOKUP_WAIT_MS = 8_000;
const LOOKUP_POLL_MS = 1_000;

/**
 * Ask whether a tag belongs to a real player through the same lookup a player
 * page visit starts, so it shares that lookup's rate limit and API budget.
 */
export async function checkPlayerTag(
  clientAddress: string | undefined,
  tag: string,
  sleep: (ms: number) => Promise<void> = (ms) =>
    new Promise((resolve) => setTimeout(resolve, ms)),
): Promise<PlayerLookup> {
  let lookup = await getPlayerLookup(tag);
  if (lookup.state === "unknown" || lookup.state === "failed") {
    lookup = await startPlayerLookup(clientAddress, tag);
  }
  for (
    let waited = 0;
    lookup.state === "checking" && waited < LOOKUP_WAIT_MS;
    waited += LOOKUP_POLL_MS
  ) {
    await sleep(LOOKUP_POLL_MS);
    lookup = await getPlayerLookup(tag);
  }
  return lookup;
}

/** Add one checked player; Python repeats every check and has the final say. */
export async function addGroupPlayer(
  identity: LoginIdentity,
  groupId: string,
  tag: string,
  idempotencyKey: string,
): Promise<GroupPlayer> {
  requireIds(groupId, idempotencyKey);
  const payload = await requestJson<unknown>(`/v1/account/groups/${groupId}/players`, {
    method: "POST",
    body: Buffer.from(JSON.stringify({ tag }), "utf8"),
    requestId: idempotencyKey,
    identity: identity,
  });
  const player = mapGroupPlayer(payload);
  if (player === null || player.tag !== tag) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return player;
}

export async function removeGroupPlayer(
  identity: LoginIdentity,
  groupId: string,
  tag: string,
  idempotencyKey: string,
): Promise<void> {
  requireIds(groupId, idempotencyKey);
  await requestJson<unknown>(
    `/v1/account/groups/${groupId}/players/${encodeURIComponent(tag)}`,
    { method: "DELETE", requestId: idempotencyKey, identity: identity },
  );
}

function requireIds(groupId: string, idempotencyKey: string): void {
  if (!isCanonicalUuid(groupId) || !isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
}
