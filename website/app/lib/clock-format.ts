import { nextResetMs } from "./dashboard";

export function timeFormatter(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  });
}

/** Whole minutes left in the Legend day, and the "9h 05m" the clocks show for them. */
export function resetCountdown(nowMs: number): { left: number; text: string } {
  const left = Math.ceil((nextResetMs(nowMs) - nowMs) / 60_000);
  return {
    left,
    text: `${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m`,
  };
}

/**
 * A time zone as a Clasher says it, with the short name the player page's
 * times use: "Europe/London" is "London time (GMT)", or (BST) in summer.
 */
export function zoneName(timeZone: string): string {
  const place = timeZone.split("/").at(-1)?.replaceAll("_", " ");
  if (!place || timeZone === "UTC") return "UTC time";
  const short = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "short" })
    .formatToParts(Date.now())
    .find((part) => part.type === "timeZoneName")?.value;
  return short ? `${place} time (${short})` : `${place} time`;
}
