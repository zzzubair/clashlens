import type { PlayerLookup } from "./contracts";

/** What a player page says about a tag without current results. */
export const LOOKUP_MESSAGES: Record<PlayerLookup["state"], string> = {
  unknown: "Waiting to check this tag with Clash of Clans.",
  checking:
    "Checking this tag with Clash of Clans. Legend I players start tracking automatically.",
  tracking: "Now tracking in Legend I. The first results are being prepared.",
  not_found:
    "Player not found. Clash of Clans did not find this tag. Check the tag and try again.",
  not_in_legend:
    "This player is not in Legend I. Clash Lens tracks Legend League players only. We have kept the tag and any saved history.",
  uncertain:
    "Clash Lens tracks Legend League players only. This player exists, but we could not confirm they are in Legend I. Any saved history is still available.",
  failed:
    "We could not finish checking this tag. This does not mean the player is missing or outside Legend I.",
};

/**
 * Why a tracked player's newest profile gives no current results, or null
 * when nothing needs explaining. Clash Lens rechecks these players' profiles
 * every 15 minutes.
 */
export function lookupExplanation(
  reason: PlayerLookup["reason"] | null,
  name: string | null | undefined,
): string | null {
  switch (reason) {
    case "no_legend_battles":
      return `${name ?? "This player"} is in Legend League but hasn't played a Legend League battle this Season.`;
    case "season_unconfirmed":
      return "Clash of Clans has not confirmed this player's Season yet. We are still checking, but current results are unavailable until it does.";
    case "unknown_tier":
      return "Clash of Clans reported a league we do not recognize for this player. We are still checking, but current results are unavailable until it reports a known league.";
    case "profile_rejected":
      return "Clash of Clans sent player details we could not use. We are still checking, but current results are unavailable until it sends valid details.";
    default:
      return null;
  }
}
