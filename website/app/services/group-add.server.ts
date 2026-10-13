import type { ListedGroup, GroupPlayer } from "../lib/account-contracts";
import { MAX_GROUP_TAGS } from "../lib/account-validation";
import type { PlayerLookup } from "../lib/contracts";
import { addGroupPlayer, checkPlayerTag } from "./group-players.server";
import type { GoogleAccountIdentity } from "./python.server";

// Adding one player to a group, shared by Your groups and Add to group.

export const INVALID_TAG =
  "Enter one valid player tag, like #2PY0LQ. Tags use only 0, 2, 8, 9 and the letters P Y L Q G R J C U V.";
export const GROUP_FULL = `This group already has ${MAX_GROUP_TAGS} players, the most a comparison shows. Remove a player to add another.`;

/** The message under the tag field for a refused add or remove, if it has one. */
export function groupTagError(code: unknown, tag: string): string | null {
  const messages: Record<string, string> = {
    group_player_exists: `${tag} is already in this group.`,
    group_full: GROUP_FULL,
    player_not_found: notFound(tag),
    player_not_checked: stillChecking(tag),
    rate_limited:
      "Too many player checks from your connection. Wait a minute and try again.",
    invalid_tag: INVALID_TAG,
  };
  return typeof code === "string" && code in messages ? messages[code]! : null;
}

function notFound(tag: string): string {
  return `Clash of Clans has no player with the tag ${tag}. Check the tag and try again.`;
}

function stillChecking(tag: string): string {
  return `Still checking ${tag} with Clash of Clans. Press Add again in a few seconds.`;
}

/** Why a checked tag cannot join a group yet; null once the game confirms the player. */
export function lookupRefusal(
  lookup: PlayerLookup,
  tag: string,
): { status: number; tagError: string } | null {
  if (lookup.state === "not_found") return { status: 422, tagError: notFound(tag) };
  if (lookup.state === "checking") return { status: 409, tagError: stillChecking(tag) };
  if (lookup.state === "failed" || lookup.state === "unknown") {
    return {
      status: 503,
      tagError: `Clash of Clans could not be reached to check ${tag}. Try again in a minute.`,
    };
  }
  return null;
}

export type AddToGroupResult =
  { status: 200; player: GroupPlayer } | { status: number; tagError: string };

/**
 * Add a player to one of the account's groups: refuse a duplicate or a full
 * group before spending a player lookup, check the tag with the game, then
 * add it. Errors without a tag message are thrown for the caller to show.
 */
export async function addToGroup(
  identity: GoogleAccountIdentity,
  clientAddress: string | undefined,
  group: ListedGroup,
  tag: string,
  idempotencyKey: string,
): Promise<AddToGroupResult> {
  const member = group.players.find((player) => player.tag === tag);
  if (member !== undefined) {
    const who = member.name === null ? member.tag : `${member.name} (${member.tag})`;
    return { status: 409, tagError: `${who} is already in this group.` };
  }
  if (group.tags.length >= MAX_GROUP_TAGS) return { status: 422, tagError: GROUP_FULL };
  const refusal = lookupRefusal(await checkPlayerTag(clientAddress, tag), tag);
  if (refusal !== null) return refusal;
  try {
    return {
      status: 200,
      player: await addGroupPlayer(identity, group.groupId, tag, idempotencyKey),
    };
  } catch (cause) {
    const { status, payload } = cause as { status?: number; payload?: unknown };
    const code =
      typeof payload === "object" && payload !== null
        ? (payload as Record<string, unknown>).error
        : undefined;
    const tagError = groupTagError(code, tag);
    if (tagError === null) throw cause;
    return { status: status ?? 422, tagError };
  }
}
