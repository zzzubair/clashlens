/**
 * Shared bases: one notebook base shown to anyone with its unlisted link, a
 * random 22-character address like the OBS overlay link's. This file reads
 * the private API's answers about a share and a shared page.
 */

import {
  BASE_ID,
  parseRecord,
  parseShare,
  SHARE_LINK,
  type BaseRecord,
  type BaseShare,
} from "./notebook";

/** One change to a base's share, as the private API takes it. */
export interface ShareChange {
  action: "share" | "settings" | "stop";
  base: string;
  include_link?: boolean;
  show_names?: boolean;
}

/** What a shared page shows: never the base ID or its note. */
export interface SharedBase {
  name: string;
  /** The in-game base link, when the Clasher includes it. */
  link: string | null;
  record: BaseRecord;
  season: { start: string; end: string };
  asOfDay: number | null;
  /** The Clasher's players that ran it, when they show names. */
  players: Array<{ name: string; record: BaseRecord }> | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function isShareLinkId(value: unknown): value is string {
  return typeof value === "string" && SHARE_LINK.test(value);
}

/** The shared page's address for a share link. */
export function sharePath(link: string): string {
  return `/b/${link}`;
}

/** The share after a change, or undefined when it does not fit the contract. */
export function parseShareSaved(value: unknown): BaseShare | null | undefined {
  if (!isRecord(value) || value.kind !== "notebook-share") return undefined;
  return parseShare(value.share);
}

/** A change the page posted, checked before it goes to the private API. */
export function parseShareChange(value: unknown): ShareChange | null {
  if (!isRecord(value)) return null;
  const { action, base, include_link: link, show_names: names, ...rest } = value;
  if (
    Object.keys(rest).length > 0 ||
    !["share", "settings", "stop"].includes(action as string) ||
    typeof base !== "string" ||
    !BASE_ID.test(base) ||
    !(link === undefined || typeof link === "boolean") ||
    !(names === undefined || typeof names === "boolean")
  )
    return null;
  return value as unknown as ShareChange;
}

/** A shared page, or null when it does not fit the contract. */
export function parseSharedBase(value: unknown): SharedBase | null {
  if (!isRecord(value) || value.kind !== "shared-base" || !isRecord(value.season))
    return null;
  const { name, link, as_of_day: asOf, players } = value;
  const { start, end } = value.season;
  const record = parseRecord(value.record);
  if (
    typeof name !== "string" ||
    !(link === null || typeof link === "string") ||
    !record ||
    typeof start !== "string" ||
    typeof end !== "string" ||
    !(asOf === null || (Number.isInteger(asOf) && (asOf as number) >= 1)) ||
    !(players === null || Array.isArray(players))
  )
    return null;
  const parsed =
    players?.map((item: unknown) =>
      isRecord(item) && typeof item.name === "string" && parseRecord(item.record)
        ? { name: item.name, record: parseRecord(item.record) as BaseRecord }
        : null,
    ) ?? null;
  if (parsed?.includes(null)) return null;
  return {
    name,
    link,
    record,
    season: { start, end },
    asOfDay: asOf as number | null,
    players: parsed as SharedBase["players"],
  };
}
