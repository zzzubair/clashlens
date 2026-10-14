/**
 * OBS overlay: what the account page saves and what an overlay in OBS reads.
 * Settings are short strings, the same as the overlay's address query, so a
 * preview and a saved link show exactly the same thing.
 */

/** Saved overlay settings: w, h, x, y, cw, ch (stream pixels), fit, text,
 * bg, font, show, popup, demo and snap. */
export type OverlaySettings = Record<string, string>;

export interface OverlayAccount {
  /** The private link's id; the overlay's address is /o/<link>. */
  link: string;
  settings: OverlaySettings;
  version: number;
  /** The verified player shown, or null when none is picked. */
  tag: string | null;
  players: Array<{ tag: string; name: string | null; league: string | null }>;
}

export interface OverlayLink {
  settings: OverlaySettings;
  version: number;
  tag: string | null;
}

export interface OverlayBattle {
  id: string;
  kind: "attack" | "defense";
  at: string;
  stars: number;
  destruction: number;
  trophies: number;
  opponent: string | null;
}

export interface OverlayPlayer {
  tag: string;
  state: "legend" | "not_in_legend" | "uncertain" | "missing";
  name?: string;
  trophies?: number;
  /** When the profile was last read from the game. */
  profileReadAt?: string;
  /** The Live Leaderboard place among tracked players. */
  rank?: number;
  day?: {
    number: number | null;
    of: number;
    /** The day's totals; null unless every battle so far is in `battles`. */
    gained: number | null;
    lost: number | null;
    net: number | null;
    battles: OverlayBattle[];
  };
}

/** The parts a Clasher can show or hide. */
export const OVERLAY_PARTS = [
  "trophies",
  "rank",
  "attacks",
  "defenses",
  "net",
  "day",
  "updated",
] as const;

export const OVERLAY_FONTS = {
  clashlens: "Clash Lens",
  clean: "Clean",
  esports: "Esports",
  tech: "Tech",
  arcade: "Arcade",
} as const;

/** How often an overlay in OBS asks for its settings and for its player. */
export const SETTINGS_POLL_MS = 3_000;
export const PLAYER_POLL_MS = 20_000;

const LINK_ID = /^[A-Za-z0-9_-]{22}$/;

export function isOverlayLinkId(value: unknown): value is string {
  return typeof value === "string" && LINK_ID.test(value);
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function settingsOf(value: unknown): OverlaySettings | null {
  if (!isRecord(value)) return null;
  const out: OverlaySettings = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item !== "string") return null;
    out[key] = item;
  }
  return out;
}

export function parseOverlayLink(payload: unknown): OverlayLink | null {
  if (!isRecord(payload) || payload.kind !== "obs-overlay-link") return null;
  const settings = settingsOf(payload.settings);
  if (
    settings === null ||
    !Number.isInteger(payload.version) ||
    !(payload.tag === null || typeof payload.tag === "string")
  )
    return null;
  return { settings, version: payload.version as number, tag: payload.tag };
}

export function parseOverlayAccount(payload: unknown): OverlayAccount | null {
  if (!isRecord(payload) || payload.kind !== "obs-overlay") return null;
  const link = parseOverlayLink({ ...payload, kind: "obs-overlay-link" });
  if (link === null || !isOverlayLinkId(payload.link) || !Array.isArray(payload.players))
    return null;
  const players = [];
  for (const player of payload.players) {
    if (
      !isRecord(player) ||
      typeof player.tag !== "string" ||
      !(player.name === null || typeof player.name === "string") ||
      !(player.league === null || typeof player.league === "string")
    )
      return null;
    players.push({ tag: player.tag, name: player.name, league: player.league });
  }
  return { ...link, link: payload.link, players };
}
