import { useEffect, useRef, useState } from "react";
import { useNavigation, useRevalidator } from "react-router";

const SEASON_MS = 28 * 24 * 60 * 60 * 1000;
// The same confirmed 28-day phase used by the server's Season validation.
const SEASON_ANCHOR_MS = 1_783_918_800_000;

type Clock = {
  loadedAt?: string;
  now: number;
  elapsed: number;
  wall: number;
  unsettled?: "slipped" | "asked";
};

function advanceClock(clock: Clock) {
  const elapsed = performance.now();
  const wall = Date.now();
  const ran = Math.max(0, elapsed - clock.elapsed);
  const moved = wall - clock.wall;
  // Some browsers pause performance.now() during device sleep. Only use the
  // device clock's elapsed time, never its absolute date.
  clock.now += Math.max(ran, moved);
  // Sleep or a device clock change: only a fresh server time can settle it.
  if (Math.abs(moved - ran) > 1_000) clock.unsettled = "slipped";
  clock.elapsed = elapsed;
  clock.wall = wall;
  return clock.now;
}

export function nextSeasonReset(loadedAt: string) {
  const loaded = Date.parse(loadedAt);
  return (
    SEASON_ANCHOR_MS +
    (Math.floor((loaded - SEASON_ANCHOR_MS) / SEASON_MS) + 1) * SEASON_MS
  );
}

/** Reread saved data at Season Reset and once a minute while awaiting recovery. */
export function useSeasonReread(
  loadedAt: string | undefined,
  waiting: boolean,
  enabled = true,
  finalAnswer = false,
) {
  const revalidator = useRevalidator();
  const navigation = useNavigation();
  const busy = useRef(false);
  busy.current = revalidator.state !== "idle" || navigation.state !== "idle";
  const lastRead = useRef(-Infinity);
  const inFlight = useRef(false);
  const clock = useRef<Clock>({
    loadedAt,
    now: loadedAt ? Date.parse(loadedAt) : 0,
    elapsed: performance.now(),
    wall: Date.now(),
  });
  advanceClock(clock.current);
  if (loadedAt && clock.current.loadedAt !== loadedAt) {
    const served = Date.parse(loadedAt);
    if (clock.current.unsettled === "asked") {
      // Requested after the device clock slipped, so it carries the real time.
      clock.current.now = lastRead.current = served;
      clock.current.unsettled = undefined;
    } else {
      // A buffered response from before Reset cannot wind the established clock back.
      clock.current.now = Math.max(clock.current.now, served);
    }
    clock.current.loadedAt = loadedAt;
  }
  const sourceTime = clock.current.loadedAt;
  const lastWaiting = useRef(waiting);
  if (loadedAt || waiting || finalAnswer) lastWaiting.current = waiting;
  const recoveryWaiting = lastWaiting.current;
  const [, setExpiredFor] = useState<string>();
  const { revalidate } = revalidator;

  useEffect(() => {
    if (!enabled || (!sourceTime && !recoveryWaiting)) return;
    const reset = sourceTime ? nextSeasonReset(sourceTime) : Infinity;
    const check = () => {
      const elapsed = advanceClock(clock.current);
      const expired = elapsed >= reset;
      if (expired && sourceTime) setExpiredFor(sourceTime);
      const { unsettled } = clock.current;
      if (
        document.hidden ||
        (!expired && !recoveryWaiting && !unsettled) ||
        busy.current ||
        inFlight.current ||
        (unsettled !== "slipped" && elapsed - lastRead.current < 60_000)
      )
        return;
      if (unsettled) clock.current.unsettled = "asked";
      lastRead.current = elapsed;
      inFlight.current = true;
      void Promise.resolve(revalidate())
        .catch(() => undefined)
        .finally(() => {
          inFlight.current = false;
        });
    };
    // One boundary read, then bounded retries if that read fails or data is pending.
    let boundary: ReturnType<typeof setTimeout> | undefined;
    const scheduleBoundary = () => {
      const remaining = reset - advanceClock(clock.current);
      // Browser timeouts cannot hold the full 28 days in one signed integer.
      if (Number.isFinite(remaining) && remaining > 0) {
        boundary = setTimeout(
          () => {
            check();
            scheduleBoundary();
          },
          Math.min(remaining, 2_147_483_647),
        );
      } else if (remaining <= 0) check();
    };
    scheduleBoundary();
    const timer = setInterval(check, 60_000);
    const visible = () => {
      if (!document.hidden) check();
    };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("pageshow", visible);
    return () => {
      clearTimeout(boundary);
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("pageshow", visible);
    };
  }, [enabled, sourceTime, recoveryWaiting, revalidate]);

  return enabled && !!sourceTime && clock.current.now >= nextSeasonReset(sourceTime);
}
