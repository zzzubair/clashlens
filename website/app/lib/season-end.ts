import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";

/**
 * True once the `season` (its first Reset, in Unix seconds) has run its 28
 * Legend days, so trophies loaded during it are no longer current. A page left
 * open re-renders at that moment and reloads its data before showing current
 * trophies again.
 */
export function useSeasonEnded(season: string | null): boolean {
  const [clock, setClock] = useState(0);
  const revalidator = useRevalidator();
  const seasonEnd = season === null ? NaN : (Number(season) + 28 * 86400) * 1000;
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
 * A Season named by its dates, Reset to Reset, such as "7 Sep – 5 Oct 2026",
 * or "29 Dec 2025 – 26 Jan 2026" across a new year. `season` is its first
 * Reset in Unix seconds, as Season IDs are.
 */
export function seasonDates(season: string | number): string {
  const start = new Date(Number(season) * 1000);
  const end = new Date(start.getTime() + 28 * 86_400_000);
  if (Number.isNaN(end.getTime())) return "Past Season";
  const first =
    start.getUTCFullYear() === end.getUTCFullYear() ? SEASON_DAY : SEASON_DATE;
  return `${first.format(start)} – ${SEASON_DATE.format(end)}`.replaceAll("Sept", "Sep");
}
