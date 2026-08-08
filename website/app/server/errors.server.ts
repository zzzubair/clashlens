import type { WebsiteErrorCode, WebsiteErrorResponse } from "../lib/contracts";

type ErrorLike = {
  status?: number;
  payload?: unknown;
};

const messages: Record<WebsiteErrorCode, string> = {
  invalid_input: "Check the submitted value and try again.",
  missing: "The requested player data is not available.",
  forbidden: "This action is not allowed.",
  conflict: "The request conflicts with current saved data.",
  rate_limited: "Refresh requests are temporarily limited. Try again shortly.",
  uncertain: "The result is uncertain. Saved data remains visible.",
  malformed: "The private service returned malformed data. Saved data is unchanged.",
  unavailable: "Saved data is still available, but the live service is unavailable.",
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function codeFor(status: number | undefined, value: unknown): WebsiteErrorCode {
  if (isObject(value)) {
    if (typeof value.error === "string" && value.error in messages) {
      return value.error as WebsiteErrorCode;
    }
    if (isObject(value.error) && typeof value.error.code === "string") {
      if (value.error.code in messages) return value.error.code as WebsiteErrorCode;
    }
  }
  switch (status) {
    case 400:
      return "invalid_input";
    case 401:
    case 403:
      return "forbidden";
    case 404:
      return "missing";
    case 409:
      return "conflict";
    case 429:
      return "rate_limited";
    case 422:
      return "uncertain";
    case 502:
      return "malformed";
    default:
      return "unavailable";
  }
}

function retryAfterSeconds(value: unknown): number | undefined {
  const nested = isObject(value) && isObject(value.error) ? value.error : undefined;
  const candidates = [
    isObject(value) ? value.retry_after_seconds : undefined,
    isObject(value) ? value.retryAfterSeconds : undefined,
    nested?.retry_after_seconds,
    nested?.retryAfterSeconds,
  ];
  const retryAfter = candidates.find(
    (candidate): candidate is number =>
      typeof candidate === "number" &&
      Number.isInteger(candidate) &&
      candidate > 0 &&
      candidate <= 3600,
  );
  return retryAfter;
}

export function safeWebsiteError(error: unknown): WebsiteErrorResponse {
  const candidate = isObject(error) ? (error as ErrorLike) : {};
  const code = codeFor(candidate.status, candidate.payload);
  const response: WebsiteErrorResponse = {
    error: { code, message: messages[code] },
  };

  if (code === "rate_limited") {
    const retryAfter = retryAfterSeconds(candidate.payload);
    if (retryAfter !== undefined) response.error.retryAfterSeconds = retryAfter;
  }
  return response;
}
