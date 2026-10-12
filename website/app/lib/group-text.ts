import type { GroupPlayer } from "./account-contracts";
import { MAX_GROUPS } from "./account-validation";

export const GROUP_LIMIT = `You have ${MAX_GROUPS} groups, the most an account can have. Delete one to create another.`;

/** What a member row says when the player is not tracked in Legend League. */
export const STATE_LABELS: Record<GroupPlayer["state"], string> = {
  tracking: "",
  not_in_legend: "Not in Legend League, no data",
  uncertain: "Not confirmed in Legend League yet, no data",
  checking: "Looking up this player…",
  unknown: "Not looked up yet",
  not_found: "Tag not found in Clash of Clans",
  failed: "Lookup failed; open the player to retry",
};

/** What the page says after a player joins a group, naming the group when given. */
export function addedNotice(player: GroupPlayer, groupName?: string): string {
  const who = player.name === null ? player.tag : `${player.name} (${player.tag})`;
  const added = groupName === undefined ? `Added ${who}` : `Added ${who} to ${groupName}`;
  if (player.state === "tracking") {
    // An add retry can return the saved response from before a Season Reset.
    return player.seasonResetPending
      ? `${added}. Waiting for this player's Season reset.`
      : `${added}.`;
  }
  return `${added}. ${STATE_LABELS[player.state]}.`;
}
