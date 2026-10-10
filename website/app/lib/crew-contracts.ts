/**
 * Response contracts for the private crew API, produced by
 * python/src/clashlens/api_crews.py and api_crew_boards.py, and how the
 * boards' numbers read. Boards arrive as totals with their day counts; the
 * averages are worked out here. A payload that does not validate is
 * rejected as malformed.
 */

import type { LinkedPlayerCard } from "./account-contracts";
import { validPlayerTag } from "./player-tag";
import { isCanonicalUuid } from "./validation";

export const MAX_CREWS = 5;
export const MIN_CREW_SIZE = 2;
export const MAX_CREW_SIZE = 100;
export const DEFAULT_CREW_SIZE = 50;

export type CrewRole = "owner" | "admin" | "member";
const ROLES: readonly CrewRole[] = ["owner", "admin", "member"];

export interface ListedCrew {
  crewId: string;
  name: string;
  size: number;
  used: number;
  role: CrewRole;
  myTags: string[];
}

export interface CrewList {
  crews: ListedCrew[];
  maxCrews: number;
}

/** Whether a placed account shows on boards this Season. */
export type CrewPlayerStatus = "tracking" | "not_in_legend" | "no_battles_this_season";
const PLAYER_STATUSES: readonly CrewPlayerStatus[] = [
  "tracking",
  "not_in_legend",
  "no_battles_this_season",
];

export interface CrewPlayer {
  tag: string;
  name: string | null;
  trophies: number | null;
  status: CrewPlayerStatus;
}

export interface CrewMember {
  username: string;
  displayName: string;
  role: CrewRole;
  you: boolean;
  players: CrewPlayer[];
}

/** A live invite link; owners and admins see every one, members their own. */
export interface CrewInviteLink {
  inviteId: string;
  madeBy: string;
  expiresAt: string;
  mine: boolean;
}

export interface Crew {
  crewId: string;
  name: string;
  size: number;
  used: number;
  myRole: CrewRole;
  members: CrewMember[];
  invites: CrewInviteLink[];
}

/** An invite link just made or reused, with the code that goes in it. */
export interface MadeInvite {
  inviteId: string;
  code: string;
  expiresAt: string;
  openPlaces: number;
}

/** One of the signed-in account's linked Clash of Clans accounts. */
export type LinkedAccount = Pick<
  LinkedPlayerCard,
  "tag" | "name" | "state" | "trophies" | "league"
>;

/** A linked account the game puts outside Legend League cannot join. */
export function canJoin(account: LinkedAccount): boolean {
  return !["not_in_legend", "uncertain", "not_found"].includes(account.state);
}

/** A made invite with its full address on this website. */
export type InviteLink = MadeInvite & { link: string };

/** When an invite link stops working, in UTC: "Mon 19 Oct, 14:20 UTC". */
export function formatInviteExpiry(expiresAt: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "UTC",
  }).formatToParts(new Date(expiresAt));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `${part("weekday")} ${part("day")} ${part("month")}, ${part("hour")}:${part("minute")} UTC`;
}

/** The code in an invite link, as the private API makes it. */
export const INVITE_CODE = /^[A-Za-z0-9_-]{22}$/;

export type InviteState = "ok" | "invalid" | "full" | "limit";
const INVITE_STATES: readonly InviteState[] = ["ok", "invalid", "full", "limit"];
export type Eligibility = "ok" | "not_in_legend" | "already_in_crew" | "unchecked";
const ELIGIBILITIES: readonly Eligibility[] = [
  "ok",
  "not_in_legend",
  "already_in_crew",
  "unchecked",
];

export interface InviteAccount {
  tag: string;
  name: string | null;
  trophies: number | null;
  eligibility: Eligibility;
}

/**
 * What an invite link shows the signed-in clasher. A link that doesn't
 * work says nothing about its crew.
 */
export type InvitePreview = {
  inCrew: boolean;
  crewCount: number;
  accounts: InviteAccount[];
} & (
  | { state: "invalid" }
  | {
      state: Exclude<InviteState, "invalid">;
      crewId: string;
      name: string;
      ownerName: string | null;
      size: number;
      used: number;
      expiresAt: string;
    }
);

export const PERIODS = ["today", "week", "season"] as const;
export type CrewPeriod = (typeof PERIODS)[number];

export const PERIOD_LABELS: Record<CrewPeriod, string> = {
  today: "Today",
  week: "Last 7 days",
  season: "Season",
};

/** A period from the address, or Season, the default. */
export function crewPeriod(value: string | null): CrewPeriod {
  return PERIODS.find((period) => period === value) ?? "season";
}

/** Board keys, in the order the crew page shows them, with their address. */
export const BOARDS = {
  live: { slug: "live", title: "Live leaderboard" },
  top: { slug: "top", title: "Top players" },
  attackers: { slug: "attackers", title: "Top attackers" },
  best_defenders: { slug: "best-defenders", title: "Best defenders" },
  worst_defenders: { slug: "worst-defenders", title: "Worst defenders" },
  streaks: { slug: "streaks", title: "Highest streaks" },
} as const;
export type BoardKey = keyof typeof BOARDS;
export const BOARD_KEYS = Object.keys(BOARDS) as BoardKey[];
/** Live and Top players are always "now"; the period switch changes the rest. */
export const NOW_BOARDS: readonly BoardKey[] = ["live", "top"];

export function boardFromSlug(slug: string | undefined): BoardKey | null {
  return BOARD_KEYS.find((key) => BOARDS[key].slug === slug) ?? null;
}

/** One line under a board's title saying what its number is. */
export function boardDescription(board: BoardKey, period: CrewPeriod): string {
  const today = period === "today";
  switch (board) {
    case "live":
      return "Trophies now";
    case "top":
      return "Trophies at the last Reset";
    case "attackers":
      return today
        ? "Trophies gained on attack today"
        : "Trophies gained on attack a day";
    case "best_defenders":
      return today
        ? "Fewest trophies lost on defense today"
        : "Fewest trophies lost on defense a day";
    case "worst_defenders":
      return today
        ? "Most trophies lost on defense today"
        : "Most trophies lost on defense a day";
    case "streaks":
      return "Three-star attacks in a row";
  }
}

export type BoardRow = {
  tag: string;
  name: string | null;
  you: boolean;
} & (
  | { kind: "trophies"; trophies: number }
  | { kind: "average"; total: number; days: number; battles: number }
  | { kind: "streak"; best: number; going: boolean; attacks: number }
);

export interface MissingAccount {
  tag: string;
  name: string | null;
  reason: string;
}

export interface Board {
  rows: BoardRow[];
  missing: MissingAccount[];
}

export interface CrewBoards {
  crewId: string;
  period: CrewPeriod;
  dayNumber: number;
  /** Finished Legend days the averages cover; empty on a Season's Day 1. */
  windowDays: string[];
  boards: Record<BoardKey, Board>;
}

const MISSING_REASONS: Record<string, string> = {
  not_in_legend: "Not in Legend League",
  no_battles_this_season: "No Legend battles this Season",
  no_days_in_period: "No Legend days in this period",
  no_attacks_in_period: "No attacks in this period",
  no_attacks_today: "No attacks yet today",
  no_defenses_today: "No defenses yet today",
  no_reset_reading: "No Reset reading yet",
  not_on_live_board: "Not on the Live leaderboard yet",
};

/** Why an account is not on a board, in words. */
export function missingReason(reason: string): string {
  return MISSING_REASONS[reason] ?? "No data here";
}

function signed(value: number, negative: boolean, digits: number): string {
  const text = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return value === 0 ? text : `${negative ? "-" : "+"}${text}`;
}

/**
 * An average board's number: trophies a Legend day with one decimal over a
 * longer period, whole trophies so far today. Defense is shown as a loss.
 */
export function formatAverage(
  row: { total: number; days: number },
  board: BoardKey,
  period: CrewPeriod,
): string {
  const negative = board !== "attackers";
  if (period === "today") return signed(row.total, negative, 0);
  return signed(row.total / row.days, negative, 1);
}

export function mapCrewList(value: unknown): CrewList | null {
  if (
    !isRecord(value) ||
    value.kind !== "crew-list" ||
    !Array.isArray(value.crews) ||
    !isCount(value.max_crews)
  ) {
    return null;
  }
  const crews: ListedCrew[] = [];
  for (const item of value.crews) {
    const header = mapHeader(item);
    if (
      header === null ||
      !isRecord(item) ||
      !isRole(item.role) ||
      !Array.isArray(item.my_tags) ||
      !item.my_tags.every(isTag)
    ) {
      return null;
    }
    crews.push({ ...header, role: item.role, myTags: item.my_tags });
  }
  return { crews, maxCrews: value.max_crews };
}

export function mapCrew(value: unknown): Crew | null {
  const header = mapHeader(value);
  if (
    header === null ||
    !isRecord(value) ||
    value.kind !== "crew" ||
    !isRole(value.my_role) ||
    !Array.isArray(value.members)
  ) {
    return null;
  }
  const members: CrewMember[] = [];
  for (const item of value.members) {
    if (
      !isRecord(item) ||
      !isString(item.username) ||
      !isString(item.display_name) ||
      !isRole(item.role) ||
      typeof item.you !== "boolean" ||
      !Array.isArray(item.players)
    ) {
      return null;
    }
    const players: CrewPlayer[] = [];
    for (const player of item.players) {
      if (
        !isRecord(player) ||
        !isTag(player.tag) ||
        !isNullableString(player.name) ||
        !(player.trophies === null || isCount(player.trophies)) ||
        !PLAYER_STATUSES.includes(player.status as CrewPlayerStatus)
      ) {
        return null;
      }
      players.push({
        tag: player.tag,
        name: player.name,
        trophies: player.trophies,
        status: player.status as CrewPlayerStatus,
      });
    }
    members.push({
      username: item.username,
      displayName: item.display_name,
      role: item.role,
      you: item.you,
      players,
    });
  }
  if (!Array.isArray(value.invites)) return null;
  const invites: CrewInviteLink[] = [];
  for (const item of value.invites) {
    if (
      !isRecord(item) ||
      !isUuid(item.invite_id) ||
      !isString(item.made_by) ||
      !isTimestamp(item.expires_at) ||
      typeof item.mine !== "boolean"
    ) {
      return null;
    }
    invites.push({
      inviteId: item.invite_id,
      madeBy: item.made_by,
      expiresAt: item.expires_at,
      mine: item.mine,
    });
  }
  return { ...header, myRole: value.my_role, members, invites };
}

/** The link a make-invite answer gives. */
export function mapMadeInvite(value: unknown): MadeInvite | null {
  if (
    !isRecord(value) ||
    !isUuid(value.invite_id) ||
    typeof value.code !== "string" ||
    !INVITE_CODE.test(value.code) ||
    !isTimestamp(value.expires_at) ||
    !isCount(value.open_places)
  ) {
    return null;
  }
  return {
    inviteId: value.invite_id,
    code: value.code,
    expiresAt: value.expires_at,
    openPlaces: value.open_places,
  };
}

export function mapInvitePreview(value: unknown): InvitePreview | null {
  if (
    !isRecord(value) ||
    value.kind !== "crew-invite" ||
    !INVITE_STATES.includes(value.state as InviteState) ||
    typeof value.in_crew !== "boolean" ||
    !isCount(value.crew_count) ||
    !Array.isArray(value.accounts)
  ) {
    return null;
  }
  const accounts: InviteAccount[] = [];
  for (const item of value.accounts) {
    if (
      !isRecord(item) ||
      !isTag(item.tag) ||
      !isNullableString(item.name) ||
      !(item.trophies === null || isCount(item.trophies)) ||
      !ELIGIBILITIES.includes(item.eligibility as Eligibility)
    ) {
      return null;
    }
    accounts.push({
      tag: item.tag,
      name: item.name,
      trophies: item.trophies,
      eligibility: item.eligibility as Eligibility,
    });
  }
  const common = { inCrew: value.in_crew, crewCount: value.crew_count, accounts };
  if (value.state === "invalid") return { ...common, state: "invalid" };
  const header = mapHeader(value);
  if (
    header === null ||
    !isNullableString(value.owner_display_name) ||
    !isTimestamp(value.expires_at)
  ) {
    return null;
  }
  return {
    ...common,
    state: value.state as Exclude<InviteState, "invalid">,
    crewId: header.crewId,
    name: header.name,
    ownerName: value.owner_display_name,
    size: header.size,
    used: header.used,
    expiresAt: value.expires_at,
  };
}

/** What a refused account in a join or add reads as. */
const ACCOUNT_REFUSALS: Record<string, (tag: string) => string> = {
  player_not_linked: (tag) => `${tag} is not linked to your account.`,
  player_not_in_legend: (tag) => `${tag} is not in Legend League, so it can't join.`,
  player_not_checked: (tag) =>
    `Still checking ${tag} with Clash of Clans. Try again in a few seconds.`,
  player_already_in_crew: (tag) => `${tag} is already in this crew.`,
};

/**
 * The message for a crew request the private API refused, from its error
 * code and the details it sent with it; null for a code with no message of
 * its own, which then reads as a general error.
 */
export function crewRefusal(
  code: string | null,
  details: { tag?: unknown; open_places?: unknown; used?: unknown } = {},
): string | null {
  if (code === null) return null;
  const tag = typeof details.tag === "string" ? details.tag : "An account";
  if (code in ACCOUNT_REFUSALS) return ACCOUNT_REFUSALS[code]!(tag);
  const open = isCount(details.open_places) ? details.open_places : null;
  const used = isCount(details.used) ? details.used : null;
  const messages: Record<string, string> = {
    crew_full:
      open === null || open === 0
        ? "The crew is full."
        : `Only ${open} ${open === 1 ? "place is" : "places are"} open. Pick fewer accounts.`,
    crew_limit_reached: `You're already in ${MAX_CREWS} crews, the most you can be in.`,
    crew_forbidden: "Only the owner or an admin can do that.",
    crew_not_found: "This crew no longer exists, or you're no longer in it.",
    crew_size_below_used:
      used === null
        ? "The crew can't have fewer places than accounts in it."
        : `${used} places are in use. Pick ${used} or more, or kick accounts first.`,
    invalid_crew_size: `Places must be from ${MIN_CREW_SIZE} to ${MAX_CREW_SIZE}.`,
    invalid_crew_name: "Choose a different crew name.",
    owner_must_hand_over: "As owner, hand over the crew or delete it first.",
    owner_role_fixed: "The owner's role can only change by handing over.",
    member_not_found: "That clasher is no longer in this crew.",
    player_not_in_crew: "That account is no longer in this crew.",
    invite_not_found: "That link is already off or expired.",
    invite_invalid: "This invite link has expired or was turned off.",
  };
  return messages[code] ?? null;
}

/** The new crew's id from a create answer. */
export function mapCreatedCrew(value: unknown): string | null {
  return isRecord(value) && isUuid(value.crew_id) ? value.crew_id : null;
}

export function mapCrewBoards(value: unknown): CrewBoards | null {
  if (
    !isRecord(value) ||
    value.kind !== "crew-boards" ||
    !isUuid(value.crew_id) ||
    !PERIODS.includes(value.period as CrewPeriod) ||
    !isCount(value.day_number) ||
    !Array.isArray(value.window_days) ||
    !value.window_days.every(isTimestamp) ||
    !isRecord(value.boards)
  ) {
    return null;
  }
  const period = value.period as CrewPeriod;
  const boards = {} as Record<BoardKey, Board>;
  for (const key of BOARD_KEYS) {
    const board = mapBoard(value.boards[key], key, period);
    if (board === null) return null;
    boards[key] = board;
  }
  return {
    crewId: value.crew_id,
    period,
    dayNumber: value.day_number,
    windowDays: value.window_days,
    boards,
  };
}

function mapBoard(value: unknown, key: BoardKey, period: CrewPeriod): Board | null {
  if (!isRecord(value) || !Array.isArray(value.rows) || !Array.isArray(value.missing)) {
    return null;
  }
  const rows: BoardRow[] = [];
  for (const item of value.rows) {
    if (
      !isRecord(item) ||
      !isTag(item.tag) ||
      !isNullableString(item.name) ||
      typeof item.you !== "boolean"
    ) {
      return null;
    }
    const base = { tag: item.tag, name: item.name, you: item.you };
    if (key === "live" || key === "top") {
      if (!isCount(item.trophies)) return null;
      rows.push({ ...base, kind: "trophies", trophies: item.trophies });
    } else if (key === "streaks") {
      if (
        !isCount(item.best) ||
        typeof item.going !== "boolean" ||
        !isCount(item.attacks)
      ) {
        return null;
      }
      rows.push({
        ...base,
        kind: "streak",
        best: item.best,
        going: item.going,
        attacks: item.attacks,
      });
    } else {
      // Today's numbers need no Legend day yet; an average needs one to divide by.
      const minimumDays = period === "today" ? 0 : 1;
      if (
        !isCount(item.total) ||
        !isCount(item.days) ||
        item.days < minimumDays ||
        !isCount(item.battles)
      ) {
        return null;
      }
      rows.push({
        ...base,
        kind: "average",
        total: item.total,
        days: item.days,
        battles: item.battles,
      });
    }
  }
  const missing: MissingAccount[] = [];
  for (const item of value.missing) {
    if (
      !isRecord(item) ||
      !isTag(item.tag) ||
      !isNullableString(item.name) ||
      !isString(item.reason)
    ) {
      return null;
    }
    missing.push({ tag: item.tag, name: item.name, reason: item.reason });
  }
  return { rows, missing };
}

function mapHeader(
  value: unknown,
): { crewId: string; name: string; size: number; used: number } | null {
  if (
    !isRecord(value) ||
    !isUuid(value.crew_id) ||
    !isString(value.name) ||
    !isCount(value.size) ||
    !isCount(value.used)
  ) {
    return null;
  }
  return { crewId: value.crew_id, name: value.name, size: value.size, used: value.used };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}

function isCount(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

function isTag(value: unknown): value is string {
  return typeof value === "string" && validPlayerTag(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && isCanonicalUuid(value);
}

function isRole(value: unknown): value is CrewRole {
  return ROLES.includes(value as CrewRole);
}
