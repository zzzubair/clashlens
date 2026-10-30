/** The permanent Clash Lens Discord invite, linked from the site header. */
export const DISCORD_INVITE_URL = "https://discord.gg/792KJQTtRf";

/** One verified player's Discord attack alert and Legend day summary switches. */
export interface AttackAlertPlayer {
  tag: string;
  name: string | null;
  on: boolean;
  summaryOn: boolean;
}

/** Why Discord did not deliver an attack alert, as the private API reports it. */
export type UndeliveredReason =
  "no_shared_server" | "dms_closed" | "unknown_user" | "other" | "unreachable";

export interface AttackAlerts {
  players: AttackAlertPlayer[];
  /** The newest alert Discord did not deliver, while none has been delivered since. */
  undelivered: { tag: string; reason: UndeliveredReason; at: string } | null;
  /** The account's switch for every attack detail in the phone notification. */
  fullNotice: boolean;
}

const REASONS: readonly UndeliveredReason[] = [
  "no_shared_server",
  "dms_closed",
  "unknown_user",
  "other",
  "unreachable",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The private API's attack alert settings, or null when malformed. */
export function mapAttackAlerts(value: unknown): AttackAlerts | null {
  if (
    !isRecord(value) ||
    !Array.isArray(value.players) ||
    typeof value.full_notice !== "boolean"
  ) {
    return null;
  }
  const fullNotice = value.full_notice;
  const players: AttackAlertPlayer[] = [];
  for (const player of value.players) {
    if (
      !isRecord(player) ||
      typeof player.tag !== "string" ||
      (player.name !== null && typeof player.name !== "string") ||
      typeof player.on !== "boolean" ||
      typeof player.summary_on !== "boolean"
    ) {
      return null;
    }
    players.push({
      tag: player.tag,
      name: player.name,
      on: player.on,
      summaryOn: player.summary_on,
    });
  }
  const missed = value.undelivered;
  if (missed === null) return { players, undelivered: null, fullNotice };
  if (
    !isRecord(missed) ||
    typeof missed.tag !== "string" ||
    typeof missed.at !== "string" ||
    !REASONS.includes(missed.reason as UndeliveredReason)
  ) {
    return null;
  }
  return {
    players,
    undelivered: {
      tag: missed.tag,
      reason: missed.reason as UndeliveredReason,
      at: missed.at,
    },
    fullNotice,
  };
}
