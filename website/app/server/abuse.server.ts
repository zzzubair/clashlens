const WINDOW_MS = 60_000;
const MAX_TRACKED_IDENTITIES = 10_000;

/** The kinds of public request each visitor may make a limited number of per minute. */
type RateLimitedRequest =
  "refresh" | "automaticRefresh" | "verification" | "liveClan" | "search";

const LIMITS: Record<RateLimitedRequest, number> = {
  refresh: 6,
  automaticRefresh: 3,
  verification: 6,
  liveClan: 6,
  // Enough for typing in the header search box.
  search: 60,
};

/**
 * One visitor's allowance for one kind of request.
 *
 * - `none`: no window, or its reset time has passed, so the allowance is
 *   renewed.
 * - `counting`: fewer requests than the limit so far; the next is allowed
 *   and counted.
 * - `exhausted`: the limit is reached; requests are refused, uncounted,
 *   until the reset time.
 *
 * Events: a request in `none` opens a 60-second window counting 1; a
 * request while `counting` adds 1; the reset time passing returns it to
 * `none`. `rateWindowState` is the only place the state is decided and
 * `admit` the only place it moves. Windows live in this process's memory
 * only: a restart forgets them all. Expired windows are dropped on every
 * request, and when 10,000 visitors are tracked, opening a window forgets
 * the longest-tracked visitor's window for that kind.
 */
export type RateWindowState = "none" | "counting" | "exhausted";

interface RateWindow {
  count: number;
  resetAt: number;
}

const windows: Record<RateLimitedRequest, Map<string, RateWindow>> = {
  refresh: new Map(),
  automaticRefresh: new Map(),
  verification: new Map(),
  liveClan: new Map(),
  search: new Map(),
};

export function rateWindowState(
  window: RateWindow | undefined,
  limit: number,
  now: number,
): RateWindowState {
  if (window === undefined || window.resetAt <= now) return "none";
  return window.count >= limit ? "exhausted" : "counting";
}

export function allowPublicRefresh(identity: string, now = Date.now()): boolean {
  return admit("refresh", identity, now);
}

export function allowAutomaticRefresh(identity: string, now = Date.now()): boolean {
  return admit("automaticRefresh", identity, now);
}

export function allowVerification(identity: string, now = Date.now()): boolean {
  return admit("verification", identity, now);
}

export function allowLiveClanRead(identity: string, now = Date.now()): boolean {
  return admit("liveClan", identity, now);
}

export function allowSearch(identity: string, now = Date.now()): boolean {
  return admit("search", identity, now);
}

/** Why this visitor may not go on, or null; an unknown address never does. */
export function visitorRefusal(
  identity: string | undefined,
  allow: (identity: string) => boolean,
) {
  if (!identity) return { status: 503, payload: { error: "unavailable" } };
  if (!allow(identity)) {
    return { status: 429, payload: { error: "rate_limited", retry_after_seconds: 60 } };
  }
  return null;
}

/** Count one request of this kind, or refuse it; true when it may go on. */
function admit(kind: RateLimitedRequest, identity: string, now: number): boolean {
  const tracked = windows[kind];
  for (const [key, window] of tracked) {
    if (rateWindowState(window, LIMITS[kind], now) === "none") tracked.delete(key);
  }

  const current = tracked.get(identity);
  const state = rateWindowState(current, LIMITS[kind], now);
  if (state === "exhausted") return false;
  if (state === "counting" && current !== undefined) {
    current.count += 1;
    return true;
  }
  if (tracked.size >= MAX_TRACKED_IDENTITIES) {
    const oldest = tracked.keys().next().value;
    if (oldest !== undefined) tracked.delete(oldest);
  }
  tracked.set(identity, { count: 1, resetAt: now + WINDOW_MS });
  return true;
}

export function clearPublicRefreshLimits(): void {
  for (const tracked of Object.values(windows)) tracked.clear();
}
