import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";

/**
 * True once the `season` (its first Reset, in Unix seconds) has run its 28
 * Legend days, so trophies loaded during it are no longer current. A page left
 * open re-renders at that moment and reloads its data before showing current
 * trophies again.
 */
export function useSeasonEnded(end: string | null): boolean {
  const [clock, setClock] = useState(0);
  const revalidator = useRevalidator();
  const seasonEnd = end === null ? NaN : Date.parse(end);
  const seasonEnded = Date.now() >= seasonEnd;
  useEffect(() => {
    if (seasonEnded) {
      void revalidator.revalidate();
      return;
    }
    if (!Number.isFinite(seasonEnd)) return;
    const timer = setTimeout(
      () => setClock((tick) => tick + 1),
      Math.min(seasonEnd - Date.now(), 2 ** 31 - 1),
    );
    return () => clearTimeout(timer);
  }, [seasonEnd, seasonEnded, clock]);
  return seasonEnded;
}

/** A player whose trophies belong to a Season that has ended. */
export function expireSeasonTrophies<
  T extends { trophies: number | null; seasonResetPending: boolean },
>(player: T): T {
  return {
    ...player,
    trophies: null,
    seasonResetPending: player.seasonResetPending || player.trophies !== null,
  };
}

const SEASON_DAY = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
const SEASON_DATE = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

/**
 * A Season named by its first and last Legend days, such as "7 Sep – 4 Oct 2026",
 * or "29 Dec 2025 – 25 Jan 2026" across a new year. `season` is its first
 * Reset in Unix seconds, as Season IDs are.
 */
export function seasonDates(
  bounds: { seasonStart: string | null; seasonEnd: string | null } | null,
): string {
  return bounds?.seasonStart && bounds.seasonEnd
    ? seasonRange(new Date(bounds.seasonStart), new Date(bounds.seasonEnd))
    : "Past Season";
}

/**
 * The Season from the Reset `start` to the Reset `end`, named by its first and
 * last Legend days so two Seasons never share a date; the year shows on the
 * first day only when it differs.
 */
export function seasonRange(start: Date, end: Date): string {
  const last = new Date(end.getTime() - 86_400_000);
  const first =
    start.getUTCFullYear() === last.getUTCFullYear() ? SEASON_DAY : SEASON_DATE;
  return `${first.format(start)} – ${SEASON_DATE.format(last)}`.replaceAll("Sept", "Sep");
}

// Kept for wording only, where Python has no dates for a Season: the error when
// both Season reads fail, and an older army Season it could not date. Never a fact or decision.
export function unavailableSeasonDates(id: string): string {
  const start = new Date(Number(id) * 1000);
  return Number.isFinite(start.getTime())
    ? seasonRange(start, new Date(start.getTime() + 28 * 86_400_000))
    : "Past Season";
}
