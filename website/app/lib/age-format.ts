export function elapsedUnits(seconds: number, rounding: "floor" | "round" = "floor") {
  const minutes = Math[rounding](seconds / 60);
  const hours = Math[rounding](minutes / 60);
  return { minutes, hours, days: Math[rounding](hours / 24) };
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return "less than 1 minute";
  const { minutes, hours, days } = elapsedUnits(seconds);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${days.toLocaleString("en-GB")} day${days === 1 ? "" : "s"}`;
}

/** An update's age as Home and Rankings word it: "4 min", not "4 minutes",
 *  so it fits beside a tag on a phone. */
export function shortAge(seconds: number) {
  return seconds < 60 ? "<1 min" : formatAge(seconds).replace(/ minutes?$/, " min");
}

/** "updated 3 min ago", or nothing until the browser knows the time. */
export function updatedAgo(
  observedAtMs: number | null,
  nowMs: number | null,
): string | null {
  if (observedAtMs === null || nowMs === null) return null;
  const { minutes, hours } = elapsedUnits(Math.max(0, (nowMs - observedAtMs) / 1000));
  if (minutes < 1) return "updated just now";
  if (minutes < 120) return `updated ${minutes} min ago`;
  return `updated ${hours} h ago`;
}
