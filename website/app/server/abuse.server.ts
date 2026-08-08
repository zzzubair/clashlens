const WINDOW_MS = 60_000;
const MAX_REQUESTS_PER_WINDOW = 6;
const MAX_TRACKED_IDENTITIES = 10_000;

const attempts = new Map<string, { count: number; resetAt: number }>();

export function allowPublicRefresh(identity: string, now = Date.now()): boolean {
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
  if (current.count >= MAX_REQUESTS_PER_WINDOW) return false;
  current.count += 1;
  return true;
}

export function clearPublicRefreshLimits(): void {
  attempts.clear();
}
