import { describe, expect, it } from "vitest";

import type { RefreshStatus, RefreshWork } from "../../app/lib/contracts";
import {
  REFRESH_IDLE,
  REFRESH_TIMED_OUT,
  automaticRefreshFor,
  lookupWaitTick,
  playerRefreshStep,
  pollAnswer,
  pollThrew,
  refreshSettled,
  reloadedHere,
  shownRefreshStatus,
  type PlayerRefresh,
  type PlayerRefreshEvent,
} from "../../app/lib/player-refresh";

const TAG = "#2PP";
const work = (state: RefreshWork["state"], workId = "work_1"): RefreshWork => ({
  kind: "refresh-work",
  workId,
  tag: TAG,
  state,
  progressPercent: 0,
  message: "",
  publishedAt: null,
});
const status = (state: RefreshWork["state"], workId = "work_1"): RefreshStatus => ({
  ...work(state, workId),
  kind: "refresh-status",
  player: null,
});
const refused = { error: { code: "rate_limited" as const, message: "Too soon." } };

function run(events: PlayerRefreshEvent[], from: PlayerRefresh = REFRESH_IDLE) {
  return events.reduce(playerRefreshStep, from);
}

describe("player Refresh", () => {
  it("goes submitted, polling, finished, and the page then reads itself", () => {
    const submitted = run([{ type: "submit", now: 1_000 }]);
    expect(submitted).toEqual({ phase: "submitted", deadline: 61_000 });
    expect(shownRefreshStatus(submitted, null)).toBeNull();
    expect(refreshSettled(submitted, false)).toBe(false);

    const polling = run(
      [{ type: "answered", now: 2_000, answer: work("queued"), tag: TAG }],
      submitted,
    );
    expect(polling).toMatchObject({ phase: "polling", deadline: 61_000 });
    expect(refreshSettled(polling, true)).toBe(false);

    const running = run([{ type: "polled", status: status("running") }], polling);
    expect(running.phase).toBe("polling");
    const finished = run([{ type: "polled", status: status("complete") }], running);
    expect(finished).toMatchObject({ phase: "finished", status: { state: "complete" } });
    expect(refreshSettled(finished, true)).toBe(true);
  });

  it.each(["failed", "unavailable"] as const)(
    "finishes when the work says %s, shown as a failure by its status",
    (state) => {
      const ended = run([
        { type: "submit", now: 0 },
        { type: "answered", now: 1, answer: work(state), tag: TAG },
      ]);
      expect(ended).toMatchObject({ phase: "finished", status: { state } });
    },
  );

  it("stops on a refused form, keeping nothing to show", () => {
    const stopped = run([
      { type: "submit", now: 0 },
      { type: "answered", now: 1, answer: refused, tag: TAG },
    ]);
    expect(stopped).toEqual({
      phase: "stopped",
      deadline: 60_000,
      status: null,
      failure: refused,
    });
    expect(refreshSettled(stopped, false)).toBe(true);
  });

  it("times out after a minute, keeping the last status shown", () => {
    const polling = run([
      { type: "submit", now: 0 },
      { type: "answered", now: 1, answer: work("running"), tag: TAG },
    ]);
    const timedOut = run([{ type: "timedOut" }], polling);
    expect(timedOut).toMatchObject({ phase: "stopped", failure: REFRESH_TIMED_OUT });
    expect(shownRefreshStatus(timedOut, null)).toMatchObject({ state: "running" });
    // An answer that lands after the minute is a time-out too.
    const late = run([
      { type: "submit", now: 0 },
      { type: "answered", now: 60_000, answer: work("queued"), tag: TAG },
    ]);
    expect(late).toMatchObject({
      phase: "stopped",
      status: null,
      failure: REFRESH_TIMED_OUT,
    });
    // A finished Refresh has nothing left to time out.
    const finished = run([
      { type: "submit", now: 0 },
      { type: "answered", now: 1, answer: work("complete"), tag: TAG },
    ]);
    expect(run([{ type: "timedOut" }], finished)).toBe(finished);
  });

  it("ignores answers it never asked for and status after polling ends", () => {
    expect(run([{ type: "answered", now: 0, answer: work("queued"), tag: TAG }])).toBe(
      REFRESH_IDLE,
    );
    const submitted = run([{ type: "submit", now: 0 }]);
    expect(
      run([{ type: "answered", now: 1, answer: work("queued"), tag: "#9QQ" }], submitted),
    ).toBe(submitted);
    expect(
      run([{ type: "answered", now: 1, answer: undefined, tag: TAG }], submitted),
    ).toBe(submitted);
    const stopped = run([{ type: "timedOut" }], submitted);
    expect(run([{ type: "polled", status: status("complete") }], stopped)).toBe(stopped);
  });

  it("follows a no-JS address, and a fresh read of it clears a failure", () => {
    expect(shownRefreshStatus(REFRESH_IDLE, status("queued"))).toMatchObject({
      state: "queued",
    });
    const following = run([
      { type: "loaderStatus", now: 5_000, status: status("queued") },
    ]);
    expect(following).toMatchObject({ phase: "polling", deadline: 65_000 });
    const failed = run([{ type: "pollFailed", failure: REFRESH_TIMED_OUT }], following);
    expect(failed.phase).toBe("stopped");
    // Read again, it keeps its first deadline and shows the newest status.
    const reread = run(
      [{ type: "loaderStatus", now: 9_000, status: status("complete") }],
      failed,
    );
    expect(reread).toMatchObject({ phase: "finished", deadline: 65_000 });
  });

  it("starts over on Refresh again after a failure", () => {
    const stopped = run([
      { type: "submit", now: 0 },
      { type: "answered", now: 1, answer: refused, tag: TAG },
      { type: "submit", now: 100_000 },
    ]);
    expect(stopped).toEqual({ phase: "submitted", deadline: 160_000 });
  });

  it("reads each status answer", () => {
    expect(pollAnswer(true, status("running"), "work_1", TAG)).toMatchObject({
      type: "polled",
    });
    expect(pollAnswer(false, refused, "work_1", TAG)).toEqual({
      type: "pollFailed",
      failure: refused,
    });
    expect(pollAnswer(true, { nonsense: true }, "work_1", TAG)).toMatchObject({
      failure: { error: { code: "unavailable" } },
    });
    expect(pollAnswer(true, status("running", "work_2"), "work_1", TAG)).toMatchObject({
      failure: { error: { code: "conflict" } },
    });
    expect(pollThrew(new DOMException("stop", "AbortError"))).toBeNull();
    expect(pollThrew(new TypeError("offline"))).toMatchObject({
      failure: { error: { code: "unavailable" } },
    });
  });
});

describe("first lookup wait", () => {
  it("reads each second, never two at once, and gives up after a minute", () => {
    expect(lookupWaitTick(0, 1_000, false)).toBe("read");
    expect(lookupWaitTick(0, 2_000, true)).toBe("hold");
    expect(lookupWaitTick(0, 59_999, false)).toBe("read");
    expect(lookupWaitTick(0, 60_000, true)).toBe("exhaust");
  });
});

describe("one automatic Refresh per visit", () => {
  it("sends on reload, on old data, and skips recent data", () => {
    expect(automaticRefreshFor(10, true)).toEqual({ trigger: "reload" });
    expect(automaticRefreshFor(61, false)).toEqual({ trigger: "automatic" });
    expect(automaticRefreshFor(60, false)).toBeNull();
  });

  it("counts only a reload of this same address", () => {
    const here = "https://clashlens.net/players/%232PP";
    expect(reloadedHere({ type: "reload", name: here }, "/players/%232PP")).toBe(true);
    expect(reloadedHere({ type: "reload", name: here }, "/players/%239QQ")).toBe(false);
    expect(reloadedHere({ type: "navigate", name: here }, "/players/%232PP")).toBe(false);
    expect(reloadedHere(undefined, "/players/%232PP")).toBe(false);
  });
});
