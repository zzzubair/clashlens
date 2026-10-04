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
