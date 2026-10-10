/**
 * Response contracts for the private crew API, produced by
 * python/src/clashlens/api_crews.py and api_crew_boards.py, and how the
 * boards' numbers read. Boards arrive as totals with their day counts; the
 * averages are worked out here. A payload that does not validate is
 * rejected as malformed.
 */

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

export interface CrewMember {
  username: string;
  displayName: string;
  role: CrewRole;
  you: boolean;
  players: { tag: string; name: string | null }[];
}

export interface Crew {
  crewId: string;
  name: string;
  size: number;
  used: number;
  myRole: CrewRole;
  members: CrewMember[];
}

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
    const players = [];
    for (const player of item.players) {
      if (!isRecord(player) || !isTag(player.tag) || !isNullableString(player.name)) {
        return null;
      }
      players.push({ tag: player.tag, name: player.name });
    }
    members.push({
      username: item.username,
      displayName: item.display_name,
      role: item.role,
      you: item.you,
      players,
    });
  }
  return { ...header, myRole: value.my_role, members };
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
