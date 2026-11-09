import { useEffect, useRef, useState } from "react";
import { useNavigation, useRevalidator } from "react-router";

/**
 * How far the page's clock can be trusted when it decides that Season Reset
 * has passed:
 *
 * - `settled`: it runs from the server's load time plus elapsed time.
 * - `slipped`: elapsed and device time disagreed by over a second (device
 *   sleep or a device clock change). Only a fresh server time can settle it,
 *   so the next check reads the page again even within a minute of the last.
 * - `asked`: that read was asked. The server time it brings becomes the clock,
 *   even if earlier, and the clock is `settled`.
 *
 * Events: each check (which may slip), a read asked by a check, and a new
 * server load time. Any other load time only moves the clock forward, so a
 * buffered answer from before Reset cannot wind it back. A failed read stays
 * `asked` and is retried once a minute; a slip while `asked` asks again at
 * once. `useSeasonReread` owns it, one per page; a reload starts `settled`.
 */
export type ClockTrust = "settled" | "slipped" | "asked";

type Clock = {
  loadedAt?: string;
  end?: string;
  now: number;
  elapsed: number;
  wall: number;
  trust: ClockTrust;
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
  if (Math.abs(moved - ran) > 1_000) clock.trust = "slipped";
  clock.elapsed = elapsed;
  clock.wall = wall;
  return clock.now;
}

/**
 * Whether a check reads the page again: never while hidden or while a read or
 * navigation runs; when Reset passed, a recovery is awaited or the clock is
 * not settled; and at most once a minute unless the clock just slipped.
 */
export function seasonCheckReads(check: {
  hidden: boolean;
  busy: boolean;
  due: boolean;
  trust: ClockTrust;
  sinceRead: number;
}): boolean {
  if (check.hidden || check.busy) return false;
  if (!check.due && check.trust === "settled") return false;
  return check.trust === "slipped" || check.sinceRead >= 60_000;
}

/**
 * Reread saved data at Season Reset (or at `seasonEnd`, the time the data stops
 * holding) and once a minute while awaiting recovery. Returns whether the
 * trusted clock has passed `expiresAt`, which defaults to that reread time.
 */
export function useSeasonReread(
  loadedAt: string | undefined,
  waiting: boolean,
  enabled = true,
  finalAnswer = false,
  seasonEnd?: string,
  expiresAt?: string,
) {
  const revalidator = useRevalidator();
  const navigation = useNavigation();
  const busy = useRef(false);
  busy.current = revalidator.state !== "idle" || navigation.state !== "idle";
  const lastRead = useRef(-Infinity);
  const inFlight = useRef(false);
  const clock = useRef<Clock>({
    loadedAt,
    end: seasonEnd,
    now: loadedAt ? Date.parse(loadedAt) : 0,
    elapsed: performance.now(),
    wall: Date.now(),
    trust: "settled",
  });
  advanceClock(clock.current);
  if (loadedAt && clock.current.loadedAt !== loadedAt) {
    const served = Date.parse(loadedAt);
    if (clock.current.trust === "asked") {
      // Requested after the device clock slipped, so it carries the real time.
      clock.current.now = lastRead.current = served;
      clock.current.trust = "settled";
    } else {
      // A buffered response from before Reset cannot wind the established clock back.
      clock.current.now = Math.max(clock.current.now, served);
    }
    clock.current.loadedAt = loadedAt;
    clock.current.end = seasonEnd;
  }
  const sourceTime = clock.current.loadedAt;
  const reset = clock.current.end ? Date.parse(clock.current.end) : Infinity;
  const lastWaiting = useRef(waiting);
  if (loadedAt || waiting || finalAnswer) lastWaiting.current = waiting;
  const recoveryWaiting = lastWaiting.current;
  const [, setExpiredFor] = useState<string>();
  const { revalidate } = revalidator;

  useEffect(() => {
    if (!enabled || (!sourceTime && !recoveryWaiting)) return;
    const check = () => {
      const elapsed = advanceClock(clock.current);
      const expired = elapsed >= reset;
      if (expired && sourceTime) setExpiredFor(sourceTime);
      const { trust } = clock.current;
      const reads = seasonCheckReads({
        hidden: document.hidden,
        busy: busy.current || inFlight.current,
        due: expired || recoveryWaiting,
        trust,
        sinceRead: elapsed - lastRead.current,
      });
      if (!reads) return;
      if (trust !== "settled") clock.current.trust = "asked";
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
    // The first load already read; only a page restored by Back or Forward needs a check.
    const restored = (event: PageTransitionEvent) => {
      if (event.persisted) visible();
    };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("pageshow", restored);
    return () => {
      clearTimeout(boundary);
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("pageshow", restored);
    };
  }, [enabled, sourceTime, reset, recoveryWaiting, revalidate]);

  const expiry = expiresAt ? Date.parse(expiresAt) : reset;
  return enabled && !!sourceTime && clock.current.now >= expiry;
}
