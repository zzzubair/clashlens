import { afterEach, expect, it, vi } from "vitest";

import type { OverlayBattle } from "../../app/lib/obs-overlay";
import {
  noticeFor,
  overlayTimers,
  readBattles,
  type BattleRead,
} from "../../app/lib/obs-overlay-view";
import {
  dragTo,
  previewMessage,
  releaseDrag,
  runAutosave,
  startDrag,
  type Autosave,
  type Choices,
  type Place,
  type SaveStatus,
} from "../../app/routes/account.obs-overlay";

const PLACE: Place = { x: 100, y: 100, w: 400, h: 200, cw: 1920, ch: 1080, anchor: "tl" };

const choices = (n: number): Choices => ({
  json: `choices ${n}`,
  tag: "#2PP",
  settings: { w: String(n) },
});

const battle = (id: string): OverlayBattle => ({
  id,
  kind: "attack",
  at: "2026-10-12T06:00:00Z",
  stars: 3,
  destruction: 100,
  trophies: 40,
  opponent: "Rival",
});

afterEach(() => {
  vi.useRealTimers();
});

it("moves the box with the pointer at the preview's scale and frees its corner", () => {
  const drag = startDrag(PLACE, 10, 10, null, 0.5);
  expect(drag.start.anchor).toBeNull();
  const moved = dragTo(drag, 20, 15, false)!;
  expect(moved.place).toMatchObject({ x: 120, y: 110, w: 400, h: 200, anchor: null });
  expect(moved.guides).toEqual({ v: null, h: null });
});

it("snaps to the middle with Shift and keeps the box on the stream", () => {
  const drag = startDrag(PLACE, 0, 0, null, 1);
  const middle = dragTo(drag, (1920 - 400) / 2 - 100 + 5, 0, true)!;
  expect(middle.place.x).toBe(760);
  expect(middle.guides).toEqual({ v: 960, h: null });
  expect(dragTo(drag, -500, 5000, false)!.place).toMatchObject({ x: 0, y: 880 });
});

it("resizes from the grabbed edge, keeps the opposite one and leaves the guides", () => {
  const drag = startDrag(PLACE, 0, 0, "w", 1);
  const resized = dragTo(drag, 50, 0, true)!;
  expect(resized.place).toMatchObject({ x: 150, w: 350, y: 100, h: 200 });
  expect(resized.place.x + resized.place.w).toBe(PLACE.x + PLACE.w);
  expect(resized.guides).toBeNull();
  expect(dragTo(drag, 1000, 0, false)!.place.w).toBe(160);
});

it("ends a drag on pointer up or cancel: no press, no guides, and moves do nothing", () => {
  const drag = startDrag(PLACE, 0, 0, null, 1);
  expect(dragTo(drag, 5, 0, true)!.guides).toEqual({ v: null, h: null });
  const snapped = dragTo(drag, 665, 0, true)!;
  expect(snapped.guides).toEqual({ v: 960, h: null });
  const ended = releaseDrag();
  expect(ended).toEqual({ drag: null, guides: { v: null, h: null } });
  expect(dragTo(ended.drag, 900, 900, true)).toBeNull();
});

it("saves the newest choices once the one sending lands, one run at a time", async () => {
  const autosave: { current: Autosave } = {
    current: { phase: "idle", saved: choices(0).json },
  };
  let latest = choices(1);
  const sent: string[] = [];
  const shown: SaveStatus[] = [];
  let land!: () => void;
  const send = vi.fn((next: Choices) => {
    sent.push(next.json);
    return new Promise<void>((resolve) => (land = resolve));
  });
  const run = runAutosave(
    autosave,
    () => latest,
    send,
    (s) => shown.push(s),
  );
  expect(autosave.current.phase).toBe("sending");
  // A change while one is sending waits for it instead of a second run.
  latest = choices(2);
  await runAutosave(
    autosave,
    () => latest,
    send,
    (s) => shown.push(s),
  );
  expect(sent).toEqual(["choices 1"]);
  land();
  await vi.waitFor(() => expect(sent).toEqual(["choices 1", "choices 2"]));
  land();
  await run;
  expect(shown).toEqual(["saving", "saving", "saved"]);
  expect(autosave.current).toEqual({ phase: "idle", saved: "choices 2" });
});

it("leaves the old choices saved after a failure, so the next change sends again", async () => {
  const autosave: { current: Autosave } = {
    current: { phase: "idle", saved: choices(0).json },
  };
  const shown: SaveStatus[] = [];
  await runAutosave(
    autosave,
    () => choices(1),
    () => Promise.reject(new Error("503")),
    (s) => shown.push(s),
  );
  expect(shown).toEqual(["saving", "notSaved"]);
  expect(autosave.current).toEqual({ phase: "idle", saved: "choices 0" });
  await runAutosave(
    autosave,
    () => choices(1),
    () => Promise.resolve(),
    (s) => shown.push(s),
  );
  expect(autosave.current).toEqual({ phase: "idle", saved: "choices 1" });
  expect(shown.at(-1)).toBe("saved");
});

it("answers the preview's handshake and shows how it laid itself out", () => {
  expect(previewMessage({ type: "cl-overlay-ready" })).toBe("ready");
  expect(previewMessage({ type: "something-else" })).toBeNull();
  expect(
    previewMessage({ type: "cl-overlay-size", layout: "card", scale: 1, hidden: [] }),
  ).toEqual({ kind: "shown", fit: { text: "Shown as a card.", warn: false } });
  expect(
    previewMessage({
      type: "cl-overlay-size",
      layout: "strip",
      scale: 0.5,
      hidden: ["rank"],
    }),
  ).toEqual({
    kind: "shown",
    fit: {
      text: "Shown as a strip. Left out to fit: rank. Too small to read on stream: make it bigger.",
      warn: true,
    },
  });
});

it("shows no notice for a source's first read, then one for the new battles", () => {
  let read: BattleRead = { kind: "first" };
  const first = readBattles(read, [battle("a"), battle("b")], false);
  expect(first.fresh).toEqual([]);
  read = first.read;
  const later = readBattles(read, [battle("a"), battle("b"), battle("c")], false);
  expect(later.fresh.map((b) => b.id)).toEqual(["c"]);
  // A quiet read, like the demo's starting battles, records without a notice.
  expect(readBattles(later.read, [battle("d")], true).fresh).toEqual([]);
  expect(noticeFor(later.fresh, true)).toEqual({ kind: "showing", battles: later.fresh });
  expect(noticeFor(later.fresh, false)).toBeNull();
  expect(noticeFor([], true)).toBeNull();
});

it("runs waiting timers only until the overlay stops", () => {
  vi.useFakeTimers();
  const clock = overlayTimers();
  const ran: string[] = [];
  clock.later(() => ran.push("hide notice"), 6_000);
  const poll = clock.later(() => ran.push("poll"), 3_000);
  clock.cancel(poll);
  vi.advanceTimersByTime(6_000);
  expect(ran).toEqual(["hide notice"]);
  clock.later(() => ran.push("after stop"), 1_000);
  clock.stop();
  vi.advanceTimersByTime(5_000);
  expect(ran).toEqual(["hide notice"]);
  expect(clock.running()).toBe(false);
});
