import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import { UpdatesNotice } from "../../app/components/UpdatesNotice";
import {
  clearUpdateStatusCache,
  loadUpdateStatus,
  mapUpdateStatus,
} from "../../app/server/update-status.server";

const ON_TIME = {
  kind: "update-status",
  checked_at: "2026-10-03T05:00:00+00:00",
  delay_seconds: 900,
  collection_delayed: false,
  last_collected_at: "2026-10-03T04:59:58+00:00",
  processing_delayed: false,
  oldest_waiting_saved_at: null,
};

function noticeText(payload: Record<string, unknown>): string | null {
  const status = mapUpdateStatus({ ...ON_TIME, ...payload });
  if (status === null) return null;
  return renderToString(createElement(UpdatesNotice, { status }))
    .replaceAll("<!-- -->", "")
    .replace(/<[^>]+>/g, "");
}

describe("delayed-updates notice", () => {
  it("stays hidden while updates are on time", () => {
    expect(noticeText({})).toBeNull();
    expect(noticeText({ collection_delayed: true, last_collected_at: null })).toBeNull();
  });

  it("names a stalled API only when no answer has arrived", () => {
    const text = noticeText({
      collection_delayed: true,
      last_collected_at: "2026-10-03T00:00:00+00:00",
    });
    expect(text).toBe(
      "Updates are delayed. No new data has arrived from the Clash of Clans API since 3 Oct 2026, 00:00 UTC (5 hours ago). Saved values stay on the page with the time they were last updated.",
    );
  });

  it("blames a processing backlog, not the API, when answers are arriving", () => {
    const text = noticeText({
      processing_delayed: true,
      oldest_waiting_saved_at: "2026-10-03T03:45:00+00:00",
    });
    expect(text).toContain(
      "New data is waiting to be processed; the oldest waiting data was saved at",
    );
    expect(text).toContain("3 Oct 2026, 03:45 UTC (1 hour ago)");
    expect(text).not.toContain("Clash of Clans API");
  });
});

describe("delayed-updates status read", () => {
  const savedUrl = process.env.CLASHLENS_PYTHON_API_URL;
  const savedSecret = process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64;

  beforeEach(() => {
    clearUpdateStatusCache();
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 =
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearUpdateStatusCache();
    if (savedUrl === undefined) delete process.env.CLASHLENS_PYTHON_API_URL;
    else process.env.CLASHLENS_PYTHON_API_URL = savedUrl;
    if (savedSecret === undefined) delete process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64;
    else process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = savedSecret;
  });

  it("shows no notice when the status cannot be read, and asks again only after 30 seconds", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("down"));
    vi.stubGlobal("fetch", fetch);
    expect(await loadUpdateStatus(0)).toBeNull();
    expect(await loadUpdateStatus(29_999)).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
    fetch.mockResolvedValue(
      Response.json({
        ...ON_TIME,
        processing_delayed: true,
        oldest_waiting_saved_at: "2026-10-03T04:00:00+00:00",
      }),
    );
    expect(await loadUpdateStatus(30_000)).toMatchObject({
      oldestWaitingSavedAt: "2026-10-03T04:00:00+00:00",
      lastCollectedAt: null,
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
