const WINDOW_MS = 60_000;
const MAX_REQUESTS_PER_WINDOW = 6;
const MAX_AUTOMATIC_REQUESTS_PER_WINDOW = 3;
const MAX_VERIFICATIONS_PER_WINDOW = 6;
const MAX_LIVE_CLAN_REQUESTS_PER_WINDOW = 6;
// Enough for typing in the header search box.
const MAX_SEARCHES_PER_WINDOW = 60;
const MAX_TRACKED_IDENTITIES = 10_000;

const attempts = new Map<string, { count: number; resetAt: number }>();
const automaticAttempts = new Map<string, { count: number; resetAt: number }>();
const verificationAttempts = new Map<string, { count: number; resetAt: number }>();
const liveClanAttempts = new Map<string, { count: number; resetAt: number }>();
const searchAttempts = new Map<string, { count: number; resetAt: number }>();

export function allowPublicRefresh(identity: string, now = Date.now()): boolean {
  return allowRefresh(attempts, MAX_REQUESTS_PER_WINDOW, identity, now);
}

export function allowAutomaticRefresh(identity: string, now = Date.now()): boolean {
  return allowRefresh(
    automaticAttempts,
    MAX_AUTOMATIC_REQUESTS_PER_WINDOW,
    identity,
    now,
  );
}

export function allowVerification(identity: string, now = Date.now()): boolean {
  return allowRefresh(verificationAttempts, MAX_VERIFICATIONS_PER_WINDOW, identity, now);
}

export function allowLiveClanRead(identity: string, now = Date.now()): boolean {
  return allowRefresh(liveClanAttempts, MAX_LIVE_CLAN_REQUESTS_PER_WINDOW, identity, now);
}

export function allowSearch(identity: string, now = Date.now()): boolean {
  return allowRefresh(searchAttempts, MAX_SEARCHES_PER_WINDOW, identity, now);
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

function allowRefresh(
  attempts: Map<string, { count: number; resetAt: number }>,
  limit: number,
  identity: string,
  now: number,
): boolean {
  for (const [key, value] of attempts) {
    if (value.resetAt <= now) attempts.delete(key);
  }

  const current = attempts.get(identity);
  if (!current || current.resetAt <= now) {
    if (attempts.size >= MAX_TRACKED_IDENTITIES) {
      const oldest = attempts.keys().next().value;
      if (oldest !== undefined) attempts.delete(oldest);
    }
    attempts.set(identity, { count: 1, resetAt: now + WINDOW_MS });
    return true;
  }
  if (current.count >= limit) return false;
  current.count += 1;
  return true;
}

export function clearPublicRefreshLimits(): void {
  attempts.clear();
  automaticAttempts.clear();
  verificationAttempts.clear();
  liveClanAttempts.clear();
  searchAttempts.clear();
}
