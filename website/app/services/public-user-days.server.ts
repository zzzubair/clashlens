import { parseCalendar } from "../lib/calendar";
import type { Calendar } from "../lib/contracts";
import type { SeasonWeek } from "../lib/contracts";
import { mapSeasonWeeks } from "./player-response.server";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isInteger,
  isRecord,
  isUtcTimestamp,
} from "./python-response.server";
import { requestJson } from "./python-transport.server";

// Under the page's five-second streaming limit; a timeout hides only the
// cards' Reset moves and heat maps.
const PUBLIC_USER_DAYS_TIMEOUT_MS = 4_000;

/** One saved Legend day of a linked player this Season, as its card draws it. */
export interface LinkedSeasonDay {
  kind: "played" | "shield" | "missing";
  /** The Season day number, 1 to 28. */
  day: number;
  attacks: number | null;
  defenses: number | null;
  /** The day's result as the player page's Daily Legend log shows it. */
  trophyChange: number | null;
  /** Known to be 5,000 to 5,000 with no battles. */
  unplayed: boolean;
  /** Shown in the player page's log: false for a day outside Legend I it leaves out. */
  listed: boolean;
}

/** What a public profile's player cards add to their numbers. */
export interface PublicUserDays {
  calendar: Calendar;
  /** The current Season's first Reset. */
  seasonStart: string;
  players: Array<{
    tag: string;
    heatmapDayNumbers: number[];
    /** The place on the last Reset's Daily board this Season; null off it,
     *  and on a Season's first Legend day. */
    resetRank: number | null;
    seasonWeeks: SeasonWeek[];
    /** This Season's saved days so far, today's included, oldest first.
     *  The API sends each as `[day, attacks, defenses, result, unplayed,
     *  listed]`, so 500 players' Seasons stay small. */
    days: LinkedSeasonDay[];
  }>;
}

const HEATMAP_KINDS: Record<string, LinkedSeasonDay["kind"]> = {
  p: "played",
  s: "shield",
  m: "missing",
};

const nullableCount = (value: unknown) =>
  value === null || (isInteger(value) && value >= 0);
const isBoolean = (value: unknown): value is boolean => typeof value === "boolean";

/** Every linked player's Reset place and Season days, in one read. */
export async function getPublicUserDays(username: string): Promise<PublicUserDays> {
  const payload = await requestJson<unknown>(
    `/v1/users/${encodeURIComponent(username)}/days`,
    {
      method: "GET",
      expectedKind: "public-user-days",
      timeoutMs: PUBLIC_USER_DAYS_TIMEOUT_MS,
    },
  );
  const malformed = () => new PythonApiError(502, { error: "malformed" });
  if (
    !isRecord(payload) ||
    !isUtcTimestamp(payload.season_start) ||
    !Array.isArray(payload.players)
  )
    throw malformed();
  const calendar = parseCalendar(payload.calendar);
  if (calendar === null) throw malformed();
  return {
    seasonStart: payload.season_start,
    calendar,
    players: payload.players.map((player) => {
      if (
        !isRecord(player) ||
        !isCanonicalPlayerTag(player.tag) ||
        !(player.reset_position === null || isInteger(player.reset_position)) ||
        typeof player.heatmap_day_kinds !== "string" ||
        !Array.isArray(player.heatmap_day_numbers) ||
        !player.heatmap_day_numbers.every(isInteger) ||
        !Array.isArray(player.days) ||
        player.heatmap_day_kinds.length !== player.days.length ||
        new Set(player.heatmap_day_numbers).size !== player.heatmap_day_numbers.length
      )
        throw malformed();
      const tuples = player.days;
      if (
        !player.heatmap_day_numbers.every((number) =>
          tuples.some((day) => Array.isArray(day) && day[0] === number),
        )
      )
        throw malformed();
      return {
        tag: player.tag,
        heatmapDayNumbers: player.heatmap_day_numbers,
        resetRank: player.reset_position,
        seasonWeeks: mapSeasonWeeks(player.season_weeks),
        days: player.days.map((day, index): LinkedSeasonDay => {
          if (
            !Array.isArray(day) ||
            day.length !== 6 ||
            !isInteger(day[0]) ||
            day[0] < 1 ||
            day[0] > calendar.day_count ||
            !nullableCount(day[1]) ||
            !nullableCount(day[2]) ||
            !(day[3] === null || isInteger(day[3])) ||
            !isBoolean(day[4]) ||
            !isBoolean(day[5])
          )
            throw malformed();
          // One letter a day, in the same order as the days: played, shield or missing.
          const kind = HEATMAP_KINDS[(player.heatmap_day_kinds as string)[index]!];
          if (kind === undefined) throw malformed();
          return {
            kind,
            day: day[0],
            attacks: day[1] as number | null,
            defenses: day[2] as number | null,
            trophyChange: day[3] as number | null,
            unplayed: day[4],
            listed: day[5],
          };
        }),
      };
    }),
  };
}
