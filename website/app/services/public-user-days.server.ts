import type { SeasonWeek } from "../lib/contracts";
import { mapSeasonWeeks } from "./player-response.server";
import { requestJson } from "./python.server";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isInteger,
  isRecord,
  isResetTimestamp,
} from "./python-response.server";

// Under the page's five-second streaming limit; a timeout hides only the
// cards' Reset moves and heat maps.
const PUBLIC_USER_DAYS_TIMEOUT_MS = 4_000;

/** One saved Legend day of a linked player this Season, as its card draws it. */
export interface LinkedSeasonDay {
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
  /** The current Season's first Reset. */
  seasonStart: string;
  players: Array<{
    tag: string;
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

const nullableCount = (value: unknown) =>
  value === null || (isInteger(value) && value >= 0);
const isBoolean = (value: unknown): value is boolean => typeof value === "boolean";

/** Every linked player's Reset place and Season days, in one read. */
export async function getPublicUserDays(username: string): Promise<PublicUserDays> {
  const payload = await requestJson<unknown>(
    `/v1/users/${encodeURIComponent(username)}/days`,
    "GET",
    undefined,
    "public-user-days",
    undefined,
    undefined,
    undefined,
    PUBLIC_USER_DAYS_TIMEOUT_MS,
  );
  const malformed = () => new PythonApiError(502, { error: "malformed" });
  if (
    !isRecord(payload) ||
    !isResetTimestamp(payload.season_start) ||
    !Array.isArray(payload.players)
  )
    throw malformed();
  return {
    seasonStart: payload.season_start,
    players: payload.players.map((player) => {
      if (
        !isRecord(player) ||
        !isCanonicalPlayerTag(player.tag) ||
        !(player.reset_position === null || isInteger(player.reset_position)) ||
        !Array.isArray(player.days)
      )
        throw malformed();
      return {
        tag: player.tag,
        resetRank: player.reset_position,
        seasonWeeks: mapSeasonWeeks(player.season_weeks),
        days: player.days.map((day): LinkedSeasonDay => {
          if (
            !Array.isArray(day) ||
            day.length !== 6 ||
            !isInteger(day[0]) ||
            day[0] < 1 ||
            day[0] > 28 ||
            !nullableCount(day[1]) ||
            !nullableCount(day[2]) ||
            !(day[3] === null || isInteger(day[3])) ||
            !isBoolean(day[4]) ||
            !isBoolean(day[5])
          )
            throw malformed();
          return {
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
