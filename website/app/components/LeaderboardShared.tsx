import type { TrackedPlayerEntry } from "../lib/contracts";
import { TrophyIcon } from "./GameIcons";

/** Rankings' rows a page, 50 on every width: short enough to read through
 *  without folding, with one pager at its foot. Links to a rank's page use it. */
export const RANKINGS_PAGE_SIZE = 50;

/** The game's trophy beside a trophy count, sized by each page's
 *  `.trophy-mark` rules. */
export function TrophyMark() {
  return <TrophyIcon className="trophy-mark" />;
}

export function latestObservation(entries: TrackedPlayerEntry[]) {
  return entries.reduce<string | null>((latest, entry) => {
    if (latest === null) return entry.freshness.observedAt;
    return Date.parse(entry.freshness.observedAt) > Date.parse(latest)
      ? entry.freshness.observedAt
      : latest;
  }, null);
}
