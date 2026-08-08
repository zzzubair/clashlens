import type { RefreshStatus, WebsiteErrorResponse } from "./contracts";

const CANONICAL_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const MAX_SEARCH_QUERY_LENGTH = 80;

export function isCanonicalUuid(value: string): boolean {
  return CANONICAL_UUID_PATTERN.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isWebsiteErrorResponse(value: unknown): value is WebsiteErrorResponse {
  if (!isRecord(value) || !isRecord(value.error)) return false;
  return (
    typeof value.error.code === "string" &&
    [
      "invalid_input",
      "missing",
      "forbidden",
      "conflict",
      "rate_limited",
      "uncertain",
      "malformed",
      "unavailable",
    ].includes(value.error.code) &&
    typeof value.error.message === "string"
  );
}

export function isRefreshStatusPayload(value: unknown): value is RefreshStatus {
  if (!isRecord(value) || value.kind !== "refresh-status") return false;
  return (
    typeof value.workId === "string" &&
    typeof value.tag === "string" &&
    ["queued", "running", "complete", "unavailable", "failed"].includes(
      value.state as string,
    ) &&
    typeof value.progressPercent === "number" &&
    Number.isFinite(value.progressPercent) &&
    typeof value.message === "string" &&
    (typeof value.publishedAt === "string" || value.publishedAt === null) &&
    "player" in value &&
    (value.player === null || isRecord(value.player))
  );
}
