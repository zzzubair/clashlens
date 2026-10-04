import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  effects: [] as Array<() => (() => void) | undefined>,
  expired: vi.fn(),
  revalidate: vi.fn(),
  busy: false,
  refs: [] as Array<{ current: unknown }>,
  refIndex: 0,
}));

vi.mock("react", () => ({
  useEffect: (effect: () => (() => void) | undefined) => mocks.effects.push(effect),
  useRef: (current: unknown) => {
    const index = mocks.refIndex++;
    return (mocks.refs[index] ??= { current });
  },
  useState: () => [undefined, mocks.expired],
}));
vi.mock("react-router", () => ({
  useRevalidator: () => ({
    state: mocks.busy ? "loading" : "idle",
    revalidate: mocks.revalidate,
  }),
  useNavigation: () => ({ state: "idle" }),
}));

import { nextSeasonReset, useSeasonReread } from "../../app/components/SeasonReread";

let cleanup: (() => void) | undefined;
let tab: EventTarget & { hidden: boolean };
const BEFORE = "2026-10-05T04:59:30Z";
const AFTER = "2026-10-05T05:00:00Z";

function start(
  loadedAt: string | undefined,
  waiting = false,
  enabled = true,
  finalAnswer = false,
) {
  cleanup?.();
  mocks.refIndex = 0;
  const expired = useSeasonReread(loadedAt, waiting, enabled, finalAnswer);
  cleanup = mocks.effects.pop()?.();
  return expired;
}

beforeEach(() => {
  vi.useFakeTimers({
    toFake: [
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
      "performance",
      "Date",
    ],
  });
  tab = Object.assign(new EventTarget(), { hidden: false });
  vi.stubGlobal("document", tab);
  vi.stubGlobal("window", new EventTarget());
  mocks.effects = [];
  mocks.refs = [];
  mocks.expired.mockClear();
  mocks.revalidate.mockReset().mockResolvedValue(undefined);
  mocks.busy = false;
});

afterEach(() => {
  cleanup?.();
  cleanup = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("expires September at exactly Monday Reset using elapsed time, regardless of device time", async () => {
  vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
  start(BEFORE);
  await vi.advanceTimersByTimeAsync(29_999);
  expect(mocks.revalidate).not.toHaveBeenCalled();
  expect(mocks.expired).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(mocks.expired).toHaveBeenCalledWith(BEFORE);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
});

it("rereads waiting saved data once a minute and stops after October recovery", async () => {
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  cleanup?.();
  start("2026-10-05T05:01:00Z", false);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
});

it("skips hidden reads, expires trophies, and rereads once on returning to the tab", async () => {
  tab.hidden = true;
  start(BEFORE);
  await vi.advanceTimersByTimeAsync(90_000);
  expect(mocks.expired).toHaveBeenCalledWith(BEFORE);
  expect(mocks.revalidate).not.toHaveBeenCalled();
  tab.hidden = false;
  tab.dispatchEvent(new Event("visibilitychange"));
  tab.dispatchEvent(new Event("visibilitychange"));
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
});

it("does not overlap slow saved-data reads", async () => {
  let finish!: () => void;
  mocks.revalidate.mockReturnValue(
    new Promise<void>((resolve) => {
      finish = resolve;
    }),
  );
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  finish();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
});

it("preserves explicit historical selections across Reset", async () => {
  start(BEFORE, true, false);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.expired).not.toHaveBeenCalled();
  expect(mocks.revalidate).not.toHaveBeenCalled();
});

it("leaves navigation and existing rereads alone", async () => {
  mocks.busy = true;
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.revalidate).not.toHaveBeenCalled();
});

it("keeps existing explained-player minute checks without a saved profile", async () => {
  start(undefined, true);
  await vi.advanceTimersByTimeAsync(120_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
  expect(mocks.expired).not.toHaveBeenCalled();
});

it("uses the next 28-day Season boundary, never the following daily Reset", () => {
  expect(nextSeasonReset(BEFORE)).toBe(Date.parse(AFTER));
  expect(nextSeasonReset(AFTER)).toBe(Date.parse("2026-11-02T05:00:00Z"));
});

it("expires and rereads on wake when the elapsed clock paused during sleep", () => {
  start(BEFORE);
  vi.setSystemTime(Date.now() + 4 * 3600_000);
  tab.dispatchEvent(new Event("visibilitychange"));
  expect(mocks.expired).toHaveBeenCalled();
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
});

it("keeps recovery reads after a pending response is followed by a failed read", async () => {
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(60_000);
  start(undefined, false);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
  start("2026-10-05T05:02:00Z", false);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
});

it("keeps a delayed pre-Reset response expired after waking across Reset", async () => {
  start("2026-10-05T02:00:00Z");
  vi.setSystemTime(Date.now() + 4 * 3600_000);
  expect(start("2026-10-05T02:10:00Z")).toBe(true);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
});

it("stops retained pending reads when the lookup gives a final answer", async () => {
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(60_000);
  start(undefined, false, true, true);
  await vi.advanceTimersByTimeAsync(180_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
});

it("retries a rejected reread without overlapping it or leaking the rejection", async () => {
  mocks.revalidate.mockRejectedValue(new Error("Read unavailable"));
  start(AFTER, true);
  await vi.advanceTimersByTimeAsync(120_000);
  expect(mocks.revalidate).toHaveBeenCalledTimes(2);
});

it("schedules Reset precisely even when it is more than the browser timeout limit away", async () => {
  // Keep only the boundary timer to avoid iterating 40,320 minute checks.
  vi.spyOn(globalThis, "setInterval").mockReturnValue(
    0 as unknown as ReturnType<typeof setInterval>,
  );
  start(AFTER);
  await vi.advanceTimersByTimeAsync(28 * 86400_000 - 1);
  expect(mocks.revalidate).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  expect(mocks.expired).toHaveBeenCalledWith(AFTER);
});
