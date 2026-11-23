const WINDOW_MS = 60_000;
const MAX_TRACKED_IDENTITIES = 10_000;

type RateLimitedRequest =
  "refresh" | "automaticRefresh" | "verification" | "liveClan" | "search";

const LIMITS: Record<RateLimitedRequest, number> = {
  refresh: 6,
  automaticRefresh: 3,
  verification: 6,
  liveClan: 6,
  search: 60,
};

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

function rateWindowState(
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

function admit(kind: RateLimitedRequest, identity: string, now: number): boolean {
  const tracked = windows[kind];
  for (const [key, window] of tracked) {
    if (rateWindowState(window, LIMITS[kind], now) !== "none") break;
    tracked.delete(key);
  }

  const current = tracked.get(identity);
  const state = rateWindowState(current, LIMITS[kind], now);
  if (state === "exhausted") return false;
  if (state === "counting" && current !== undefined) {
    current.count += 1;
    return true;
  }
  tracked.delete(identity);
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
