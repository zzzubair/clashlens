import { describe, expect, it } from "vitest";

import { safeWebsiteError } from "../../app/server/errors.server";

describe("safe website errors", () => {
  it("maps a private API rate limit without exposing backend details", () => {
    const result = safeWebsiteError({
      status: 429,
      payload: {
        error: "rate_limited",
        retry_after_seconds: 9,
        detail: "database connection string must not escape",
      },
    });

    expect(result).toEqual({
      error: {
        code: "rate_limited",
        message: "Refresh requests are temporarily limited. Try again shortly.",
        retryAfterSeconds: 9,
      },
    });
    expect(JSON.stringify(result)).not.toContain("database");
  });

  it("returns a stable unavailable response for unknown failures", () => {
    expect(safeWebsiteError(new Error("private stack detail"))).toEqual({
      error: {
        code: "unavailable",
        message: "Saved data is still available, but the live service is unavailable.",
      },
    });
  });

  it("preserves a nested private error code without forwarding its detail", () => {
    const result = safeWebsiteError({
      status: 409,
      payload: {
        error: {
          code: "conflict",
          message: "private account identifier",
          retry_after_seconds: 12,
        },
      },
    });

    expect(result).toEqual({
      error: {
        code: "conflict",
        message: "The request conflicts with current saved data.",
      },
    });
    expect(JSON.stringify(result)).not.toContain("private");
  });

  it("keeps malformed service responses distinct from temporary unavailability", () => {
    expect(
      safeWebsiteError({
        status: 502,
        payload: { error: { code: "malformed", message: "raw response detail" } },
      }),
    ).toEqual({
      error: {
        code: "malformed",
        message: "The private service returned malformed data. Saved data is unchanged.",
      },
    });
  });

  it("maps HTTP 403 to a safe forbidden response", () => {
    expect(safeWebsiteError({ status: 403 })).toEqual({
      error: {
        code: "forbidden",
        message: "This action is not allowed.",
      },
    });
  });
});
