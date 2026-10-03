import type { UpdateStatus } from "../lib/contracts";
import { isRecord, isUtcTimestamp } from "../services/python-response.server";
import { requestJson } from "../services/python.server";

// Every page shows this, so read it at most twice a minute and never wait long.
const CACHE_MS = 30_000;
const TIMEOUT_MS = 1_000;

let cached: { at: number; value: Promise<UpdateStatus | null> } | null = null;

/** The delayed-updates notice, or null when updates are on time or unknown. */
export function loadUpdateStatus(now = Date.now()): Promise<UpdateStatus | null> {
  if (cached === null || now - cached.at >= CACHE_MS) {
    cached = { at: now, value: readUpdateStatus() };
  }
  return cached.value;
}

export function clearUpdateStatusCache(): void {
  cached = null;
}

async function readUpdateStatus(): Promise<UpdateStatus | null> {
  try {
    const payload = await requestJson<unknown>(
      "/v1/status",
      "GET",
      undefined,
      "update-status",
      undefined,
      undefined,
      undefined,
      TIMEOUT_MS,
    );
    return mapUpdateStatus(payload);
  } catch {
    return null;
  }
}

export function mapUpdateStatus(payload: unknown): UpdateStatus | null {
  if (!isRecord(payload) || !isUtcTimestamp(payload.checked_at)) return null;
  const lastCollectedAt =
    payload.collection_delayed === true && isUtcTimestamp(payload.last_collected_at)
      ? payload.last_collected_at
      : null;
  const oldestWaitingSavedAt =
    payload.processing_delayed === true && isUtcTimestamp(payload.oldest_waiting_saved_at)
      ? payload.oldest_waiting_saved_at
      : null;
  if (lastCollectedAt === null && oldestWaitingSavedAt === null) return null;
  return { checkedAt: payload.checked_at, lastCollectedAt, oldestWaitingSavedAt };
}
